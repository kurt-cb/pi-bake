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
    _BAKERS,
    _DEFAULT_BAKER_CODENAME,
    _FIRSTRUN_CMDLINE,
    _NEWEST_BAKER,
    _OLDEST_BAKER,
    _RaspbianBakerBase,
    _RaspbianBookwormBaker,
    _RaspbianTrixieBaker,
    _detect_codename,
    _fallback_for_unknown_codename,
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


def test_firstrun_sh_sets_timezone():
    """The Raspbian-timezone gap closed in v0.5.1: firstrun.sh
    now writes /etc/timezone + the /etc/localtime symlink from
    NodeConfig.timezone. Latent gap since v0.2 — schema accepted
    the field but Raspbian dropped the value."""
    s = _firstrun_sh(
        _node(timezone="America/New_York"), "$6$abc$hash",
    )
    assert "echo 'America/New_York' > /etc/timezone" in s
    assert (
        "ln -sf /usr/share/zoneinfo/America/New_York /etc/localtime"
        in s
    )


def test_firstrun_sh_sets_locale():
    """v0.5.1+: pi-bake writes /etc/default/locale + runs
    locale-gen for the operator's chosen locale. Pi OS Lite
    ships only en_GB.UTF-8 pre-generated; other locales need
    explicit generation."""
    s = _firstrun_sh(
        _node(locale="en_US.UTF-8"), "$6$abc$hash",
    )
    assert "locale-gen 'en_US.UTF-8'" in s
    assert "update-locale 'LANG=en_US.UTF-8'" in s
    # The locale.gen sed uncomments the chosen locale line.
    assert "sed -i 's|^# *en_US.UTF-8 |en_US.UTF-8 |' /etc/locale.gen" in s


def test_firstrun_sh_rejects_shell_unsafe_timezone():
    """Belt-and-suspenders. NodeConfig doesn't validate timezone
    format yet, so a hand-constructed instance could carry shell
    metachars."""
    safe = _node()
    object.__setattr__(safe, "timezone", "; rm -rf /")
    with pytest.raises(ValueError, match="shell-unsafe"):
        _firstrun_sh(safe, "$6$abc$hash")


def test_firstrun_sh_rejects_shell_unsafe_locale():
    safe = _node()
    object.__setattr__(safe, "locale", "en_US.UTF-8; echo pwned")
    with pytest.raises(ValueError, match="shell-unsafe"):
        _firstrun_sh(safe, "$6$abc$hash")


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


# ---------- per-codename baker classes + dispatch ----------


def test_bookworm_baker_codename_is_bookworm():
    assert _RaspbianBookwormBaker().codename == "bookworm"


def test_trixie_baker_codename_is_trixie():
    assert _RaspbianTrixieBaker().codename == "trixie"


def test_bakers_dict_has_both_codenames():
    assert set(_BAKERS) == {"bookworm", "trixie"}


def test_default_baker_codename_is_newest():
    """Newest-first: fallback for unknown URLs should be the
    newest codename we ship (currently Trixie)."""
    assert _DEFAULT_BAKER_CODENAME == "trixie"


def test_both_subclasses_inherit_from_base():
    """The whole point of the refactor is shared base behavior;
    if a subclass doesn't inherit, codename divergence would
    require duplicating every method instead of overriding one."""
    assert issubclass(_RaspbianBookwormBaker, _RaspbianBakerBase)
    assert issubclass(_RaspbianTrixieBaker, _RaspbianBakerBase)


def test_detect_codename_from_bookworm_url():
    url = (
        "https://downloads.raspberrypi.com/raspios_lite_arm64/"
        "images/raspios_lite_arm64-2025-05-13/"
        "2025-05-13-raspios-bookworm-arm64-lite.img.xz"
    )
    assert _detect_codename(url) == "bookworm"


def test_detect_codename_from_trixie_url():
    url = (
        "https://downloads.raspberrypi.com/raspios_lite_arm64/"
        "images/raspios_lite_arm64-2026-04-21/"
        "2026-04-21-raspios-trixie-arm64-lite.img.xz"
    )
    assert _detect_codename(url) == "trixie"


