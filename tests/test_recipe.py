"""Recipe schema + YAML round-trip tests.

Covers:
  - load → validate → reject unknown keys, missing required, bad types
  - dump → load is identity (or close enough — the dumped form is
    canonical, so dump(load(dump(x))) == dump(x))
  - The shipped `examples/*.yaml` all parse cleanly
  - CLI flag → Recipe → CLI flag idempotence
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from pi_bake.recipe import (
    NetworkSpec, OutputSpec, Recipe, WifiSpec,
    dump_recipe, load_recipe, recipe_to_node_config,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = REPO_ROOT / "examples"
REFERENCE = REPO_ROOT / "pi-bake.example.yaml"

_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItesting test@host"


def _minimal_yaml() -> str:
    return f"""
hostname: pi-test
board: pi-5
os: alpine
ssh_pubkey: "{_PUBKEY}"
output:
  path: /tmp/pi-test.img.gz
"""


def _write_and_load(tmp_path, body: str) -> Recipe:
    p = tmp_path / "recipe.yaml"
    p.write_text(body)
    return load_recipe(p)


# --------------------------------------------------------------------------- #
# Loading + validation                                                         #
# --------------------------------------------------------------------------- #

def test_minimal_yaml_loads(tmp_path):
    r = _write_and_load(tmp_path, _minimal_yaml())
    assert r.hostname == "pi-test"
    assert r.board == "pi-5"
    assert r.os == "alpine"
    assert r.os_version == ""
    assert r.timezone == "UTC"
    assert r.network.mode == "dhcp"
    assert r.network.send_hostname is True
    assert r.wifi is None
    assert r.packages == []
    assert r.output.path == "/tmp/pi-test.img.gz"


def test_full_yaml_loads(tmp_path):
    body = f"""
hostname: td-pi5-1
board: pi-5
os: alpine
os_version: edge
timezone: America/New_York
ssh_pubkey: "{_PUBKEY}"
extra_pubkeys:
  - "{_PUBKEY}"
network:
  mode: static
  address: 192.168.4.111/24
  gateway: 192.168.4.1
  send_hostname: false
wifi:
  ssid: my-network
  psk: hunter2
  country: GB
packages:
  - avahi
  - dbus
output:
  path: /tmp/td.img.gz
  image_size_mb: 800
"""
    r = _write_and_load(tmp_path, body)
    assert r.os_version == "edge"
    assert r.timezone == "America/New_York"
    assert r.network.mode == "static"
    assert r.network.address == "192.168.4.111/24"
    assert r.network.gateway == "192.168.4.1"
    assert r.network.send_hostname is False
    assert r.wifi.ssid == "my-network"
    assert r.wifi.country == "GB"
    assert r.packages == ["avahi", "dbus"]
    assert r.output.image_size_mb == 800


def test_missing_required_raises(tmp_path):
    # No ssh_pubkey.
    body = """
hostname: x
board: pi-5
os: alpine
output:
  path: /tmp/x.img.gz
"""
    with pytest.raises(ValueError, match="ssh_pubkey"):
        _write_and_load(tmp_path, body)


def test_unknown_top_key_raises(tmp_path):
    body = _minimal_yaml() + "extra_field: surprise\n"
    with pytest.raises(ValueError, match="unknown key"):
        _write_and_load(tmp_path, body)


def test_unknown_network_subkey_raises(tmp_path):
    body = _minimal_yaml() + """
network:
  mode: dhcp
  addres: typo-here
"""
    with pytest.raises(ValueError, match="unknown key"):
        _write_and_load(tmp_path, body)


def test_unknown_wifi_subkey_raises(tmp_path):
    body = _minimal_yaml() + """
wifi:
  ssid: x
  psk: y
  passwrd: typo
"""
    with pytest.raises(ValueError, match="unknown key"):
        _write_and_load(tmp_path, body)


def test_static_without_gateway_raises(tmp_path):
    body = _minimal_yaml() + """
network:
  mode: static
  address: 1.1.1.1/24
"""
    with pytest.raises(ValueError, match="static"):
        _write_and_load(tmp_path, body)


def test_dhcp_with_static_fields_raises(tmp_path):
    body = _minimal_yaml() + """
network:
  mode: dhcp
  address: 1.1.1.1/24
  gateway: 1.1.1.1
"""
    with pytest.raises(ValueError, match="dhcp"):
        _write_and_load(tmp_path, body)


def test_packages_must_be_list_of_strings(tmp_path):
    body = _minimal_yaml() + """
packages:
  - 42
