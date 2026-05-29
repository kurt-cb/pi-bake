"""Raspberry Pi OS Lite (`raspios_lite_arm64`) image baker.

Operator inputs come from NodeConfig + bake() kwargs; output is an
`.img.xz` operator dd's to an SD card (or serves via PXE).

Pi OS Lite ships as a partitioned .img.xz with:
  - p1: vfat /boot/firmware   (~512 MB, bootloader + config + initramfs)
  - p2: ext4 /                (~1.5 GB, OS; auto-expands on first boot
                               via init_resize.sh to fill the SD card)

## Architecture: per-codename baker classes

Pi OS evolves: Bullseye -> Bookworm -> Trixie -> (whatever's next).
Each release tends to slip in one or two surprises that break a
headless bake — userconf-pi's shell default flipped from /bin/bash
to /usr/sbin/nologin in Trixie, etc. The pattern that fits this
trajectory is per-codename baker subclasses sharing a common base:

  _RaspbianBakerBase                  — version-agnostic logic
  ├── _RaspbianBookwormBaker          — codename = "bookworm"
  └── _RaspbianTrixieBaker            — codename = "trixie"

Right now both subclasses are empty — every fix in v0.4 (firstrun.sh,
regenerate_ssh_host_keys mask, userconfig autologin removal) works
identically on Bookworm and Trixie because the firstrun.sh approach
sidesteps Pi-OS-specific mechanisms entirely. When a future fix
applies to only one codename, it lives in that codename's class:
the override has one obvious home, no `if codename == "trixie"`
branches scattered through the code.

The module-level `bake()` is a dispatcher: it parses the codename
from the upstream URL (Pi OS names files `<date>-raspios-<codename>-
<arch>-lite.img.xz`, which has been stable since Buster) and routes
to the matching baker instance. Unknown codenames default to the
newest baker we ship — better-than-nothing if Pi OS publishes a
"Forky" filename before we add a class for it.

## Bake steps (any codename)

1. Fetch + decompress the .img.xz from downloads.raspberrypi.com
   (cached at ~/.cache/pi-bake/).
2. losetup -fP the raw .img, mount both partitions (needs sudo).
3. Boot partition writes:
   - `/firstrun.sh` — pi-bake-generated first-boot script. Runs once
     via `systemd.run=` injected into cmdline.txt, creates the pi
     user with /bin/bash shell (sidesteps Pi OS Trixie's userconf-pi
     nologin-default that breaks SSH login on key-only auth),
     installs authorized_keys, enables sshd, then self-deletes.
   - `/ssh` + `/userconf.txt` — legacy Pi-OS first-boot markers,
     kept as a fallback for the cases firstrun.sh fails to run.
   - `/cmdline.txt` — gets systemd.run= directives appended.
   - `/wpa_supplicant.conf` (when wifi is configured).
   - `/usercfg.txt` (when node.config_txt is set; included by config.txt).
4. Rootfs writes:
   - `/etc/hostname`
   - `/home/pi/.ssh/authorized_keys` (uid 1000, mode 600)
   - `/root/.ssh/authorized_keys` (root key auth for early ops)
   - `/etc/dhcpcd.conf` static-IP block (when node.has_static_ip)
   - `/etc/ssh/ssh_host_<type>_key{,.pub}` if ssh_host_key provided
     + an empty unit file masking regenerate_ssh_host_keys.service
     so the host key isn't clobbered on first boot.
   - Delete /etc/systemd/system/getty@tty1.service.d/autologin.conf
     so the userconfig setup wizard doesn't autologin on tty1.
   - `/etc/modules` (when node.modules is set).
5. Unmount + losetup -d + xz the modified .img -> out path.

Sudo is required for steps 2 + 4; bakes typically run inside an LXC
container or with sudoers entries allowing losetup/mount without
password. See README.

## Lessons baked in from operator experience

  - sshd needs a password set OR root pubkey + PermitRootLogin
    prohibit-password. Pi OS Bookworm rejects empty pi password.
  - Pi OS Trixie's userconf-pi service creates the pi user with
    `/usr/sbin/nologin` shell — SSH key auth succeeds then login is
    immediately rejected. firstrun.sh sidesteps this by creating the
    user explicitly with `-s /bin/bash` + `usermod -s /bin/bash pi`.
    Confirmed root cause 2026-05-28 on a failed pi5-smoke bake.
  - regenerate_ssh_host_keys.service rm -f's /etc/ssh/ssh_host_* on
    first boot. Masked at bake time via empty unit file so the
    pre-baked host key actually sticks. Latent since v0.2, exposed
    by `ssh_host_key: usehost` making the expected fingerprint
    predictable.
  - userconfig wizard autologin on tty1 prompts for locale/keyboard
    forever on headless bakes. autologin.conf removed at bake time.
  - SSH host keys regenerate on first boot unless we pre-bake them
    into /etc/ssh (see node.ssh_host_key_*); same logic as Alpine.
  - dhcpcd is Pi OS Lite's network manager (matches Alpine choice).
"""
from __future__ import annotations

