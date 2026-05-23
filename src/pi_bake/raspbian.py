"""Raspberry Pi OS / Debian image baker — losetup-based, needs root.

The .img.xz Raspberry Pi OS publishes is a partitioned image: FAT32
boot + ext4 rootfs. To inject `firstrun.sh` (which Pi OS's
init wires up automatically when it finds it on the boot partition),
we have to mount the boot partition. That requires `losetup -P`,
which requires root.

Stubbed in v0.1 with a clear error path. The Alpine baker covers
Pi Zeros (no root needed). Most Pi 4 / Pi 5 deployments will
bootstrap with rpi-imager's existing pre-fill flow until this is
fleshed out.

To implement (v0.2 roadmap, in order):
  1. fetch + xz-decompress (xz is python stdlib; decompresses the
     `.img.xz` to a `.img`).
  2. `sudo losetup --show -fP <img>` → /dev/loopN with -P giving
     /dev/loopNp1 (boot) + /dev/loopNp2 (rootfs).
  3. mkdir /tmp/boot; sudo mount /dev/loopNp1 /tmp/boot.
  4. Write `/tmp/boot/firstrun.sh` with hostname / pubkey / wifi
     setup commands (Pi OS reads `systemd.run` from cmdline.txt
     and execs it on first boot, then strips itself).
  5. Append `systemd.run=/boot/firstrun.sh systemd.run_success_action=reboot
     systemd.unit=kernel-command-line.target` to cmdline.txt.
  6. sudo umount /tmp/boot; sudo losetup -d /dev/loopN.
  7. gzip → out.

References:
  - rpi-imager's firstrun.sh template:
    https://github.com/raspberrypi/rpi-imager/blob/qml/src/cli.cpp
  - Pi OS's `init_resize.sh` for the cmdline mods.
"""
from __future__ import annotations

from pathlib import Path

from pi_bake.config import NodeConfig


def bake(*, url: str, node: NodeConfig, out_path: Path,
         image_size_mb: int = 0) -> Path:
    """Bake Pi OS / Debian image. NOT IMPLEMENTED in v0.1."""
    raise NotImplementedError(
        "pi-bake v0.1 only supports Alpine. Raspberry Pi OS / Debian "
        "baking is on the v0.2 roadmap (needs sudo + losetup). "
        "Until then, bootstrap Pi 4 / Pi 5 with rpi-imager's "
        "pre-fill SSH/user/wifi flow — the Pi joins the network "
        "the same way and totaldns discovers it via DHCP either way."
    )
