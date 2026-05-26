"""Fedora ARM image baker (v0.3 — scaffolding + caveats).

Fedora publishes generic aarch64 `.raw.xz` images at
https://download.fedoraproject.org/ for the Cloud Base and IoT
editions. Unlike Raspbian / raspi.debian.net, **Fedora does NOT
ship Pi-specific images** — the upstream image targets generic
UEFI/uboot aarch64 hosts. To boot on a Raspberry Pi you also
need to shim Pi-specific firmware (`start4.elf`, `fixup4.dat`,
etc.) into the boot partition.

This backend handles the pi-bake part: mount the image, inject
SSH keys + hostname + network config via cloud-init NoCloud.
The Pi-bootloader-shim is **NOT** in scope for v0.3.

Operator workflow (until the shim lands):
  1. `pi-bake build --os fedora` → produces a flashable .img.xz
  2. Flash to SD card
  3. Run `arm-image-installer --target=rpi4 …` from a Fedora host
     to inject the Pi firmware (one extra step, well-documented
     upstream)
  4. Boot the Pi

OR run pi-bake on a Fedora host, install fedora-arm-installer,
and use it to flash directly. The image pi-bake produces IS a
valid Fedora aarch64 image with cloud-init pre-configured — the
firmware shimming is a separate concern.

Tracked as a future enhancement (will land alongside #16
operator-controlled FAT contents — same shape: inject extra files
into the boot partition at bake time).

Cloud-init NoCloud datasource
-----------------------------
Fedora Cloud-Base images include cloud-init by default. The
NoCloud datasource reads `user-data` and `meta-data` from a vfat
filesystem labelled `CIDATA` OR from `/boot/user-data` +
`/boot/meta-data` on the existing boot partition (works for our
case). Pi-bake writes these two files; cloud-init does the rest
on first boot.

Sudo required for losetup + mount (same constraint as raspbian.py
+ debian.py).
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from pi_bake.config import NodeConfig
from pi_bake.download import fetch
from pi_bake import imgxz

LOG = logging.getLogger("pi_bake.fedora")

# Fedora Cloud-Base aarch64 has 3 partitions:
#   p1: EFI System Partition (vfat, ~600 MB)
#   p2: /boot (ext4, ~1 GB)
#   p3: / (ext4 or btrfs depending on version)
# cloud-init NoCloud reads user-data from EFI (p1) or wherever a
# vfat with label CIDATA mounts. We use the EFI partition.
_BOOT_PART = 1
_OSBOOT_PART = 2
_ROOT_PART = 3


def bake(
    *, url: str, node: NodeConfig, out_path: Path,
    image_size_mb: int = 0,
) -> Path:
    """Bake a Fedora aarch64 .raw.xz for `node`. Returns out_path.

    Caveat: the produced image needs Pi-bootloader-shim before it
    boots on a Pi. See module docstring.
    """
    LOG.warning(
        "fedora backend: produced image is NOT directly Pi-bootable. "
        "Run `arm-image-installer --target=rpi4 …` (or rpi5) on the "
        "output to inject Pi-specific firmware. See module docstring."
    )

    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    xz_path = fetch(url)

    with tempfile.TemporaryDirectory(prefix="pi-bake-fedora-") as td:
        td = Path(td)
        raw = imgxz.decompress_xz(xz_path, td / "raw")
        LOG.info("raw image: %s (%d MB)",
                 raw.name, raw.stat().st_size >> 20)

        mi = imgxz.mount_image(raw, td / "mounts")
        try:
            # Find a partition with vfat (the EFI one) for NoCloud.
            # Fedora Cloud-Base has p1 = ESP (vfat).
            efi = mi.mounts.get(_BOOT_PART)
            if efi is None:
                raise RuntimeError(
                    f"expected partition {_BOOT_PART} (EFI/vfat) — "
                    f"got partitions {sorted(mi.mounts)}. Image "
                    f"layout may have changed."
                )
            _write_nocloud(efi, node)

            # Rootfs is the highest-numbered partition.
            root = mi.mounts[max(mi.mounts)]
            _write_root_partition(root, node)
        finally:
            imgxz.unmount_image(mi)

        imgxz.recompress_xz(raw, out_path)

    LOG.info("DONE: %s (%d MB)", out_path, out_path.stat().st_size >> 20)
    LOG.warning(
        "Reminder: pi-bake's Fedora backend does NOT inject Pi-"
        "specific firmware. Use arm-image-installer to make the "
        "image bootable on Pi hardware."
    )
    return out_path


def _write_nocloud(efi: Path, node: NodeConfig) -> None:
    """Write cloud-init NoCloud datasource files (user-data +
    meta-data) onto the EFI partition. cloud-init looks here
    automatically on Fedora Cloud-Base."""

    # meta-data — minimal; cloud-init wants this file present even
    # if it's nearly empty.
    meta_data = f"instance-id: {node.hostname}\nlocal-hostname: {node.hostname}\n"
    imgxz.write_file(efi, "meta-data", meta_data, mode=0o644)

    # user-data — the meat. YAML cloud-config that sets up:
    #   - hostname (redundant with meta-data, both standard)
    #   - SSH pubkey on the default 'fedora' user (root by SSH is
    #     blocked by Fedora cloud-config defaults; the fedora user
    #     has sudo)
    #   - Optional WiFi via NetworkManager nmconnection
    #   - Optional static IP via NetworkManager
    user_data_lines = [
        "#cloud-config",
        f"hostname: {node.hostname}",
        f"fqdn: {node.hostname}",
        "ssh_pwauth: false",
        "users:",
        "  - default",
        "  - name: fedora",
        "    groups: wheel",
        "    sudo: ALL=(ALL) NOPASSWD:ALL",
        "    shell: /bin/bash",
        "    ssh_authorized_keys:",
    ]
    for key in node.all_pubkeys:
        user_data_lines.append(f"      - {key}")

    if node.has_wifi:
        # NetworkManager keyfile syntax via cloud-config write_files.
        nm_keyfile = (
            "[connection]\n"
            "id=pi-bake-wifi\n"
            "type=wifi\n"
            "interface-name=wlan0\n"
            "autoconnect=true\n"
            "\n"
            "[wifi]\n"
            "mode=infrastructure\n"
            f"ssid={node.wifi_ssid}\n"
            "\n"
            "[wifi-security]\n"
            "key-mgmt=wpa-psk\n"
            f"psk={node.wifi_psk}\n"
            "\n"
            "[ipv4]\n"
            "method=auto\n"
        )
        user_data_lines.extend([
            "write_files:",
            "  - path: /etc/NetworkManager/system-connections/pi-bake-wifi.nmconnection",
            "    permissions: '0600'",
            "    content: |",
        ])
        for line in nm_keyfile.splitlines():
            user_data_lines.append(f"      {line}")

    if node.has_static_ip:
        # NetworkManager static eth0 connection.
        eth0_keyfile = (
            "[connection]\n"
            "id=pi-bake-eth0\n"
            "type=ethernet\n"
            "interface-name=eth0\n"
            "autoconnect=true\n"
            "\n"
            "[ipv4]\n"
            f"method=manual\n"
            f"address1={node.static_ipv4},{node.gateway_ipv4}\n"
            f"dns=1.1.1.1;8.8.8.8;\n"
        )
        # Append to existing write_files: if it exists; else create it.
        if "write_files:" not in user_data_lines:
            user_data_lines.append("write_files:")
        user_data_lines.extend([
            "  - path: /etc/NetworkManager/system-connections/pi-bake-eth0.nmconnection",
            "    permissions: '0600'",
            "    content: |",
        ])
        for line in eth0_keyfile.splitlines():
            user_data_lines.append(f"      {line}")

    user_data = "\n".join(user_data_lines) + "\n"
    imgxz.write_file(efi, "user-data", user_data, mode=0o644)
    LOG.info("efi: NoCloud user-data + meta-data written for "
             "hostname=%s", node.hostname)


def _write_root_partition(root: Path, node: NodeConfig) -> None:
    """Rootfs edits not covered by cloud-init: SSH host keys,
    kernel modules. Hostname / users / networks all handled via
    cloud-init NoCloud user-data (see _write_nocloud)."""

    if node.ssh_host_key_priv and node.ssh_host_key_pub:
        ktype = node.ssh_host_key_type
        imgxz.write_file(
            root, f"etc/ssh/ssh_host_{ktype}_key",
            node.ssh_host_key_priv, mode=0o600,
        )
        imgxz.write_file(
            root, f"etc/ssh/ssh_host_{ktype}_key.pub",
            node.ssh_host_key_pub, mode=0o644,
        )
        LOG.info("root: pre-baked ssh_host_%s_key", ktype)

    if node.modules:
        # Fedora uses /etc/modules-load.d/ drop-ins (systemd-
        # modules-load). One file per logical module set.
        body = (
            "# Added by pi-bake — operator-declared kernel modules\n"
            + "\n".join(node.modules) + "\n"
        )
        imgxz.write_file(
            root, "etc/modules-load.d/pi-bake.conf",
            body, mode=0o644,
        )
        LOG.info("root: /etc/modules-load.d/pi-bake.conf with %d module(s)",
                 len(node.modules))
