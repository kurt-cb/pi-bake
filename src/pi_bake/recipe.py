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
    "hostname", "board", "os", "os_version", "os_mode", "timezone",
    "locale",
    "ssh_pubkey", "extra_pubkeys", "ssh_host_key",
    "network", "wifi", "packages", "apk_fetch",
    "config_txt", "modules", "output", "pxe",
})
_PXE_KEYS = frozenset({"server_url"})

# Valid os_mode values per os. Empty set = mode doesn't apply
# (backend has only one layout). "" entry is the back-compat
# default (= "diskless" for alpine).
_VALID_OS_MODES: dict[str, frozenset[str]] = {
    "alpine": frozenset({"", "diskless", "ext4", "pxe"}),
}
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
class PxeSpec:
    """`os_mode: pxe`-specific recipe block.

    `server_url` — the base HTTP URL where the lab host serves the
    baked tree. Becomes the prefix for `apkovl=` and `alpine_repo=`
    in cmdline.txt. Example:
        http://192.168.4.2/td-cm4

    Trailing slash is stripped — pi-bake's cmdline templates always
    append the rest as `/<host>.apkovl.tar.gz` etc.
    """
    server_url: str = ""

    def __post_init__(self) -> None:
        if self.server_url and not self.server_url.startswith(
            ("http://", "https://")
        ):
            raise ValueError(
                f"pxe.server_url must start with http:// or https://; "
                f"got {self.server_url!r}"
            )
        # Strip trailing slash so templates concat predictably.
        self.server_url = self.server_url.rstrip("/")


