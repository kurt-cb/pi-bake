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
    """raspbian only supports `pxe` (v0.6.5+); other modes like ext4
    are alpine-only and surface a per-os allow-list error."""
    body = f"""
hostname: x
board: pi-5
os: raspbian
os_mode: ext4
ssh_pubkey: "{_PUBKEY}"
output:
  path: /tmp/x.img.gz
"""
    with pytest.raises(ValueError, match="not valid for os='raspbian'"):
        _write_and_load(tmp_path, body)


def test_os_mode_rejected_for_debian_fedora(tmp_path):
    """debian/fedora have no os_mode support yet — set any value and
    the catch-all `only meaningful for os: alpine` error surfaces."""
    for os_name in ("debian", "fedora"):
        body = f"""
hostname: x
board: pi-5
os: {os_name}
os_mode: pxe
ssh_pubkey: "{_PUBKEY}"
output:
  path: /tmp/x.img.gz
"""
        with pytest.raises(ValueError, match="only meaningful"):
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
# Raspbian PXE (NFS-root) — pxe.nfs_server etc. (v0.6.5+)                     #
# --------------------------------------------------------------------------- #


def _minimal_raspbian_pxe_yaml() -> str:
    return f"""
hostname: cm4-pxe
board: pi-cm4
os: raspbian
os_mode: pxe
ssh_pubkey: "{_PUBKEY}"
pxe:
  nfs_server: 192.168.4.2:8801:/srv/nfs/pi-bake/cm4-pxe
output:
  path: /tmp/cm4-pxe-bake
"""


def test_raspbian_pxe_loads_yaml(tmp_path):
    r = _write_and_load(tmp_path, _minimal_raspbian_pxe_yaml())
    assert r.os == "raspbian"
    assert r.os_mode == "pxe"
    assert r.pxe.nfs_server == "192.168.4.2:8801:/srv/nfs/pi-bake/cm4-pxe"


def test_raspbian_pxe_requires_nfs_server(tmp_path):
    """os: raspbian + os_mode: pxe with no pxe.nfs_server is a misconfig."""
    body = f"""
hostname: cm4-pxe
board: pi-cm4
os: raspbian
os_mode: pxe
ssh_pubkey: "{_PUBKEY}"
output:
  path: /tmp/cm4-pxe-bake
"""
    with pytest.raises(ValueError, match="nfs_server"):
        _write_and_load(tmp_path, body)


def test_raspbian_pxe_nfs_server_format_validated(tmp_path):
    """`nfs_server` must look like `<host>[:<port>]:<path>`."""
    body = f"""
hostname: cm4-pxe
board: pi-cm4
os: raspbian
os_mode: pxe
ssh_pubkey: "{_PUBKEY}"
pxe:
  nfs_server: nope-no-path
output:
  path: /tmp/cm4-pxe-bake
"""
    with pytest.raises(ValueError, match="<host>"):
        _write_and_load(tmp_path, body)


def test_raspbian_pxe_accepts_optional_mount_options_and_push(tmp_path):
    body = f"""
hostname: cm4-pxe
board: pi-cm4
os: raspbian
os_mode: pxe
ssh_pubkey: "{_PUBKEY}"
pxe:
  nfs_server: 192.168.4.2:8801:/srv/nfs/pi-bake/cm4-pxe
  nfs_mount_options: vers=3,proto=tcp,mountport=8803,nolock
  nfs_push: incus:nfs-pi-bake
output:
  path: /tmp/cm4-pxe-bake
"""
    r = _write_and_load(tmp_path, body)
    assert r.pxe.nfs_mount_options.startswith("vers=3")
    assert r.pxe.nfs_push == "incus:nfs-pi-bake"


def test_raspbian_pxe_threaded_to_build_kwargs():
    from pi_bake.recipe import PxeSpec
    r = Recipe(
        hostname="cm4-pxe", board="pi-cm4", os="raspbian",
        os_mode="pxe",
        pxe=PxeSpec(
            nfs_server="192.168.4.2:8801:/srv/nfs/pi-bake/cm4-pxe",
            nfs_mount_options="vers=3,nolock",
            nfs_push="incus:nfs-pi-bake",
        ),
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/cm4-pxe-bake"),
    )
    _, build_kwargs = recipe_to_node_config(r)
    assert build_kwargs.get("os_mode") == "pxe"
    assert build_kwargs.get("pxe_nfs_server").startswith("192.168.4.2:8801")
    assert build_kwargs.get("pxe_nfs_mount_options") == "vers=3,nolock"
    assert build_kwargs.get("pxe_nfs_push") == "incus:nfs-pi-bake"


