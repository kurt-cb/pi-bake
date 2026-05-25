"""YAML recipe — declarative bake input for pi-bake.

A `Recipe` is the complete description of one Pi bake: board,
OS, hostname, SSH keys, network, optional WiFi, optional extra
packages. It maps 1:1 onto what the CLI's `build` subcommand
expects, plus an `extra_packages` list that has no CLI equivalent
(operator declares it in YAML or the Python API).

Two operator workflows:

  1. Operator writes a YAML by hand (or starts from
     `pi-bake.example.yaml` / one of the `examples/`), then
     `pi-bake build --config <yaml>`.

  2. Operator runs `pi-bake build <flags...> --to-yaml <path>`
     to round-trip a known-good CLI invocation into a YAML
     file they can version-control + edit + re-bake from.

Why YAML over JSON / TOML:
  - List literals + multi-line strings (SSH keys, package lists)
    are friendlier than JSON's quoting; less line-noise than TOML.
  - Inline comments survive the operator's own editing pass even
    if our round-trip drops them — PyYAML doesn't preserve
    comments on load. We provide the annotated reference
    (`pi-bake.example.yaml`) so operators have a canonical
    template to crib from; that file is read-only documentation,
    not something we'd ever round-trip.

Strict load: unknown top-level keys + unknown sub-keys raise.
A typo like `network: {addres: ...}` should fail loudly, not
silently bake with the field ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# PyYAML is a runtime dep declared in pyproject.toml. Import-time
# helpful error if the user is on a stripped install missing it.
try:
    import yaml
except ImportError as e:   # pragma: no cover
    raise ImportError(
        "PyYAML is required for pi-bake recipe support. "
        "Install with: pip install py-pi-bake (PyYAML is a declared dep) "
        "or: pip install pyyaml"
    ) from e


_TOP_KEYS = frozenset({
    "hostname", "board", "os", "os_version", "timezone",
    "ssh_pubkey", "extra_pubkeys",
    "network", "wifi", "packages", "output",
})
_NETWORK_KEYS = frozenset({"mode", "address", "gateway", "send_hostname"})
_WIFI_KEYS    = frozenset({"ssid", "psk", "country"})
_OUTPUT_KEYS  = frozenset({"path", "image_size_mb"})


# --------------------------------------------------------------------------- #
# Schema                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class NetworkSpec:
    """eth0 network config.

    - `mode="dhcp"`: standard DHCP; `address` + `gateway` must be empty.
    - `mode="static"`: `address` (CIDR like "192.168.4.111/24") + `gateway`
      (e.g. "192.168.4.1") both required.
    - `send_hostname`: whether dhcpcd announces the system hostname via
      DHCP option 12 on its DISCOVER/REQUEST. Default True (friendly).
    """
    mode: str = "dhcp"
    address: str = ""
    gateway: str = ""
    send_hostname: bool = True

    def __post_init__(self) -> None:
        if self.mode not in ("dhcp", "static"):
            raise ValueError(
                f"network.mode must be 'dhcp' or 'static', got {self.mode!r}"
            )
        if self.mode == "static":
            if not self.address or not self.gateway:
                raise ValueError(
                    "network.mode=static requires both network.address "
                    "(CIDR) and network.gateway"
                )
            if "/" not in self.address:
                raise ValueError(
                    f"network.address must be CIDR form "
                    f"(e.g. 192.168.4.111/24); got {self.address!r}"
                )
        else:
            if self.address or self.gateway:
                raise ValueError(
                    "network.mode=dhcp leaves network.address + "
                    "network.gateway empty"
                )


@dataclass
class WifiSpec:
    """Bootstrap WiFi creds (wpa_supplicant.conf written into the
    image). Omit the whole `wifi:` block for wired-only nodes."""
    ssid: str
    psk: str
    country: str = "US"

    def __post_init__(self) -> None:
        if not self.ssid or not self.psk:
            raise ValueError("wifi.ssid + wifi.psk must both be non-empty")


@dataclass
class OutputSpec:
    """Where the baked image lands."""
    path: str
    image_size_mb: int = 0   # 0 → backend default

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("output.path is required")


@dataclass
class Recipe:
    """A complete bake recipe — everything one Pi needs.

    Required: hostname, board, os, ssh_pubkey, output.path.
    Everything else has a default.

    `os_version`: explicit OS version (e.g. "3.21.4", "edge",
    "bookworm"). Empty/None → latest known-good for `os`.

    `extra_pubkeys`: list of paths OR inline pubkey strings;
    paths are detected by '~/' prefix or being a real existing
    file. Mixed list allowed.

    `packages`: extra apk package names appended to
    `/etc/apk/world` (Alpine only). Today these require network
    on first boot when not in the stock /apks cache — bake-time
    cache enrichment is a v0.3 ROADMAP item.
    """
    hostname: str
    board: str
    os: str
    ssh_pubkey: str
    output: OutputSpec
    os_version: str = ""
    timezone: str = "UTC"
    extra_pubkeys: list[str] = field(default_factory=list)
    network: NetworkSpec = field(default_factory=NetworkSpec)
    wifi: WifiSpec | None = None
    packages: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# YAML <-> Recipe                                                              #
# --------------------------------------------------------------------------- #


def load_recipe(path: str | Path) -> Recipe:
    """Read a YAML file → Recipe. Strict: unknown keys raise."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise FileNotFoundError(f"recipe YAML not found: {p}")
    with open(p, "r") as f:
        try:
            data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"YAML parse error in {p}: {e}") from e
    if data is None:
        raise ValueError(f"recipe YAML is empty: {p}")
    if not isinstance(data, dict):
        raise ValueError(
            f"recipe YAML top-level must be a mapping, got {type(data).__name__}"
        )
    return _from_dict(data, source=str(p))


