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
os_version: 3.21.4
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
    assert r.os_version == "3.21.4"
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
        hostname="td", board="pi-5", os="alpine", os_version="3.21.4",
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
    "pi-zero-w-armhf.yaml",
    "pi-5-can-rs485.yaml",
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
    with pytest.raises(ValueError, match="path not found"):
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
# apk_fetch — DEPRECATED no-op since #3 (always-on when packages: is non-empty)#
# --------------------------------------------------------------------------- #

def test_apk_fetch_defaults_false_in_schema(tmp_path):
    """Field defaults False on a recipe with no explicit value.
    Functionally meaningless (#3 made bake-time fetch always-on
    whenever packages: is non-empty), but the field still loads
    so old recipes don't fail-load."""
    r = _write_and_load(tmp_path, _minimal_yaml())
    assert r.apk_fetch is False


def test_apk_fetch_yaml_true_still_loads(tmp_path):
    """Existing recipes with `apk_fetch: true` keep loading;
    field is accepted but no longer affects bake behavior."""
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


def test_apk_fetch_non_bool_still_rejected(tmp_path):
    """Schema validation still catches type errors on the field
    even though the value's a no-op — operator typos shouldn't
    silently parse."""
    body = _minimal_yaml() + 'apk_fetch: "yes"\n'
    with pytest.raises(ValueError, match="apk_fetch"):
        _write_and_load(tmp_path, body)


def test_apk_fetch_NOT_in_build_kwargs():
    """recipe_to_node_config intentionally drops apk_fetch from
    build_kwargs since the bake doesn't honor it anymore. This
    test pins the behavior so future drive-by edits don't
    re-add it."""
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY, apk_fetch=True,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    _, kwargs = recipe_to_node_config(r)
    assert "apk_fetch" not in kwargs


# --------------------------------------------------------------------------- #
# HAT overlays — config_txt + modules YAML fields                              #
# --------------------------------------------------------------------------- #

def test_config_txt_and_modules_default_empty(tmp_path):
    r = _write_and_load(tmp_path, _minimal_yaml())
    assert r.config_txt == []
    assert r.modules == []


def test_config_txt_yaml_loads_in_order(tmp_path):
    body = _minimal_yaml() + """
config_txt:
  - dtparam=spi=on
  - dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25,spimaxfrequency=2000000
  - enable_uart=1
"""
    r = _write_and_load(tmp_path, body)
    assert r.config_txt == [
        "dtparam=spi=on",
        "dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25,spimaxfrequency=2000000",
        "enable_uart=1",
    ]


def test_modules_yaml_loads(tmp_path):
    body = _minimal_yaml() + """
modules:
  - mcp251x
  - can_dev
"""
    r = _write_and_load(tmp_path, body)
    assert r.modules == ["mcp251x", "can_dev"]


def test_config_txt_non_list_rejected(tmp_path):
    body = _minimal_yaml() + 'config_txt: "dtparam=spi=on"\n'
    with pytest.raises(ValueError, match="config_txt"):
        _write_and_load(tmp_path, body)


def test_modules_non_string_entry_rejected(tmp_path):
    body = _minimal_yaml() + "modules:\n  - 42\n"
    with pytest.raises(ValueError, match="modules"):
        _write_and_load(tmp_path, body)


def test_config_txt_and_modules_round_trip(tmp_path):
    r1 = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        config_txt=[
            "dtparam=pciex1",
            "dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25",
        ],
        modules=["mcp251x", "can_dev"],
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    p = tmp_path / "round.yaml"
    p.write_text(dump_recipe(r1))
    r2 = load_recipe(p)
    assert r2 == r1


def test_config_txt_and_modules_passed_to_nodeconfig():
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        config_txt=["dtparam=spi=on"],
        modules=["mcp251x"],
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    node, _ = recipe_to_node_config(r)
    assert node.config_txt == ["dtparam=spi=on"]
    assert node.modules == ["mcp251x"]


# --------------------------------------------------------------------------- #
# os_mode: diskless / ext4 selection + edge gating                             #
# --------------------------------------------------------------------------- #


def test_os_mode_defaults_empty(tmp_path):
    r = _write_and_load(tmp_path, _minimal_yaml())
    # Empty string = back-compat default (alpine diskless).
    assert r.os_mode == ""


def test_os_mode_ext4_loads(tmp_path):
    body = _minimal_yaml() + "os_mode: ext4\n"
    r = _write_and_load(tmp_path, body)
    assert r.os_mode == "ext4"