"""
    with pytest.raises(ValueError, match="packages"):
        _write_and_load(tmp_path, body)


def test_empty_yaml_raises(tmp_path):
    with pytest.raises(ValueError, match="empty"):
        _write_and_load(tmp_path, "")


def test_top_level_not_mapping_raises(tmp_path):
    with pytest.raises(ValueError, match="mapping"):
        _write_and_load(tmp_path, "- one\n- two\n")


# --------------------------------------------------------------------------- #
# Dump + round-trip                                                            #
# --------------------------------------------------------------------------- #

def test_dump_is_valid_yaml():
    r = Recipe(
        hostname="pi-test", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    text = dump_recipe(r)
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)
    assert parsed["hostname"] == "pi-test"


def test_round_trip_minimal(tmp_path):
    r1 = _write_and_load(tmp_path, _minimal_yaml())
    text = dump_recipe(r1)
    p = tmp_path / "round.yaml"
    p.write_text(text)
    r2 = load_recipe(p)
    assert r2 == r1


def test_round_trip_with_wifi_and_packages(tmp_path):
    r1 = Recipe(
        hostname="td", board="pi-5", os="alpine", os_version="edge",
        ssh_pubkey=_PUBKEY,
        extra_pubkeys=[_PUBKEY],
        network=NetworkSpec(mode="static", address="10.0.0.5/24", gateway="10.0.0.1"),
        wifi=WifiSpec(ssid="x", psk="y", country="GB"),
        packages=["avahi", "linux-firmware-intel"],
        output=OutputSpec(path="/tmp/td.img.gz", image_size_mb=800),
    )
    text = dump_recipe(r1)
    p = tmp_path / "round.yaml"
    p.write_text(text)
    r2 = load_recipe(p)
    assert r2 == r1


def test_dump_canonical_form_idempotent(tmp_path):
    """dump(load(dump(x))) == dump(x) — once normalized, stays normalized."""
    r = Recipe(
        hostname="td", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    first = dump_recipe(r)
    (tmp_path / "1.yaml").write_text(first)
    second = dump_recipe(load_recipe(tmp_path / "1.yaml"))
    assert first == second


# --------------------------------------------------------------------------- #
# Shipped examples all parse                                                   #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", [
    "pi-zero-2-w-wifi-station.yaml",
    "pi-5-wired-dhcp.yaml",
    "pi-5-be200-edge.yaml",
    "pi-zero-w-armhf.yaml",
])
def test_shipped_example_parses(name):
    p = EXAMPLES / name
    assert p.is_file(), f"missing shipped example: {p}"
    r = load_recipe(p)
    assert r.hostname
    assert r.board
    assert r.os == "alpine"
    # All examples target the same SSH key path so a fresh checkout
    # without that file shouldn't break loading — we explicitly DON'T
    # resolve ssh_pubkey paths in load_recipe (that happens in
    # recipe_to_node_config). Just sanity-check the field is set.
    assert r.ssh_pubkey


def test_reference_example_parses():
    """pi-bake.example.yaml is annotated reference — must parse but
    its content is documentation, not directly usable."""
    # The reference file documents fields by commenting them out;
    # the loader sees only the uncommented baseline. Confirm the
    # uncommented subset is itself a valid recipe.
    r = load_recipe(REFERENCE)
    assert r.hostname
    assert r.os
    assert r.output.path


# --------------------------------------------------------------------------- #
# Recipe → NodeConfig                                                          #
# --------------------------------------------------------------------------- #

def test_recipe_to_node_config_inline_pubkey():
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    node, kwargs = recipe_to_node_config(r)
    assert node.hostname == "t"
    assert node.ssh_pubkey == _PUBKEY
    assert kwargs["board"] == "pi-5"
    assert kwargs["os_name"] == "alpine"
    assert kwargs["version"] is None
    assert kwargs["extra_packages"] == []


def test_recipe_to_node_config_pubkey_from_file(tmp_path):
    pk_file = tmp_path / "key.pub"
    pk_file.write_text(_PUBKEY + "\n")
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=str(pk_file),
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    node, _ = recipe_to_node_config(r)
    assert node.ssh_pubkey == _PUBKEY


def test_recipe_to_node_config_bad_pubkey_errors():
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey="just a random string",
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    with pytest.raises(ValueError, match="OpenSSH key"):
        recipe_to_node_config(r)


def test_recipe_static_network_maps_to_nodeconfig():
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        network=NetworkSpec(
            mode="static", address="1.2.3.4/24", gateway="1.2.3.1",
        ),
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    node, _ = recipe_to_node_config(r)
    assert node.static_ipv4 == "1.2.3.4/24"
    assert node.gateway_ipv4 == "1.2.3.1"


def test_recipe_edge_version_passes_through():
    r = Recipe(
        hostname="t", board="pi-5", os="alpine", os_version="edge",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    _, kwargs = recipe_to_node_config(r)
    assert kwargs["version"] == "edge"


# --------------------------------------------------------------------------- #
# ssh_host_key — operator-managed stable SSH host identity (v0.2)              #
# --------------------------------------------------------------------------- #

def test_ssh_host_key_yaml_loads_and_round_trips(tmp_path):
    """ssh_host_key is a top-level YAML field; load + dump preserve it."""
    pk_file = tmp_path / "key.pub"
    pk_file.write_text(_PUBKEY + "\n")
    host_priv = tmp_path / "host_ed25519"
    host_pub = tmp_path / "host_ed25519.pub"
    host_priv.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n-----END OPENSSH PRIVATE KEY-----\n")
    host_pub.write_bytes(b"ssh-ed25519 AAAATEST operator@td-pi5-1\n")
    body = f"""
