"""Debian (community Raspberry Pi images) baker.

Source: https://raspi.debian.net/ ships `.img.xz` disk images
purpose-built for the Pi (firmware + u-boot already in the boot
partition). Same partitioned-image shape as Pi OS (boot vfat +
ext4 root), so we reuse imgxz.py for fetch + losetup + mount +
repack.

Differences from Raspberry Pi OS Lite (raspbian.py):

  - No 'pi' user pre-created. The default user is 'root' with
    password 'raspberry'. Pi-bake disables password auth + writes
    only /root/.ssh/authorized_keys.
  - No firstrun.sh / userconf.txt mechanism — config is direct
    rootfs edits.
  - sshd is enabled out of the box (no /boot/ssh marker needed).
  - Uses /etc/network/interfaces for static IP (no dhcpcd).
  - config.txt: no usercfg.txt include by default; we append the
    include and create /boot/usercfg.txt fresh (same as Pi OS).

Sudo required for losetup + mount (same constraint as raspbian.py).
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from pi_bake.config import NodeConfig
from pi_bake.download import fetch
from pi_bake import imgxz

LOG = logging.getLogger("pi_bake.debian")

_BOOT_PART = 1
_ROOT_PART = 2


def bake(
    *, url: str, node: NodeConfig, out_path: Path,
    image_size_mb: int = 0,
) -> Path:
    """Bake a raspi.debian.net image for `node`. Returns out_path.

    Sudo required for losetup + mount. Run inside an LXC container
    or with passwordless sudoers entries for losetup/mount.
    """
    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    xz_path = fetch(url)

    with tempfile.TemporaryDirectory(prefix="pi-bake-debian-") as td:
        td = Path(td)
        raw = imgxz.decompress_xz(xz_path, td / "raw")
        LOG.info("raw image: %s (%d MB)",
                 raw.name, raw.stat().st_size >> 20)

        mi = imgxz.mount_image(raw, td / "mounts")
        try:
            _write_boot_partition(mi.mounts[_BOOT_PART], node)
            _write_root_partition(mi.mounts[_ROOT_PART], node)
        finally:
            imgxz.unmount_image(mi)

        imgxz.recompress_xz(raw, out_path)

    LOG.info("DONE: %s (%d MB)", out_path, out_path.stat().st_size >> 20)
    return out_path


def _write_boot_partition(boot: Path, node: NodeConfig) -> None:
    """Boot partition edits — config.txt include + wifi only.

    No /ssh marker needed (Debian enables sshd by default), no
    /userconf.txt (no pi user)."""
    if node.has_wifi:
        # Debian community images use systemd-networkd OR ifupdown
        # depending on the variant. wpa_supplicant.conf in /boot is
        # not auto-consumed; we write straight into /etc/ from the
        # rootfs writer below. So skip here.
        pass

    if node.config_txt:
        body = "# pi-bake operator-declared HAT/peripheral overlays\n"
        body += "\n".join(node.config_txt) + "\n"
        imgxz.write_file(boot, "usercfg.txt", body, mode=0o644)
        imgxz.append_file(
            boot, "config.txt",
            "\n# Added by pi-bake\ninclude usercfg.txt\n",
        )
        LOG.info("boot: usercfg.txt + config.txt include for %d HAT line(s)",
                 len(node.config_txt))


def _write_root_partition(root: Path, node: NodeConfig) -> None:
    """Rootfs edits: hostname, SSH keys, network, modules."""

    imgxz.write_file(root, "etc/hostname",
                     f"{node.hostname}\n", mode=0o644)
    imgxz.append_file(root, "etc/hosts",
                      f"127.0.1.1\t{node.hostname}\n")
    LOG.info("root: hostname=%s", node.hostname)

    # SSH key for root. Debian Pi images ship with root password
    # auth enabled by default — pi-bake replaces the password-auth
    # path with pubkey-only by writing authorized_keys + tweaking
    # sshd_config.
    pi_keys = node.authorized_keys_text()
    imgxz.write_file(
        root, "root/.ssh/authorized_keys", pi_keys, mode=0o600,
    )
    imgxz.chown(root, "root/.ssh", uid=0, gid=0)
    imgxz.chown(root, "root/.ssh/authorized_keys", uid=0, gid=0)
    LOG.info("root: /root/.ssh/authorized_keys (%d key%s)",
             len(node.all_pubkeys),
             "" if len(node.all_pubkeys) == 1 else "s")

    # sshd_config: disable password auth + permit root pubkey login.
    # Done via a drop-in under /etc/ssh/sshd_config.d/ which Debian's
    # sshd_config Include line picks up.
    sshd_drop_in = (
        "# Added by pi-bake\n"
        "PermitRootLogin prohibit-password\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
    )
    imgxz.write_file(
        root, "etc/ssh/sshd_config.d/00-pi-bake.conf",
        sshd_drop_in, mode=0o600,
    )
    LOG.info("root: sshd_config.d drop-in (no password auth)")

    # SSH host keys (same shape as Alpine / Pi OS).
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

    # WiFi via /etc/wpa_supplicant/wpa_supplicant.conf. The
    # interfaces file references it.
    if node.has_wifi:
        imgxz.write_file(
            root, "etc/wpa_supplicant/wpa_supplicant.conf",
            node.wpa_supplicant_conf(), mode=0o600,
        )
        # Ensure wlan0 stanza in /etc/network/interfaces.d/.
        wlan_block = (
            "# Added by pi-bake (wifi bootstrap)\n"
            "allow-hotplug wlan0\n"
            "iface wlan0 inet dhcp\n"
            "    wpa-conf /etc/wpa_supplicant/wpa_supplicant.conf\n"
        )
        imgxz.write_file(
            root, "etc/network/interfaces.d/wlan0",
            wlan_block, mode=0o644,
        )
        LOG.info("root: wifi configured for ssid=%r", node.wifi_ssid)

    # Static IP via /etc/network/interfaces.d/eth0 drop-in.
    if node.has_static_ip:
        eth0_block = (
            f"# Added by pi-bake (operator static IP)\n"
            f"allow-hotplug eth0\n"
            f"iface eth0 inet static\n"
            f"    address {node.static_address_only}\n"
            f"    netmask {node.static_netmask}\n"
            f"    gateway {node.gateway_ipv4}\n"
            f"    dns-nameservers 1.1.1.1 8.8.8.8\n"
        )
        imgxz.write_file(
            root, "etc/network/interfaces.d/eth0",
            eth0_block, mode=0o644,
        )
        LOG.info("root: eth0 static IP %s via %s",
                 node.static_ipv4, node.gateway_ipv4)

    if node.modules:
        body = (
            "# Added by pi-bake — operator-declared kernel modules\n"
            + "\n".join(node.modules) + "\n"
        )
        imgxz.append_file(root, "etc/modules", body)
        LOG.info("root: /etc/modules += %d module(s)", len(node.modules))
