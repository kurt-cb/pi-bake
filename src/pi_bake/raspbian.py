"""Raspberry Pi OS Lite (`raspios_lite_arm64`) image baker.

Operator inputs come from NodeConfig + bake() kwargs; output is an
`.img.xz` operator dd's to an SD card (or serves via PXE).

Pi OS Lite ships as a partitioned .img.xz with:
  - p1: vfat /boot   (~512 MB, holds bootloader + config.txt + initramfs)
  - p2: ext4 /       (~1.5 GB, contains the OS; auto-expands on first
                      boot via init_resize.sh to fill the SD card)

Pi-bake's job per bake:
  1. Fetch + decompress the .img.xz from downloads.raspberrypi.com
     (cached at ~/.cache/pi-bake/).
  2. losetup -fP the raw .img, mount both partitions (needs sudo).
  3. Boot partition writes:
     - `/ssh` (empty marker) — Pi OS init enables sshd when it sees this
     - `/userconf.txt` — `pi:<sha-512-crypted-pass>` to set the pi user's
       password (mandatory on Bookworm+; default 'raspberry' is rejected)
     - `/wpa_supplicant.conf` (when wifi is configured)
     - `/usercfg.txt` (when node.config_txt is set; included by config.txt)
  4. Rootfs writes:
     - `/etc/hostname`
     - `/home/pi/.ssh/authorized_keys` (uid 1000, mode 600)
     - `/root/.ssh/authorized_keys` (root key auth for early ops)
     - `/etc/dhcpcd.conf` static-IP block (when node.has_static_ip)
     - `/etc/modules` (when node.modules is set)
  5. Unmount + losetup -d + xz the modified .img → out path.

Sudo is required for steps 2 + 4; bakes typically run inside an LXC
container or with sudoers entries allowing losetup/mount without
password. See README.

Lessons baked in from operator experience (mirror Alpine's):
  - sshd needs a password set OR root pubkey + PermitRootLogin
    prohibit-password. Pi OS Bookworm rejects empty pi password.
  - SSH host keys regenerate on first boot unless we pre-bake them
    into /etc/ssh (see node.ssh_host_key_*); same logic as Alpine.
  - dhcpcd is Pi OS Lite's network manager (matches Alpine choice).
"""
from __future__ import annotations

import crypt
import logging
import secrets
import tempfile
from pathlib import Path

from pi_bake.config import NodeConfig
from pi_bake.download import fetch
from pi_bake import imgxz

LOG = logging.getLogger("pi_bake.raspbian")

# Default partitions per the Pi OS image layout. If a future Pi OS
# release reorganizes, override per-recipe (out of scope for v0.3).
_BOOT_PART = 1
_ROOT_PART = 2

# Mandatory placeholder password for the 'pi' user. The userconf.txt
# is required by Pi OS Bookworm — otherwise sshd refuses logins from
# the 'pi' account. We bake a random, non-discoverable password
# (never used: password auth disabled, only the operator's pubkey
# matters), then disable password auth in sshd_config separately.
def _random_locked_password_hash() -> str:
    """sha-512 crypt of a random throwaway password. Locks the
    'pi' account against password login as a defence-in-depth even
    if sshd_config got reverted somehow."""
    salt = "$6$" + secrets.token_urlsafe(12)
    junk = secrets.token_urlsafe(32)
    return crypt.crypt(junk, salt)


def bake(
    *, url: str, node: NodeConfig, out_path: Path,
    image_size_mb: int = 0,                 # ignored: Pi OS image is fixed-size
) -> Path:
    """Bake a Raspberry Pi OS Lite .img.xz for `node`. Returns
    out_path.

    Sudo is required for losetup + mount steps. Pi-bake will
    surface the sudo prompt directly; in CI / LXC contexts make
    sure the operator's user can losetup + mount without password.
    """
    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    xz_path = fetch(url)

    with tempfile.TemporaryDirectory(prefix="pi-bake-raspbian-") as td:
        td = Path(td)

        # 1. xz -d the cached .img.xz into our tempdir. Reused
        #    across re-bakes of the same upstream image.
        raw = imgxz.decompress_xz(xz_path, td / "raw")
        LOG.info("raw image: %s (%d MB)",
                 raw.name, raw.stat().st_size >> 20)

        # 2. losetup + mount both partitions.
        mi = imgxz.mount_image(raw, td / "mounts")
        try:
            boot = mi.mounts[_BOOT_PART]
            root = mi.mounts[_ROOT_PART]
            _write_boot_partition(boot, node)
            _write_root_partition(root, node)
        finally:
            # Always teardown — losetup leaks are gnarly.
            imgxz.unmount_image(mi)

        # 3. Re-xz the modified .img → operator's out path.
        imgxz.recompress_xz(raw, out_path)

    LOG.info("DONE: %s (%d MB)", out_path, out_path.stat().st_size >> 20)
    return out_path


# --------------------------------------------------------------------------- #
# Boot partition (FAT) writes                                                  #
# --------------------------------------------------------------------------- #