def test_detect_codename_no_token_uses_newest_silently(caplog):
    """The Pi OS permanent-redirect URL has no codename token.
    No WARNING — operator's recipe is asking for whatever
    upstream serves, and we route to our newest baker as
    the best guess for `latest`."""
    import logging
    with caplog.at_level(logging.INFO):
        cn = _detect_codename(
            "https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
        )
    assert cn == _NEWEST_BAKER
    # INFO is OK, WARNING is not — `_latest` is a documented
    # case, not an error condition.
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_detect_codename_newer_than_newest_warns_and_uses_newest(caplog):
    """A future Forky URL with no baker -> fall forward to Trixie
    (newest), but WARN loudly: this is an UNTESTED COMBINATION."""
    import logging
    url = (
        "https://downloads.raspberrypi.com/raspios_lite_arm64/"
        "images/raspios_lite_arm64-2027-04-01/"
        "2027-04-01-raspios-forky-arm64-lite.img.xz"
    )
    with caplog.at_level(logging.WARNING):
        cn = _detect_codename(url)
    assert cn == _NEWEST_BAKER
    assert any(
        "UNTESTED" in r.message and "forky" in r.message
        for r in caplog.records
    )


def test_detect_codename_older_than_oldest_warns_and_uses_oldest(caplog):
    """A Bullseye URL (older than Bookworm, our oldest baker) ->
    fall BACK to Bookworm and WARN. Older Pi OS releases shared
    enough first-boot behavior with their immediate successor
    that the closest-supported neighbor is the safest fallback."""
    import logging
    url = (
        "https://downloads.raspberrypi.com/raspios_lite_arm64/"
        "images/raspios_lite_arm64-2023-12-06/"
        "2023-12-05-raspios-bullseye-arm64-lite.img.xz"
    )
    with caplog.at_level(logging.WARNING):
        cn = _detect_codename(url)
    assert cn == _OLDEST_BAKER
    assert any(
        "UNTESTED" in r.message and "bullseye" in r.message
        for r in caplog.records
    )


def test_fallback_unknown_codename_uses_newest():
    """A codename not in our chronology at all (e.g. Pi OS renames
    a release) -> best-guess newest baker."""
    assert _fallback_for_unknown_codename("notarealdebianrelease") == _NEWEST_BAKER


def test_fallback_in_between_codename_picks_nearest_older():
    """If pi-bake skipped a codename in its catalog (e.g. shipped
    Bookworm + Trixie but not whatever fictional codename sits
    between them), the fallback walks BACKWARDS to the nearest
    supported one — conservative direction. Today we have no
    gap in _BAKERS, but the logic is exercised by ensuring it
    doesn't crash on an interpolated case."""
    # Construct an artificial chronology with a gap to exercise
    # the path.
    import pi_bake.raspbian as r
    orig = r._DEBIAN_CODENAMES_OLDEST_FIRST
    orig_bakers = r._BAKERS.copy()
    try:
        # Pretend there's a 'midcodename' between bookworm + trixie
        # and we don't have a baker for it.
        r._DEBIAN_CODENAMES_OLDEST_FIRST = (
            "bullseye", "bookworm", "midcodename", "trixie", "forky",
        )
        # _BAKERS still only has bookworm + trixie
        assert _fallback_for_unknown_codename("midcodename") == "bookworm"
    finally:
        r._DEBIAN_CODENAMES_OLDEST_FIRST = orig
        r._BAKERS.clear()
        r._BAKERS.update(orig_bakers)


def test_oldest_baker_is_bookworm():
    """Catalog invariant — bookworm is our oldest shipped baker."""
    assert _OLDEST_BAKER == "bookworm"


def test_newest_baker_matches_default():
    """Compat shim: _DEFAULT_BAKER_CODENAME is an alias for
    _NEWEST_BAKER. Tests on either name verify the same thing."""
    assert _DEFAULT_BAKER_CODENAME == _NEWEST_BAKER


def test_subclasses_produce_identical_firstrun_today(monkeypatch):
    """Both subclasses MUST produce identical firstrun.sh today
    (the v0.4 fixes work on both Bookworm and Trixie because
    firstrun.sh sidesteps Pi-OS-specific mechanisms). When this
    test fails, that's a signal that a divergence has been
    introduced — review whether the override belongs in the
    correct subclass."""
    # Stabilize the random hash so output is comparable.
    monkeypatch.setattr(
        "pi_bake.raspbian._random_locked_password_hash",
        lambda: "$6$abc$hash",
    )
    n = _node()
    bw = _RaspbianBookwormBaker().firstrun_sh(n, "$6$abc$hash")
    tx = _RaspbianTrixieBaker().firstrun_sh(n, "$6$abc$hash")
    assert bw == tx
