"""Bake-time Alpine edge kernel upgrade (`os_version: edge`).

The pi-bake v0.0.5 design treated `os_version: edge` as "use the
latest stable RPi tarball for bootloader/FAT layout, but point
`/etc/apk/repositories` at edge so post-boot `apk upgrade` rolls
the kernel forward."

That design **does not work in practice on Alpine RPi diskless**.
Operator reproduction: `uname -r` reports stable kernel forever,
`iwlwifi` modules missing, BE200 doesn't enumerate. The
post-boot upgrade path can't physically rewrite the kernel +
modules + initramfs because (1) FAT is mounted ro, (2) modloop
is squashfs (also ro), (3) `apk info -L linux-rpi` returns
empty (Alpine packages boot artefacts via install-time hooks,
not via the file list), (4) those hooks don't fire correctly
outside a fresh `setup-alpine` flow, (5) `mkinitfs` isn't
installed on the running Pi anyway.

The fix is to do the upgrade **at bake time** in a separate
chroot.

Architecture (v0.2.2 rewrite, after v0.2.1's first attempt
chrooted into the FAT tarball — which has no /sbin/apk and
fails immediately):

  1. Download alpine-minirootfs-<ver>-<arch>.tar.gz from
     Alpine's `releases/<arch>/` directory. This IS a chroot-
     ready busybox + musl + apk-tools rootfs (~3.8 MB), as
     opposed to the RPi tarball which is FAT-layout boot files
     + an apk cache (not chroot-able).
  2. Extract the minirootfs into a SEPARATE tmpdir — DON'T
     pollute the RPi tarball tree. We want to mcopy the RPi
     tarball into FAT untouched except for the boot artefacts
     we replace.
  3. Copy qemu-<arch>-static + /etc/resolv.conf into the
     chroot, bind-mount /proc /sys /dev (binfmt_misc handles
     transparent aarch64 binary execution).
  4. Point the chroot's /etc/apk/repositories at edge main +
     community.
  5. `apk update` + `apk upgrade --available --latest` to bring
     the whole chroot to edge (avoids musl/apk-tools version
     skew between stable minirootfs and edge linux-rpi).
  6. `apk add linux-rpi linux-firmware-rpi mkinitfs`. The
     mkinitfs install hook regenerates /boot/vmlinuz-rpi +
     /boot/initramfs-rpi + /boot/modloop-rpi inside the chroot.
  7. Copy those three boot artefacts FROM chroot/boot/ TO
     extracted_root/boot/ — replacing the stable versions that
     came with the RPi tarball. (modloop-rpi IS the squashfs
     of /lib/modules/<edge-ver>/, so this single copy carries
     the entire edge module set.)
  8. Cleanup: unmount binds, delete chroot dir, remove staged
     qemu binary.

Bake-host requirements
----------------------
- root (chroot needs CAP_SYS_CHROOT)
- qemu-user-static for the target arch (`qemu-aarch64-static` /
  `qemu-arm-static`)
- binfmt_misc registration for the target arch (usually
  auto-registered when qemu-user-static is installed via
  `systemd-binfmt` or `qemu-user-binfmt`)
- network access from the bake host to dl-cdn.alpinelinux.org

When these aren't available, the upgrade is SKIPPED with a clear
warning and the bake proceeds with the stable kernel. The image is
still bootable; it just doesn't carry the edge kernel.

Install paths on a typical Linux:
  - Fedora: `sudo dnf install qemu-user-static qemu-user-binfmt`
  - Debian/Ubuntu: `sudo apt install qemu-user-static binfmt-support`
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from pi_bake.download import fetch

LOG = logging.getLogger("pi_bake.alpine_edge")

# Alpine edge repository URLs. Apk inside the chroot points its
# /etc/apk/repositories at these for the upgrade.
EDGE_REPO_MAIN = "http://dl-cdn.alpinelinux.org/alpine/edge/main"
EDGE_REPO_COMMUNITY = "http://dl-cdn.alpinelinux.org/alpine/edge/community"

# Packages installed (with edge repos active) to bring in the
# edge kernel + firmware + boot-artefact generator. mkinitfs's
# install hook regenerates /boot/{vmlinuz-rpi,initramfs-rpi,
# modloop-rpi} when linux-rpi installs.
EDGE_UPGRADE_PACKAGES = ("mkinitfs", "linux-rpi", "linux-firmware-rpi")

# Boot artefacts we copy OUT of the chroot INTO the RPi tarball
# tree after the upgrade. Order: kernel, initramfs, kernel-module
# squashfs.
_BOOT_ARTEFACTS = ("vmlinuz-rpi", "initramfs-rpi", "modloop-rpi")


class EdgeKernelSkipped(Exception):
    """Raised by check_requirements() when the upgrade can't run.

    The caller catches this + logs the message + falls through to
    a stable-kernel bake. Operators get the message in the bake log
    so they know what to install if they want edge.
    """


def check_requirements(target_arch: str = "aarch64") -> None:
    """Verify the bake host can do the chroot+qemu kernel upgrade.

    Raises EdgeKernelSkipped with a clear message describing the
    missing piece. The caller logs + skips the upgrade.
    """
    # Root check
    if os.geteuid() != 0:
        raise EdgeKernelSkipped(
            "bake-time edge kernel upgrade requires root (chroot "
            "needs CAP_SYS_CHROOT). Re-run pi-bake with sudo OR "
            "from a privileged LXC container."
        )

    # qemu-user-static binary
    qemu_bin = f"qemu-{target_arch}-static"
    if shutil.which(qemu_bin) is None:
        raise EdgeKernelSkipped(
            f"{qemu_bin} not on PATH. Install:\n"
            f"  Fedora: sudo dnf install qemu-user-static qemu-user-binfmt\n"
            f"  Debian: sudo apt install qemu-user-static binfmt-support"
        )

    # binfmt_misc registration for the target arch
    binfmt_path = Path(f"/proc/sys/fs/binfmt_misc/qemu-{target_arch}")
    if not binfmt_path.is_file():
        raise EdgeKernelSkipped(
            f"binfmt_misc not registered for {target_arch}. Usually "
            f"auto-registered when qemu-user-static is installed via "
            f"systemd-binfmt or qemu-user-binfmt. To register manually:\n"
            f"  sudo systemctl restart systemd-binfmt"
        )

    # Confirm the binfmt entry is enabled (not just registered)
    content = binfmt_path.read_text()
    if "enabled" not in content:
        raise EdgeKernelSkipped(
            f"binfmt_misc for {target_arch} is registered but not "
            f"enabled. Enable: "
            f"sudo sh -c 'echo 1 > {binfmt_path}'"
        )

    # chroot binary
    if shutil.which("chroot") is None:
        raise EdgeKernelSkipped("chroot not on PATH (impossible?)")


def minirootfs_url(rpi_tarball_url: str) -> str:
    """Derive the matching alpine-minirootfs URL from a RPi tarball URL.

    Both URLs share the same {branch, arch, version} path structure;
    only the filename prefix differs:

      alpine-rpi-<ver>-<arch>.tar.gz       (FAT-layout, NOT chroot-able)
      alpine-minirootfs-<ver>-<arch>.tar.gz  (chroot-ready rootfs)

    Example:
      in  : .../v3.21/releases/aarch64/alpine-rpi-3.21.4-aarch64.tar.gz
      out : .../v3.21/releases/aarch64/alpine-minirootfs-3.21.4-aarch64.tar.gz
    """
    if "alpine-rpi-" not in rpi_tarball_url:
        raise ValueError(
            f"can't derive minirootfs URL from {rpi_tarball_url!r} "
            f"(expected 'alpine-rpi-' in path)"
        )
    return rpi_tarball_url.replace("alpine-rpi-", "alpine-minirootfs-")


def upgrade_to_edge_kernel(
    extracted_root: Path,
    *,
    rpi_tarball_url: str,
    workdir: Path,
    target_arch: str = "aarch64",
) -> None:
    """Replace the extracted RPi tarball's stable kernel with edge.

    Bakes a fresh chroot from alpine-minirootfs (the actual rootfs
    tarball — separate from the FAT-layout RPi tarball), upgrades
    that chroot's kernel to edge via apk-in-chroot + qemu-user-static,
    then copies the resulting /boot/{vmlinuz-rpi, initramfs-rpi,
    modloop-rpi} into `extracted_root/boot/`. The RPi tarball's
    other contents (bootcode.bin, start4.elf, overlays/, apks/,
    etc.) are left untouched — only the 3 boot artefacts get
    swapped to edge.

    Pre-conditions (check via `check_requirements()` first):
      - bake host is root
      - qemu-<target_arch>-static is installed + binfmt_misc registered
      - network access to dl-cdn.alpinelinux.org

    Side effects in `extracted_root` after this returns:
      - /boot/vmlinuz-rpi, /boot/initramfs-rpi, /boot/modloop-rpi:
        edge versions
      - Nothing else changed

    Side effects in `workdir`:
      - workdir/edge-chroot/ created (not cleaned up — caller's
        tempdir is normally a TemporaryDirectory, gets cleaned at
        with-block exit)
    """
    LOG.info("edge kernel upgrade: target=%s arch=%s",
             extracted_root, target_arch)
    check_requirements(target_arch)

    # Step 1. Download alpine-minirootfs at the same stable version
    # as the RPi tarball. download.fetch() caches + verifies sha256
    # against the .sha256 sidecar.
    mr_url = minirootfs_url(rpi_tarball_url)
    LOG.info("edge kernel upgrade: fetching minirootfs %s",
             mr_url.rsplit("/", 1)[-1])
    mr_tarball = fetch(mr_url)

    # Step 2. Extract minirootfs into a SEPARATE tmpdir.
    # filter="tar" (not "data"): Python 3.12+'s strict `data`
    # filter rejects absolute symlinks (AbsoluteLinkError), which
    # blows up on alpine-minirootfs because busybox is wired up
    # via absolute symlinks (/usr/bin/yes → /bin/busybox etc.).
    # We trust the tarball (sha256-verified against Alpine's
    # signed .sha256 sidecar by download.fetch()), so the looser
    # `tar` filter is correct: still blocks path traversal +
    # absolute paths, allows symlinks. Pre-3.12 Python lacks the
    # filter kwarg, so wrap.
    chroot = workdir / "edge-chroot"
    chroot.mkdir(parents=True, exist_ok=True)
    LOG.info("edge kernel upgrade: extracting minirootfs → %s", chroot)
    with tarfile.open(mr_tarball, "r:*") as tf:
        try:
            tf.extractall(chroot, filter="tar")
        except TypeError:
            tf.extractall(chroot)

    # Step 3. Copy qemu-static + resolv.conf into the chroot.
    # binfmt_misc with the `F` flag preserves the interpreter at
    # registration time, but per-distro defaults vary — copying
    # qemu into the chroot's /usr/bin is the belt-and-suspenders
    # path that works regardless.
    qemu_src = shutil.which(f"qemu-{target_arch}-static")
    assert qemu_src is not None   # check_requirements verified
    qemu_dst = chroot / "usr/bin" / Path(qemu_src).name
    qemu_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(qemu_src, qemu_dst)
    qemu_dst.chmod(0o755)

    host_resolv = Path("/etc/resolv.conf")
    chroot_resolv = chroot / "etc/resolv.conf"
    if host_resolv.is_file():
        chroot_resolv.parent.mkdir(parents=True, exist_ok=True)
        # `cp` (not symlink) because the chroot may resolve names
        # outside of host's resolver namespace.
        shutil.copy(host_resolv, chroot_resolv)

    # Step 4. Point chroot's apk at edge.
    repos = chroot / "etc/apk/repositories"
    repos.parent.mkdir(parents=True, exist_ok=True)
    repos.write_text(f"{EDGE_REPO_MAIN}\n{EDGE_REPO_COMMUNITY}\n")

    # Bind /proc /sys /dev — install hooks read /proc for kernel
    # info, mkinitfs reads /proc/self/mounts, etc.
    binds = ("proc", "sys", "dev")
    for d in binds:
        target = chroot / d
        target.mkdir(exist_ok=True)
        subprocess.run(
            ["mount", "--bind", f"/{d}", str(target)],
            check=True, capture_output=True, text=True,
        )

    try:
        # Step 5. Refresh apk's view of edge + upgrade base packages.
        # `apk upgrade --available --latest` brings the chroot's
        # musl/busybox/apk-tools to edge versions, avoiding any
        # cross-version dep weirdness when linux-rpi installs.
        LOG.info("edge kernel upgrade: apk update")
        _run_chroot(chroot, ["/sbin/apk", "update", "--no-cache"])

        LOG.info("edge kernel upgrade: apk upgrade --available --latest")
        _run_chroot(
            chroot,
            ["/sbin/apk", "upgrade", "--available", "--latest",
             "--no-cache"],
        )

        # Step 6. Install linux-rpi + linux-firmware-rpi + mkinitfs.
        # mkinitfs's post-install hook regenerates the boot
        # artefacts in /boot.
        LOG.info("edge kernel upgrade: apk add %s",
                 " ".join(EDGE_UPGRADE_PACKAGES))
        _run_chroot(
            chroot,
            ["/sbin/apk", "add", "--no-cache", *EDGE_UPGRADE_PACKAGES],
        )

        # Verify the artefacts were produced.
        chroot_boot = chroot / "boot"
        missing = [f for f in _BOOT_ARTEFACTS
                   if not (chroot_boot / f).is_file()]
        if missing:
            raise RuntimeError(
                f"edge upgrade ran but {missing} missing from "
                f"{chroot_boot} — mkinitfs hook may not have fired. "
                f"List what IS there:\n"
                f"{sorted(p.name for p in chroot_boot.iterdir())}"
            )

        # Step 7. Replace stable boot artefacts with edge ones in
        # the RPi tarball tree.
        target_boot = extracted_root / "boot"
        target_boot.mkdir(parents=True, exist_ok=True)
        for f in _BOOT_ARTEFACTS:
            src = chroot_boot / f
            dst = target_boot / f
            shutil.copy(src, dst)
        sizes = {
            f: (target_boot / f).stat().st_size
            for f in _BOOT_ARTEFACTS
        }
        LOG.info("edge kernel upgrade: boot artefacts swapped → %s",
                 sizes)

    finally:
        # Step 8. Cleanup binds (reverse order for safety).
        for d in reversed(binds):
            subprocess.run(
                ["umount", "-l", str(chroot / d)],
                check=False, capture_output=True,
            )


def _run_chroot(chroot: Path, argv: list[str]) -> None:
    """`chroot <chroot> <argv...>` with full stderr on failure."""
    cmd = ["chroot", str(chroot), *argv]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"chroot command failed (rc={r.returncode}):\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  stdout: {r.stdout}\n"
            f"  stderr: {r.stderr}"
        )
