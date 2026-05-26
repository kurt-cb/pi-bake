"""Bake-time Alpine edge kernel upgrade (`os_version: edge`).

The pi-bake v0.0.5 design treated `os_version: edge` as "use the
latest stable RPi tarball for bootloader/FAT layout, but point
`/etc/apk/repositories` at edge so post-boot `apk upgrade` rolls
the kernel forward."

That design **does not work in practice on Alpine RPi diskless**.
The operator's reproduction:

  $ uname -r
  6.12.13-0-rpi          # ← stable's kernel, NOT edge's 6.12.85+
  $ modinfo iwlwifi
  modinfo: module 'iwlwifi' not found

Why:
  1. /media/mmcblk0 (FAT) is mounted ro by default on the running Pi.
  2. `apk fix linux-rpi --reinstall` doesn't update the FAT's
     vmlinuz-rpi / initramfs-rpi / modloop-rpi.
  3. `apk info -L linux-rpi` returns an empty file list on Alpine
     — the boot artefacts come from install-time hooks (mkinitfs)
     that don't fire correctly outside `setup-alpine`.
  4. Even if regenerated, /lib/modules is a squashfs (modloop-rpi)
     mount — read-only by design.

Net: the post-boot `apk upgrade` path can't physically rewrite
the kernel + modules + initramfs on a diskless Alpine RPi.

The fix is to do the upgrade **at bake time** by running apk in a
chroot of the extracted Alpine RPi tarball, using qemu-user-static
to execute aarch64 binaries on the bake host. This triggers the
proper install hooks (mkinitfs) which regenerate vmlinuz-rpi,
initramfs-rpi, modloop-rpi inside the chroot's /boot. Those files
get mcopy'd into the FAT image as usual; the sealed `.img.gz`
ships with the edge kernel pre-installed.

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
from pathlib import Path

LOG = logging.getLogger("pi_bake.alpine_edge")

# Alpine edge repository URLs. Apk inside the chroot points its
# /etc/apk/repositories at these for the upgrade.
EDGE_REPO_MAIN = "http://dl-cdn.alpinelinux.org/alpine/edge/main"
EDGE_REPO_COMMUNITY = "http://dl-cdn.alpinelinux.org/alpine/edge/community"

# Packages to upgrade to edge. linux-rpi pulls the kernel + modules;
# linux-firmware-rpi pulls the firmware blobs. We also upgrade
# `mkinitfs` itself so its hook runs against current edge tooling.
EDGE_UPGRADE_PACKAGES = ("mkinitfs", "linux-rpi", "linux-firmware-rpi")


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


def upgrade_to_edge_kernel(
    extracted_root: Path, target_arch: str = "aarch64",
) -> None:
    """Replace the extracted tarball's stable kernel with edge.

    Runs apk inside a chroot of the extracted Alpine RPi tarball,
    pointing /etc/apk/repositories at edge. The linux-rpi /
    linux-firmware-rpi / mkinitfs upgrade triggers the mkinitfs
    install hook, which regenerates the boot artefacts in /boot
    of the chroot. Those files then get mcopy'd into the FAT
    image by the normal bake flow.

    Pre-conditions (check via `check_requirements()` first):
      - bake host is root
      - qemu-<target_arch>-static is installed + binfmt_misc registered
      - network access to dl-cdn.alpinelinux.org

    Side effects in `extracted_root` after this returns:
      - /boot/vmlinuz-rpi, initramfs-rpi, modloop-rpi: edge versions
      - /lib/modules/<edge-version>/: kernel modules for edge
      - /etc/apk/repositories: points at edge (NOT reverted — the
        apkovl writer rewrites this to its own value later, so
        leaving edge here is harmless)
    """
    LOG.info("edge kernel upgrade: chroot=%s arch=%s",
             extracted_root, target_arch)
    check_requirements(target_arch)

    # Copy the static qemu binary into the chroot so binfmt_misc
    # can find it when executing aarch64 binaries via the chroot.
    qemu_src = shutil.which(f"qemu-{target_arch}-static")
    assert qemu_src is not None   # check_requirements verified
    qemu_dst = extracted_root / "usr/bin" / Path(qemu_src).name
    qemu_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(qemu_src, qemu_dst)
    qemu_dst.chmod(0o755)

    # Point apk inside the chroot at edge repos. The chroot's
    # /etc/apk/repositories pre-existing entries (typically
    # `/media/mmcblk0/apks` from the tarball) would block this if
    # we left them — apk would resolve to the stable cache first.
    # Overwrite with edge-only for the duration of the upgrade.
    repos = extracted_root / "etc/apk/repositories"
    repos.parent.mkdir(parents=True, exist_ok=True)
    repos.write_text(f"{EDGE_REPO_MAIN}\n{EDGE_REPO_COMMUNITY}\n")

    # Copy resolv.conf so apk inside the chroot can resolve
    # dl-cdn.alpinelinux.org. (The apkovl writer later overwrites
    # /etc/resolv.conf for static-IP nodes; for DHCP nodes dhcpcd
    # writes it post-boot. Either way, this temporary copy doesn't
    # affect the final image.)
    host_resolv = Path("/etc/resolv.conf")
    chroot_resolv = extracted_root / "etc/resolv.conf"
    if host_resolv.is_file():
        shutil.copy(host_resolv, chroot_resolv)

    # Bind-mount /proc, /sys, /dev so apk's hooks have a working
    # /proc/self/exe etc. (mkinitfs reads /proc for kernel version).
    binds = ("proc", "sys", "dev")
    for d in binds:
        target = extracted_root / d
        target.mkdir(exist_ok=True)
        subprocess.run(
            ["mount", "--bind", f"/{d}", str(target)],
            check=True, capture_output=True, text=True,
        )

    try:
        # Refresh apk's view of edge repos.
        LOG.info("edge kernel upgrade: apk update")
        r = subprocess.run(
            ["chroot", str(extracted_root),
             "/sbin/apk", "update", "--no-cache"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"chroot apk update failed (rc={r.returncode}):\n"
                f"stdout: {r.stdout}\nstderr: {r.stderr}"
            )

        # Upgrade kernel + firmware + mkinitfs. The mkinitfs hook
        # regenerates /boot/vmlinuz-rpi / initramfs-rpi / modloop-rpi.
        LOG.info("edge kernel upgrade: apk upgrade %s",
                 " ".join(EDGE_UPGRADE_PACKAGES))
        r = subprocess.run(
            ["chroot", str(extracted_root),
             "/sbin/apk", "add", "--upgrade", "--latest",
             *EDGE_UPGRADE_PACKAGES],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"chroot apk upgrade failed (rc={r.returncode}):\n"
                f"stdout: {r.stdout}\nstderr: {r.stderr}"
            )

        # Verify the edge kernel landed.
        boot = extracted_root / "boot"
        for f in ("vmlinuz-rpi", "initramfs-rpi", "modloop-rpi"):
            if not (boot / f).is_file():
                raise RuntimeError(
                    f"edge upgrade ran but {boot / f} missing — "
                    f"mkinitfs hook may not have fired."
                )

        # Report what we ended up with so the operator can sanity-
        # check the bake log.
        sizes = {
            f: (boot / f).stat().st_size
            for f in ("vmlinuz-rpi", "initramfs-rpi", "modloop-rpi")
        }
        LOG.info("edge kernel upgrade: post-upgrade boot files: %s",
                 sizes)
    finally:
        # Unmount the binds. Reverse order to handle nested mounts.
        for d in reversed(binds):
            subprocess.run(
                ["umount", "-l", str(extracted_root / d)],
                check=False, capture_output=True,
            )
        # Remove the staging qemu binary (don't ship it in the image).
        try:
            qemu_dst.unlink()
        except FileNotFoundError:
            pass
