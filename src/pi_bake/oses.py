"""OS image catalog.

`OSImage` describes a *flavor* (alpine / raspbian / debian). The
`url_template` says where the upstream image lives — `{arch}` and
`{version}` get interpolated at resolve time. The `bake_backend`
field tells the bake driver which baker module handles that flavor.

(board, os) support matrix lives on the OSImage itself via
`supports_boards`. The CLI's `list-os --board X` filters by that.

Versions are intentionally hardcoded for v0.1. A future
`refresh_versions()` (TODO in README) can fetch the upstream index
and update the list dynamically — but the catalog already exposes
the latest known-good version per flavor, which covers the
common case.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OSImage:
    name: str                    # short slug, e.g. "alpine"
    pretty: str                  # "Alpine Linux"
    bake_backend: str            # "alpine" | "raspbian"  (module under pi_bake/)
    versions: tuple[str, ...]    # known-good versions (newest first)
    url_template: str            # URL with {version} + {arch} placeholders
    image_kind: str              # "tarball" (Alpine) | "img_xz" (Raspbian)
    supports_boards: frozenset[str] = field(default_factory=frozenset)
    notes: str = ""

    def latest(self) -> str:
        return self.versions[0]

    def __str__(self) -> str:
        return f"{self.name} {self.versions[0]} — {self.pretty}"


# Alpine RPi tarballs (FAT-extractable; apkovl-based overlay).
# Tarballs for each (arch, version) live at:
#   https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/<arch>/alpine-rpi-3.21.x-<arch>.tar.gz
# Versions list updated on each Alpine point release; CLI prints
# "(may be stale — check upstream)" when more than ~6 months old.
ALPINE = OSImage(
    name="alpine",
    pretty="Alpine Linux",
    bake_backend="alpine",
    versions=("3.21.4", "3.21.3", "3.20.5", "3.19.7"),
    url_template=(
        "https://dl-cdn.alpinelinux.org/alpine/"
        "v{minor_version}/releases/{arch}/"
        "alpine-rpi-{version}-{arch}.tar.gz"
    ),
    image_kind="tarball",
    supports_boards=frozenset({
        "pi-zero-w",      # armhf
        "pi-zero-2-w",    # aarch64
        "pi-3",           # aarch64
        "pi-4",           # aarch64
        "pi-5",           # aarch64 — experimental on 3.20+, settled on 3.21+
    }),
    notes=(
        "Pi 5 support is recent. If 3.21 fails on Pi 5, try 3.22+. "
        "armhf is for the original Pi Zero W ONLY — every other board "
        "wants aarch64."
    ),
)

# Raspberry Pi OS Lite (Debian-based, partitioned .img.xz).
# Released as raspios_lite_arm64-YYYY-MM-DD/<>.img.xz on:
#   https://downloads.raspberrypi.com/raspios_lite_arm64/images/
RASPBIAN = OSImage(
    name="raspbian",
    pretty="Raspberry Pi OS Lite",
    bake_backend="raspbian",
    # Versions here are the named Debian releases the Pi OS tracks.
    # Each maps to the latest dated image in the catalog at bake time.
    versions=("bookworm",),
    url_template=(
        "https://downloads.raspberrypi.com/raspios_lite_{arch}/"
        "images/raspios_lite_{arch}-latest/"
        "raspios_lite_{arch}-latest.img.xz"
    ),
    image_kind="img_xz",
    supports_boards=frozenset({"pi-3", "pi-4", "pi-5"}),
    notes=(
        "Recommended for Pi 4 / Pi 5. arch=arm64 in the URL maps to "
        "Board.arch=aarch64. 32-bit raspbian for Pi Zero W is a "
        "separate URL — added when needed."
    ),
)

# Plain Debian (community Pi 4/5 images).
# Less polish than Raspberry Pi OS but useful for users who don't
# want the Raspberry-Pi-Foundation branding/configs.
DEBIAN = OSImage(
    name="debian",
    pretty="Debian",
    bake_backend="raspbian",   # same .img.xz partitioned shape
    versions=("12.7-arm64",),
    url_template=(
        "https://raspi.debian.net/tested/"
        "{version}/{board}-debian-{version}.img.xz"
    ),
    image_kind="img_xz",
    supports_boards=frozenset({"pi-4", "pi-5"}),
    notes=(
        "Community Pi-on-Debian images. URL template uses board slug "
        "in place of arch. Pi 5 support: yes; check raspi.debian.net "
        "for the current dated build."
    ),
)


OSES: tuple[OSImage, ...] = (ALPINE, RASPBIAN, DEBIAN)
_BY_NAME: dict[str, OSImage] = {o.name: o for o in OSES}


def list_oses(board: str | None = None) -> list[OSImage]:
    """Every catalog OS, optionally filtered to those that run on
    the given board slug."""
    if board is None:
        return list(OSES)
    return [o for o in OSES if board in o.supports_boards]


def get_os(name: str) -> OSImage:
    """Look up an OS by slug. Raises KeyError on miss."""
    norm = name.strip().lower()
    # Common synonyms.
    norm = {"rpi-os": "raspbian", "raspberry-pi-os": "raspbian"}.get(norm, norm)
    if norm in _BY_NAME:
        return _BY_NAME[norm]
    raise KeyError(f"unknown OS {name!r}; known: {sorted(_BY_NAME)}")


def resolve_image(os_name: str, version: str | None, board_arch: str) -> tuple[OSImage, str, str]:
    """Pick OSImage + version + computed URL for a (os, version,
    arch) request.

    `version=None` means "use the latest known-good for this OS".
    Returns `(OSImage, resolved_version, url)`.
    """
    os_ = get_os(os_name)
    if version is None:
        version = os_.latest()
    if version not in os_.versions:
        # Allow operator to force a version not in the catalog — they
        # might know a newer point release exists. CLI surfaces a warning.
        pass
    # Alpine URLs need both `version` (3.21.4) and `minor_version` (3.21).
    minor = ".".join(version.split(".")[:2]) if "." in version else version
    # Arch substitution is backend-specific. Alpine + plain Debian use
    # `aarch64`; Raspberry Pi OS uses `arm64` (Debian's name). Apply
    # the swap only where it's needed — Alpine URLs break otherwise.
    if os_.bake_backend == "raspbian" and board_arch == "aarch64":
        url_arch = "arm64"
    else:
        url_arch = board_arch
    url = os_.url_template.format(
        version=version,
        minor_version=minor,
        arch=url_arch,
        board="?",   # only used by Debian; resolve_image overrides via .format
    )
    return os_, version, url
