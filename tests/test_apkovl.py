"""apkovl tarball content + structure tests.

`_write_apkovl` produces a tar.gz containing the per-node overlay
files Alpine restores on first boot. Each test extracts an
in-memory archive and asserts shape — no SD card, no Pi, no
network needed.
"""
from __future__ import annotations

import io
import tarfile

import pytest

from pi_bake.alpine import _write_apkovl
from pi_bake.config import NodeConfig

_PUBKEY = "ssh-ed25519 AAAA primary"


def _bake(node: NodeConfig, tmp_path, **kw) -> tarfile.TarFile:
    out = tmp_path / "node.apkovl.tar.gz"
    _write_apkovl(out, node, **kw)
    return tarfile.open(out, "r:gz")


def _extract(tf: tarfile.TarFile, path: str) -> str:
    return tf.extractfile(path).read().decode()


def test_minimal_overlay_has_required_files(tmp_path):
    n = NodeConfig(hostname="pi-radio-1", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        names = set(tf.getnames())
    required = {
        "etc/hostname",
        "etc/hosts",
        "etc/timezone",
        "etc/ssh/sshd_config",
        "root/.ssh/authorized_keys",
        "etc/network/interfaces",
        "etc/apk/world",
        "etc/apk/repositories",
        "etc/runlevels/default/sshd",
        "etc/runlevels/default/chronyd",
        "etc/runlevels/default/dhcpcd",
        "etc/runlevels/default/networking",
    }
    missing = required - names
    assert not missing, f"missing overlay paths: {missing}"


def test_wifi_overlay_adds_wpa_supplicant(tmp_path):
    n = NodeConfig(
        hostname="pi-radio-1", ssh_pubkey=_PUBKEY,
        wifi_ssid="totaldns-lab", wifi_psk="secret",
    )
    with _bake(n, tmp_path) as tf:
        names = set(tf.getnames())
    assert "etc/wpa_supplicant/wpa_supplicant.conf" in names
    assert "etc/runlevels/default/wpa_supplicant" in names
    assert "etc/conf.d/wpa_supplicant" in names


def test_no_wifi_overlay_omits_wpa(tmp_path):
    n = NodeConfig(hostname="pi-wired", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        names = set(tf.getnames())
    assert "etc/wpa_supplicant/wpa_supplicant.conf" not in names
    assert "etc/runlevels/default/wpa_supplicant" not in names


def test_hostname_content(tmp_path):
    n = NodeConfig(hostname="boat-pi-1", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        data = _extract(tf, "etc/hostname")
    assert data == "boat-pi-1\n"


def test_authorized_keys_perms_strict(tmp_path):
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        info = tf.getmember("root/.ssh/authorized_keys")
    # Must be 0600 — sshd refuses to use it otherwise.
    assert info.mode == 0o600


def test_runlevel_entries_are_symlinks(tmp_path):
    """Alpine's runlevel "enable" mechanism is a symlink at
    /etc/runlevels/<rl>/<svc> → /etc/init.d/<svc>."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        sshd = tf.getmember("etc/runlevels/default/sshd")
    assert sshd.issym()
    assert sshd.linkname == "/etc/init.d/sshd"


def test_apk_world_includes_sshd_and_dhcpcd(tmp_path):
    """Alpine RPi init runs `apk add --root $sysroot --no-network`
    reading /etc/apk/world. Anything we list here gets installed at
    first boot from the local /apks cache — no network required.

    openssh-sftp-server is required because modern scp (openssh
    9.0+) uses SFTP by default, and pyinfra needs SFTP too."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        world = set(_extract(tf, "etc/apk/world").split())
    # The minimum viable set: real sshd (with sftp), DHCP, clock.
    assert {"openssh-server", "openssh-sftp-server",
            "dhcpcd", "dhcpcd-openrc", "chrony", "openssh-client-default"} <= world


def test_apk_world_adds_wpa_supplicant_for_wifi(tmp_path):
    n = NodeConfig(
        hostname="pi", ssh_pubkey=_PUBKEY,
        wifi_ssid="x", wifi_psk="y",
    )
    with _bake(n, tmp_path) as tf:
        world = set(_extract(tf, "etc/apk/world").split())
    assert {"wpa_supplicant", "wpa_supplicant-openrc"} <= world


def test_apk_world_omits_wpa_supplicant_for_wired(tmp_path):
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        world = _extract(tf, "etc/apk/world").split()
    assert "wpa_supplicant" not in world


def test_dhcpcd_conf_sends_hostname_option(tmp_path):
    """Without the bare `hostname` directive in /etc/dhcpcd.conf,
    dhcpcd does NOT send DHCP option 12 — even though /etc/hostname
    is set. totaldns then logs leases against a synthesized
    `unknown-<6mac>` placeholder instead of the operator-chosen
    name (found on real hardware 2026-05-23)."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        conf = _extract(tf, "etc/dhcpcd.conf")
    # The bare `hostname` directive, on its own line — no `# hostname`
    # comment or `hostname foo` static override.
    lines = [l.strip() for l in conf.splitlines() if not l.startswith("#")]
    assert "hostname" in lines


def test_dhcp_send_hostname_false_disables_option_12(tmp_path):
    """`NodeConfig(dhcp_send_hostname=False)` bakes the `hostname`
    directive as a comment instead, so the device intentionally
    skips DHCP option 12 — useful as a test fixture for DHCP
    servers that need to recover the hostname via mDNS."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY,
                   dhcp_send_hostname=False)
    with _bake(n, tmp_path) as tf:
        conf = _extract(tf, "etc/dhcpcd.conf")
    # No active `hostname` line (must not match `hostname` as the
    # whole word at start of a non-comment line).
    active_lines = [l.strip() for l in conf.splitlines()
                    if l.strip() and not l.lstrip().startswith("#")]
    assert "hostname" not in active_lines
    # But the disabled-by-pi-bake comment IS present so operators
    # can see WHY it's off.
    assert "--no-dhcp-hostname" in conf


def test_apk_repositories_includes_local_cache(tmp_path):
    """Without the local /media/mmcblk0/apks line, init's apk add
    (run with --no-network on default cmdline) finds nothing."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        repos = _extract(tf, "etc/apk/repositories")
    assert "/media/mmcblk0/apks" in repos


def test_dhcp_interfaces_omits_eth0(tmp_path):
    """dhcpcd watches all interfaces as a daemon. If we ALSO listed
    eth0 in /etc/network/interfaces, busybox udhcpc would fire via
    `networking` and race dhcpcd (and on Pi 5's macb driver, udhcpc
    hangs). So for DHCP nodes, only lo is in interfaces."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        ifaces = _extract(tf, "etc/network/interfaces")
    assert "eth0" not in ifaces
    assert "iface lo inet loopback" in ifaces


def test_static_ip_interfaces_carries_eth0(tmp_path):
    n = NodeConfig(
        hostname="pi", ssh_pubkey=_PUBKEY,
        static_ipv4="192.168.4.111/24", gateway_ipv4="192.168.4.1",
    )
    with _bake(n, tmp_path) as tf:
        ifaces = _extract(tf, "etc/network/interfaces")
        names = set(tf.getnames())
    assert "iface eth0 inet static" in ifaces
    assert "192.168.4.111" in ifaces
    assert "192.168.4.1" in ifaces
    # dhcpcd is redundant when eth0 is static — and would actively
    # confuse things by trying to DHCP over the static config.
    assert "etc/runlevels/default/dhcpcd" not in names


def test_static_ip_omits_dhcpcd_from_world(tmp_path):
    n = NodeConfig(
        hostname="pi", ssh_pubkey=_PUBKEY,
        static_ipv4="192.168.4.111/24", gateway_ipv4="192.168.4.1",
    )
    with _bake(n, tmp_path) as tf:
        world = _extract(tf, "etc/apk/world").split()
    assert "dhcpcd" not in world
    assert "dhcpcd-openrc" not in world


def test_default_boot_services_marker_present(tmp_path):
    """Alpine RPi's /init only wires up modloop+modules+etc. in the
    sysinit/boot runlevels when /etc/.default_boot_services is
    present (or when there's no apkovl at all). Without that, the
    squashfs of kernel modules never mounts, and af_packet is
    missing — every DHCP client fails with "Address family not
    supported". This empty marker file is THE thing that makes
    networking work."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        info = tf.getmember("etc/.default_boot_services")
    assert info.isreg()
    assert info.size == 0


def test_lbu_conf_sets_media(tmp_path):
    """`lbu commit` (Alpine's local-backup tool, how the operator
    persists apkovl changes across reboots) refuses to do anything
    without LBU_MEDIA set. The stock /etc/lbu/lbu.conf has it
    commented out."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        conf = _extract(tf, "etc/lbu/lbu.conf")
    assert "LBU_MEDIA" in conf
    assert "BACKUP_LIMIT" in conf
    assert "mmcblk0" in conf


def test_sshd_config_omits_unsupported_directives(tmp_path):
    """Alpine's openssh is built WITHOUT PAM. `UsePAM yes` makes
    sshd refuse to start ('Bad configuration option')."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        cfg = _extract(tf, "etc/ssh/sshd_config")
    assert "UsePAM" not in cfg
    # ChallengeResponseAuthentication was renamed in openssh 8.7
    # to KbdInteractiveAuthentication. The old spelling still
    # parses on 9.x but is misleading.
    assert "ChallengeResponseAuthentication" not in cfg


def test_no_firstboot_script_when_no_extras(tmp_path):
    """v0.1.x used a /etc/local.d/pi-bake-firstboot.start that did
    `apk update && apk add ...` unconditionally. That broke whenever
    the Pi had no network at boot or wrong clock (no RTC). v0.1.x
    onward installs the baseline from /apks cache via /etc/apk/world.

    The `local` runlevel + /etc/local.d/install-extras.start ONLY
    appears when `extra_packages` is non-empty (v0.0.9+). With no
    extras + no wifi, both stay absent."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        names = set(tf.getnames())
    assert "etc/local.d/pi-bake-firstboot.start" not in names
    assert "etc/local.d/install-extras.start" not in names
    assert "etc/runlevels/default/local" not in names


def test_extra_packages_NOT_in_world(tmp_path):
    """Regression for the v0.0.8 DHCP bug. Alpine init's first-boot
    `apk add --root $sysroot --no-network $world` FAILS WHOLESALE if
    any package in /etc/apk/world isn't in the local /apks cache
    (stock RPi tarball only ships ~150 packages — no avahi, dbus,
    linux-firmware-intel, etc.). That whole-transaction failure left
    dhcpcd uninstalled → no DHCP → 169.x APIPA on first boot.

    v0.0.9 fix: extras go to /etc/local.d/install-extras.start which
    runs ONLINE after dhcpcd has converged. /etc/apk/world stays
    baseline-only."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    extras = ["avahi", "dbus", "linux-firmware-intel"]
    with _bake(n, tmp_path, extra_packages=extras) as tf:
        world = set(_extract(tf, "etc/apk/world").split())
        names = set(tf.getnames())
    for pkg in extras:
        assert pkg not in world, f"{pkg} leaked into /etc/apk/world"
    assert "etc/local.d/install-extras.start" in names
    assert "etc/runlevels/default/local" in names


def test_install_extras_script_runs_apk_add(tmp_path):
    """The install-extras.start script must invoke `apk add` with the
    declared extras, wait for network convergence, and self-disable
    on success so re-runs are no-ops."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path, extra_packages=["avahi", "dbus"]) as tf:
        script = _extract(tf, "etc/local.d/install-extras.start")
    assert script.startswith("#!/bin/sh")
    assert "apk add avahi dbus" in script
    # Network-convergence wait — must not fire apk add against a
    # network that hasn't come up yet.
    assert "ip route get" in script
    # Self-disable marker — second boot should be a no-op.
    assert "install-extras.done" in script


# --------------------------------------------------------------------------- #
# SSH host keys — pre-baked identity (v0.2 feature)                            #
# --------------------------------------------------------------------------- #

def test_ssh_host_key_auto_generated_when_not_provided(tmp_path):
    """No NodeConfig host key → baker generates a fresh ed25519 pair
    and embeds it. Stable across reflashes of the same .img.gz."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        names = set(tf.getnames())
        pub = _extract(tf, "etc/ssh/ssh_host_ed25519_key.pub")
        priv = _extract(tf, "etc/ssh/ssh_host_ed25519_key")
    assert "etc/ssh/ssh_host_ed25519_key" in names
    assert "etc/ssh/ssh_host_ed25519_key.pub" in names
    # ssh-keygen ed25519 output is unmistakable.
    assert pub.startswith("ssh-ed25519 ")
    assert "pi-bake@pi" in pub
    assert priv.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")


def test_ssh_host_key_perms_strict(tmp_path):
    """sshd refuses to use a host private key unless its perms are
    0600. The .pub stays 0644 (world-readable, like any pubkey)."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        priv = tf.getmember("etc/ssh/ssh_host_ed25519_key")
        pub = tf.getmember("etc/ssh/ssh_host_ed25519_key.pub")
    assert priv.mode == 0o600
    assert pub.mode == 0o644


def test_ssh_host_key_provided_pair_is_baked_verbatim(tmp_path):
    """When NodeConfig supplies the keypair, bake those exact bytes
    (no rewrite, no regen). Stable across `pi-bake build` runs."""
    sentinel_priv = b"-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n"
    sentinel_pub = b"ssh-ed25519 AAAATEST operator-managed@td-pi5-1\n"
    n = NodeConfig(
        hostname="pi", ssh_pubkey=_PUBKEY,
        ssh_host_key_priv=sentinel_priv,
        ssh_host_key_pub=sentinel_pub,
    )
    with _bake(n, tmp_path) as tf:
        priv = tf.extractfile("etc/ssh/ssh_host_ed25519_key").read()
        pub = tf.extractfile("etc/ssh/ssh_host_ed25519_key.pub").read()
    assert priv == sentinel_priv
    assert pub == sentinel_pub


def test_ssh_host_key_rsa_lands_at_rsa_filename(tmp_path):
    """Key type is derived from the pubkey's first word — an RSA
    keypair lands at /etc/ssh/ssh_host_rsa_key, not ed25519."""
    n = NodeConfig(
        hostname="pi", ssh_pubkey=_PUBKEY,
        ssh_host_key_priv=b"-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n",
        ssh_host_key_pub=b"ssh-rsa AAAATEST operator@laptop\n",
    )
    with _bake(n, tmp_path) as tf:
        names = set(tf.getnames())
    assert "etc/ssh/ssh_host_rsa_key" in names
    assert "etc/ssh/ssh_host_rsa_key.pub" in names
    assert "etc/ssh/ssh_host_ed25519_key" not in names


def test_ssh_host_key_partial_pair_rejected():
    """NodeConfig validation: priv + pub both or neither."""
    with pytest.raises(ValueError, match="both be set or both empty"):
        NodeConfig(
            hostname="pi", ssh_pubkey=_PUBKEY,
            ssh_host_key_priv=b"-----BEGIN ...",
            ssh_host_key_pub=b"",
        )


def test_ssh_host_key_unknown_type_rejected():
    """NodeConfig validation: unrecognized pubkey type fails fast."""
    with pytest.raises(ValueError, match="OpenSSH key type"):
        NodeConfig(
            hostname="pi", ssh_pubkey=_PUBKEY,
            ssh_host_key_priv=b"fake-priv",
            ssh_host_key_pub=b"dsa-not-supported AAAA test\n",
        )


# --------------------------------------------------------------------------- #
# Bake-time apk-fetch — air-gap install-extras script (v0.2)                   #
# --------------------------------------------------------------------------- #

def test_install_extras_offline_when_apk_fetch_used(tmp_path):
    """With apk_fetch_used=True the install-extras script must use
    `apk add --no-network --allow-untrusted` against the bake-time-
    staged .apks in /media/mmcblk0/apks/*/extras/. NEVER `apk update`
    or talk to the network."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path,
               extra_packages=["avahi", "dbus"],
               apk_fetch_used=True) as tf:
        script = _extract(tf, "etc/local.d/install-extras.start")
    assert script.startswith("#!/bin/sh")
    # Offline install command.
    assert "--no-network" in script
    assert "--allow-untrusted" in script
    # Points at the bake-time staging dir.
    assert "/media/mmcblk0/apks/*/extras" in script
    # Must NOT use the online-install commands the v0.0.9 path uses.
    assert "apk update" not in script
    assert "ip route get" not in script
    # Self-disable marker is still there.
    assert "install-extras.done" in script


def test_install_extras_offline_does_not_list_package_names(tmp_path):
    """The offline script installs by FILE GLOB, not by package name —
    package names from `packages:` aren't needed at first-boot time
    because the .apk files (with all metadata) are already staged."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path,
               extra_packages=["linux-firmware-intel"],
               apk_fetch_used=True) as tf:
        script = _extract(tf, "etc/local.d/install-extras.start")
    # The script is keyed off the staged files, not the recipe pkg list.
    assert "linux-firmware-intel" not in script


def test_install_extras_online_unchanged_when_apk_fetch_off(tmp_path):
    """Default (apk_fetch_used=False) preserves v0.0.9 online-install
    behavior. Used when the operator opts OUT of bake-time fetch (or
    just hasn't opted in yet)."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path, extra_packages=["avahi", "dbus"]) as tf:
        script = _extract(tf, "etc/local.d/install-extras.start")
    # Online flavor: apk update + apk add by name + ip route wait.
    assert "apk update" in script
    assert "apk add avahi dbus" in script
    assert "--no-network" not in script
    assert "--allow-untrusted" not in script