def test_raspbian_pxe_round_trip(tmp_path):
    from pi_bake.recipe import PxeSpec
    r1 = Recipe(
        hostname="cm4-pxe", board="pi-cm4", os="raspbian",
        os_mode="pxe",
        pxe=PxeSpec(
            nfs_server="192.168.4.2:8801:/srv/nfs/pi-bake/cm4-pxe",
            nfs_mount_options="vers=3,proto=tcp,mountport=8803,nolock",
            nfs_push="incus:nfs-pi-bake",
        ),
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/cm4-pxe-bake"),
    )
    p = tmp_path / "raspbian-pxe.yaml"
    p.write_text(dump_recipe(r1))
    r2 = load_recipe(p)
    assert r2 == r1


def test_raspbian_pxe_rejects_nfs_fields_without_pxe_mode(tmp_path):
    body = f"""
hostname: cm4-sd
board: pi-cm4
os: raspbian
ssh_pubkey: "{_PUBKEY}"
pxe:
  nfs_server: 192.168.4.2:8801:/srv/nfs/cm4
output:
  path: /tmp/cm4-sd.img.gz
"""
    with pytest.raises(ValueError, match="only meaningful"):
        _write_and_load(tmp_path, body)


def test_raspbian_with_sd_mode_unaffected():
    """Default os_mode (SD) for raspbian still works — no regression."""
    r = Recipe(
        hostname="t", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.gz"),
    )
    assert r.os_mode == ""
    assert r.pxe.nfs_server == ""


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


# --------------------------------------------------------------------------- #
# user: block (v0.6.0+)                                                        #
# --------------------------------------------------------------------------- #

def test_user_block_loads_from_yaml(tmp_path):
    """`user:` block with name only — groups + shell default."""
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
user:
  name: kurt
output:
  path: /tmp/x.img.xz
''')
    r = load_recipe(src)
    assert r.user is not None
    assert r.user.name == "kurt"
    assert "sudo" in r.user.groups   # default
    assert r.user.shell == "/bin/bash"


def test_user_block_explicit_groups_and_shell(tmp_path):
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
user:
  name: kurt
  groups:
    - sudo
    - docker
  shell: /usr/bin/zsh
output:
  path: /tmp/x.img.xz
''')
    r = load_recipe(src)
    assert r.user.groups == ["sudo", "docker"]
    assert r.user.shell == "/usr/bin/zsh"


def test_user_block_missing_name_raises(tmp_path):
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
user:
  groups: [sudo]
output:
  path: /tmp/x.img.xz
''')
    with pytest.raises(ValueError, match="user.name is required"):
        load_recipe(src)


def test_user_block_invalid_username_raises(tmp_path):
    """Username must be a valid Unix login name — alphanum + _- only,
    must start with a letter or underscore. Otherwise useradd fails
    at first-boot and we want it to fail at bake time instead."""
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
user:
  name: "1cantstartwithdigit"
output:
  path: /tmp/x.img.xz
''')
    with pytest.raises(ValueError, match="valid Unix username"):
        load_recipe(src)


def test_user_block_shell_injection_rejected(tmp_path):
    """Shell path is interpolated into bash; reject metachars."""
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
user:
  name: kurt
  shell: "/bin/bash; rm -rf /"
output:
  path: /tmp/x.img.xz
''')
    with pytest.raises(ValueError, match="shell-unsafe"):
        load_recipe(src)


def test_user_block_unknown_subkey_rejected(tmp_path):
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
user:
  name: kurt
  homedir: /opt/kurt
output:
  path: /tmp/x.img.xz
''')
    with pytest.raises(ValueError, match="unknown key"):
        load_recipe(src)


