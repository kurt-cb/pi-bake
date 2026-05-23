"""NodeConfig validation + rendered-file contents."""
from __future__ import annotations

import pytest

from pi_bake.config import NodeConfig

_GOOD_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINQy operator@workstation"


def test_minimal_node_ok():
    n = NodeConfig(hostname="pi-radio-1", ssh_pubkey=_GOOD_PUBKEY)
    assert n.has_wifi is False
    assert n.wpa_supplicant_conf() == ""
    assert _GOOD_PUBKEY in n.authorized_keys_text()
    assert n.authorized_keys_text().endswith("\n")


def test_wifi_complete_ok():
    n = NodeConfig(
        hostname="pi-radio-1",
        ssh_pubkey=_GOOD_PUBKEY,
        wifi_ssid="totaldns-lab",
        wifi_psk="secret",
    )
    conf = n.wpa_supplicant_conf()
    assert n.has_wifi
    assert 'ssid="totaldns-lab"' in conf
    assert 'psk="secret"' in conf
    assert "country=US" in conf
    assert "key_mgmt=WPA-PSK" in conf


def test_wifi_partial_rejected():
    """SSID without PSK (and vice versa) is a config error."""
    with pytest.raises(ValueError, match="wifi_ssid.*wifi_psk"):
        NodeConfig(
            hostname="pi", ssh_pubkey=_GOOD_PUBKEY,
            wifi_ssid="x", wifi_psk="",
        )
    with pytest.raises(ValueError, match="wifi_ssid.*wifi_psk"):
        NodeConfig(
            hostname="pi", ssh_pubkey=_GOOD_PUBKEY,
            wifi_ssid="", wifi_psk="y",
        )


def test_invalid_hostname_rejected():
    for bad in ("Pi_With_Underscores", "-leading-hyphen", "trailing-",
                "way-too-long-" * 10, "has spaces"):
        with pytest.raises(ValueError, match="hostname"):
            NodeConfig(hostname=bad, ssh_pubkey=_GOOD_PUBKEY)


def test_pubkey_format_check():
    with pytest.raises(ValueError, match="ssh_pubkey"):
        NodeConfig(hostname="pi", ssh_pubkey="not a key")
    # Various valid prefixes.
    for prefix in ("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256"):
        NodeConfig(hostname="pi", ssh_pubkey=f"{prefix} AAA test")


def test_extra_pubkeys_appended():
    second = "ssh-rsa AAAB second-key"
    n = NodeConfig(
        hostname="pi", ssh_pubkey=_GOOD_PUBKEY,
        extra_pubkeys=[second],
    )
    text = n.authorized_keys_text()
    assert _GOOD_PUBKEY in text
    assert second in text
    # Newline-separated, one per line.
    assert text.count("\n") == 2 + 0   # two keys + final newline


def test_extra_pubkeys_dedup():
    n = NodeConfig(
        hostname="pi", ssh_pubkey=_GOOD_PUBKEY,
        extra_pubkeys=[_GOOD_PUBKEY, _GOOD_PUBKEY],
    )
    # Should appear exactly once.
    assert n.authorized_keys_text().count(_GOOD_PUBKEY) == 1
