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
#
# `edge` is special: Alpine's edge branch does NOT ship an RPi
# release tarball, but its `linux-rpi` apk has drivers the stable
# branch lacks (notably `iwlwifi` for Intel BE200 / Wi-Fi 7 as of
# 6.12.85, missing from stable 3.21's 6.12.13). The bake uses the
# latest stable RPi tarball for boot/firmware layout, then points
# /etc/apk/repositories at edge so post-boot `apk upgrade` rolls
# the kernel + drivers + firmware forward to edge versions on
# first run. See `enable_be200` in totaldns for the deploy-side
# kernel-upgrade step and `§30 fw-be200` for the full context.
ALPINE = OSImage(
    name="alpine",
    pretty="Alpine Linux",
    bake_backend="alpine",
    # Order matters: latest() returns versions[0]. `edge` is NOT a
    # latest default — operator must ask for it explicitly via
    # os_version: edge (YAML) or --version edge (CLI).
    versions=("3.21.4", "3.21.3", "3.20.5", "3.19.7", "edge"),
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
        "wants aarch64. Use `edge` for newer drivers (e.g. Intel "
        "BE200 iwlwifi); the bake uses stable's tarball but points "
        "/etc/apk/repositories at edge."
    ),
)

# Raspberry Pi OS Lite (Debian-based, partitioned .img.xz).
# Released as raspios_lite_arm64-YYYY-MM-DD/<>.img.xz on:
#   https://downloads.raspberrypi.com/raspios_lite_arm64/images/
# The `_latest` endpoint is a permanent redirect to the current
# build (e.g. 2026-04-21-raspios-trixie-arm64-lite.img.xz).
RASPBIAN = OSImage(
    name="raspbian",
    pretty="Raspberry Pi OS Lite",
    bake_backend="raspbian",
    # "latest" means "follow the permanent redirect" — pi-bake's
    # download.fetch() handles 30x. Operator pins via the `_YYYY`
    # versions when bumping.
    versions=("latest",),
    url_template=(
        "https://downloads.raspberrypi.com/raspios_lite_{arch}_latest"
    ),
    image_kind="img_xz",
    supports_boards=frozenset({"pi-3", "pi-4", "pi-5"}),
    notes=(
        "Recommended for Pi 4 / Pi 5. arch=arm64 in the URL maps to "
        "Board.arch=aarch64. The `latest` version follows Raspberry "
        "Pi's permanent redirect to the current dated build (likely "
        "trixie as of 2026). 32-bit raspbian for Pi Zero W is a "
        "separate URL — added when needed."
    ),
)

# Plain Debian (community Pi 4/5 images).
# Less polish than Raspberry Pi OS but useful for users who don't
# want the Raspberry-Pi-Foundation branding/configs.
#
# raspi.debian.net publishes dated tested builds; the convention
# is `YYYYMMDD_raspi_<N>_<codename>.img.xz`. The version field below
# is the date string; bumped manually until #14 (dynamic version
# discovery) lands.
DEBIAN = OSImage(
    name="debian",
    pretty="Debian",
    bake_backend="debian",
    # raspi.debian.net's tested-image release cadence is slow;
    # 2023-11-09 was the most recent "all 4 Pi models on bookworm"
    # build as of 2026-05. Bump when a newer one lands.
    versions=("20231109",),
    url_template=(
        "https://raspi.debian.net/tested/"
        "{version}_raspi_{rpi_n}_bookworm.img.xz"
    ),
    image_kind="img_xz",
    # No Pi 5 tested build at raspi.debian.net as of 2026-05; Pi 4
    # is the safe target. Pi 3 also works via the raspi_3 image.
    supports_boards=frozenset({"pi-3", "pi-4"}),
    notes=(
        "Community Pi-on-Debian images from raspi.debian.net. Ships "
        "Pi-specific firmware in the boot partition — boots directly "
        "(unlike Fedora, which needs arm-image-installer for the "
        "firmware shim). Version field is the YYYYMMDD build date. "
        "Pi 5 is NOT in the catalog yet — raspi.debian.net doesn't "
        "publish a Pi 5 tested build (2026-05). Use Raspbian or "
        "Alpine for Pi 5."
    ),
)