import crypt
import logging
import os
import re
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


# --------------------------------------------------------------------------- #
# Baker classes                                                                #
# --------------------------------------------------------------------------- #

class _RaspbianBakerBase:
    """Pi OS Lite baker. Subclasses override per-codename quirks; the
    base implements everything that's identical across Bookworm and
    Trixie. As Pi OS evolves, override one method per quirk that
    surfaces — the override has a single obvious home, no `if
    codename == "trixie"` branches scattered through the code.

    Method semantics:
      - `bake()`: the orchestrator. Don't override.
      - `firstrun_sh()`: returns the bash script body. Override
        to add codename-specific commands (e.g. a Trixie-only
        unit to mask).
      - `patch_cmdline_txt()`: appends systemd.run= directives.
        Override if a codename moves cmdline.txt.
      - `write_boot_partition()` / `write_root_partition()`:
        override to add or skip codename-specific files.
    """
    codename: str = ""  # set on subclasses

    def bake(
        self, *, url: str, node: NodeConfig, out_path: Path,
        image_size_mb: int = 0,
    ) -> Path:
        """Bake a Raspberry Pi OS Lite .img.xz for `node`. Returns
        out_path.

        Sudo is required for losetup + mount steps. Pi-bake will
        surface the sudo prompt directly; in CI / LXC contexts make
        sure the operator's user can losetup + mount without password.

        `image_size_mb` is ignored — Pi OS images are fixed-size from
        upstream. Kwarg kept for parity with other backends.
        """
        del image_size_mb
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
                self.write_boot_partition(boot, node)
                self.write_root_partition(root, node)
            finally:
                # Always teardown — losetup leaks are gnarly.
                imgxz.unmount_image(mi)

            # 3. Re-xz the modified .img → operator's out path.
            imgxz.recompress_xz(raw, out_path)

        LOG.info("DONE: %s (%d MB)", out_path, out_path.stat().st_size >> 20)
        return out_path

    # ----- firstrun.sh + cmdline.txt -----

    def firstrun_sh(self, node: NodeConfig, pi_hash: str) -> str:
        """Render the bash script run once at first boot.

        Idempotent: re-running is safe. Logs to
        /var/log/pi-bake-firstrun.log. On success the script
        self-deletes and strips its systemd.run additions from
        cmdline.txt so subsequent boots are clean.
        """
        # Hard-fail the bake if any field contains a shell escape —
        # we interpolate them into a bash script and don't want to
        # chase the operator's typos at runtime. Hostname is DNS-
        # label-bounded; the auth keys are operator-controlled but
        # already validated upstream.
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
            "# Belt-and-suspenders: mask Pi OS's host-key regenerator\n"
            "# in case the bake-time mask (empty unit file) didn't take.\n"
            "# Without this, /etc/ssh/ssh_host_* gets clobbered on the\n"
            "# next boot and the operator-known fingerprint stops matching.\n"
            "systemctl mask regenerate_ssh_host_keys.service 2>/dev/null || true\n"
            "\n"
            "# Belt-and-suspenders: kill the userconfig first-boot wizard\n"
            "# autologin on tty1. Headless bakes have no console; the\n"
            "# prompt blocks visible-but-not-functionally-blocking forever.\n"
            "rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf\n"
            "rmdir /etc/systemd/system/getty@tty1.service.d 2>/dev/null || true\n"
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

    def patch_cmdline_txt(self, boot: Path) -> None:
        """Append the systemd.run= directives to
        /boot/firmware/cmdline.txt. cmdline.txt is a single line;
        we read, rstrip, append, write back.
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

    # ----- partition writes -----

    def write_boot_partition(self, boot: Path, node: NodeConfig) -> None:
        """All boot-partition (FAT) edits: firstrun.sh + cmdline.txt
        patch, legacy ssh+userconf fallback markers, wifi,
        config_txt additions.
        """
        pi_hash = _random_locked_password_hash()

        # firstrun.sh: the load-bearing first-boot mechanism since
        # v0.4. Creates the pi user with /bin/bash shell, installs
        # authorized keys, enables sshd, then self-deletes.
        # Triggered by the systemd.run= directive appended below.
        imgxz.write_file(
            boot, "firstrun.sh",
            self.firstrun_sh(node, pi_hash), mode=0o755,
        )
        self.patch_cmdline_txt(boot)
        LOG.info("boot: /firstrun.sh + cmdline.txt systemd.run= hook")

        # Legacy first-boot markers — kept as a fallback for the
        # case firstrun.sh fails to run (cmdline.txt corruption,
        # etc.). On Bookworm both paths produce the same result;
        # on Trixie firstrun.sh wins and pre-empts userconf-pi.
        # firstrun.sh's self-cleanup also deletes these so
        # userconf-pi doesn't fire on the post-reboot multi-user
        # boot.
        imgxz.write_file(boot, "ssh", b"", mode=0o644)
        LOG.info("boot: /ssh marker written (fallback)")

        imgxz.write_file(
            boot, "userconf.txt", f"pi:{pi_hash}\n", mode=0o600,
        )
        LOG.info("boot: /userconf.txt with random locked pi password (fallback)")

        # wpa_supplicant.conf for wifi. Pi OS Bookworm moved away
        # from this file (NetworkManager is the default now), but
        # Pi OS still copies legacy wpa_supplicant.conf into
        # /etc/wpa_supplicant/ on first boot for back-compat —
        # works.
        if node.has_wifi:
            imgxz.write_file(
                boot, "wpa_supplicant.conf",
                node.wpa_supplicant_conf(), mode=0o600,
            )
            LOG.info("boot: /wpa_supplicant.conf written for ssid=%r",
                     node.wifi_ssid)

        # config.txt additions go into /usercfg.txt (Pi OS doesn't
        # ship one by default; we create it and reference from
        # config.txt via an include line). Operator's `config_txt:`
        # recipe field lands here.
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

    def write_root_partition(self, root: Path, node: NodeConfig) -> None:
        """All rootfs (ext4) edits: hostname, SSH keys (host +
        authorized), dhcpcd config for static IP, /etc/modules,
        userconfig autologin removal.
        """
        # /etc/hostname
        imgxz.write_file(root, "etc/hostname",
                         f"{node.hostname}\n", mode=0o644)
        # /etc/hosts — Pi OS ships a default. Append 127.0.1.1
        # line so `hostname --fqdn` resolves to itself (Debian
        # convention).
        imgxz.append_file(root, "etc/hosts",
                          f"127.0.1.1\t{node.hostname}\n")
        LOG.info("root: hostname=%s", node.hostname)

        # SSH authorized_keys for the pi user. Pi OS pre-creates
        # uid 1000 = pi:pi, so we can chown directly.
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

        # Also drop the operator's key into /root/.ssh/ for the
        # rare case operator wants `ssh root@pi` (e.g. early
        # debug). PermitRootLogin defaults to "prohibit-password"
        # on Pi OS so this only enables key-based root login
        # (already what we want).
        imgxz.write_file(
            root, "root/.ssh/authorized_keys",
            pi_keys, mode=0o600,
        )
        imgxz.chown(root, "root/.ssh", uid=0, gid=0)
        imgxz.chown(root, "root/.ssh/authorized_keys", uid=0, gid=0)

        # SSH host keys (Recipe.ssh_host_key path → NodeConfig
        # bytes). If unset, sshd-keygen regenerates on first boot
        # (Pi OS default). Same logic as Alpine: predictable
        # identity → no known_hosts churn.
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
            # Mask Pi OS's regenerate_ssh_host_keys.service so it
            # doesn't `rm -f /etc/ssh/ssh_host_*` on first boot
            # and generate fresh random keys. systemd treats an
            # empty unit file in /etc/systemd/system as masked
            # (equivalent to `systemctl mask`). Latent bug since
            # v0.2 — got noticed once `ssh_host_key: usehost`
            # made the expected fingerprint predictable.
            imgxz.write_file(
                root,
                "etc/systemd/system/regenerate_ssh_host_keys.service",
                b"", mode=0o644,
            )
            LOG.info(
                "root: pre-baked ssh_host_%s_key + masked "
                "regenerate_ssh_host_keys.service", ktype,
            )

        # Static IP via dhcpcd (Pi OS default network manager —
        # same as Alpine). Append an interface block to
        # /etc/dhcpcd.conf.
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

        # Pi OS's first-boot setup wizard autologins as a special
        # `userconfig` user on tty1 and prompts for keyboard /
        # locale / timezone / user creation. Headless bakes never
        # see a console. The override is at
        # /etc/systemd/system/getty@tty1.service.d/autologin.conf;
        # removing it lets getty@tty1 run normally. firstrun.sh
        # re-deletes belt-and-suspenders.
        subprocess.run(
            ["sudo", "rm", "-f",
             str(root / "etc/systemd/system/getty@tty1.service.d/autologin.conf")],
            check=False,
        )
        LOG.info("root: removed userconfig autologin override (headless bake)")

        # /etc/modules for kernel module force-load (same semantics
        # as Alpine). Pi OS reads this file via systemd-modules-
        # load.
        if node.modules:
            body = (
                "# Added by pi-bake — operator-declared kernel modules\n"
                + "\n".join(node.modules) + "\n"
            )
            imgxz.append_file(root, "etc/modules", body)
            LOG.info("root: /etc/modules += %d module(s)", len(node.modules))


class _RaspbianBookwormBaker(_RaspbianBakerBase):
    """Pi OS Bookworm (Debian 12). Last release before userconf-pi's
    nologin-shell default. Currently identical to base — Bookworm-
    specific overrides go here when one surfaces.
    """
    codename = "bookworm"


class _RaspbianTrixieBaker(_RaspbianBakerBase):
    """Pi OS Trixie (Debian 13). userconf-pi creates the pi user
    with /usr/sbin/nologin shell, which firstrun.sh overrides at
    the base level. Currently identical to base — Trixie-specific
    overrides go here when one surfaces.
    """
    codename = "trixie"


# --------------------------------------------------------------------------- #
# Dispatch                                                                     #
# --------------------------------------------------------------------------- #

_BAKERS: dict[str, _RaspbianBakerBase] = {
    "bookworm": _RaspbianBookwormBaker(),
    "trixie": _RaspbianTrixieBaker(),
}

# Debian release codenames in chronological order. Pi OS's Lite
# tracks Debian's codename for the corresponding release. The
# slice of this tuple that overlaps with `_BAKERS.keys()` is
# what's officially supported; entries outside that slice are
# used only to decide which direction to fall back when an
# unknown codename shows up in a URL. Update when Debian
# announces a new codename even if we don't yet ship a baker.
_DEBIAN_CODENAMES_OLDEST_FIRST: tuple[str, ...] = (
    "jessie",
    "stretch",
    "buster",
    "bullseye",
    "bookworm",   # oldest baker pi-bake ships
    "trixie",     # newest baker pi-bake ships
    "forky",      # next; not yet shipped
    "duke",
)

# Newest + oldest baker codenames pi-bake actually ships. Derived
# from _BAKERS and the chronology above; kept as constants so
# downstream code (e.g. compat shims, dispatcher fallbacks) can
# reference them by name.
_NEWEST_BAKER: str = next(
    cn for cn in reversed(_DEBIAN_CODENAMES_OLDEST_FIRST) if cn in _BAKERS
)
_OLDEST_BAKER: str = next(
    cn for cn in _DEBIAN_CODENAMES_OLDEST_FIRST if cn in _BAKERS
)

# Back-compat alias for code that referenced the old name. Used
# by the module-level compat shims at the bottom of this file.
_DEFAULT_BAKER_CODENAME: str = _NEWEST_BAKER

# Pi OS filenames: `<date>-raspios-<codename>-<arch>-lite.img.xz`.
# Stable convention since Buster.
_CODENAME_RE = re.compile(r"raspios-([a-z]+)-arm64-")


def _detect_codename(url: str) -> str:
    """Parse the Pi OS codename from the upstream URL + dispatch
    to the matching baker, or fall back with a clear warning.

    Three cases:

    1. URL contains a known codename in _BAKERS -> use it.
    2. URL contains a codename pi-bake DOESN'T have a baker for:
       fall back to the chronologically-closest baker (newest if
       the unknown is newer-than-newest, oldest if older-than-
       oldest, nearest-older otherwise). Emits a loud WARNING:
       this is an UNTESTED COMBINATION. The fallback is a best
       guess that the codename's first-boot quirks haven't
       changed since the closest supported neighbor.
    3. URL has no codename token at all (e.g. the `_latest`
       permanent-redirect endpoint, which doesn't embed the
       codename): use the newest baker we ship, silent INFO.
       Operator's recipe is asking for whatever upstream serves.
    """
    m = _CODENAME_RE.search(url)
    if m is None:
        # Case 3: no codename in URL.
        LOG.info(
            "url has no codename token; using %s baker",
            _NEWEST_BAKER,
        )
        return _NEWEST_BAKER

    found = m.group(1)
    if found in _BAKERS:
        return found  # Case 1

    # Case 2: unknown codename. Decide which side of our supported
    # slice it falls on.
    fallback = _fallback_for_unknown_codename(found)
    LOG.warning(
        "Pi OS codename %r is not in pi-bake's baker set "
        "(known: %s); falling back to %r baker — UNTESTED "
        "COMBINATION, expect first-boot quirks the firstrun.sh "
        "script may not handle. Please file an issue or update "
        "pi-bake.",
        found, sorted(_BAKERS), fallback,
    )
    return fallback


def _fallback_for_unknown_codename(unknown: str) -> str:
    """Pick the chronologically-closest baker for `unknown`.

    Strategy:
      - If we don't know `unknown` at all (not in the chronology),
        fall forward to the newest baker. Best guess: newer Pi OS
        releases usually retain backwards-compatible first-boot
        behavior with the immediately preceding one.
      - If `unknown` is older than our oldest baker, fall back to
        oldest.
      - If newer than our newest baker, fall back to newest.
      - If between oldest and newest (i.e. pi-bake skipped a
        release in its catalog), pick the nearest older baker —
        the conservative direction.
    """
    if unknown not in _DEBIAN_CODENAMES_OLDEST_FIRST:
        return _NEWEST_BAKER
    idx_unknown = _DEBIAN_CODENAMES_OLDEST_FIRST.index(unknown)
    idx_oldest = _DEBIAN_CODENAMES_OLDEST_FIRST.index(_OLDEST_BAKER)
    idx_newest = _DEBIAN_CODENAMES_OLDEST_FIRST.index(_NEWEST_BAKER)
    if idx_unknown < idx_oldest:
        return _OLDEST_BAKER
    if idx_unknown > idx_newest:
        return _NEWEST_BAKER
    # Between: walk backwards from `unknown` to the nearest
    # supported baker.
    for cn in reversed(_DEBIAN_CODENAMES_OLDEST_FIRST[:idx_unknown]):
        if cn in _BAKERS:
            return cn
    return _OLDEST_BAKER  # unreachable given the bracket check above


def bake(
    *, url: str, node: NodeConfig, out_path: Path,
    image_size_mb: int = 0,
) -> Path:
    """Bake a Raspberry Pi OS Lite .img.xz for `node`.

    Dispatches to the matching per-codename baker (Bookworm /
    Trixie) by parsing the codename from the URL. Sudo is
    required for losetup + mount steps.
    """
    cn = _detect_codename(url)
    baker = _BAKERS[cn]
    LOG.info("raspbian: dispatching to %s baker", cn)
    return baker.bake(
        url=url, node=node, out_path=out_path,
        image_size_mb=image_size_mb,
    )


# --------------------------------------------------------------------------- #
# Compatibility shims for module-level imports                                 #
# --------------------------------------------------------------------------- #

# Existing tests + external consumers reference module-level
# helpers. Keep them callable here, delegating to the default
# baker (firstrun.sh + cmdline patch are identical across
# codenames today; if they diverge, importers should call the
# class directly).
def _firstrun_sh(node: NodeConfig, pi_hash: str) -> str:
    return _BAKERS[_DEFAULT_BAKER_CODENAME].firstrun_sh(node, pi_hash)


def _patch_cmdline_txt(boot: Path) -> None:
    _BAKERS[_DEFAULT_BAKER_CODENAME].patch_cmdline_txt(boot)


def _write_boot_partition(boot: Path, node: NodeConfig) -> None:
    _BAKERS[_DEFAULT_BAKER_CODENAME].write_boot_partition(boot, node)


def _write_root_partition(root: Path, node: NodeConfig) -> None:
    _BAKERS[_DEFAULT_BAKER_CODENAME].write_root_partition(root, node)
