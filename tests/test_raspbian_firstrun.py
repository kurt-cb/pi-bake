"""firstrun.sh + cmdline.txt patch — the Pi OS Trixie fix.

The bug we're regression-testing:
  Pi OS Trixie's userconf-pi service creates the pi user with
  /usr/sbin/nologin shell. SSH key auth succeeds, then login is
  immediately rejected ("This account is currently not available").
  Confirmed root cause 2026-05-28 on a failing pi5-smoke bake.

The fix (v0.4): bake a firstrun.sh that creates the pi user
explicitly with `useradd -s /bin/bash` and force-sets the shell
with `usermod -s /bin/bash pi` before the legacy userconf-pi
service can fight us. Triggered by systemd.run= in cmdline.txt.
"""
from __future__ import annotations

import pytest

from pi_bake.config import NodeConfig
from pi_bake.raspbian import (
    _FIRSTRUN_CMDLINE,
    _firstrun_sh,
    _random_locked_password_hash,
)

_PUBKEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeFakeFakeFakeFakeFake"
    "FakeFakeFakeFakeFake op@bake"
)


def _node(**overrides) -> NodeConfig:
    defaults = dict(
        hostname="td-pi5-1",
        ssh_pubkey=_PUBKEY,
        board="pi-5",
    )
    defaults.update(overrides)
    return NodeConfig(**defaults)


# ---------- script contents: the load-bearing pieces ----------


def test_firstrun_sh_starts_with_bash_shebang():
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert s.startswith("#!/bin/bash\n")


def test_firstrun_sh_forces_bash_shell():
    """The 'usermod -s /bin/bash pi' is the load-bearing fix for
    Trixie's userconf-pi nologin default. If this line ever gets
    removed, the original bug returns."""
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "usermod -s /bin/bash pi" in s


def test_firstrun_sh_creates_pi_user_with_bash_when_missing():
    """useradd with -s /bin/bash covers the case where userconf-pi
    didn't run yet (firstrun.sh races ahead)."""
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "useradd -m -G sudo," in s
    assert "-s /bin/bash pi" in s


def test_firstrun_sh_sets_password_via_chpasswd_e():
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "chpasswd -e" in s
    assert "$6$abc$hash" in s


def test_firstrun_sh_installs_authorized_keys_with_pi_ownership():
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "install -o pi -g pi -m 700 -d /home/pi/.ssh" in s
    assert "EOAUTH" in s
    assert "ssh-ed25519" in s
    assert "chown pi:pi /home/pi/.ssh/authorized_keys" in s
    assert "chmod 600 /home/pi/.ssh/authorized_keys" in s


def test_firstrun_sh_enables_ssh_service():
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "systemctl enable ssh.service" in s


def test_firstrun_sh_kills_userconfig_autologin():
    """Pi OS Lite's first-boot setup wizard autologins as a
    special `userconfig` user on tty1 via
    /etc/systemd/system/getty@tty1.service.d/autologin.conf
    and prompts for keyboard/locale/timezone/user. Headless
    bakes have no console to interact with. raspbian.py
    deletes the autologin.conf at bake time; firstrun.sh
    re-deletes belt-and-suspenders."""
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert (
        "rm -f /etc/systemd/system/getty@tty1.service.d/autologin.conf"
        in s
    )


def test_firstrun_sh_masks_host_key_regenerator():
    """Pi OS's regenerate_ssh_host_keys.service runs once at first
    boot and `rm -f /etc/ssh/ssh_host_*` + ssh-keygen -A's. That
    blows away any pre-baked host key (the whole point of
    ssh_host_key:). raspbian.py masks the unit at bake time via
    an empty unit file; firstrun.sh masks again as belt-and-
    suspenders in case a future Pi OS adds a different regen
    path. Latent bug since v0.2 — exposed by `ssh_host_key:
    usehost` making the expected fingerprint predictable."""
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "systemctl mask regenerate_ssh_host_keys.service" in s


def test_firstrun_sh_self_deletes_and_strips_cmdline():
    """After the work is done, the script removes itself and
    strips both systemd.run= and systemd.unit= directives from
    cmdline.txt so subsequent boots are clean."""
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "rm -f /boot/firmware/firstrun.sh /boot/firstrun.sh" in s
    # The sed strips both directives (escaped dots in the regex).
    assert "sed -i" in s
    assert "systemd\\.run" in s
    assert "systemd\\.unit" in s
    # Covers both Bookworm (/boot/firmware) and any future
    # remapping where /boot is the FAT directly.
    assert "/boot/firmware/cmdline.txt" in s
    assert "/boot/cmdline.txt" in s


