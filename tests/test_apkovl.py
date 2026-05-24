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


def _bake(node: NodeConfig, tmp_path) -> tarfile.TarFile:
    out = tmp_path / "node.apkovl.tar.gz"
    _write_apkovl(out, node)
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
    first boot from the local /apks cache — no network required."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        world = set(_extract(tf, "etc/apk/world").split())
    # The minimum viable set: real sshd, working DHCP, working clock.
    assert {"openssh-server", "dhcpcd", "dhcpcd-openrc", "chrony"} <= world


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


def test_no_firstboot_script(tmp_path):
    """v0.1.x used a /etc/local.d/pi-bake-firstboot.start that did
    `apk update && apk add ...` on first boot. That broke whenever
    the Pi had no network at boot (every time, on a fresh deployment)
    or wrong clock (every Pi, no RTC). v0.1.x onward installs from
    the local /apks cache via /etc/apk/world — no firstboot dance."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        names = set(tf.getnames())
    assert "etc/local.d/pi-bake-firstboot.start" not in names
    assert "etc/runlevels/default/local" not in names