def test_os_mode_invalid_value_rejected(tmp_path):
    body = _minimal_yaml() + "os_mode: zfs\n"
    with pytest.raises(ValueError, match="os_mode"):
        _write_and_load(tmp_path, body)


def test_os_mode_rejected_for_non_alpine(tmp_path):
    body = f"""
hostname: x
board: pi-5
os: raspbian
os_mode: ext4
ssh_pubkey: "{_PUBKEY}"
output:
  path: /tmp/x.img.gz
"""
    with pytest.raises(ValueError, match="os_mode is only meaningful"):
        _write_and_load(tmp_path, body)


def test_edge_in_diskless_rejected(tmp_path):
    body = _minimal_yaml() + "os_version: edge\n"
    with pytest.raises(ValueError, match="edge is not supported"):
        _write_and_load(tmp_path, body)


def test_edge_in_explicit_diskless_rejected(tmp_path):
    body = _minimal_yaml() + "os_version: edge\nos_mode: diskless\n"
    with pytest.raises(ValueError, match="edge is not supported"):
        _write_and_load(tmp_path, body)


def test_edge_in_ext4_accepted(tmp_path):
    body = _minimal_yaml() + "os_version: edge\nos_mode: ext4\n"
    r = _write_and_load(tmp_path, body)
    assert r.os_version == "edge"
    assert r.os_mode == "ext4"


def test_os_mode_round_trip(tmp_path):
    """ext4 + edge survives a dump/load cycle."""
    r1 = Recipe(
        hostname="t", board="pi-5", os="alpine",
        os_version="edge", os_mode="ext4",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    p = tmp_path / "round.yaml"
    p.write_text(dump_recipe(r1))
    r2 = load_recipe(p)
    assert r2 == r1


def test_os_mode_threaded_to_build_kwargs():
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        os_version="edge", os_mode="ext4",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    _, build_kwargs = recipe_to_node_config(r)
    assert build_kwargs.get("os_mode") == "ext4"


def test_os_mode_diskless_default_not_threaded():
    """Default diskless mode: build_kwargs has no os_mode key
    (back-compat — bake.build() defaults to '' and dispatches
    to alpine.py's diskless backend)."""
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    _, build_kwargs = recipe_to_node_config(r)
    assert "os_mode" not in build_kwargs


# --------------------------------------------------------------------------- #
# os_mode: pxe + pxe.server_url validation                                     #
# --------------------------------------------------------------------------- #


def test_pxe_loads_yaml(tmp_path):
    body = _minimal_yaml() + """
os_mode: pxe
pxe:
  server_url: http://192.168.4.2/td-cm4
"""
    r = _write_and_load(tmp_path, body)
    assert r.os_mode == "pxe"
    assert r.pxe.server_url == "http://192.168.4.2/td-cm4"


def test_pxe_strips_trailing_slash(tmp_path):
    body = _minimal_yaml() + """
os_mode: pxe
pxe:
  server_url: http://192.168.4.2/td-cm4/
"""
    r = _write_and_load(tmp_path, body)
    # Trailing slash stripped by PxeSpec.__post_init__ so cmdline.txt
    # templates concat predictably.
    assert r.pxe.server_url == "http://192.168.4.2/td-cm4"


def test_pxe_invalid_url_scheme_rejected(tmp_path):
    body = _minimal_yaml() + """
os_mode: pxe
pxe:
  server_url: ftp://lab/td-cm4
"""
    with pytest.raises(ValueError, match="http://"):
        _write_and_load(tmp_path, body)


def test_pxe_without_server_url_rejected(tmp_path):
    body = _minimal_yaml() + "os_mode: pxe\n"
    with pytest.raises(ValueError, match="server_url"):
        _write_and_load(tmp_path, body)


def test_pxe_server_url_without_pxe_mode_rejected(tmp_path):
    body = _minimal_yaml() + """
pxe:
  server_url: http://lab/td-cm4
"""
    with pytest.raises(ValueError, match="pxe.* only meaningful"):
        _write_and_load(tmp_path, body)


def test_pxe_unknown_subkey_rejected(tmp_path):
    body = _minimal_yaml() + """
os_mode: pxe
pxe:
  server_url: http://lab/td-cm4
  endpoint: typo-here
"""
    with pytest.raises(ValueError, match="unknown key"):
        _write_and_load(tmp_path, body)


def test_pxe_threaded_to_build_kwargs():
    from pi_bake.recipe import PxeSpec
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        os_mode="pxe",
        pxe=PxeSpec(server_url="http://lab/td-cm4"),
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/td-cm4-tftp"),
    )
    _, build_kwargs = recipe_to_node_config(r)
    assert build_kwargs.get("os_mode") == "pxe"
    assert build_kwargs.get("pxe_server_url") == "http://lab/td-cm4"


