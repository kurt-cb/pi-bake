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
    """Run a privileged command; raise on non-zero with stderr surfaced.

    Prefixes with `sudo` only when euid != 0. When pi-bake is invoked
    as root (typical for LXC containers or remote bake hosts SSH'd
    into as root), the `sudo` binary may not even be installed, and
    prefixing with it just adds a useless dependency. Running as
    non-root remains the normal local-laptop case and gets the sudo
    prompt as before.

    capture=False is for `mount` calls where stderr is informational
    and we want it on the operator's terminal directly.

    On failure (capture=True), re-raise as RuntimeError with stdout
    + stderr inlined so the operator gets actionable output instead
    of a bare `CalledProcessError` (subprocess swallows captured
    streams unless the caller pulls them off the exception).
    """
    import os as _os
    cmd = list(args) if _os.geteuid() == 0 else ["sudo", *args]
    LOG.debug("privileged: %s", " ".join(cmd))
    if not capture:
        return subprocess.run(cmd, check=True, text=True)
    r = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"command failed (rc={r.returncode}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{r.stdout}"
            f"--- stderr ---\n{r.stderr}"
        )
    return r


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


def read_partition_layout(raw_img: Path) -> list[tuple[int, int, int]]:
    """Read partition table from a raw image; return [(idx, start_bytes,
    size_bytes), ...] sorted by partition index.

    Parses `sfdisk -d <image>` (sfdisk reads the partition table directly
    from a file — no loop attach needed). Output looks like:

        label: dos
        ...
        /tmp/x.img1 : start=2048, size=524288, type=c, bootable
        /tmp/x.img2 : start=526336, size=3670016, type=83

    Sectors are 512 bytes (sfdisk's default unit on dos labels).
    """
    r = _sudo("sfdisk", "-d", str(raw_img))
    parts: list[tuple[int, int, int]] = []
    for line in r.stdout.splitlines():
        # sfdisk prints partition lines like:
        #   /tmp/x.img1 : start=        2048, size=       10240, type=c
        # (note the SPACE before the colon, and padded numbers).
        line = line.strip()
        if ":" not in line or "start=" not in line or "size=" not in line:
            continue
        head, rest = line.split(":", 1)
        head = head.rstrip()      # strip the trailing space before ':'
        # head is `/path/to/image.imgN` — pull off the trailing digits
        # as the partition index.
        i = len(head)
        while i > 0 and head[i - 1].isdigit():
            i -= 1
        if i == len(head):
            continue
        try:
            idx = int(head[i:])
        except ValueError:
            continue
        # rest is ` start=<S>, size=<Z>, type=..., [bootable]`. Values
        # may be padded with spaces; int() handles that.
        fields: dict[str, str] = {}
        for kv in rest.split(","):
            if "=" not in kv:
                continue
            k, v = kv.split("=", 1)
            fields[k.strip()] = v.strip()
        try:
            start = int(fields["start"]) * 512
            size = int(fields["size"]) * 512
        except (KeyError, ValueError):
            continue
        parts.append((idx, start, size))
    return sorted(parts)


def attach_partition_loops(
    raw_img: Path,
) -> dict[int, str]:
    """Attach one loop device per partition using `losetup -o <offset>
    --sizelimit <size>`. Returns {part_idx: loop_dev}.

    Why not just `losetup -fP image.img` + use `/dev/loop0pN` partition
    nodes: in container environments where incus / lxc applies a cgroup
    BPF device controller, partition device nodes use major 259
    (BLOCK_EXT_MAJOR) which the BPF program may block while still
    allowing major 7 (loop). Attaching each partition as its own
    top-level loop device sidesteps that — every device pi-bake touches
    is major 7.

    Side benefit: simpler teardown story (each partition is just
    another loop dev to detach), and no dependence on udev / kernel
    partition-rescan timing.
    """
    parts = read_partition_layout(raw_img)
    if not parts:
        raise RuntimeError(
            f"sfdisk -d {raw_img} reported no partitions — image "
            f"doesn't have a recognizable partition table"
        )
    out: dict[int, str] = {}
    for idx, start, size in parts:
        r = _sudo(
            "losetup", "-f", "--show",
            "-o", str(start), "--sizelimit", str(size),
            str(raw_img),
        )
        loop_dev = r.stdout.strip()
        if not loop_dev.startswith("/dev/loop"):
            raise RuntimeError(
                f"losetup returned unexpected device {loop_dev!r} for "
                f"partition {idx}"
            )
        out[idx] = loop_dev
        LOG.info("losetup: partition %d (offset=%d, size=%d) → %s",
                 idx, start, size, loop_dev)
    return out


def detach_loops(loops: dict[int, str]) -> None:
    """losetup -d every loop dev in the dict; best-effort (warn,
    don't raise). Mirrors `unmount_image`'s teardown discipline.
    """
    for idx, loop in loops.items():
        try:
            _sudo("losetup", "-d", loop, capture=False)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            LOG.warning("losetup -d %s (part %d) failed: %s",
                        loop, idx, e)