def _write_boot_partition(boot: Path, node: NodeConfig) -> None:
    """All boot-partition edits: sshd marker, userconf, wifi,
    config_txt additions. These are world-readable since FAT
    has no perm semantics anyway."""

    # `ssh` empty marker: Pi OS Lite's init enables sshd when it
    # sees this file. Removed after first boot.
    imgxz.write_file(boot, "ssh", b"", mode=0o644)
    LOG.info("boot: /ssh marker written")

    # userconf.txt: `<user>:<sha-512-crypted-password>` — required
    # by Bookworm+ to set up the default 'pi' user. We bake a random
    # locked-out password; password auth is separately disabled.
    pi_pass = _random_locked_password_hash()
    imgxz.write_file(
        boot, "userconf.txt", f"pi:{pi_pass}\n", mode=0o600,
    )
    LOG.info("boot: /userconf.txt with random locked pi password")

    # wpa_supplicant.conf for wifi. Pi OS Bookworm moved away from
    # this file (NetworkManager is the default now), but the
    # /boot/firstrun.sh in Pi OS still copies legacy
    # wpa_supplicant.conf into /etc/wpa_supplicant/ on first boot
    # for back-compat — works.
    if node.has_wifi:
        imgxz.write_file(
            boot, "wpa_supplicant.conf",
            node.wpa_supplicant_conf(), mode=0o600,
        )
        LOG.info("boot: /wpa_supplicant.conf written for ssid=%r",
                 node.wifi_ssid)

    # config.txt additions go into /usercfg.txt (Pi OS doesn't ship
    # one by default; we create it and reference from config.txt
    # via an include line). Operator's `config_txt:` recipe field
    # lands here.
    if node.config_txt:
        body = "# pi-bake operator-declared HAT/peripheral overlays\n"
        body += "\n".join(node.config_txt) + "\n"
        imgxz.write_file(boot, "usercfg.txt", body, mode=0o644)
        # Pi OS config.txt doesn't pre-include usercfg.txt (Alpine
        # does). Append the include line.
        imgxz.append_file(
            boot, "config.txt",
            "\n# Added by pi-bake\ninclude usercfg.txt\n",
        )
        LOG.info("boot: usercfg.txt + config.txt include for %d HAT line(s)",
                 len(node.config_txt))


# --------------------------------------------------------------------------- #
# Root partition (ext4) writes                                                 #
# --------------------------------------------------------------------------- #

def _write_root_partition(root: Path, node: NodeConfig) -> None:
    """All rootfs edits: hostname, SSH keys (host + authorized),
    dhcpcd config for static IP, /etc/modules."""

    # /etc/hostname
    imgxz.write_file(root, "etc/hostname",
                     f"{node.hostname}\n", mode=0o644)
    # /etc/hosts — Pi OS ships a default. Append 127.0.1.1 line so
    # `hostname --fqdn` resolves to itself (Debian convention).
    imgxz.append_file(root, "etc/hosts",
                      f"127.0.1.1\t{node.hostname}\n")
    LOG.info("root: hostname=%s", node.hostname)

    # SSH authorized_keys for the pi user. Pi OS pre-creates uid
    # 1000 = pi:pi, so we can chown directly.
    pi_keys = node.authorized_keys_text()
    imgxz.write_file(
        root, "home/pi/.ssh/authorized_keys",
        pi_keys, mode=0o600,
    )
    imgxz.chown(root, "home/pi/.ssh", uid=1000, gid=1000)
    imgxz.chown(root, "home/pi/.ssh/authorized_keys",
                uid=1000, gid=1000)
    LOG.info("root: /home/pi/.ssh/authorized_keys (%d key%s)",
             len(node.all_pubkeys),
             "" if len(node.all_pubkeys) == 1 else "s")

    # Also drop the operator's key into /root/.ssh/ for the rare
    # case operator wants `ssh root@pi` (e.g. early debug).
    # PermitRootLogin defaults to "prohibit-password" on Pi OS so
    # this only enables key-based root login (already what we want).
    imgxz.write_file(
        root, "root/.ssh/authorized_keys",
        pi_keys, mode=0o600,
    )
    imgxz.chown(root, "root/.ssh", uid=0, gid=0)
    imgxz.chown(root, "root/.ssh/authorized_keys", uid=0, gid=0)

    # SSH host keys (Recipe.ssh_host_key path → NodeConfig bytes).
    # If unset, sshd-keygen regenerates on first boot (Pi OS
    # default). Same logic as Alpine: predictable identity → no
    # known_hosts churn.
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

    # Static IP via dhcpcd (Pi OS default network manager — same
    # as Alpine). Append an interface block to /etc/dhcpcd.conf.
    if node.has_static_ip:
        block = (
            f"\n# Added by pi-bake (operator static IP)\n"
            f"interface eth0\n"
            f"static ip_address={node.static_ipv4}\n"
            f"static routers={node.gateway_ipv4}\n"
            f"static domain_name_servers=1.1.1.1 8.8.8.8\n"
        )
        imgxz.append_file(root, "etc/dhcpcd.conf", block)
        LOG.info("root: dhcpcd static IP %s via %s",
                 node.static_ipv4, node.gateway_ipv4)

    # /etc/modules for kernel module force-load (same semantics as
    # Alpine). Pi OS reads this file via systemd-modules-load.
    if node.modules:
        body = (
            "# Added by pi-bake — operator-declared kernel modules\n"
            + "\n".join(node.modules) + "\n"
        )
        imgxz.append_file(root, "etc/modules", body)
        LOG.info("root: /etc/modules += %d module(s)", len(node.modules))
