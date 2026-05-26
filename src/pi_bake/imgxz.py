"""Shared scaffolding for `.img.xz`-style partitioned-image bakes.

Three backends use this: Raspberry Pi OS Lite (raspbian.py), Debian
Pi community images (debian.py), and Fedora ARM (fedora.py). Each
ships its rootfs + boot partition as a `.img.xz` (or `.raw.xz`)
disk image we have to mount, edit, and repack.

The Alpine baker doesn't use this module — Alpine ships a flat
tarball that pi-bake assembles into a FAT image via mtools as a
regular user. losetup-style bakes are the price of working with
upstream-produced disk images.

Sudo required
-------------
`losetup -P` (set up loop device with partition table scanning)
and the subsequent `mount` calls need CAP_SYS_ADMIN. pi-bake
shells out to `sudo` for these steps. Operators typically run
pi-bake inside a privileged LXC container or with sudoers entries
that allow losetup/mount without password — see README. LXC
setup is outside pi-bake's scope; the README documents the
requirement.

Net flow per backend
--------------------
1. fetch the .img.xz via `download.fetch()`
2. xz -d into a tmpdir → raw .img
3. losetup -fP → /dev/loopN with partprobe
4. mount each partition under tmpdir
5. backend-specific writes (hostname, SSH key, wifi, config_txt, …)
6. sync + umount + losetup -d
7. xz the modified .img → out.img.xz

This module owns 1–4 and 6–7. Backends own step 5.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("pi_bake.imgxz")


@dataclass
class MountedImage:
    """Handle to a losetup-mounted image. Returned by `mount_image`,
    consumed by `unmount_and_repack`.

    `loop_dev` is the `/dev/loopN` the kernel allocated; `mounts`
    maps partition-index (1, 2, 3, …) to the temporary directory
    each partition is mounted at.

    The backend writes whatever it needs into the mount dirs; this
    module handles teardown + repack.
    """
    raw_img: Path
    loop_dev: str
    mounts: dict[int, Path]
    workdir: Path                       # tempdir owning the mounts


def _require_tools(*names: str) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise RuntimeError(
            f"missing required tool(s) on PATH: {missing}. "
            f"On Fedora: sudo dnf install xz util-linux. "
            f"On Debian/Ubuntu: sudo apt install xz-utils util-linux."
        )


def _sudo(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a sudo command; raise on non-zero with stderr surfaced.

    capture=False is for `mount` calls where stderr is informational
    and we want it on the operator's terminal directly.
    """
    cmd = ["sudo", *args]
    LOG.debug("sudo: %s", " ".join(cmd))
    kwargs: dict = {"check": True, "text": True}
    if capture:
        kwargs["capture_output"] = True
    return subprocess.run(cmd, **kwargs)


def decompress_xz(xz_path: Path, out_dir: Path) -> Path:
    """xz -d the input into out_dir; return the resulting .img path.

    Content-sniff instead of extension-trust: the Raspberry Pi
    downloads server publishes the latest Pi OS image at
    `https://downloads.raspberrypi.com/raspios_lite_arm64_latest`
    (a permanent redirect with NO `.xz` suffix on the path). The
    fetcher names the cached file after the URL's last segment,
    so the on-disk path can be `raspios_lite_arm64_latest` — a
    valid xz file with no `.xz` extension. Rejecting based on
    suffix means the latest-URL bakes never get past decompress.

    Output filename: strip a trailing `.xz` if present; otherwise
    append `.img` (so the result has a sensible extension downstream
    code can recognize).

    Idempotent: if the decompressed file already exists in out_dir
    it gets reused (decompressing a 4 GB image takes ~30 s; not
    worth redoing on every bake).
    """
    _require_tools("xz")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Content sniff — xz file magic is FD 37 7A 58 5A 00. If the
    # input isn't xz-compressed, fail loudly with a clear message
    # instead of letting `xz -d` print its own (less helpful) error.
    XZ_MAGIC = bytes((0xFD, 0x37, 0x7A, 0x58, 0x5A, 0x00))
    with open(xz_path, "rb") as f:
        head = f.read(len(XZ_MAGIC))
    if head != XZ_MAGIC:
        raise ValueError(
            f"{xz_path} doesn't look like an xz file (magic was "
            f"{head!r}, expected {XZ_MAGIC!r}). The Raspberry Pi / "
            f"Debian / Fedora download URLs may have moved; check "
            f"upstream + bump the catalog version."
        )

    # Output filename.
    stem = xz_path.name
    raw_path = out_dir / (stem[:-3] if stem.endswith(".xz") else stem + ".img")
    if raw_path.is_file():
        LOG.info("decompressed image cached: %s", raw_path)
        return raw_path
    LOG.info("xz -d %s → %s", xz_path.name, raw_path.name)
    # -c writes to stdout so we control the output path.
    with open(raw_path, "wb") as out:
        subprocess.run(
            ["xz", "-d", "-c", str(xz_path)],
            check=True, stdout=out,
        )
    return raw_path


