"""Raspberry Pi OS Lite (`raspios_lite_arm64`) image baker.

Operator inputs come from NodeConfig + bake() kwargs; output is an
`.img.xz` operator dd's to an SD card (or serves via PXE).

Pi OS Lite ships as a partitioned .img.xz with:
  - p1: vfat /boot/firmware   (~512 MB, bootloader + config + initramfs)
  - p2: ext4 /                (~1.5 GB, OS; auto-expands on first boot
                               via init_resize.sh to fill the SD card)

Pi-bake's job per bake:
  1. Fetch + decompress the .img.xz from downloads.raspberrypi.com
     (cached at ~/.cache/pi-bake/).
  2. losetup -fP the raw .img, mount both partitions (needs sudo).
  3. Boot partition writes:
     - `/firstrun.sh` — pi-bake-generated first-boot script. Runs once
       via `systemd.run=` injected into cmdline.txt, creates the pi
       user with /bin/bash shell (sidesteps Pi OS Trixie's userconf-pi
       nologin-default that breaks SSH login on key-only auth),
       installs authorized_keys, enables sshd, then self-deletes.
       This is the load-bearing first-boot mechanism since v0.4.
     - `/ssh` + `/userconf.txt` — legacy Pi-OS first-boot markers,
       kept as a fallback for the cases firstrun.sh fails to run
       (e.g. cmdline.txt corruption). On Bookworm both paths produce
       the same result; on Trixie firstrun.sh wins and pre-empts the
       userconf-pi service.
     - `/cmdline.txt` — get systemd.run=/boot/firmware/firstrun.sh
       appended (one-line file; we read+rewrite).
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
  - Pi OS Trixie's userconf-pi service creates the pi user with
    `/usr/sbin/nologin` shell — SSH key auth succeeds then login is
    immediately rejected. firstrun.sh sidesteps this by creating the
    user explicitly with `-s /bin/bash` + `usermod -s /bin/bash pi`.
    Confirmed root cause 2026-05-28 on a failed pi5-smoke bake.
  - SSH host keys regenerate on first boot unless we pre-bake them
    into /etc/ssh (see node.ssh_host_key_*); same logic as Alpine.
  - dhcpcd is Pi OS Lite's network manager (matches Alpine choice).
"""
from __future__ import annotations

import crypt
import logging
import os
import secrets
import subprocess
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


# cmdline.txt directives appended at bake-time. systemd reads these
# as a transient unit, runs the script, and reboots on success
# (systemd.run_success_action=reboot). systemd.unit=kernel-command-
# line.target prevents multi-user.target from activating, so the
# legacy userconf-pi.service doesn't race with firstrun.sh.
_FIRSTRUN_CMDLINE = (
    " systemd.run=/boot/firmware/firstrun.sh"
    " systemd.run_success_action=reboot"
    " systemd.unit=kernel-command-line.target"
)