hostname: pi-test
board: pi-5
os: alpine
ssh_pubkey: "{_PUBKEY}"
ssh_host_key: {host_priv}
output:
  path: /tmp/pi-test.img.gz
"""
    p = tmp_path / "recipe.yaml"
    p.write_text(body)
    r1 = load_recipe(p)
    assert r1.ssh_host_key == str(host_priv)
    # Round-trip via dump/load.
    p2 = tmp_path / "round.yaml"
    p2.write_text(dump_recipe(r1))
    r2 = load_recipe(p2)
    assert r2 == r1


def test_recipe_to_node_config_ssh_host_key_loads_bytes(tmp_path):
    """recipe_to_node_config reads the private + public key files
    pointed at by ssh_host_key and populates NodeConfig.ssh_host_key_*."""
    host_priv = tmp_path / "host_ed25519"
    host_pub = tmp_path / "host_ed25519.pub"
    priv_bytes = b"-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEPRIV\n-----END OPENSSH PRIVATE KEY-----\n"
    pub_bytes = b"ssh-ed25519 AAAAPUB operator@td-pi5-1\n"
    host_priv.write_bytes(priv_bytes)
    host_pub.write_bytes(pub_bytes)
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        ssh_host_key=str(host_priv),
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    node, _ = recipe_to_node_config(r)
    assert node.ssh_host_key_priv == priv_bytes
    assert node.ssh_host_key_pub == pub_bytes


def test_recipe_to_node_config_ssh_host_key_missing_priv_errors(tmp_path):
    """Missing private key file → clear error before bake starts."""
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        ssh_host_key=str(tmp_path / "does_not_exist"),
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    with pytest.raises(ValueError, match="private key not found"):
        recipe_to_node_config(r)


def test_recipe_to_node_config_ssh_host_key_missing_pub_errors(tmp_path):
    """Private key exists but `.pub` sibling doesn't — distinct error."""
    host_priv = tmp_path / "host_ed25519"
    host_priv.write_bytes(b"fake")
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        ssh_host_key=str(host_priv),
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    with pytest.raises(ValueError, match="public key not found"):
        recipe_to_node_config(r)


# --------------------------------------------------------------------------- #
# apk_fetch — bake-time package fetch for air-gap appliances (v0.2)            #
# --------------------------------------------------------------------------- #

def test_apk_fetch_defaults_false(tmp_path):
    """Omitted apk_fetch defaults to False (v0.0.9 behavior preserved)."""
    r = _write_and_load(tmp_path, _minimal_yaml())
    assert r.apk_fetch is False


def test_apk_fetch_yaml_true_loads(tmp_path):
    body = _minimal_yaml() + "apk_fetch: true\npackages:\n  - avahi\n"
    r = _write_and_load(tmp_path, body)
    assert r.apk_fetch is True
    assert r.packages == ["avahi"]


def test_apk_fetch_round_trips(tmp_path):
    r1 = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        packages=["avahi"], apk_fetch=True,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    p = tmp_path / "round.yaml"
    p.write_text(dump_recipe(r1))
    r2 = load_recipe(p)
    assert r2 == r1
    assert r2.apk_fetch is True


def test_apk_fetch_non_bool_rejected(tmp_path):
    body = _minimal_yaml() + 'apk_fetch: "yes"\n'
    with pytest.raises(ValueError, match="apk_fetch"):
        _write_and_load(tmp_path, body)


def test_apk_fetch_passes_to_build_kwargs():
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY, apk_fetch=True,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    _, kwargs = recipe_to_node_config(r)
    assert kwargs["apk_fetch"] is True