def test_firstrun_sh_disarms_legacy_markers():
    """Once firstrun.sh has set up the user, the /boot/firmware/
    userconf.txt + /boot/firmware/ssh fallback markers should be
    deleted — otherwise userconf-pi.service fires on the next
    boot and could re-clobber the shell back to nologin."""
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "/boot/firmware/userconf.txt" in s
    assert "rm -f /boot/firmware/userconf.txt" in s


def test_firstrun_sh_sets_hostname():
    s = _firstrun_sh(_node(hostname="td-pi5-1"), "$6$abc$hash")
    assert "echo 'td-pi5-1' > /etc/hostname" in s
    assert "127.0.1.1" in s


def test_firstrun_sh_logs_to_var_log():
    """Operator-friendly: when something goes wrong on first boot,
    the log file is the first place to look."""
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "/var/log/pi-bake-firstrun.log" in s


def test_firstrun_sh_uses_set_plus_e_to_continue_on_errors():
    """firstrun.sh must NOT abort on the first failure — each step
    is best-effort and idempotent; partial success is better than
    no first-boot setup at all."""
    s = _firstrun_sh(_node(), "$6$abc$hash")
    assert "set +e" in s


# ---------- shell injection defenses ----------


def test_firstrun_sh_hostname_is_dns_label_constrained():
    """NodeConfig validates hostname as a DNS label (lowercase
    alphanumeric + hyphen) — that's the primary defense against
    shell-injection in firstrun.sh, since the hostname is
    interpolated into the bash script body."""
    for evil in ("'; rm -rf /", "a$b", "a`b`", 'a"b', "a\nb", "a\\b"):
        with pytest.raises(ValueError, match="DNS label"):
            _node(hostname=evil)
    # _firstrun_sh keeps a belt-and-suspenders shell-metachar check
    # in case a future caller bypasses NodeConfig validation.
    # Bypass via dataclasses.replace would still re-validate, so
    # use object.__setattr__ on a frozen-but-not-really NodeConfig.
    import dataclasses
    safe = _node()
    # NodeConfig isn't frozen; mutating it directly is the
    # smallest possible bypass.
    object.__setattr__(safe, "hostname", "a$b")
    with pytest.raises(ValueError, match="shell-unsafe"):
        _firstrun_sh(safe, "$6$abc$hash")
    del dataclasses  # unused; kept for documentation


def test_firstrun_sh_rejects_authorized_keys_with_heredoc_terminator():
    """authorized_keys is embedded in a heredoc; a key containing
    'EOAUTH' would break out of it. Refuse at bake time."""
    bad = _PUBKEY + " EOAUTH-trick"
    node = NodeConfig(
        hostname="x", ssh_pubkey=bad, board="pi-5",
    )
    with pytest.raises(ValueError, match="heredoc terminator"):
        _firstrun_sh(node, "$6$abc$hash")


# ---------- cmdline.txt directives ----------


def test_firstrun_cmdline_contains_required_directives():
    """cmdline.txt gets three space-separated systemd.* params
    appended:
      - systemd.run=<script>  — the unit body
      - systemd.run_success_action=reboot  — drop to normal boot
        after first-boot work completes
      - systemd.unit=kernel-command-line.target  — prevents
        multi-user.target activation, so userconf-pi.service
        doesn't race with firstrun.sh
    """
    assert "systemd.run=/boot/firmware/firstrun.sh" in _FIRSTRUN_CMDLINE
    assert "systemd.run_success_action=reboot" in _FIRSTRUN_CMDLINE
    assert "systemd.unit=kernel-command-line.target" in _FIRSTRUN_CMDLINE
    # The leading space matters: cmdline.txt is single-line and we
    # append after the existing one.
    assert _FIRSTRUN_CMDLINE.startswith(" ")


# ---------- password hash format ----------


def test_random_locked_password_hash_is_sha512_format():
    h = _random_locked_password_hash()
    assert h.startswith("$6$")


def test_random_locked_password_hash_is_unique():
    """Different bakes should never produce the same hash —
    otherwise an attacker who cracks one hash unlocks every Pi."""
    assert _random_locked_password_hash() != _random_locked_password_hash()