def mount_image(raw_img: Path, workdir: Path) -> MountedImage:
    """losetup -fP the image and mount each partition.

    Returns a `MountedImage` with `mounts[part_idx] = mount_dir`.
    Caller is responsible for calling `unmount_image()` on it (use
    a try/finally — losetup leaks are gnarly to debug).

    Mount discipline:
      - Each partition mounted RW under workdir/p<N>
      - Mount type auto-detected by the kernel (vfat / ext4 / btrfs)
      - No mount options beyond defaults
    """
    _require_tools("losetup", "mount", "umount", "partprobe", "lsblk")
    workdir.mkdir(parents=True, exist_ok=True)

    # losetup -f finds a free loop, -P scans the partition table,
    # --show prints the device name we got.
    r = _sudo("losetup", "-fP", "--show", str(raw_img))
    loop_dev = r.stdout.strip()
    if not loop_dev.startswith("/dev/loop"):
        raise RuntimeError(
            f"losetup returned unexpected device {loop_dev!r}"
        )
    LOG.info("losetup: %s → %s", raw_img.name, loop_dev)

    # In case kernel hasn't fully scanned the partition table yet.
    _sudo("partprobe", loop_dev, capture=False)

    # Enumerate partitions via /dev/loopNpM glob — lsblk -nlo NAME
    # gives stable ordering.
    r = _sudo(
        "lsblk", "-nlo", "NAME", "-x", "NAME", loop_dev,
    )
    part_names = [
        line.strip() for line in r.stdout.splitlines()
        if line.strip().startswith(Path(loop_dev).name + "p")
    ]
    if not part_names:
        # Image has no partition table — treat the whole loop dev as
        # one partition (rare for our targets but graceful).
        part_names = [Path(loop_dev).name]
    LOG.info("partitions: %s", part_names)

    mounts: dict[int, Path] = {}
    for idx, name in enumerate(part_names, start=1):
        dev = f"/dev/{name}"
        mp = workdir / f"p{idx}"
        mp.mkdir(exist_ok=True)
        _sudo("mount", dev, str(mp), capture=False)
        mounts[idx] = mp

    return MountedImage(
        raw_img=raw_img, loop_dev=loop_dev,
        mounts=mounts, workdir=workdir,
    )


def unmount_image(mi: MountedImage) -> None:
    """Sync, unmount everything, detach the loop device.

    Safe to call multiple times — silently skips already-cleaned
    mounts. Always tries to detach the loop dev even if some
    unmount failed (avoid losetup leaks).
    """
    # Sync first — otherwise pending writes can land after umount.
    subprocess.run(["sync"], check=False)

    for mp in mi.mounts.values():
        try:
            _sudo("umount", str(mp), capture=False)
        except subprocess.CalledProcessError as e:
            LOG.warning("umount %s failed (ignoring): %s", mp, e)

    try:
        _sudo("losetup", "-d", mi.loop_dev)
    except subprocess.CalledProcessError as e:
        LOG.warning("losetup -d %s failed (ignoring): %s",
                    mi.loop_dev, e)


def recompress_xz(raw_img: Path, out_xz: Path,
                  level: int = 6) -> Path:
    """xz the raw .img → out_xz. Returns out_xz.

    Compression level 6 is xz's default — good ratio, ~30 s for a
    2 GB image on a modern desktop. Bump to 9 for ~10% smaller
    output but 3× the time.
    """
    _require_tools("xz")
    out_xz.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("xz -%d %s → %s", level, raw_img.name, out_xz.name)
    with open(out_xz, "wb") as out:
        subprocess.run(
            ["xz", "-c", f"-{level}", "-T0", str(raw_img)],
            check=True, stdout=out,
        )
    return out_xz


def write_file(
    mount_root: Path, rel_path: str, content: str | bytes,
    *, mode: int = 0o644,
) -> Path:
    """Write a file inside a mounted partition with sudo tee.

    Direct Python `open()` would fail when the operator doesn't own
    the mounted ext4 (root does, after losetup). `sudo tee` is the
    portable trick.
    """
    target = mount_root / rel_path.lstrip("/")
    # Make sure the parent dir exists (sudo mkdir -p is harmless if
    # it already does).
    _sudo("mkdir", "-p", str(target.parent), capture=False)
    body = content.encode() if isinstance(content, str) else content
    # `sudo tee` reads from stdin; pipe via subprocess.
    p = subprocess.Popen(
        ["sudo", "tee", str(target)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
    )
    assert p.stdin is not None
    p.stdin.write(body)
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError(f"failed to write {target}")
    _sudo("chmod", f"{mode:o}", str(target), capture=False)
    return target


def chown(mount_root: Path, rel_path: str, uid: int, gid: int) -> None:
    """sudo chown a file in a mounted partition."""
    target = mount_root / rel_path.lstrip("/")
    _sudo("chown", f"{uid}:{gid}", str(target), capture=False)


def append_file(
    mount_root: Path, rel_path: str, content: str,
    *, ensure_trailing_newline: bool = True,
) -> Path:
    """Append `content` to a file inside a mounted partition.

    If the file doesn't exist, creates it. Use for additions to
    existing config files (e.g. config.txt) where we want the
    operator's lines after the shipped baseline.
    """
    target = mount_root / rel_path.lstrip("/")
    if ensure_trailing_newline and not content.endswith("\n"):
        content = content + "\n"
    # sudo sh -c 'cat >> target' to do the append as root.
    p = subprocess.Popen(
        ["sudo", "sh", "-c", f"cat >> {target}"],
        stdin=subprocess.PIPE,
    )
    assert p.stdin is not None
    p.stdin.write(content.encode())
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError(f"failed to append to {target}")
    return target