def _from_dict(d: dict, *, source: str = "<dict>") -> Recipe:
    """Build a Recipe from a parsed YAML mapping. Validates strictly."""
    _check_keys(d, _TOP_KEYS, f"top level of {source}")

    # Required fields — raise with the YAML field name, not the dataclass
    # field name, so error messages match what the operator sees.
    for required in ("hostname", "board", "os", "ssh_pubkey"):
        if required not in d:
            raise ValueError(f"{source}: required field {required!r} missing")

    nw_raw = d.get("network") or {}
    if not isinstance(nw_raw, dict):
        raise ValueError(
            f"{source}: `network` must be a mapping, got {type(nw_raw).__name__}"
        )
    _check_keys(nw_raw, _NETWORK_KEYS, f"{source}: network")
    network = NetworkSpec(**nw_raw)

    wifi: WifiSpec | None = None
    if "wifi" in d:
        wi_raw = d["wifi"] or {}
        if not isinstance(wi_raw, dict):
            raise ValueError(
                f"{source}: `wifi` must be a mapping, got {type(wi_raw).__name__}"
            )
        _check_keys(wi_raw, _WIFI_KEYS, f"{source}: wifi")
        wifi = WifiSpec(**wi_raw)

    out_raw = d.get("output") or {}
    if not isinstance(out_raw, dict):
        raise ValueError(
            f"{source}: `output` must be a mapping, got {type(out_raw).__name__}"
        )
    _check_keys(out_raw, _OUTPUT_KEYS, f"{source}: output")
    output = OutputSpec(**out_raw)

    extra_pubkeys = d.get("extra_pubkeys") or []
    if not isinstance(extra_pubkeys, list):
        raise ValueError(
            f"{source}: `extra_pubkeys` must be a list, got {type(extra_pubkeys).__name__}"
        )
    packages = d.get("packages") or []
    if not isinstance(packages, list):
        raise ValueError(
            f"{source}: `packages` must be a list, got {type(packages).__name__}"
        )
    if not all(isinstance(p, str) for p in packages):
        raise ValueError(f"{source}: every entry in `packages` must be a string")

    return Recipe(
        hostname=d["hostname"],
        board=d["board"],
        os=d["os"],
        ssh_pubkey=d["ssh_pubkey"],
        output=output,
        os_version=d.get("os_version") or "",
        timezone=d.get("timezone") or "UTC",
        extra_pubkeys=[str(k) for k in extra_pubkeys],
        network=network,
        wifi=wifi,
        packages=list(packages),
    )


def _check_keys(d: dict, allowed: frozenset[str], where: str) -> None:
    unknown = set(d.keys()) - allowed
    if unknown:
        raise ValueError(
            f"{where}: unknown key(s) {sorted(unknown)!r}; allowed: {sorted(allowed)!r}"
        )


def dump_recipe(r: Recipe) -> str:
    """Render a Recipe as annotated YAML — short comments per
    field so the output is self-documenting without the
    operator opening the reference. Stable field order.
    """
    lines: list[str] = []
    lines.append("# pi-bake recipe — generated by `pi-bake build ... --to-yaml`")
    lines.append("# Edit + re-bake with: pi-bake build --config <this-file>")
    lines.append("# Full annotated reference: pi-bake.example.yaml")
    lines.append("")
    lines.append(f"hostname: {_yaml_str(r.hostname)}")
    lines.append(f"board: {_yaml_str(r.board)}")
    lines.append(f"os: {_yaml_str(r.os)}")
    if r.os_version:
        lines.append(f"os_version: {_yaml_str(r.os_version)}")
    if r.timezone and r.timezone != "UTC":
        lines.append(f"timezone: {_yaml_str(r.timezone)}")
    lines.append("")
    lines.append("# Primary OpenSSH pubkey (path on the bake host OR inline string)")
    lines.append(f"ssh_pubkey: {_yaml_str(r.ssh_pubkey)}")
    if r.extra_pubkeys:
        lines.append("extra_pubkeys:")
        for k in r.extra_pubkeys:
            lines.append(f"  - {_yaml_str(k)}")
    lines.append("")
    lines.append("# eth0 — mode dhcp or static")
    lines.append("network:")
    lines.append(f"  mode: {_yaml_str(r.network.mode)}")
    if r.network.mode == "static":
        lines.append(f"  address: {_yaml_str(r.network.address)}")
        lines.append(f"  gateway: {_yaml_str(r.network.gateway)}")
    lines.append(f"  send_hostname: {str(r.network.send_hostname).lower()}")
    if r.wifi is not None:
        lines.append("")
        lines.append("# Bootstrap WiFi (wpa_supplicant.conf baked in)")
        lines.append("wifi:")
        lines.append(f"  ssid: {_yaml_str(r.wifi.ssid)}")
        lines.append(f"  psk: {_yaml_str(r.wifi.psk)}")
        lines.append(f"  country: {_yaml_str(r.wifi.country)}")
    if r.packages:
        lines.append("")
        lines.append("# Extra apk packages (in addition to the stock RPi cache)")
        lines.append("packages:")
        for p in r.packages:
            lines.append(f"  - {_yaml_str(p)}")
    lines.append("")
    lines.append("# Where the .img.gz lands")
    lines.append("output:")
    lines.append(f"  path: {_yaml_str(r.output.path)}")
    if r.output.image_size_mb:
        lines.append(f"  image_size_mb: {r.output.image_size_mb}")
    return "\n".join(lines) + "\n"