# Fedora ARM (Cloud-Base aarch64). Generic ARM image — does NOT
# ship Pi-specific firmware out of the box. pi-bake's Fedora
# backend produces a flashable .img.xz with cloud-init NoCloud
# pre-configured (hostname / SSH key / wifi / static IP), but
# operator must run `arm-image-installer --target=rpi4|rpi5` on
# the output to inject Pi firmware before flashing. See
# fedora.py module docstring for the workflow.
FEDORA = OSImage(
    name="fedora",
    pretty="Fedora Server Host-Generic ARM",
    bake_backend="fedora",
    # Fedora 43 is current as of 2026-05. The Server Host-Generic
    # aarch64 image is the right generic ARM target (no AmazonEC2
    # specifics). Bump as Fedora releases happen (~6 month cadence).
    versions=("43-1.6",),
    url_template=(
        "https://download.fedoraproject.org/pub/fedora/linux/"
        "releases/{minor_version}/Server/{arch}/images/"
        "Fedora-Server-Host-Generic-{version}.{arch}.raw.xz"
    ),
    image_kind="img_xz",
    supports_boards=frozenset({"pi-4", "pi-5"}),
    notes=(
        "Generic Fedora aarch64 image with cloud-init NoCloud "
        "preset by pi-bake. NOT directly Pi-bootable — operator "
        "must run arm-image-installer --target=rpi4|rpi5 to inject "
        "Pi firmware. Pi-bootloader-shim is a future pi-bake "
        "enhancement (see fedora.py docstring + ROADMAP)."
    ),
)


OSES: tuple[OSImage, ...] = (ALPINE, RASPBIAN, DEBIAN, FEDORA)
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


def resolve_image(
    os_name: str, version: str | None, board_arch: str,
    board_slug: str = "",
) -> tuple[OSImage, str, str]:
    """Pick OSImage + version + computed URL for a (os, version,
    arch) request.

    `version=None` means "use the latest known-good for this OS".
    `board_slug` (e.g. "pi-5") is used by URL templates that
    encode the Pi model in the filename (Debian's raspi.debian.net
    convention). Default empty for back-compat — Alpine ignores it.
    Returns `(OSImage, resolved_version, url)`.

    Special case for Alpine `edge`: no RPi release tarball exists
    on edge, so we use the latest stable tarball for the boot/FAT
    layout and the backend writes edge repositories into the
    apkovl. The returned `resolved_version` is still `edge` so
    downstream code can branch on it.
    """
    os_ = get_os(os_name)
    if version is None:
        version = os_.latest()
    if version not in os_.versions:
        # Allow operator to force a version not in the catalog — they
        # might know a newer point release exists. CLI surfaces a warning.
        pass

    # Alpine edge: fetch latest stable tarball, declare version=edge
    # so the backend knows to point repos at edge. The actual kernel
    # roll-forward happens via `apk upgrade` post-boot.
    download_version = version
    if os_.name == "alpine" and version == "edge":
        download_version = next(
            v for v in os_.versions if v != "edge"
        )

    # Alpine URLs need both `version` (3.21.4) and `minor_version` (3.21).
    # Fedora URLs need `minor_version` = the major (e.g. "41" from "41-1.4").
    if os_.name == "fedora":
        # "41-1.4" → minor_version="41" (Fedora release), version="41-1.4"
        minor = download_version.split("-", 1)[0]
    elif "." in download_version:
        minor = ".".join(download_version.split(".")[:2])
    else:
        minor = download_version
    # Arch substitution is backend-specific:
    #   alpine + fedora: `aarch64`
    #   raspbian:        `arm64` (Pi OS uses Debian's name)
    #   debian:          uses {rpi_n} in URL (rpi_4 / rpi_5), not arch
    if os_.bake_backend == "raspbian" and board_arch == "aarch64":
        url_arch = "arm64"
    else:
        url_arch = board_arch
    # Debian's raspi.debian.net puts the Pi model in the filename
    # ("raspi_4" / "raspi_5"). Derive from board slug.
    rpi_n = ""
    if os_.bake_backend == "debian" and board_slug:
        # board slug "pi-5" → "5"; "pi-4" → "4"
        if board_slug.startswith("pi-") and board_slug[3:].isdigit():
            rpi_n = board_slug[3:]
        elif board_slug == "pi-5":
            rpi_n = "5"
        elif board_slug == "pi-4":
            rpi_n = "4"
        else:
            # Fallback to Pi 5 (latest supported on raspi.debian.net).
            rpi_n = "5"
    elif os_.bake_backend == "debian":
        # No board_slug passed (old caller) — default to Pi 5.
        rpi_n = "5"
    url = os_.url_template.format(
        version=download_version,
        minor_version=minor,
        arch=url_arch,
        board="?",   # only used by old templates; ignored otherwise
        rpi_n=rpi_n,
    )
    return os_, version, url