def test_pxe_round_trip(tmp_path):
    from pi_bake.recipe import PxeSpec
    r1 = Recipe(
        hostname="td-cm4", board="pi-5", os="alpine",
        os_mode="pxe",
        pxe=PxeSpec(server_url="http://192.168.4.2/td-cm4"),
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/td-cm4-tftp"),
    )
    p = tmp_path / "pxe.yaml"
    p.write_text(dump_recipe(r1))
    r2 = load_recipe(p)
    assert r2 == r1


# --------------------------------------------------------------------------- #
# locale field (v0.5.1+)                                                       #
# --------------------------------------------------------------------------- #

def test_locale_defaults_to_en_gb_utf8():
    """No surprise change — Pi OS Lite's shipped default."""
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    assert r.locale == "en_GB.UTF-8"


def test_locale_loads_from_yaml(tmp_path):
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: alpine
ssh_pubkey: "{_PUBKEY}"
locale: en_US.UTF-8
output:
  path: /tmp/x.img.gz
''')
    r = load_recipe(src)
    assert r.locale == "en_US.UTF-8"


def test_locale_round_trips(tmp_path):
    r1 = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        locale="ja_JP.UTF-8",
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    p = tmp_path / "r.yaml"
    p.write_text(dump_recipe(r1))
    r2 = load_recipe(p)
    assert r2.locale == "ja_JP.UTF-8"


def test_locale_threaded_to_node_config():
    r = Recipe(
        hostname="t", board="pi-5", os="alpine",
        ssh_pubkey=_PUBKEY,
        locale="en_US.UTF-8",
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    node, _ = recipe_to_node_config(r)
    assert node.locale == "en_US.UTF-8"


# --------------------------------------------------------------------------- #
# ssh_pubkey glob support (v0.5.1+)                                            #
# --------------------------------------------------------------------------- #

def test_ssh_pubkey_glob_expands_to_all_matches(tmp_path):
    """`~/.ssh/*.pub` style globs scoop up every key the operator
    has on the bake host without naming them individually. Useful
    for the common case where the operator wants their personal
    set of keys (laptop + yubikey + etc.) trusted on every bake."""
    (tmp_path / "a.pub").write_text("ssh-ed25519 AAAA-FIRST op-a\n")
    (tmp_path / "b.pub").write_text("ssh-ed25519 AAAA-SECOND op-b\n")
    from pi_bake.recipe import _resolve_pubkey
    out = _resolve_pubkey(str(tmp_path / "*.pub"))
    assert "AAAA-FIRST" in out
    assert "AAAA-SECOND" in out
    # Multi-line, ready to drop into authorized_keys verbatim.
    assert out.count("\n") == 1  # 2 keys -> 1 newline between them


def test_ssh_pubkey_glob_matches_none_raises(tmp_path):
    from pi_bake.recipe import _resolve_pubkey
    with pytest.raises(ValueError, match="matched no files"):
        _resolve_pubkey(str(tmp_path / "*.pub"))


def test_ssh_pubkey_glob_results_are_sorted(tmp_path):
    """Sorted output -> deterministic across bake hosts. An
    operator running the same recipe on two laptops should get
    byte-identical authorized_keys."""
    (tmp_path / "z.pub").write_text("ssh-ed25519 AAAA-Z opZ\n")
    (tmp_path / "a.pub").write_text("ssh-ed25519 AAAA-A opA\n")
    from pi_bake.recipe import _resolve_pubkey
    out = _resolve_pubkey(str(tmp_path / "*.pub"))
    # a.pub comes before z.pub alphabetically -> AAAA-A first
    assert out.index("AAAA-A") < out.index("AAAA-Z")


def test_ssh_pubkey_path_without_glob_chars_unchanged(tmp_path):
    """Glob handling is keyed off `*`, `?`, `[`. A plain path
    keeps the v0.0+ behavior — read the single file."""
    p = tmp_path / "id_ed25519.pub"
    p.write_text("ssh-ed25519 AAAA-SINGLE op@host\n")
    from pi_bake.recipe import _resolve_pubkey
    out = _resolve_pubkey(str(p))
    assert out == "ssh-ed25519 AAAA-SINGLE op@host"
