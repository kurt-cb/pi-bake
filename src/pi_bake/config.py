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
      - extra_pubkeys: additional authorized_keys entries beyond the
                     primary pubkey (useful for shared deployments).
    """

    hostname: str
    ssh_pubkey: str
    wifi_ssid: str = ""
    wifi_psk: str = ""
    wifi_country: str = "US"
    timezone: str = "UTC"
    extra_pubkeys: list[str] = field(default_factory=list)

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