@dataclass
class Recipe:
    """A complete bake recipe — everything one Pi needs.

    Required: hostname, board, os, ssh_pubkey, output.path.
    Everything else has a default.

    `os_version`: explicit OS version (e.g. "3.21.4",
    "bookworm"). Empty/None → latest known-good for `os`.

    `extra_pubkeys`: list of paths OR inline pubkey strings;
    paths are detected by '~/' prefix or being a real existing
    file. Mixed list allowed.

    `packages`: extra apk package names. When non-empty, pi-bake
    fetches them + all recursive deps from upstream Alpine at
    bake time, drops the .apk files into `/apks/<arch>/`
    alongside the stock cache, regenerates + signs a fresh
    APKINDEX, and adds the packages to `/etc/apk/world` so
    init's `apk add --no-network` installs them at INIT TIME
    (before sshd starts). Offline first boot, no late-boot
    install script. Bake-time requirements: network access to
    dl-cdn.alpinelinux.org, `tar` + `cpio` + `openssl` on the
    bake host.

    `apk_fetch`: DEPRECATED no-op. Bake-time fetch is always-on
    whenever `packages:` is non-empty (per the #3 redesign — see
    `design/#3_study.md`). Field kept in the schema so existing
    recipes don't fail-load with "unknown key"; the value is
    ignored.

    `ssh_host_key`: how to obtain the Pi's stable SSH host
    identity. Accepts three forms:

      - A **file path** (e.g. `~/.ssh/host-keys/td-pi5-1.ed25519`):
        baker reads the OpenSSH private key from `<path>` and the
        matching pubkey from `<path>.pub`.
      - `usehost`: derive an ed25519 keypair deterministically
        from the hostname (SHA-256 KDF). Same hostname -> same
        key, on any bake host, with no file to manage.
        **NOT a SECURE option, use for testing and labs only**
        — the key is predictable from the hostname, which means
        anyone who knows the hostname can compute the private
        key offline and impersonate / MITM the device.
      - `seed:<string>`: derive ed25519 deterministically from
        `<string>` literal. Use when you want to share the same
        key across several hostnames (e.g. an HA pair), or to
        salt with a deployment-specific secret.
        **NOT a SECURE option, use for testing and labs only**
        — same predictability concern as `usehost`. A seed
        committed to a version-controlled recipe is public.

    When set, the keypair is baked into
    `/etc/ssh/ssh_host_<type>_key{,.pub}` so the Pi's SSH
    identity is stable across reflashes — no `known_hosts`
    "REMOTE HOST IDENTIFICATION HAS CHANGED" warnings on
    rebuild. Empty (default): the baker generates a fresh
    ed25519 pair at bake time. Stable across reflashes of the
    same .img.gz; changes on each new `pi-bake build`. For
    production / WAN-exposed deployments, use the file-path
    form with a per-host keypair generated from /dev/urandom.

    `config_txt`: list of `dtoverlay=` / `dtparam=` / etc. lines
    appended to `/boot/usercfg.txt` on the FAT partition. The
    stock RPi config.txt `include`s usercfg.txt, so additions
    layer cleanly without editing the shipped file. Use for
    HAT-specific enablement (e.g. `dtparam=pciex1` for PCIe on
    the Pi 5, `dtoverlay=mcp2515-can0,...` for SPI CAN HATs).
    Operator declares one line per list entry, in the literal
    form that goes into the file.

    `modules`: list of kernel module names written to
    `/etc/modules` in the apkovl, one per line. OpenRC's
    `kmod-static-nodes` + the kernel's udev autoload generally
    cover most cases, but `modules:` is the override for cards
    that need an explicit `modprobe` at boot (e.g. `mcp251x`
    for the MCP2515 CAN controller). Order is preserved.

    `os_mode`: image layout for `os: alpine`. Defaults to
    `diskless` (the v0.0+ Alpine RPi tarball shape — apkovl
    overlay + modloop squashfs on FAT, no-root bake). Set to
    `ext4` for sys-mode Alpine on a real partitioned image
    (FAT `/boot` + ext4 `/`). ext4 mode requires sudo (losetup
    + mount) but makes `apk upgrade linux-rpi` work normally,
    which is the only honest way to use `os_version: edge`.
    Has no effect when `os` is not `alpine`.
    """
    hostname: str
    board: str
    os: str
    ssh_pubkey: str
    output: OutputSpec
    os_version: str = ""
    os_mode: str = ""
    timezone: str = "UTC"
    locale: str = "en_GB.UTF-8"
    extra_pubkeys: list[str] = field(default_factory=list)
    network: NetworkSpec = field(default_factory=NetworkSpec)
    wifi: WifiSpec | None = None
    packages: list[str] = field(default_factory=list)
    apk_fetch: bool = False
    ssh_host_key: str = ""
    config_txt: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    pxe: PxeSpec = field(default_factory=PxeSpec)

    def __post_init__(self) -> None:
        # os_mode validity per os
        if self.os in _VALID_OS_MODES:
            allowed = _VALID_OS_MODES[self.os]
            if self.os_mode not in allowed:
                raise ValueError(
                    f"os_mode={self.os_mode!r} not valid for os={self.os!r}; "
                    f"allowed: {sorted(allowed - {''})}"
                )
        elif self.os_mode:
            raise ValueError(
                f"os_mode is only meaningful for os: alpine; "
                f"got os={self.os!r} with os_mode={self.os_mode!r}"
            )
        # Alpine edge requires ext4 — diskless has no upstream
        # edge RPi tarball and modloop-on-FAT makes post-boot
        # kernel upgrade require a manual ritual we won't
        # paper over. See feature_request.md for full context.
        if self.os == "alpine" and self.os_version == "edge":
            effective_mode = self.os_mode or "diskless"
            if effective_mode != "ext4":
                raise ValueError(
                    "os_version: edge is not supported in Alpine diskless "
                    "mode (Alpine upstream ships no RPi edge tarball, and "
                    "diskless's modloop-on-FAT makes post-boot kernel "
                    "upgrade require a manual ritual). "
                    "Use `os_mode: ext4` for edge kernel support."
                )
        # pxe mode requires pxe.server_url (the lab HTTP base URL).
        # Validated here so a malformed recipe fails fast at load,
        # not late in the bake when the cmdline.txt template fires.
        if self.os_mode == "pxe" and not self.pxe.server_url:
            raise ValueError(
                "os_mode: pxe requires pxe.server_url — the base HTTP URL "
                "the lab host serves the baked tree at (e.g. "
                "http://192.168.4.2/<hostname>). pi-bake substitutes this "
                "into cmdline.txt's apkovl=... and alpine_repo=... params."
            )
        if self.os_mode != "pxe" and self.pxe.server_url:
            raise ValueError(
                "pxe.server_url set but os_mode is not pxe — pxe.* fields "
                "are only meaningful when os_mode: pxe."
            )


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

    pxe_raw = d.get("pxe") or {}
    if not isinstance(pxe_raw, dict):
        raise ValueError(
            f"{source}: `pxe` must be a mapping, got {type(pxe_raw).__name__}"
        )
    _check_keys(pxe_raw, _PXE_KEYS, f"{source}: pxe")
    pxe = PxeSpec(**pxe_raw)

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

    apk_fetch_raw = d.get("apk_fetch", False)
    if not isinstance(apk_fetch_raw, bool):
        raise ValueError(
            f"{source}: `apk_fetch` must be a boolean, "
            f"got {type(apk_fetch_raw).__name__}"
        )

    config_txt = d.get("config_txt") or []
    if not isinstance(config_txt, list):
        raise ValueError(
            f"{source}: `config_txt` must be a list of strings, "
            f"got {type(config_txt).__name__}"
        )
    if not all(isinstance(line, str) for line in config_txt):
        raise ValueError(
            f"{source}: every entry in `config_txt` must be a string"
        )

    modules = d.get("modules") or []
    if not isinstance(modules, list):
        raise ValueError(
            f"{source}: `modules` must be a list of strings, "
            f"got {type(modules).__name__}"
        )
    if not all(isinstance(m, str) for m in modules):
        raise ValueError(
            f"{source}: every entry in `modules` must be a string"
        )

    return Recipe(
        hostname=d["hostname"],
        board=d["board"],
        os=d["os"],
        ssh_pubkey=d["ssh_pubkey"],
        output=output,
        os_version=d.get("os_version") or "",
        os_mode=d.get("os_mode") or "",
        timezone=d.get("timezone") or "UTC",
        locale=d.get("locale") or "en_GB.UTF-8",
        extra_pubkeys=[str(k) for k in extra_pubkeys],
        network=network,
        wifi=wifi,
        packages=list(packages),
        apk_fetch=apk_fetch_raw,
        ssh_host_key=d.get("ssh_host_key") or "",
        config_txt=list(config_txt),
        modules=list(modules),
        pxe=pxe,
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
    if r.os_mode:
        lines.append(f"os_mode: {_yaml_str(r.os_mode)}")
    if r.timezone and r.timezone != "UTC":
        lines.append(f"timezone: {_yaml_str(r.timezone)}")
    if r.locale and r.locale != "en_GB.UTF-8":
        lines.append(f"locale: {_yaml_str(r.locale)}")
    lines.append("")
    lines.append("# Primary OpenSSH pubkey (path on the bake host OR inline string)")
    lines.append(f"ssh_pubkey: {_yaml_str(r.ssh_pubkey)}")
    if r.extra_pubkeys:
        lines.append("extra_pubkeys:")
        for k in r.extra_pubkeys:
            lines.append(f"  - {_yaml_str(k)}")
    if r.ssh_host_key:
        lines.append("")
        lines.append("# Stable SSH host identity (private key on bake host;")
        lines.append("# matching .pub at <path>.pub). Avoids known_hosts churn.")
        lines.append(f"ssh_host_key: {_yaml_str(r.ssh_host_key)}")
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
    if r.apk_fetch:
        lines.append("")
        lines.append("# Bake-time apk-fetch: stage extras + deps offline.")
        lines.append("apk_fetch: true")
    if r.config_txt:
        lines.append("")
        lines.append("# Lines appended to /boot/usercfg.txt on FAT —")
        lines.append("# dtoverlay= / dtparam= for HATs + peripherals.")
        lines.append("config_txt:")
        for line in r.config_txt:
            lines.append(f"  - {_yaml_str(line)}")
    if r.modules:
        lines.append("")
        lines.append("# Kernel modules written to /etc/modules (one per line).")
        lines.append("modules:")
        for m in r.modules:
            lines.append(f"  - {_yaml_str(m)}")
    if r.pxe.server_url:
        lines.append("")
        lines.append("# PXE-mode lab HTTP base URL (apkovl + alpine_repo target).")
        lines.append("pxe:")
        lines.append(f"  server_url: {_yaml_str(r.pxe.server_url)}")
    lines.append("")
    lines.append("# Where the baked artifact lands (file for img bakes, dir for pxe)")
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

    priv_bytes = b""
    pub_bytes = b""
    if r.ssh_host_key:
        from pi_bake.host_keys import resolve_host_key_spec
        resolved = resolve_host_key_spec(r.ssh_host_key, r.hostname)
        if resolved is not None:
            priv_bytes, pub_bytes = resolved

    node = NodeConfig(
        hostname=r.hostname,
        ssh_pubkey=primary,
        extra_pubkeys=extras,
        wifi_ssid=r.wifi.ssid if r.wifi else "",
        wifi_psk=r.wifi.psk if r.wifi else "",
        wifi_country=r.wifi.country if r.wifi else "US",
        timezone=r.timezone,
        locale=r.locale,
        static_ipv4=r.network.address if r.network.mode == "static" else "",
        gateway_ipv4=r.network.gateway if r.network.mode == "static" else "",
        dhcp_send_hostname=r.network.send_hostname,
        ssh_host_key_priv=priv_bytes,
        ssh_host_key_pub=pub_bytes,
        config_txt=list(r.config_txt),
        modules=list(r.modules),
        board=r.board,
    )

    build_kwargs = {
        "board": r.board,
        "os_name": r.os,
        "version": r.os_version or None,
        "out_path": str(Path(r.output.path).expanduser()),
        "extra_packages": list(r.packages),
        # r.apk_fetch is intentionally NOT threaded through: it's a
        # deprecated no-op since #3 made init-time install the only
        # path. Kept as a schema field so old recipes don't break.
    }
    if r.os_mode:
        build_kwargs["os_mode"] = r.os_mode
    if r.output.image_size_mb:
        build_kwargs["image_size_mb"] = r.output.image_size_mb
    if r.pxe.server_url:
        build_kwargs["pxe_server_url"] = r.pxe.server_url
    return node, build_kwargs


def _resolve_pubkey(s: str) -> str:
    """Read a pubkey from a path, expand a glob to multiple keys,
    or pass through a literal pubkey.

    Heuristic for paths: any of (a) starts with `~`, (b) starts
    with `/`, (c) starts with `./` is treated as a file path.
    Strings starting with an OpenSSH key prefix (ssh-rsa,
    ssh-ed25519, ecdsa-) are passed through verbatim. Everything
    else: try to read as a file; if it doesn't exist, error with
    both possibilities flagged.

    Glob support (v0.5.1+): if the path contains `*`, `?`, or
    `[`, it's expanded via `glob.glob()`. All matched files are
    read and their contents joined with newlines, producing a
    multi-line authorized_keys-ready string. Useful for
    `~/.ssh/*.pub` which scoops up every key the operator has on
    the bake host without naming them individually. Errors loudly
    if the glob matches zero files.
    """
    import glob as _glob

    s = s.strip()
    if s.startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-")):
        return s
    if s.startswith(("~", "/", "./")):
        expanded = str(Path(s).expanduser())
        if any(c in s for c in "*?["):
            matches = sorted(_glob.glob(expanded))
            if not matches:
                raise ValueError(
                    f"ssh_pubkey glob {s!r} matched no files "
                    f"(expanded: {expanded!r})"
                )
            return "\n".join(
                Path(m).read_text().strip() for m in matches
            )
        return Path(expanded).read_text().strip()
    # Ambiguous: try as file, fall through to verbatim if reading fails.
    p = Path(s).expanduser()
    if p.is_file():
        return p.read_text().strip()
    raise ValueError(
        f"ssh_pubkey value {s!r} doesn't look like an OpenSSH key "
        f"(no ssh-rsa/ssh-ed25519/ecdsa- prefix) and isn't a readable "
        f"file path. Provide either an inline key string, a path "
        f"starting with ~, /, or ./, or a glob like ~/.ssh/*.pub"
    )


# Aliased for cleaner imports.
__all__ = [
    "Recipe", "NetworkSpec", "WifiSpec", "OutputSpec",
    "load_recipe", "dump_recipe", "recipe_to_node_config",
]
