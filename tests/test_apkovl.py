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
        "etc/local.d/pi-bake-firstboot.start",
        "etc/runlevels/default/sshd",
        "etc/runlevels/default/local",
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


def test_no_wifi_overlay_omits_wpa(tmp_path):
    n = NodeConfig(hostname="pi-wired", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        names = set(tf.getnames())
    assert "etc/wpa_supplicant/wpa_supplicant.conf" not in names
    assert "etc/runlevels/default/wpa_supplicant" not in names


def test_hostname_content(tmp_path):
    n = NodeConfig(hostname="boat-pi-1", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        data = tf.extractfile("etc/hostname").read().decode()
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


def test_firstboot_script_self_disables(tmp_path):
    """Without self-disable, the first-boot script would re-run
    every reboot — slow + noisy."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        script = tf.extractfile("etc/local.d/pi-bake-firstboot.start").read().decode()
    assert "mv /etc/local.d/pi-bake-firstboot.start" in script


def test_wifi_firstboot_installs_wpa_supplicant(tmp_path):
    n = NodeConfig(
        hostname="pi", ssh_pubkey=_PUBKEY,
        wifi_ssid="x", wifi_psk="y",
    )
    with _bake(n, tmp_path) as tf:
        script = tf.extractfile("etc/local.d/pi-bake-firstboot.start").read().decode()
    assert "wpa_supplicant" in script
    assert "wireless-regdb" in script


def test_wired_firstboot_skips_wifi_pkgs(tmp_path):
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        script = tf.extractfile("etc/local.d/pi-bake-firstboot.start").read().decode()
    assert "wpa_supplicant" not in script
    assert "openssh-server" in script


def test_firstboot_installs_avahi_for_mdns(tmp_path):
    """Headless Pis need `<hostname>.local` resolution or finding
    them on a LAN is a pain — avahi-daemon serves that role."""
    n = NodeConfig(hostname="pi", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        script = tf.extractfile("etc/local.d/pi-bake-firstboot.start").read().decode()
    assert "avahi" in script
    assert "rc-service avahi-daemon start" in script


def test_interfaces_carries_dhcp_hostname(tmp_path):
    """/etc/network/interfaces should pass the hostname to udhcpc
    via DHCP option 12 — routers + totaldns name-locking key off it."""
    n = NodeConfig(hostname="pi-radio-1", ssh_pubkey=_PUBKEY)
    with _bake(n, tmp_path) as tf:
        ifaces = tf.extractfile("etc/network/interfaces").read().decode()
    assert "hostname pi-radio-1" in ifaces