def test_user_block_threaded_to_node_config():
    from pi_bake.recipe import UserSpec
    r = Recipe(
        hostname="t", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        user=UserSpec(name="kurt", groups=["sudo", "docker"]),
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    node, _ = recipe_to_node_config(r)
    assert len(node.users) == 1
    assert node.users[0].name == "kurt"
    assert node.users[0].groups == ["sudo", "docker"]
    assert node.users[0].shell == "/bin/bash"
    # Single user with no per-user key override inherits the
    # top-level ssh_pubkey.
    assert _PUBKEY in node.users[0].authorized_keys


def test_no_user_block_falls_back_to_pi_defaults():
    """Back-compat: recipes without `user:` keep current behavior
    (raspbian creates the `pi` user). NodeConfig.users empty =
    'use backend default'."""
    r = Recipe(
        hostname="t", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    node, _ = recipe_to_node_config(r)
    assert node.users == []


def test_user_block_round_trips(tmp_path):
    from pi_bake.recipe import UserSpec
    r1 = Recipe(
        hostname="td", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        user=UserSpec(name="kurt", groups=["sudo", "docker"]),
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    text = dump_recipe(r1)
    p = tmp_path / "round.yaml"
    p.write_text(text)
    r2 = load_recipe(p)
    assert r2.user is not None
    assert r2.user.name == r1.user.name
    assert r2.user.groups == r1.user.groups


# --------------------------------------------------------------------------- #
# dtparam: block (v0.6.0+)                                                     #
# --------------------------------------------------------------------------- #

def test_dtparam_loads_from_yaml(tmp_path):
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
dtparam:
  spi: on
  i2c_arm: on
output:
  path: /tmp/x.img.xz
''')
    r = load_recipe(src)
    # YAML's `on` / `off` are auto-coerced to bool; pi-bake
    # stringifies them to the config.txt convention "on" / "off".
    assert r.dtparam == {"spi": "on", "i2c_arm": "on"}


def test_dtparam_off_normalizes_to_off_string(tmp_path):
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
dtparam:
  audio: off
output:
  path: /tmp/x.img.xz
''')
    r = load_recipe(src)
    assert r.dtparam == {"audio": "off"}


def test_dtparam_translates_to_config_txt(tmp_path):
    """The whole point: each dtparam entry becomes a config.txt
    `dtparam=<key>=<value>` line in NodeConfig.config_txt,
    prepended ahead of the operator's explicit lines."""
    r = Recipe(
        hostname="t", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        dtparam={"spi": "on", "i2c_arm": "on"},
        config_txt=["dtoverlay=mcp2515-can0,oscillator=16000000"],
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    node, _ = recipe_to_node_config(r)
    assert node.config_txt[0] == "dtparam=spi=on"
    assert node.config_txt[1] == "dtparam=i2c_arm=on"
    # Operator's explicit lines come AFTER the shortcuts, so they
    # can override.
    assert node.config_txt[2] == "dtoverlay=mcp2515-can0,oscillator=16000000"


def test_dtparam_invalid_key_rejected(tmp_path):
    """dtparam keys are alphanum + underscore. Anything with
    special chars (=, spaces, etc.) is a typo we'd rather catch
    at bake time than have config.txt silently malformed."""
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
dtparam:
  "spi=on": true
output:
  path: /tmp/x.img.xz
''')
    with pytest.raises(ValueError, match="dtparam key"):
        load_recipe(src)


def test_dtparam_round_trips(tmp_path):
    r1 = Recipe(
        hostname="td", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        dtparam={"spi": "on", "i2c_arm": "on"},
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    text = dump_recipe(r1)
    p = tmp_path / "round.yaml"
    p.write_text(text)
    r2 = load_recipe(p)
    assert r2.dtparam == r1.dtparam


def test_dtparam_empty_default():
    r = Recipe(
        hostname="t", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    assert r.dtparam == {}


# --------------------------------------------------------------------------- #
# users: plural (v0.6.1+)                                                      #
# --------------------------------------------------------------------------- #

def test_users_plural_loads_from_yaml(tmp_path):
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
users:
  - name: alice
    groups: [sudo]
  - name: bob
    groups: [users]
output:
  path: /tmp/x.img.xz
''')
    r = load_recipe(src)
    assert len(r.users) == 2
    assert r.users[0].name == "alice"
    assert r.users[1].name == "bob"


def test_users_plural_per_user_keys(tmp_path):
    """Per-user ssh_pubkey overrides the top-level one for that
    user's authorized_keys."""
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "ssh-ed25519 AAAA-TOP top@host"
users:
  - name: alice
    ssh_pubkey: "ssh-ed25519 AAAA-ALICE alice@laptop"
  - name: bob
    # no ssh_pubkey -> inherits top-level
output:
  path: /tmp/x.img.xz
''')
    r = load_recipe(src)
    node, _ = recipe_to_node_config(r)
    # alice has her own key; bob inherits the top-level key.
    alice = next(u for u in node.users if u.name == "alice")
    bob = next(u for u in node.users if u.name == "bob")
    assert "AAAA-ALICE" in alice.authorized_keys
    assert "AAAA-TOP" not in alice.authorized_keys
    assert "AAAA-TOP" in bob.authorized_keys
    assert "AAAA-ALICE" not in bob.authorized_keys


def test_users_plural_extra_pubkeys_compose_on_top_when_no_pubkey():
    """When a user block has extra_pubkeys but no ssh_pubkey, the
    extras compose ON TOP of the top-level set — additive, not
    replacing."""
    from pi_bake.recipe import UserSpec
    r = Recipe(
        hostname="t", board="pi-5", os="raspbian",
        ssh_pubkey="ssh-ed25519 AAAA-TOP top@host",
        extra_pubkeys=["ssh-ed25519 AAAA-TOP-EXTRA extra@host"],
        users=[UserSpec(
            name="alice",
            extra_pubkeys=["ssh-ed25519 AAAA-ALICE-EXTRA alice@extra"],
        )],
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    node, _ = recipe_to_node_config(r)
    keys = node.users[0].authorized_keys
    assert "AAAA-TOP" in keys
    assert "AAAA-TOP-EXTRA" in keys
    assert "AAAA-ALICE-EXTRA" in keys


def test_user_and_users_both_set_rejected(tmp_path):
    """Set exactly one — `user:` (singular) OR `users:` (plural),
    not both. The intent is unclear otherwise."""
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
user:
  name: kurt
users:
  - name: alice
output:
  path: /tmp/x.img.xz
''')
    with pytest.raises(ValueError, match="EITHER `user:` .* OR `users:`"):
        load_recipe(src)


def test_users_plural_empty_list_rejected(tmp_path):
    """An empty `users:` list is operator confusion — they probably
    meant to omit the block. Refuse loudly."""
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
users: []
output:
  path: /tmp/x.img.xz
''')
    with pytest.raises(ValueError, match="at least one entry"):
        load_recipe(src)


def test_users_plural_duplicate_name_rejected(tmp_path):
    """Two users with the same name would have firstrun.sh trying
    to useradd a second time + clobbering the first user's setup.
    Refuse at bake time."""
    src = tmp_path / "r.yaml"
    src.write_text(f'''
hostname: foo
board: pi-5
os: raspbian
ssh_pubkey: "{_PUBKEY}"
users:
  - name: alice
  - name: alice
output:
  path: /tmp/x.img.xz
''')
    with pytest.raises(ValueError, match="duplicate name"):
        load_recipe(src)


def test_users_plural_round_trips_as_plural(tmp_path):
    """A two-user recipe dumps as `users:` (plural)."""
    from pi_bake.recipe import UserSpec
    r1 = Recipe(
        hostname="td", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        users=[
            UserSpec(name="alice", groups=["sudo"]),
            UserSpec(name="bob", groups=["users"]),
        ],
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    text = dump_recipe(r1)
    assert "users:" in text
    assert "- name: alice" in text
    assert "- name: bob" in text
    p = tmp_path / "round.yaml"
    p.write_text(text)
    r2 = load_recipe(p)
    assert len(r2.users) == 2
    assert {u.name for u in r2.users} == {"alice", "bob"}


def test_single_user_dumps_as_singular(tmp_path):
    """One user, no per-user keys -> emit `user:` (singular) for
    less round-trip clutter."""
    from pi_bake.recipe import UserSpec
    r1 = Recipe(
        hostname="td", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        user=UserSpec(name="kurt"),
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    text = dump_recipe(r1)
    assert "\nuser:" in text
    assert "\nusers:" not in text


def test_single_user_with_own_key_dumps_as_plural(tmp_path):
    """One user with a per-user ssh_pubkey -> must use plural
    form since `user:` (singular) schema has no ssh_pubkey field
    in its dumped form."""
    from pi_bake.recipe import UserSpec
    r1 = Recipe(
        hostname="td", board="pi-5", os="raspbian",
        ssh_pubkey=_PUBKEY,
        users=[UserSpec(
            name="kurt",
            ssh_pubkey="~/.ssh/kurt-laptop.pub",
        )],
        output=OutputSpec(path="/tmp/x.img.xz"),
    )
    text = dump_recipe(r1)
    assert "\nusers:" in text
    assert "ssh_pubkey: ~/.ssh/kurt-laptop.pub" in text
