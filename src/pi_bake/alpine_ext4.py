"""Alpine sys-mode ext4 baker — partitioned image, normal kernel upgrades.

Companion to `alpine.py` (the diskless / apkovl / modloop baker). Same
operator-visible recipe schema; different on-disk shape:

  - p1: vfat /boot  (256 MB) — Pi firmware blobs, kernel image, DTBs,
                                cmdline.txt + config.txt
  - p2: ext4  /     (rest)   — real Alpine root, normal /lib/modules,
                                normal /etc, openrc runlevels

Why a separate backend
----------------------
Alpine's RPi *release tarball* is built for diskless mode (apkovl
overlay + modloop squashfs on FAT). Sys-mode has no upstream tarball
— you have to bootstrap it the way `setup-disk` does on real
hardware: `apk add --root <mnt> --initdb alpine-base linux-rpi
raspberrypi-bootloader …` against upstream repos. So this module
does NOT consume `OSImage.url_template`; it builds the image from
the apk repositories directly.

Why this trade-off is worth taking
----------------------------------
Diskless mode's modloop-on-FAT layer is what makes `apk upgrade
linux-rpi` an awkward multi-step ritual (regenerate modloop, write
to FAT, lbu commit, reboot). In sys-mode, `apk upgrade linux-rpi`
just works — apk writes to a real `/boot` and a real `/lib/modules`,
next boot picks them up. That makes `os_version: edge` an honest
option (you actually get the edge kernel after one `apk upgrade`),
and makes [[cache_packages]] a complete air-gap upgrade story.

Cross-arch on x86_64 bake hosts
-------------------------------
The bake host runs x86_64 (or aarch64) but produces an aarch64
image. apk-tools-static handles cross-arch fetch+install via
`--arch aarch64`. The wrinkle is package post-install scripts:
those are aarch64 binaries that don't run on an x86_64 host
unless qemu-user-static + binfmt_misc are set up.

Our solution: install with `--no-scripts`. The packages' file
content lands in the target root; hooks (mkinitfs, depmod,
update-extlinux, etc.) don't run. We do the essential
hook-equivalents ourselves at bake time:

  - `depmod` — we ship a portable equivalent (it's just
    /lib/modules/<ver>/modules.dep generation from modules' headers,
    but Alpine's apk already ships pre-generated modules.dep in the
    linux-rpi package, so we don't even need to regenerate).
  - `mkinitfs` — Alpine ships pre-built `initramfs-rpi` as part of
    linux-rpi's data files; we don't need to regenerate at bake.

The remaining hooks (e.g. ssh-host-keygen, depmod for newly added
modules) get triggered on first boot by a one-shot service we
install: `/etc/init.d/pi-bake-firstboot-fix` runs
`apk fix --no-network` to finalize any pending triggers, then
removes itself from the runlevel. This costs a few seconds on
first boot but guarantees correctness with no qemu requirement
on the bake host.

Sudo requirement
----------------
Same as raspbian/debian/fedora: `losetup`, `mount`, `mkfs`,
`partprobe`, and `sudo tee` into a root-owned mountpoint. Pi-bake
shells out to `sudo`. Operators typically run inside a privileged
LXC container or with sudoers entries — see README.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from pi_bake import apkfetch, imgxz
from pi_bake.config import NodeConfig

LOG = logging.getLogger("pi_bake.alpine_ext4")


# Default image size — large enough for baseline + room for the
# operator's `apk add` later. Operator can override via
# image_size_mb. 2 GB is a safe minimum; the resulting .img.gz
# compresses to ~250 MB for a baseline install.
DEFAULT_IMAGE_SIZE_MB = 2048

# Boot partition size — Pi firmware + kernel + DTBs + initramfs
# easily fit in 200 MB. 256 MB leaves headroom for cache_packages
# spillover and config.txt experimentation.
BOOT_PART_MB = 256

# Baseline package set. Mirrors the Alpine diskless backend's
# `/etc/apk/world` baseline (the set that makes a Pi network-
# reachable on first boot) plus the kernel + bootloader (which
# the diskless backend pulls in via the tarball, not apk).
_BASELINE_PACKAGES = (
    # Core userland + init
    "alpine-base",
    "openrc",
    "busybox-openrc",
    # Pi kernel + Pi firmware blobs (start*.elf, bootcode.bin,
    # fixup*.dat, bcm*.dtb, overlays/*.dtbo). raspberrypi-bootloader
    # is the apk that ships these; without it the Pi firmware can't
    # find a kernel to load.
    "linux-rpi",
    "raspberrypi-bootloader",
    # initramfs builder — apk hooks would normally run this; we
    # invoke it ourselves (or first-boot does) since hooks are off.
    "mkinitfs",
    # Network
    "dhcpcd", "dhcpcd-openrc",
    "iproute2",
    # SSH (sftp-server is mandatory — modern scp uses SFTP, see
    # CLAUDE.md lesson #5)
    "openssh-server", "openssh-sftp-server",
    "openssh-client-default",
    # Time sync
    "chrony", "chrony-openrc",
    # Tools used during first-boot fix-up
    "tzdata",
)

# Conditional extras:
_WIFI_PACKAGES = (
    "wpa_supplicant", "wpa_supplicant-openrc",
    # BCM43-family firmware for the Pi's onboard radios — needed
    # for Pi 3/4/5/Zero W's built-in wifi to work at all.
    "linux-firmware-brcm",
)


def bake(
    *,
    node: NodeConfig,
    out_path: Path,
    arch: str = "aarch64",
    alpine_version: str = "3.21.4",
    extra_packages: list[str] | None = None,
    image_size_mb: int | None = None,
) -> Path:
    """Bake an Alpine sys-mode .img.gz for `node`.

    Sudo required (losetup + mount + mkfs).

    `alpine_version` — point release ("3.21.4") OR "edge". When
    "edge", `/etc/apk/repositories` in the baked image points at
    edge so `apk upgrade` post-boot rolls forward to edge versions;
    the bootstrap itself uses the latest stable branch (edge ships
    apks but no installer assumptions, so we install from stable
    and let post-boot upgrade do its thing).

    `extra_packages` — operator's `packages:` list. These join the
    baseline `apk add` so init's already-installed state matches
    what the diskless backend would have produced.

    `image_size_mb` — total image size. Default 2 GB.
    """
    # Privilege check: ext4 bake needs CAP_SYS_ADMIN (losetup + mount
    # + mkfs). The `imgxz._sudo` helper transparently handles the
    # root-vs-sudo distinction; we only need to make sure ONE of
    # them is available. Running as root: fine. Running as a normal
    # user: need sudo on PATH so the helper can elevate.
    if os.geteuid() != 0 and shutil.which("sudo") is None:
        raise RuntimeError(
            "alpine_ext4 backend needs root or sudo (losetup + mount). "
            "Either run as root or install sudo."
        )
    imgxz._require_tools(
        "losetup", "mount", "umount",
        "sfdisk", "mkfs.vfat", "mkfs.ext4", "xz",
    )

    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    size_mb = image_size_mb if image_size_mb else DEFAULT_IMAGE_SIZE_MB
    if size_mb < BOOT_PART_MB + 512:
        raise ValueError(
            f"image_size_mb={size_mb} too small; need at least "
            f"{BOOT_PART_MB + 512} MB (FAT /boot + minimal root)"
        )

    # Pin bootstrap to the latest stable branch even when target is
    # edge — Alpine's edge installer assumptions can drift, and we
    # only need a working initial system; post-boot `apk upgrade`
    # rolls forward to edge.
    if alpine_version == "edge":
        bootstrap_branch = "v3.21"
        runtime_branch = "edge"
    else:
        minor = ".".join(alpine_version.split(".")[:2])
        bootstrap_branch = f"v{minor}"
        runtime_branch = bootstrap_branch

    LOG.info(
        "alpine ext4 bake: hostname=%s arch=%s bootstrap=%s runtime=%s size=%d MB",
        node.hostname, arch, bootstrap_branch, runtime_branch, size_mb,
    )

    pkgs = list(_BASELINE_PACKAGES)
    if node.has_wifi:
        pkgs.extend(_WIFI_PACKAGES)
    if extra_packages:
        # Dedup but preserve insertion order
        seen = set(pkgs)
        for p in extra_packages:
            if p not in seen:
                pkgs.append(p)
                seen.add(p)

    with tempfile.TemporaryDirectory(prefix="pi-bake-alpine-ext4-") as td:
        td = Path(td)
        raw = td / "image.img"

        # Sequence: blank image → partition table → mkfs (separate
        # losetup attach/detach so mount_image sees a partitioned
        # AND formatted image) → mount_image for the actual writes.
        _create_blank_image(raw, size_mb)
        _partition_image(raw)
        _format_partitions(raw)

        mi = imgxz.mount_image(raw, td / "mounts")
        try:
            boot_mnt = mi.mounts[1]
            root_mnt = mi.mounts[2]

            # 1. Bind-mount /boot inside the rootfs so apk's
            # `raspberrypi-bootloader` post-install (when we run
            # `apk fix` on first boot) lands its blobs on FAT.
            # We don't bind-mount at bake time (different mount
            # tree); instead we make /boot a normal subdir of root
            # during install, then move the files to FAT after.
            _bootstrap_alpine(
                root_mnt=root_mnt,
                arch=arch,
                bootstrap_branch=bootstrap_branch,
                packages=pkgs,
            )

            # 1.5. Recreate busybox applet symlinks. The bootstrap
            # used --no-scripts (cross-arch safety), which skipped
            # busybox's post-install hook that creates /sbin/init,
            # /bin/sh, and ~302 other symlinks to /bin/busybox.
            # Without /sbin/init the kernel panics at userspace
            # exec → fix this BEFORE the system has to boot.
            _install_busybox_symlinks(root_mnt)

            # 2. Move /boot contents from rootfs onto the FAT
            # partition. raspberrypi-bootloader writes its files
            # into the rootfs's /boot dir; the Pi bootloader reads
            # from the FIRST partition (FAT). Move + bind-mount via
            # fstab so the running system sees them as /boot.
            _migrate_boot_to_fat(root_mnt=root_mnt, boot_mnt=boot_mnt)

            # 3. Write per-node config (hostname, network, ssh, …)
            _write_root_config(
                root_mnt=root_mnt, boot_mnt=boot_mnt, node=node,
                runtime_branch=runtime_branch,
                packages=pkgs,
            )
            _write_boot_config(
                boot_mnt=boot_mnt, root_mnt=root_mnt,
                node=node, raw_img=raw,
            )

            # 4. Install the first-boot fix-up service. Runs once
            # to finalize any pending apk triggers (since we
            # installed with --no-scripts), then removes itself.
            _install_firstboot_fix_service(root_mnt=root_mnt)

            # 5. OpenRC runlevel set-up. We can't run `rc-update
            # add` (chroot+exec), so we manipulate the symlinks
            # directly — that's all rc-update does.
            _configure_runlevels(root_mnt=root_mnt, node=node)
        finally:
            imgxz.unmount_image(mi)

        # 6. xz-compress to operator's out path. Match the existing
        # backends' naming: .img.xz.
        if out_path.suffix == ".gz":
            # Map .img.gz → .img.xz silently? No — be loud about
            # what we produce.
            LOG.warning(
                "alpine_ext4 produces .img.xz, not .img.gz; output "
                "extension will be ignored — final file: %s.xz",
                out_path,
            )
            out_path = out_path.with_suffix(".xz")
        elif out_path.suffix != ".xz":
            out_path = Path(str(out_path) + ".xz")
        imgxz.recompress_xz(raw, out_path)

    LOG.info("DONE: %s (%d MB)", out_path, out_path.stat().st_size >> 20)
    return out_path


# --------------------------------------------------------------------------- #
# Image creation                                                               #
# --------------------------------------------------------------------------- #

def _create_blank_image(path: Path, size_mb: int) -> None:
    """Allocate a zero-filled image file of exactly `size_mb` MB."""
    size_bytes = size_mb * 1024 * 1024
    with open(path, "wb") as f:
        f.truncate(size_bytes)
    LOG.info("blank image: %s (%d MB)", path, size_mb)


def _partition_image(path: Path) -> None:
    """sfdisk an MBR with two partitions: FAT32 boot + ext4 root.

    MBR (not GPT) — Pi firmware reads MBR/FAT to find the bootcode.
    Partition 1 is type `c` (FAT32 LBA). Partition 2 is type `83`
    (Linux). Total layout:

      sector 0..2047     :  reserved (no FS, just the MBR + gap)
      sector 2048..      :  p1 FAT32 boot (BOOT_PART_MB)
      then               :  p2 ext4 root  (rest of image)
    """
    boot_start = 2048              # standard 1 MiB alignment
    boot_size_sectors = BOOT_PART_MB * 2048
    root_start = boot_start + boot_size_sectors
    script = (
        "label: dos\n"
        f"unit: sectors\n"
        f"start={boot_start}, size={boot_size_sectors}, type=c, bootable\n"
        f"start={root_start}, type=83\n"
    )
    LOG.info("partitioning %s (boot=%d MB FAT32 + ext4 rest)",
             path.name, BOOT_PART_MB)
    p = subprocess.Popen(
        ["sfdisk", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = p.communicate(script.encode())
    if p.returncode != 0:
        raise RuntimeError(
            f"sfdisk failed (rc={p.returncode}):\n"
            f"--- script ---\n{script}\n"
            f"--- stdout ---\n{out.decode(errors='replace')}\n"
            f"--- stderr ---\n{err.decode(errors='replace')}"
        )


def _format_partitions(raw_img: Path) -> None:
    """Run mkfs.vfat + mkfs.ext4 on each partition.

    Uses per-partition loop devices (offset+sizelimit) via
    `imgxz.attach_partition_loops` so we never touch major-259
    partition nodes — those can be blocked by incus/lxc cgroup-BPF
    device controllers even in privileged containers. Each partition
    here is its own top-level loop dev (major 7), which is universally
    allowed.
    """
    loops = imgxz.attach_partition_loops(raw_img)
    try:
        boot_dev = loops[1]
        root_dev = loops[2]
        LOG.info("mkfs.vfat -F32 -n PI-BAKE %s", boot_dev)
        imgxz._sudo("mkfs.vfat", "-F", "32", "-n", "PI-BAKE", boot_dev,
                    capture=False)
        LOG.info("mkfs.ext4 -L alpine-root %s", root_dev)
        imgxz._sudo("mkfs.ext4", "-q", "-F", "-L", "alpine-root",
                    root_dev, capture=False)
    finally:
        imgxz.detach_loops(loops)


# --------------------------------------------------------------------------- #
# Bootstrap: apk-tools-static --no-scripts install                             #
# --------------------------------------------------------------------------- #

def _install_busybox_symlinks(root_mnt: Path) -> None:
    """Recreate the busybox applet symlinks that `apk add --no-scripts`
    skipped at bootstrap time.

    On Alpine, `/sbin/init`, `/bin/sh`, `/sbin/reboot`, etc. (~304
    paths total) are symlinks to `/bin/busybox`. They're created at
    package install time by busybox's `.post-install` script which
    runs `busybox --install -s`. We can't execute aarch64 busybox on
    an x86_64 bake host (would require qemu-user-static), so we
    replicate the symlink creation in Python.

    The applet manifest is at `/etc/busybox-paths.d/busybox` inside
    the bootstrapped rootfs — a plain-text list of relative paths
    (one per line), shipped by the busybox apk itself, that lists
    every applet path. We iterate it and `ln -s /bin/busybox` to
    each. Idempotent: skips if a file/symlink already exists at the
    target path.

    Without this step the kernel boots, mounts root, then panics with
    "Cannot find init" — observed firsthand 2026-05-27 bake test.

    Privileges: the mount is root-owned when the bake's `sudo mount`
    ran as a normal user with sudo (the documented local-laptop flow).
    Native Python writes into it fail with PermissionError. Fix:
    detect writability once, then either do native ops (fast — used
    by tests and by root-in-LXC bakes that own the mount) or batch
    the whole symlink set into a single `sudo sh -c '...'` invocation
    (~300 applet paths in one shell-out, not 300 sudo subprocesses).
    """
    import shlex
    manifest = root_mnt / "etc" / "busybox-paths.d" / "busybox"
    if not manifest.is_file():
        raise RuntimeError(
            f"busybox manifest missing: {manifest}. The busybox apk "
            f"didn't install correctly — check the `apk add` output."
        )
    # Decide once: can we write into the mount as our own euid? If yes,
    # native pathlib ops are fastest. If no (typical: non-root operator
    # who sudo-mounted a partition root owns), batch via `sudo sh -c`.
    need_sudo = not os.access(root_mnt, os.W_OK)
    n_created = 0
    n_skipped = 0
    script_lines: list[str] = ["set -e"]
    parents_seen: set[Path] = set()
    for rel_path in manifest.read_text().splitlines():
        rel_path = rel_path.strip()
        if not rel_path or rel_path.startswith("#"):
            continue
        target = root_mnt / rel_path
        # Skip if anything already lives at the target path (file,
        # symlink, or directory). Other apks may have shipped real
        # binaries at these paths.
        if target.exists() or target.is_symlink():
            n_skipped += 1
            continue
        if need_sudo:
            # Defer all writes to one batched shell-out below.
            if target.parent not in parents_seen:
                script_lines.append(
                    f"mkdir -p {shlex.quote(str(target.parent))}"
                )
                parents_seen.add(target.parent)
            script_lines.append(
                f"ln -s /bin/busybox {shlex.quote(str(target))}"
            )
        else:
            # Native fast path — bake runs as root (LXC), or test owns
            # the temp tree, etc.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to("/bin/busybox")
        n_created += 1
    if need_sudo and n_created > 0:
        imgxz._sudo("sh", "-c", "\n".join(script_lines), capture=False)
    LOG.info(
        "busybox symlinks: %d created (incl. /sbin/init), %d skipped",
        n_created, n_skipped,
    )
    # Final sanity: /sbin/init MUST exist now or boot will panic.
    if not (root_mnt / "sbin" / "init").is_symlink() and not (
        root_mnt / "sbin" / "init"
    ).is_file():
        raise RuntimeError(
            "/sbin/init still missing after busybox symlink install — "
            "manifest may have an unexpected format. Check the manifest "
            "at /etc/busybox-paths.d/busybox."
        )


def _bootstrap_alpine(
    *, root_mnt: Path, arch: str, bootstrap_branch: str,
    packages: list[str],
) -> None:
    """apk.static --root <mnt> --initdb --no-scripts add <pkgs>.

    Pulls each package + its recursive deps from upstream Alpine
    repos. Cross-arch via `--arch`. Trust set comes from the host's
    `/etc/apk/keys/` (apk-tools-static carries Alpine's signing
    keys); we also drop the result into the target's
    `/etc/apk/keys/`.

    `--no-scripts` is key: target binaries are aarch64 on an
    x86_64 host. The first-boot fix-up service finishes any
    pending triggers.
    """
    apk_static = apkfetch.ensure_apk_static()
    # apk's stock key set lives next to the apk.static binary's
    # parent dir's etc/ — but apkfetch only extracts the binary.
    # Best source of trusted Alpine keys: the apkfetch helper that
    # extracts them from a known initramfs. We don't have an
    # initramfs at this point (we're bootstrapping from scratch).
    # Workaround: fetch the apkfetch helper's apk-keys-* package
    # equivalent OR download keys directly. Simpler: vendor the
    # current Alpine release keys here. They rotate rarely.
    #
    # Pragmatic v0.1 approach: fetch alpine-keys.apk and extract
    # /etc/apk/keys from it into a temp dir, use that as
    # --keys-dir for the install + leave the resulting
    # /etc/apk/keys/ in the target root.
    keys_dir = _fetch_alpine_keys(arch=arch, branch=bootstrap_branch)

    repo_base = f"https://dl-cdn.alpinelinux.org/alpine/{bootstrap_branch}"
    # Use imgxz._sudo, which handles the root-vs-sudo distinction:
    # prefixes with `sudo` only when euid != 0. Lets us run on
    # containers as root without sudo installed.
    apk_args = [
        str(apk_static),
        "--root", str(root_mnt),
        "--arch", arch,
        "--initdb",
        "--keys-dir", str(keys_dir),
        "-X", f"{repo_base}/main",
        "-X", f"{repo_base}/community",
        "--no-scripts",
        "--no-cache",
        "add",
        *packages,
    ]
    LOG.info("apk bootstrap: %d package(s) → %s",
             len(packages), root_mnt)
    # _sudo raises RuntimeError on non-zero with stderr surfaced —
    # which is what we want here for apk fetch failures.
    imgxz._sudo(*apk_args)

    # Copy keys into target's /etc/apk/keys (apk init didn't do
    # this since we passed --keys-dir pointing at our temp dir).
    target_keys = root_mnt / "etc" / "apk" / "keys"
    imgxz._sudo("mkdir", "-p", str(target_keys), capture=False)
    for k in keys_dir.iterdir():
        if k.is_file():
            imgxz._sudo("cp", str(k), str(target_keys / k.name),
                        capture=False)


def _fetch_alpine_keys(*, arch: str, branch: str) -> Path:
    """Get Alpine's per-arch signing pubkeys for `branch` + `arch`.

    Downloads `alpine-keys-<ver>.apk` from upstream and extracts
    the per-arch keys + common keys into a cache dir (one cache
    per branch+arch combo).

    apk-tools-static needs --keys-dir to verify upstream APKINDEX
    signatures during the initial install. Each Alpine release
    arch is signed by a DIFFERENT key (aarch64 uses different keys
    than x86_64), so we must extract the target-arch keys, not
    just `etc/apk/keys/` (which only carries the host-arch subset
    that ships in the installed package).

    The alpine-keys package layout is:
        etc/apk/keys/                  — host-arch keys only
        usr/share/apk/keys/            — common + per-arch subdirs
        usr/share/apk/keys/aarch64/    — aarch64-specific keys
        usr/share/apk/keys/x86_64/     — x86_64-specific keys
        ...

    We grab both `usr/share/apk/keys/*.rsa.pub` (common) and
    `usr/share/apk/keys/<arch>/*.rsa.pub` (per-arch) into one flat
    keys-dir for apk.static.
    """
    from pi_bake.download import fetch, cache_dir
    cache = cache_dir() / "alpine-keys" / f"{branch}-{arch}"
    keys_dir = cache / "keys"
    if keys_dir.is_dir() and any(keys_dir.iterdir()):
        return keys_dir
    cache.mkdir(parents=True, exist_ok=True)
    keys_dir.mkdir(parents=True, exist_ok=True)
    # Fetch alpine-keys for the host arch path (the .apk file itself
    # is content-identical across architectures — it's an
    # arch-independent collection of key files).
    # Version pin: alpine-keys is versioned slowly (2.5-r0 has been
    # stable on v3.20+). Bump if Alpine releases a new alpine-keys
    # major version; the .apk URL 404s if wrong, surfacing the
    # mismatch immediately.
    host_arch_slug = apkfetch.host_arch()
    url = (
        f"https://dl-cdn.alpinelinux.org/alpine/{branch}/main/"
        f"{host_arch_slug}/alpine-keys-2.5-r0.apk"
    )
    LOG.info("alpine-keys: fetching %s", url)
    apk_file = fetch(url)
    # Extract into a staging dir, then flatten the relevant subtrees
    # into keys_dir.
    stage = cache / "stage"
    stage.mkdir(exist_ok=True)
    subprocess.run(
        ["tar", "-xzf", str(apk_file), "-C", str(stage),
         "usr/share/apk/keys"],
        check=True, capture_output=True, text=True,
    )
    src_root = stage / "usr" / "share" / "apk" / "keys"
    # Common keys (directly under usr/share/apk/keys/)
    for k in src_root.glob("*.rsa.pub"):
        shutil.copy2(k, keys_dir / k.name)
    # Per-arch keys (under usr/share/apk/keys/<arch>/)
    arch_dir = src_root / arch
    if arch_dir.is_dir():
        for k in arch_dir.glob("*.rsa.pub"):
            shutil.copy2(k, keys_dir / k.name)
    else:
        LOG.warning(
            "alpine-keys: no per-arch dir for %r in alpine-keys "
            "package — apk verification may fail for %s repos",
            arch, arch,
        )
    shutil.rmtree(stage, ignore_errors=True)
    n_keys = len(list(keys_dir.glob("*.rsa.pub")))
    LOG.info("alpine-keys: extracted %d keys for arch=%s",
             n_keys, arch)
    return keys_dir


# --------------------------------------------------------------------------- #
# Boot-partition migration + config                                            #
# --------------------------------------------------------------------------- #

def _migrate_boot_to_fat(*, root_mnt: Path, boot_mnt: Path) -> None:
    """Move /boot contents from rootfs onto the FAT partition.

    `raspberrypi-bootloader` + `linux-rpi` install their files
    into `<root>/boot/`. For the Pi to boot, those files must be
    on the FAT partition (the Pi firmware doesn't speak ext4).
    We move them, then leave `<root>/boot` empty as a mount point
    for the fstab entry to bind /dev/mmcblk0p1 over it.
    """
    src = root_mnt / "boot"
    if not src.is_dir():
        raise RuntimeError(
            f"{src} doesn't exist after apk bootstrap — linux-rpi or "
            f"raspberrypi-bootloader may have failed to install"
        )
    # Move every item (incl. dotfiles) from <root>/boot/ → <boot>/.
    # We can't just `mv` because the source dir is owned by root
    # inside a sudo-mounted partition. Use rsync via sudo.
    if shutil.which("rsync") is not None:
        imgxz._sudo(
            "rsync", "-aH", "--remove-source-files",
            f"{src}/", f"{boot_mnt}/",
            capture=False,
        )
        # rsync --remove-source-files leaves empty dirs behind.
        imgxz._sudo("find", str(src), "-mindepth", "1", "-type",
                    "d", "-empty", "-delete", capture=False)
    else:
        # Fallback: cp -a + rm -r.
        imgxz._sudo("sh", "-c",
                    f"cp -a {src}/. {boot_mnt}/ && "
                    f"find {src} -mindepth 1 -delete",
                    capture=False)
    LOG.info("boot: migrated rootfs /boot/* → FAT partition")


def _write_boot_config(
    *, boot_mnt: Path, root_mnt: Path,
    node: NodeConfig, raw_img: Path,
) -> None:
    """Write /boot/cmdline.txt + /boot/config.txt + usercfg.txt.

    cmdline.txt: kernel command line. Point root at the ext4
    partition by PARTUUID (stable across reflashes; doesn't depend
    on the mmcblk device name).

    config.txt: Pi firmware reads this. We write a minimal one
    that loads the kernel + DTB Pi-side. raspberrypi-bootloader's
    package may ship one; if so, we replace it with ours so
    operator additions land predictably.

    usercfg.txt: operator-declared config_txt: lines. config.txt
    `include`s it, so operator edits layer cleanly on top of our
    defaults.
    """
    # cmdline.txt — root by PARTUUID for stability. The MBR
    # disk identifier comes from sfdisk's auto-generated UUID;
    # blkid against the loop dev gets it.
    root_partuuid = _read_root_partuuid(raw_img)
    # No `modules=` — that's an Alpine-initramfs hint and we don't
    # ship an initramfs (kernel mounts root directly; see
    # _write_boot_config for the rationale).
    #
    # No `quiet` — pi-bake's first-boot story is "the operator wants
    # to know what went wrong on the serial console". Removing the
    # ~30 lines of "quiet"-suppressed kernel boot messages saves no
    # time and hides exactly the diagnostics first-boot bugs need.
    cmdline = (
        f"root=PARTUUID={root_partuuid}-02 rootfstype=ext4 rootwait "
        f"console=tty1 console=serial0,115200 "
        f"net.ifnames=0\n"
    )
    imgxz.write_file(boot_mnt, "cmdline.txt", cmdline, mode=0o644)
    LOG.info("boot: cmdline.txt → root=PARTUUID=%s-02", root_partuuid)

    # config.txt — replace whatever raspberrypi-bootloader shipped
    # with our explicit baseline. Pi 4/5 want arm_64bit=1 for the
    # aarch64 kernel; the kernel filename is `vmlinuz-rpi` on
    # Alpine (NOT kernel8.img like Pi OS).
    #
    # NO initramfs reference: Alpine's linux-rpi kernel has
    # CONFIG_EXT4_FS=y + CONFIG_MMC_BLOCK=y + CONFIG_MMC_BCM2835=y
    # (all built-in), so it mounts the ext4 root from mmcblk0
    # directly via cmdline.txt's PARTUUID — no initramfs needed.
    # This sidesteps Alpine's mkinitfs hook (which we skip at bake
    # time via apk --no-scripts since it doesn't run cross-arch).
    config_txt = (
        "# pi-bake — Alpine sys-mode (ext4)\n"
        "# Auto-generated. Operator additions go in usercfg.txt.\n"
        "\n"
        "arm_64bit=1\n"
        "enable_uart=1\n"
        "kernel=vmlinuz-rpi\n"
        # `dtoverlay=` etc. for HATs/peripherals: operator's
        # config_txt: ends up in usercfg.txt; we just include it.
        "\n"
        "include usercfg.txt\n"
    )
    imgxz.write_file(boot_mnt, "config.txt", config_txt, mode=0o644)

    # usercfg.txt — operator's config_txt: lines (or empty
    # placeholder so config.txt's `include` doesn't warn).
    if node.config_txt:
        body = (
            "# pi-bake — operator-declared HAT/peripheral overlays\n"
            + "\n".join(node.config_txt) + "\n"
        )
    else:
        body = "# usercfg.txt — operator overrides go here\n"
    imgxz.write_file(boot_mnt, "usercfg.txt", body, mode=0o644)
    if node.config_txt:
        LOG.info("boot: usercfg.txt += %d line(s)",
                 len(node.config_txt))


def _read_root_partuuid(raw_img: Path) -> str:
    """Get the MBR disk identifier (8 hex digits) from the image.

    Linux's PARTUUID for MBR partitions is `<disk-id>-<part-num>`
    where disk-id is the 4-byte signature at offset 0x1B8 of the
    MBR. We read it via dd + hex.
    """
    with open(raw_img, "rb") as f:
        f.seek(0x1B8)
        raw = f.read(4)
    if len(raw) != 4:
        raise RuntimeError(
            f"couldn't read MBR disk signature from {raw_img}"
        )
    # Little-endian → hex.
    val = int.from_bytes(raw, "little")
    return f"{val:08x}"


# --------------------------------------------------------------------------- #
# Root-partition config (hostname, network, ssh, fstab, …)                     #
# --------------------------------------------------------------------------- #

def _write_root_config(
    *, root_mnt: Path, boot_mnt: Path, node: NodeConfig,
    runtime_branch: str, packages: list[str],
) -> None:
    """Per-node config files in the ext4 root."""

    # /etc/hostname
    imgxz.write_file(root_mnt, "etc/hostname",
                     f"{node.hostname}\n", mode=0o644)
    # /etc/hosts — Alpine ships a default; we replace with a
    # 127.0.0.1/::1 + hostname entry that matches what setup-alpine
    # would have written.
    imgxz.write_file(
        root_mnt, "etc/hosts",
        f"127.0.0.1\t{node.hostname} localhost.localdomain localhost\n"
        f"::1\t\t{node.hostname} localhost.localdomain localhost\n",
        mode=0o644,
    )
    LOG.info("root: hostname=%s", node.hostname)

    # /etc/timezone + /etc/localtime symlink
    imgxz.write_file(root_mnt, "etc/timezone",
                     f"{node.timezone}\n", mode=0o644)
    # /etc/localtime symlink to the tzdata zone file.
    imgxz._sudo("ln", "-sf",
                f"/usr/share/zoneinfo/{node.timezone}",
                str(root_mnt / "etc" / "localtime"),
                capture=False)

    # /etc/fstab — boot FAT bind-mounted at /boot, root ext4 at /
    fstab = (
        f"# pi-bake — Alpine sys-mode (ext4)\n"
        f"LABEL=alpine-root  /       ext4   defaults,noatime  0 1\n"
        f"LABEL=PI-BAKE      /boot   vfat   defaults          0 2\n"
        f"tmpfs              /tmp    tmpfs  defaults,nosuid,nodev  0 0\n"
        f"proc               /proc   proc   defaults          0 0\n"
        f"sysfs              /sys    sysfs  defaults          0 0\n"
    )
    imgxz.write_file(root_mnt, "etc/fstab", fstab, mode=0o644)

    # /etc/apk/repositories — runtime branch (may be edge!) so
    # post-boot `apk upgrade` rolls forward if operator selected
    # os_version: edge.
    repos = (
        f"http://dl-cdn.alpinelinux.org/alpine/{runtime_branch}/main\n"
        f"http://dl-cdn.alpinelinux.org/alpine/{runtime_branch}/community\n"
    )
    imgxz.write_file(root_mnt, "etc/apk/repositories", repos, mode=0o644)
    if runtime_branch == "edge":
        LOG.info("root: /etc/apk/repositories → edge (post-boot `apk "
                 "upgrade` will roll kernel forward)")

    # /etc/apk/world — the world is what the system "wants
    # installed". apk maintains this file but since we used
    # --no-scripts the file may or may not be in the right shape.
    # Write it explicitly to match `packages`.
    world_text = "\n".join(sorted(set(packages))) + "\n"
    imgxz.write_file(root_mnt, "etc/apk/world", world_text, mode=0o644)

    # /etc/securetty — ensure tty1 + serial console are allowed
    # for root (Alpine default usually has them but sys-mode
    # bootstrap can miss this).
    imgxz.write_file(
        root_mnt, "etc/securetty",
        "tty1\ntty2\ntty3\ntty4\ntty5\ntty6\nttyAMA0\nttyS0\n",
        mode=0o600,
    )

    # SSH host keys — pre-baked or auto-generated.
    _write_ssh_host_keys(root_mnt=root_mnt, node=node)

    # /root/.ssh/authorized_keys
    imgxz.write_file(
        root_mnt, "root/.ssh/authorized_keys",
        node.authorized_keys_text(), mode=0o600,
    )
    imgxz._sudo("chmod", "700", str(root_mnt / "root" / ".ssh"),
                capture=False)
    LOG.info("root: /root/.ssh/authorized_keys (%d key%s)",
             len(node.all_pubkeys),
             "" if len(node.all_pubkeys) == 1 else "s")

    # /etc/ssh/sshd_config — same lessons as Alpine diskless
    # (lesson #3: no UsePAM, no ChallengeResponseAuthentication).
    sshd_conf = (
        "# pi-bake — Alpine sys-mode sshd_config\n"
        "PermitRootLogin prohibit-password\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PubkeyAuthentication yes\n"
        "Subsystem sftp /usr/lib/ssh/sftp-server\n"
    )
    imgxz.write_file(root_mnt, "etc/ssh/sshd_config",
                     sshd_conf, mode=0o644)

    # /etc/dhcpcd.conf — hostname option 12 + optional static IP
    # (CLAUDE.md lesson #4: dhcpcd without `hostname` directive
    # sends nothing to the DHCP server).
    dhcpcd_conf = (
        "# pi-bake — dhcpcd config\n"
        "duid\n"
        "persistent\n"
        "vendorclassid\n"
        "option domain_name_servers, domain_name, domain_search\n"
        "option classless_static_routes\n"
        "option interface_mtu\n"
        "require dhcp_server_identifier\n"
        "slaac private\n"
    )
    if node.dhcp_send_hostname:
        dhcpcd_conf += "hostname\n"
    if node.has_static_ip:
        dhcpcd_conf += (
            f"\n# pi-bake — operator static IP\n"
            f"interface eth0\n"
            f"static ip_address={node.static_ipv4}\n"
            f"static routers={node.gateway_ipv4}\n"
            f"static domain_name_servers=1.1.1.1 8.8.8.8\n"
        )
        LOG.info("root: dhcpcd static IP %s via %s",
                 node.static_ipv4, node.gateway_ipv4)
    imgxz.write_file(root_mnt, "etc/dhcpcd.conf",
                     dhcpcd_conf, mode=0o644)

    # wpa_supplicant for wifi
    if node.has_wifi:
        wpa_conf_path = "etc/wpa_supplicant/wpa_supplicant.conf"
        imgxz.write_file(root_mnt, wpa_conf_path,
                         node.wpa_supplicant_conf(), mode=0o600)
        LOG.info("root: wpa_supplicant.conf for ssid=%r",
                 node.wifi_ssid)

    # /etc/modules — operator-declared kernel modules
    if node.modules:
        body = (
            "# pi-bake — operator-declared kernel modules\n"
            + "\n".join(node.modules) + "\n"
        )
        imgxz.append_file(root_mnt, "etc/modules", body)
        LOG.info("root: /etc/modules += %d module(s)",
                 len(node.modules))


def _write_ssh_host_keys(*, root_mnt: Path, node: NodeConfig) -> None:
    """Pre-bake operator-provided host key OR auto-generate ed25519.

    Same logic as alpine.py — predictable identity → no
    known_hosts churn across rebuilds.
    """
    if node.ssh_host_key_priv and node.ssh_host_key_pub:
        ktype = node.ssh_host_key_type
        imgxz.write_file(
            root_mnt, f"etc/ssh/ssh_host_{ktype}_key",
            node.ssh_host_key_priv, mode=0o600,
        )
        imgxz.write_file(
            root_mnt, f"etc/ssh/ssh_host_{ktype}_key.pub",
            node.ssh_host_key_pub, mode=0o644,
        )
        LOG.info("root: pre-baked ssh_host_%s_key", ktype)
        return
    # Auto-generate fresh ed25519 pair at bake time so the Pi has
    # a stable identity across reflashes of the same .img.xz.
    if shutil.which("ssh-keygen") is None:
        LOG.warning(
            "ssh-keygen not on PATH — host keys will be generated "
            "on first boot (loses stable-identity property)"
        )
        return
    with tempfile.TemporaryDirectory(prefix="pi-bake-hostkey-") as td:
        td = Path(td)
        key = td / "ssh_host_ed25519_key"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "",
             "-C", f"pi-bake@{node.hostname}",
             "-f", str(key), "-q"],
            check=True, capture_output=True, text=True,
        )
        imgxz.write_file(
            root_mnt, "etc/ssh/ssh_host_ed25519_key",
            key.read_bytes(), mode=0o600,
        )
        imgxz.write_file(
            root_mnt, "etc/ssh/ssh_host_ed25519_key.pub",
            (td / "ssh_host_ed25519_key.pub").read_bytes(),
            mode=0o644,
        )
    LOG.info("root: auto-generated ed25519 host key")


# --------------------------------------------------------------------------- #
# OpenRC runlevels + first-boot fix-up service                                 #
# --------------------------------------------------------------------------- #

# Services to add to each runlevel. We do this by symlink (the same
# action `rc-update add <svc> <level>` performs) since we can't
# exec aarch64 rc-update on an x86_64 host.
_RUNLEVELS = {
    "sysinit": ("devfs", "dmesg", "mdev", "hwdrivers", "cgroups"),
    "boot": ("modules", "sysctl", "hostname", "bootmisc", "syslog"),
    "default": ("dhcpcd", "sshd", "chronyd", "pi-bake-firstboot-fix"),
    "shutdown": ("mount-ro", "killprocs", "savecache"),
}
# Conditional: wpa_supplicant joins default when wifi is on.


def _configure_runlevels(*, root_mnt: Path, node: NodeConfig) -> None:
    """Create /etc/runlevels/<level>/<svc> → ../../init.d/<svc> symlinks.

    This is exactly what rc-update does. Doing it as symlinks
    avoids invoking the aarch64 rc-update binary on the host.
    """
    levels = {k: list(v) for k, v in _RUNLEVELS.items()}
    if node.has_wifi:
        levels["default"].append("wpa_supplicant")
    for level, svcs in levels.items():
        level_dir = root_mnt / "etc" / "runlevels" / level
        imgxz._sudo("mkdir", "-p", str(level_dir), capture=False)
        for svc in svcs:
            init_d = root_mnt / "etc" / "init.d" / svc
            link = level_dir / svc
            # The init.d script may not exist (e.g. for the first-
            # boot service we install separately). For the
            # baseline services, they're installed by alpine-base
            # / openrc / openssh-server / etc.
            target = f"/etc/init.d/{svc}"
            imgxz._sudo(
                "ln", "-sf", target, str(link),
                capture=False,
            )
    LOG.info("root: openrc runlevels configured (%d services)",
             sum(len(v) for v in levels.values()))


# The first-boot fix-up service. apk install was --no-scripts at
# bake time, so a handful of post-install hooks didn't run:
#   - depmod (Alpine ships pre-built modules.dep with linux-rpi,
#     so this is mostly already done — but custom modules from
#     extra_packages may need it)
#   - sshd-keygen for any missing host key types
#   - mkinitfs (if it didn't run during bootstrap)
#
# We install an init.d script that runs `apk fix --no-network` to
# trigger those, then removes itself from the default runlevel so
# it only runs ONCE.

_FIRSTBOOT_FIX_SCRIPT = """#!/sbin/openrc-run
# pi-bake — first-boot apk fix-up
# Runs `apk fix --no-network` to finalize any pending package
# triggers that bake-time `--no-scripts` skipped. Then removes
# itself from the default runlevel so it only runs once.

description="pi-bake: finalize bake-time apk install (one-shot)"

depend() {
    after sysinit boot
    before sshd
}

start() {
    ebegin "pi-bake first-boot apk fix-up"
    if [ -f /var/lib/pi-bake/firstboot.done ]; then
        einfo "already done; skipping"
        eend 0
        return 0
    fi
    mkdir -p /var/lib/pi-bake
    # --no-network: rely on what's on disk; bake-time fetched
    # everything we need. --no-scripts already ran at bake time
    # is what we're now compensating for, so DON'T re-pass it.
    /sbin/apk fix --no-network 2>&1 | logger -t pi-bake-firstboot
    rc=${PIPESTATUS:-$?}
    if [ "$rc" -eq 0 ]; then
        touch /var/lib/pi-bake/firstboot.done
        # Self-remove from the default runlevel.
        /sbin/rc-update del pi-bake-firstboot-fix default 2>/dev/null || true
    fi
    eend "$rc"
}
"""


def _install_firstboot_fix_service(*, root_mnt: Path) -> None:
    """Drop the first-boot fix-up init script into /etc/init.d/."""
    imgxz.write_file(
        root_mnt, "etc/init.d/pi-bake-firstboot-fix",
        _FIRSTBOOT_FIX_SCRIPT, mode=0o755,
    )
    LOG.info("root: /etc/init.d/pi-bake-firstboot-fix installed "
             "(runs `apk fix` on first boot)")