def ensure_partition_nodes(loop_dev: str) -> list[str]:
    """Create /dev/<loop>p<N> nodes for every partition the kernel
    scanned on `loop_dev`. Returns the list of partition device paths.

    Background: `losetup -fP` causes the kernel to read the partition
    table and register each partition in sysfs (`/sys/block/<loop>/
    <loop>pN/`). On a normal host, udev watches these uevents and
    creates the matching `/dev/<loop>pN` block-device nodes. In an
    LXC container (or any environment without udev — Alpine minirootfs,
    busybox-only systems), no node ever gets created, and the next
    `mkfs.vfat /dev/loop0p1` fails with ENOENT.

    Fix: read the partition's major:minor from sysfs and mknod the
    node ourselves. Idempotent (no-op when the node already exists).

    Returns the partition device paths in numeric order
    (`['/dev/loop0p1', '/dev/loop0p2', ...]`).
    """
    import os as _os
    loop_name = loop_dev.rsplit("/", 1)[-1]
    sys_block = f"/sys/block/{loop_name}"
    if not _os.path.isdir(sys_block):
        raise RuntimeError(
            f"loop device sysfs entry {sys_block} missing — partition "
            f"scan didn't register. Check `losetup -P` ran cleanly."
        )
    # Sort by partition index suffix so /dev/loop0p1 precedes p2 etc.
    parts: list[tuple[int, str, str, str]] = []
    for name in sorted(_os.listdir(sys_block)):
        if not name.startswith(loop_name + "p"):
            continue
        idx_str = name[len(loop_name) + 1:]
        if not idx_str.isdigit():
            continue
        dev_file = f"{sys_block}/{name}/dev"
        try:
            with open(dev_file) as f:
                major, minor = f.read().strip().split(":")
        except (OSError, ValueError):
            continue
        parts.append((int(idx_str), name, major, minor))

    dev_paths: list[str] = []
    for idx, name, major, minor in sorted(parts):
        dev_path = f"/dev/{name}"
        dev_paths.append(dev_path)
        if _os.path.exists(dev_path):
            continue
        _sudo("mknod", dev_path, "b", major, minor, capture=False)
        _sudo("chmod", "660", dev_path, capture=False)
    return dev_paths


def mount_image(raw_img: Path, workdir: Path) -> MountedImage:
    """Attach a loop dev per partition (offset-based) and mount each.

    Returns a `MountedImage` with `mounts[part_idx] = mount_dir`.
    Caller is responsible for calling `unmount_image()` on it (use
    a try/finally — losetup leaks are gnarly to debug).

    Why per-partition (not `losetup -fP` + `/dev/loopNpM`): see
    [[attach_partition_loops]] doc — partition nodes use major 259
    which incus / lxc cgroup-BPF can block. Top-level loop devices
    use major 7, already allowed in most container policies.

    Mount discipline:
      - Each partition mounted RW under workdir/p<N>
      - Mount type auto-detected by the kernel (vfat / ext4 / btrfs)
      - No mount options beyond defaults
    """
    _require_tools("losetup", "mount", "umount", "sfdisk")
    workdir.mkdir(parents=True, exist_ok=True)

    loops = attach_partition_loops(raw_img)

    mounts: dict[int, Path] = {}
    for idx, loop_dev in sorted(loops.items()):
        mp = workdir / f"p{idx}"
        mp.mkdir(exist_ok=True)
        _sudo("mount", loop_dev, str(mp), capture=False)
        mounts[idx] = mp

    # Store the first partition's loop_dev in the legacy `loop_dev`
    # field so back-compat callers still see a string there; the
    # full per-partition map lives in `_loops` (private — direct
    # consumers should call `unmount_image()` to clean up).
    primary = sorted(loops.values())[0] if loops else ""
    mi = MountedImage(
        raw_img=raw_img, loop_dev=primary,
        mounts=mounts, workdir=workdir,
    )
    # Stash the full loop map on the dataclass so unmount can detach
    # them all. Using an attribute set rather than dataclass field so
    # we don't break MountedImage's public surface.
    mi._loops = loops  # type: ignore[attr-defined]
    return mi


def unmount_image(mi: MountedImage) -> None:
    """Sync, unmount every partition, detach every per-partition loop.

    Safe to call multiple times — silently skips already-cleaned
    mounts. Always tries to detach all loops even if some unmount
    failed (avoid losetup leaks).
    """
    # Sync first — otherwise pending writes can land after umount.
    subprocess.run(["sync"], check=False)

    for mp in mi.mounts.values():
        try:
            _sudo("umount", str(mp), capture=False)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            LOG.warning("umount %s failed (ignoring): %s", mp, e)

    loops = getattr(mi, "_loops", None)
    if loops:
        detach_loops(loops)
    elif mi.loop_dev:
        # Back-compat: caller built the MountedImage without _loops
        try:
            _sudo("losetup", "-d", mi.loop_dev, capture=False)
        except (subprocess.CalledProcessError, RuntimeError) as e:
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
    import os as _os
    target = mount_root / rel_path.lstrip("/")
    # Make sure the parent dir exists.
    _sudo("mkdir", "-p", str(target.parent), capture=False)
    body = content.encode() if isinstance(content, str) else content
    # As root, write directly. Non-root: pipe through `sudo tee`.
    if _os.geteuid() == 0:
        with open(target, "wb") as f:
            f.write(body)
    else:
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
    import os as _os
    target = mount_root / rel_path.lstrip("/")
    if ensure_trailing_newline and not content.endswith("\n"):
        content = content + "\n"
    # As root: open with append mode directly. Non-root: pipe through
    # `sudo sh -c 'cat >> target'`.
    if _os.geteuid() == 0:
        with open(target, "ab") as f:
            f.write(content.encode())
    else:
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
