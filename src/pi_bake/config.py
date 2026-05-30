"""Per-node bake config — what gets baked into the image.

Intentionally minimal. The image is meant to come up on the
network, accept the operator's SSH pubkey, and stop. Everything
role-specific (hostapd, totaldns, wpa_supplicant tweaks) gets
applied via pyinfra AFTER the device is up + discovered, which
keeps the image generic and reusable across projects.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


_VALID_HOSTNAME = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE,
)


@dataclass
class NodeConfig:
    """Bake recipe for a single Pi.

    Required:
      - hostname:    DNS-label-safe; becomes `/etc/hostname` and the
                     name the network sees via DHCP option 12.
      - ssh_pubkey:  one OpenSSH-format pubkey line. Gets written to
                     `/root/.ssh/authorized_keys` (Alpine) or to the
                     `pi` user's authorized_keys (Raspbian). Password
                     auth disabled in both cases.

    Optional:
      - wifi_ssid + wifi_psk: if either is set both must be. Bakes
                     `wpa_supplicant.conf` so the Pi auto-joins on
                     first boot. Omit for wired-only nodes (eth gets
                     DHCP automatically).
      - wifi_country: regulatory domain ("US", "GB", etc.). Defaults
                     to "US". Needed for 5 GHz channel unlock.
      - timezone:    e.g. "America/New_York". Defaults to "UTC".
                     Backends write `/etc/timezone` + symlink
                     `/etc/localtime` to /usr/share/zoneinfo/<tz>.
      - locale:      e.g. "en_US.UTF-8". Defaults to "en_GB.UTF-8"
                     (matches Pi OS Lite's shipped default, no
                     surprise change). Backends ensure the locale
                     is generated + set as LANG. Only the encoding
                     part (UTF-8) matters for most CLI work; the
                     en_US vs en_GB choice is mostly cosmetic
                     unless you care about date formats / etc.
      - extra_pubkeys: additional authorized_keys entries beyond the
                     primary pubkey (useful for shared deployments).
    """

    hostname: str
    ssh_pubkey: str
    wifi_ssid: str = ""
    wifi_psk: str = ""
    wifi_country: str = "US"
    timezone: str = "UTC"
    locale: str = "en_GB.UTF-8"
    extra_pubkeys: list[str] = field(default_factory=list)
    # Optional static IP for eth0. Format "<addr>/<bits>" e.g.
    # "192.168.4.111/24". When set, /etc/network/interfaces uses
    # `iface eth0 inet static` instead of dhcp + carries the
    # gateway. Empty → DHCP (the default).
    static_ipv4: str = ""
    gateway_ipv4: str = ""
    # Whether dhcpcd announces the system hostname in DHCP option 12
    # on its DISCOVER/REQUEST. Default True (the correct, friendly
    # behavior — operator-chosen hostname shows up in DHCP server
    # logs + lease tables). Set to False to bake an intentional
    # test fixture: a device that doesn't advertise its name via
    # DHCP, so the server has to fall back to mDNS or accept a
    # synthesized placeholder. Useful for exercising mDNS-based
    # hostname recovery paths on the DHCP server side.
    dhcp_send_hostname: bool = True
    # FAT-side /boot/usercfg.txt edits — one line per entry, in
    # the literal form the bootloader expects (`dtoverlay=...`,
    # `dtparam=...`, etc.). Appended to usercfg.txt which the
    # stock Alpine RPi config.txt `include`s, so we layer cleanly
    # without touching the shipped file.
    config_txt: list[str] = field(default_factory=list)
    # Kernel modules forced loaded at boot via /etc/modules in
    # the apkovl. Most modules autoload via udev / kernel
    # builtins; this is the override for cards that need an
    # explicit modprobe before the runlevel using them comes up.
    modules: list[str] = field(default_factory=list)
    # Board slug (e.g. "pi-5", "pi-zero-w"). Optional — used by
    # the backend to make board-specific decisions, e.g. picking
    # the right LBU_MEDIA name in /etc/lbu/lbu.conf (Pi Zero W
    # needs `mmcblk0p1`, every other supported board needs
    # `mmcblk0`). Empty default = backend uses board-agnostic
    # fallback values where possible. The recipe loader always
    # populates this from Recipe.board; only direct NodeConfig()
    # users need to set it manually.
    board: str = ""
    # SSH host keypair baked into /etc/ssh/. Both bytes-fields set
    # together OR both empty.
    #
    # Empty (default): the baker generates a fresh ed25519 host
    # keypair at bake time and embeds it. Stable across reflashes
    # of the SAME .img.gz (a property the default sshd-regenerates-
    # on-first-boot path doesn't give); changes on each new
    # `pi-bake build`. Useful when an operator flashes one image
    # onto many SD cards and doesn't want each card to look like a
    # different host to known_hosts.
    #
    # Set: operator-managed host identity, stable across `pi-bake
    # build` invocations forever — no `known_hosts` warnings on
    # re-bake. The bytes are the literal OpenSSH-format private and
    # public key file contents. Key type is auto-detected from the
    # pubkey's first word (ssh-ed25519 / ssh-rsa / ecdsa-sha2-*),
    # and the files land at /etc/ssh/ssh_host_<type>_key{,.pub}
    # with 600/644 perms.
    ssh_host_key_priv: bytes = b""
    ssh_host_key_pub: bytes = b""

    def __post_init__(self) -> None:
        # Hostname must be a DNS label.
        if not _VALID_HOSTNAME.match(self.hostname):
            raise ValueError(
                f"hostname {self.hostname!r} isn't a valid DNS label "
                f"(use lowercase letters / digits / hyphens, "
                f"≤63 chars, no leading/trailing hyphen)"
            )
        pk = self.ssh_pubkey.strip()
        if not pk or not pk.split(None, 1)[0].startswith(
            ("ssh-rsa", "ssh-ed25519", "ecdsa-sha2-")
        ):
            raise ValueError(
                f"ssh_pubkey doesn't look like an OpenSSH key "
                f"(starts with {pk[:24]!r})"
            )
        # WiFi: both or neither.
        if bool(self.wifi_ssid) != bool(self.wifi_psk):
            raise ValueError(
                "wifi_ssid + wifi_psk must both be set or both empty"
            )
        # Static IP: address requires a gateway too.
        if bool(self.static_ipv4) != bool(self.gateway_ipv4):
            raise ValueError(
                "static_ipv4 + gateway_ipv4 must both be set or both empty"
            )
        if self.static_ipv4 and "/" not in self.static_ipv4:
            raise ValueError(
                f"static_ipv4 must be CIDR form (e.g. 192.168.4.111/24); "
                f"got {self.static_ipv4!r}"
            )
        # SSH host key: both halves or neither.
        if bool(self.ssh_host_key_priv) != bool(self.ssh_host_key_pub):
            raise ValueError(
                "ssh_host_key_priv + ssh_host_key_pub must both be set "
                "or both empty"
            )
        if self.ssh_host_key_pub:
            first = self.ssh_host_key_pub.split(None, 1)[0].decode(errors="replace")
            if not (first == "ssh-ed25519" or first == "ssh-rsa"
                    or first.startswith("ecdsa-sha2-")):
                raise ValueError(
                    f"ssh_host_key_pub first field {first!r} isn't an "
                    f"OpenSSH key type — expected ssh-ed25519, ssh-rsa, "
                    f"or ecdsa-sha2-*"
                )

    @property
    def ssh_host_key_type(self) -> str:
        """`ed25519` | `rsa` | `ecdsa` — the type segment for
        /etc/ssh/ssh_host_<type>_key filename. Empty when no key
        is set (auto-gen path)."""
        if not self.ssh_host_key_pub:
            return ""
        first = self.ssh_host_key_pub.split(None, 1)[0].decode(errors="replace")
        if first == "ssh-ed25519":
            return "ed25519"
        if first == "ssh-rsa":
            return "rsa"
        if first.startswith("ecdsa-sha2-"):
            return "ecdsa"
        # Unreachable thanks to __post_init__, but defensive.
        return ""

    @property
    def all_pubkeys(self) -> list[str]:
        """All authorized_keys lines (primary + extras), de-duped."""
        seen: set[str] = set()
        out: list[str] = []
        for k in [self.ssh_pubkey, *self.extra_pubkeys]:
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out

    @property
    def has_wifi(self) -> bool:
        return bool(self.wifi_ssid)

    @property
    def has_static_ip(self) -> bool:
        return bool(self.static_ipv4)

    @property
    def static_address_only(self) -> str:
        """`192.168.4.111` part of `192.168.4.111/24`."""
        return self.static_ipv4.split("/", 1)[0] if self.has_static_ip else ""

    @property
    def static_prefixlen(self) -> str:
        return self.static_ipv4.split("/", 1)[1] if self.has_static_ip else ""

    @property
    def static_netmask(self) -> str:
        """Dotted-decimal form for /etc/network/interfaces."""
        if not self.has_static_ip:
            return ""
        bits = int(self.static_prefixlen)
        mask_int = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
        return ".".join(str((mask_int >> (8 * (3 - i))) & 0xFF) for i in range(4))

    def wpa_supplicant_conf(self) -> str:
        """Render `/etc/wpa_supplicant/wpa_supplicant.conf` text.
        Empty string when no WiFi is configured."""
        if not self.has_wifi:
            return ""
        return (
            f"ctrl_interface=/var/run/wpa_supplicant\n"
            f"country={self.wifi_country}\n"
            f"update_config=1\n"
            f"\n"
            f"network={{\n"
            f"    ssid=\"{self.wifi_ssid}\"\n"
            f"    psk=\"{self.wifi_psk}\"\n"
            f"    key_mgmt=WPA-PSK\n"
            f"    scan_ssid=1\n"
            f"}}\n"
        )

    def authorized_keys_text(self) -> str:
        """Render `~/.ssh/authorized_keys` text — one line per key,
        trailing newline."""
        return "\n".join(self.all_pubkeys) + "\n"