def _yaml_str(s: str) -> str:
    """Quote a YAML scalar only when it needs it. PyYAML's emitter
    quotes more aggressively than necessary; for an operator-edited
    file we want minimal quoting."""
    if s == "":
        return '""'
    # Quote if the string starts with a YAML-special character or
    # contains anything that could confuse the parser.
    if (s[0] in "!&*-[]{}|>%@`?,#" or s[0].isspace()
            or s != s.strip()
            or any(c in s for c in ":#\n\t")
            or s.lower() in ("yes", "no", "true", "false", "null", "~", "on", "off")):
        # YAML double-quote with backslash-escaped specials.
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    return s


# --------------------------------------------------------------------------- #
# Recipe → NodeConfig (the bake-time intermediate)                             #
# --------------------------------------------------------------------------- #


def recipe_to_node_config(r: Recipe):
    """Materialize a NodeConfig + sibling args from a Recipe.

    Pubkey strings are resolved here: anything that looks like a file
    path on the bake host gets read, everything else is passed through
    as a literal pubkey string. The operator can mix the two in one
    recipe — useful when committing the YAML alongside operator keys
    that live elsewhere.

    Returns (NodeConfig, build_kwargs) — build_kwargs maps directly
    to `pi_bake.bake.build(...)`.
    """
    from pi_bake.config import NodeConfig

    primary = _resolve_pubkey(r.ssh_pubkey)
    extras = [_resolve_pubkey(k) for k in r.extra_pubkeys]

    node = NodeConfig(
        hostname=r.hostname,
        ssh_pubkey=primary,
        extra_pubkeys=extras,
        wifi_ssid=r.wifi.ssid if r.wifi else "",
        wifi_psk=r.wifi.psk if r.wifi else "",
        wifi_country=r.wifi.country if r.wifi else "US",
        timezone=r.timezone,
        static_ipv4=r.network.address if r.network.mode == "static" else "",
        gateway_ipv4=r.network.gateway if r.network.mode == "static" else "",
        dhcp_send_hostname=r.network.send_hostname,
    )

    build_kwargs = {
        "board": r.board,
        "os_name": r.os,
        "version": r.os_version or None,
        "out_path": str(Path(r.output.path).expanduser()),
        "extra_packages": list(r.packages),
    }
    if r.output.image_size_mb:
        build_kwargs["image_size_mb"] = r.output.image_size_mb
    return node, build_kwargs


def _resolve_pubkey(s: str) -> str:
    """Read a pubkey from a path, or pass through a literal pubkey.

    Heuristic: any of (a) starts with `~`, (b) starts with `/`,
    (c) starts with `./` is treated as a file path. Strings starting
    with an OpenSSH key prefix (ssh-rsa, ssh-ed25519, ecdsa-) are
    passed through verbatim. Everything else: try to read as a file;
    if it doesn't exist, error with both possibilities flagged.
    """
    s = s.strip()
    if s.startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-")):
        return s
    if s.startswith(("~", "/", "./")):
        return Path(s).expanduser().read_text().strip()
    # Ambiguous: try as file, fall through to verbatim if reading fails.
    p = Path(s).expanduser()
    if p.is_file():
        return p.read_text().strip()
    raise ValueError(
        f"ssh_pubkey value {s!r} doesn't look like an OpenSSH key "
        f"(no ssh-rsa/ssh-ed25519/ecdsa- prefix) and isn't a readable "
        f"file path. Provide either an inline key string or a path "
        f"starting with ~, /, or ./"
    )


# Aliased for cleaner imports.
__all__ = [
    "Recipe", "NetworkSpec", "WifiSpec", "OutputSpec",
    "load_recipe", "dump_recipe", "recipe_to_node_config",
]