def _firstrun_sh(node: NodeConfig, pi_hash: str) -> str:
    """Render the bash script run once at first boot.

    Idempotent: re-running is safe. Logs to /var/log/pi-bake-firstrun.log.
    On success the script self-deletes and strips its systemd.run
    additions from cmdline.txt so subsequent boots are clean.
    """
    # Hard-fail the bake if any field contains a shell escape — we
    # interpolate them into a bash script and don't want to chase
    # the operator's typos at runtime. Hostname is DNS-label-bounded;
    # the auth keys are operator-controlled but already validated.
    if any(c in node.hostname for c in "'\"`$\\\n"):
        raise ValueError(
            f"hostname {node.hostname!r} contains shell-unsafe chars"
        )
    if "EOAUTH" in node.authorized_keys_text():
        raise ValueError(
            "authorized_keys contains heredoc terminator 'EOAUTH' — "
            "refusing to bake"
        )
    return (
        "#!/bin/bash\n"
        "# pi-bake-generated firstrun.sh — runs once via systemd.run=\n"
        "# in cmdline.txt. Sidesteps Pi OS Trixie's userconf-pi\n"
        "# nologin-shell default that breaks SSH login on key-only\n"
        "# auth. Idempotent: re-running is safe.\n"
        "set +e\n"
        "exec >/var/log/pi-bake-firstrun.log 2>&1\n"
        "echo \"pi-bake firstrun.sh starting at $(date)\"\n"
        "\n"
        "# Hostname.\n"
        f"echo {node.hostname!r} > /etc/hostname\n"
        f"hostname {node.hostname!r}\n"
        "sed -i '/^127\\.0\\.1\\.1/d' /etc/hosts\n"
        f"echo $'127.0.1.1\\t{node.hostname}' >> /etc/hosts\n"
        "\n"
        "# pi user: create if missing, then force /bin/bash shell.\n"
        "# Trixie's userconf-pi defaults to /usr/sbin/nologin; the\n"
        "# usermod below is the load-bearing fix.\n"
        "if ! id pi >/dev/null 2>&1; then\n"
        "  useradd -m -G sudo,video,audio,plugdev,users,games,input"
        " -s /bin/bash pi\n"
        "fi\n"
        "usermod -s /bin/bash pi\n"
        f"echo 'pi:{pi_hash}' | chpasswd -e\n"
        "\n"
        "# Authorized_keys for pi.\n"
        "install -o pi -g pi -m 700 -d /home/pi/.ssh\n"
        "cat > /home/pi/.ssh/authorized_keys <<'EOAUTH'\n"
        f"{node.authorized_keys_text()}"
        "EOAUTH\n"
        "chown pi:pi /home/pi/.ssh/authorized_keys\n"
        "chmod 600 /home/pi/.ssh/authorized_keys\n"
        "\n"
        "# Enable sshd. ssh.service is installed but not enabled by\n"
        "# default on Pi OS Lite.\n"
        "systemctl enable ssh.service 2>/dev/null || true\n"
        "\n"
        "# Disarm legacy first-boot markers so userconf-pi.service\n"
        "# doesn't fight us on the post-reboot multi-user.target.\n"
        "rm -f /boot/firmware/userconf.txt /boot/firmware/ssh"
        " /boot/userconf.txt /boot/ssh\n"
        "\n"
        "# Self-cleanup: remove this script + strip the systemd.run\n"
        "# hook from cmdline.txt so the next boot is clean.\n"
        "rm -f /boot/firmware/firstrun.sh /boot/firstrun.sh\n"
        "for c in /boot/firmware/cmdline.txt /boot/cmdline.txt; do\n"
        "  [ -f \"$c\" ] || continue\n"
        "  sed -i 's| systemd\\.run[^ ]*||g; s| systemd\\.unit[^ ]*||g'"
        " \"$c\"\n"
        "done\n"
        "echo \"pi-bake firstrun.sh done at $(date)\"\n"
        "exit 0\n"
    )


def _patch_cmdline_txt(boot: Path) -> None:
    """Append the systemd.run= directives to /boot/firmware/cmdline.txt.

    cmdline.txt is a single line; we read, rstrip, append, write back.
    No partial-state risk: if write fails the original stays intact
    on the FAT (mtools-style atomic-ish for small files).
    """
    cmdline = boot / "cmdline.txt"
    if os.geteuid() == 0:
        original = cmdline.read_text().rstrip("\r\n ")
    else:
        original = subprocess.check_output(
            ["sudo", "cat", str(cmdline)],
        ).decode().rstrip("\r\n ")
    # Idempotent: don't append if already present (re-bake-safe).
    if "systemd.run=/boot/firmware/firstrun.sh" in original:
        return
    new = original + _FIRSTRUN_CMDLINE + "\n"
    imgxz.write_file(boot, "cmdline.txt", new, mode=0o644)


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
    """All boot-partition edits: firstrun.sh + cmdline.txt patch,
    legacy ssh+userconf fallback markers, wifi, config_txt
    additions. FAT has no perm semantics so mode is mostly
    advisory."""

    pi_hash = _random_locked_password_hash()

    # firstrun.sh: the load-bearing first-boot mechanism since v0.4.
    # Creates the pi user with /bin/bash shell, installs authorized
    # keys, enables sshd, then self-deletes. Triggered by the
    # systemd.run= directive appended to cmdline.txt below.
    imgxz.write_file(
        boot, "firstrun.sh", _firstrun_sh(node, pi_hash), mode=0o755,
    )
    _patch_cmdline_txt(boot)
    LOG.info("boot: /firstrun.sh + cmdline.txt systemd.run= hook")

    # Legacy first-boot markers — kept as a fallback for the case
    # firstrun.sh fails to run (cmdline.txt corruption, etc.). On
    # Bookworm both paths produce the same result; on Trixie
    # firstrun.sh wins and pre-empts userconf-pi.service. The
    # firstrun.sh self-cleanup also deletes these so userconf-pi
    # doesn't re-run on the post-reboot multi-user boot.
    imgxz.write_file(boot, "ssh", b"", mode=0o644)
    LOG.info("boot: /ssh marker written (fallback)")

    imgxz.write_file(
        boot, "userconf.txt", f"pi:{pi_hash}\n", mode=0o600,
    )
    LOG.info("boot: /userconf.txt with random locked pi password (fallback)")

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
