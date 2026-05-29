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
    versions: tuple[str, ...]    # selectable versions (newest first)
    url_template: str            # URL with {version} + {arch} placeholders
    image_kind: str              # "tarball" (Alpine) | "img_xz" (Raspbian)
    supports_boards: frozenset[str] = field(default_factory=frozenset)
    notes: str = ""
    # Two sentinels can appear as `os_version:` in a recipe:
    #   `latest` -> the URL builder picks the bleeding-edge upstream.
    #     For Raspbian this is the permanent-redirect endpoint
    #     (truly bleeding edge — moves whenever upstream cuts a
    #     build); for OSes without an upstream "latest" alias this
    #     resolves to versions[0] (the newest catalog entry, which
    #     we bump by hand on each pi-bake release).
    #   `stable` -> pi-bake's curated known-good pick. May lag
    #     `latest` deliberately to dodge upstream regressions. The
    #     `stable_version` field below is the concrete answer; the
    #     resolver interpolates it through the URL template.
    # Empty `stable_version` falls back to versions[0].
    stable_version: str = ""

    def latest(self) -> str:
        return self.versions[0]

    def stable(self) -> str:
        return self.stable_version or self.versions[0]

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
    # Order matters: latest() returns versions[0]. `edge` is only
    # valid with os_mode: ext4 — see recipe.py validation. In
    # diskless mode Alpine upstream ships no RPi tarball for edge,
    # and modloop-on-FAT makes post-boot kernel upgrade a manual
    # ritual we won't paper over.
    versions=("3.21.4", "3.21.3", "3.20.5", "3.19.7", "edge"),
    # `stable`: 3.21.4 is hardware-validated on Pi 5 / CM4 with both
    # diskless and ext4 modes. Bump after a new point release ships
    # and clears smoke-bake.
    stable_version="3.21.4",
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
        "wants aarch64. `edge` requires os_mode: ext4."
    ),
)

# Raspberry Pi OS Lite (Debian-based, partitioned .img.xz).
# Released as raspios_lite_arm64-YYYY-MM-DD/<>.img.xz on:
#   https://downloads.raspberrypi.com/raspios_lite_arm64/images/
# The `_latest` endpoint is a permanent redirect to the current
# build (e.g. 2026-04-21-raspios-trixie-arm64-lite.img.xz).
#
# RASPBIAN_BUILDS catalogs the dated archives. Key = directory
# date (also what the operator types as os_version). Value =
# (codename, file_date) — codename for the filename, file_date
# for the cases where the .img.xz inside the dated directory is
# stamped one day earlier than the directory itself (a Pi-OS
# release-pipeline quirk; e.g. directory 2025-10-02 contains a
# 2025-10-01-built image). Without storing both, the URL builder
# would 404. Bump this dict when a new build lands upstream —
# `pi-bake list-os-versions --os raspbian` displays the menu.
RASPBIAN_BUILDS: dict[str, tuple[str, str]] = {
    # directory-date     -> (codename, file-date-in-filename)
    "2026-04-21": ("trixie", "2026-04-21"),
    "2026-04-14": ("trixie", "2026-04-13"),
    "2025-12-04": ("trixie", "2025-12-04"),
    "2025-11-24": ("trixie", "2025-11-24"),
    "2025-10-02": ("trixie", "2025-10-01"),  # first trixie release
    "2025-05-13": ("bookworm", "2025-05-13"),  # last bookworm
    "2024-11-19": ("bookworm", "2024-11-19"),
    "2024-07-04": ("bookworm", "2024-07-04"),
    "2024-03-15": ("bookworm", "2024-03-15"),
    "2023-12-11": ("bookworm", "2023-12-11"),
    "2023-12-06": ("bookworm", "2023-12-05"),
}

# versions tuple holds `latest` (permanent-redirect) first, then
# every dated build in newest-first order. `latest()` returns the
# tuple's first element, so the permanent-redirect remains the
# default when os_version is unset.
_RASPBIAN_DATES_NEWEST_FIRST = tuple(
    sorted(RASPBIAN_BUILDS.keys(), reverse=True)
)

RASPBIAN = OSImage(
    name="raspbian",
    pretty="Raspberry Pi OS Lite",
    bake_backend="raspbian",
    versions=("latest",) + _RASPBIAN_DATES_NEWEST_FIRST,
    # `stable`: 2025-05-13 is the last Bookworm build before Pi OS
    # moved to Trixie. Trixie's userconf-pi service creates the pi
    # user with /usr/sbin/nologin by default, which breaks pi-bake's
    # SSH-key-only login flow. Bookworm's userconf-pi sets /bin/bash
    # and works out of the box. Bump after a future Trixie release
    # fixes this or pi-bake gains its own firstrun.sh.
    stable_version="2025-05-13",
    url_template=(
        "https://downloads.raspberrypi.com/raspios_lite_{arch}_latest"
    ),
    image_kind="img_xz",
    supports_boards=frozenset({"pi-3", "pi-4", "pi-5"}),
    notes=(
        "Recommended for Pi 4 / Pi 5. arch=arm64 in the URL maps to "
        "Board.arch=aarch64. `latest` follows Raspberry Pi's "
        "permanent redirect to the current dated build (currently "
        "Trixie — see stable note). `stable` -> 2025-05-13 "
        "(last Bookworm, sidesteps Trixie's userconf-pi nologin "
        "default). Pin to a specific date (e.g. os_version: "
        "2025-05-13) to lock. Run `pi-bake list-os-versions --os "
        "raspbian` for the menu. 32-bit raspbian for Pi Zero W is "
        "a separate URL — added when needed."
    ),
)


def raspbian_url(version: str, arch: str) -> str:
    """Build the upstream URL for a Raspbian version + arch.

    `version="latest"` returns the permanent-redirect endpoint;
    every other value must be a key in RASPBIAN_BUILDS (a dated
    directory). Raises KeyError on unknown date strings — the CLI
    surfaces this as "Run `pi-bake list-os-versions --os raspbian`".
    """
    if version == "latest":
        return f"https://downloads.raspberrypi.com/raspios_lite_{arch}_latest"
    if version not in RASPBIAN_BUILDS:
        raise KeyError(
            f"unknown raspbian version {version!r}; "
            f"known: latest, {', '.join(_RASPBIAN_DATES_NEWEST_FIRST)}"
        )
    codename, file_date = RASPBIAN_BUILDS[version]
    return (
        f"https://downloads.raspberrypi.com/raspios_lite_{arch}/images/"
        f"raspios_lite_{arch}-{version}/"
        f"{file_date}-raspios-{codename}-{arch}-lite.img.xz"
    )

# Plain Debian (community Pi 4/5 images).
# Less polish than Raspberry Pi OS but useful for users who don't
# want the Raspberry-Pi-Foundation branding/configs.
#
# raspi.debian.net publishes dated tested builds; the convention
# is `YYYYMMDD_raspi_<N>_<codename>.img.xz`. The version field below
# is the date string; bumped manually until #14 (dynamic version
# discovery) lands.
# Debian (raspi.debian.net) tested builds. Both the date AND
# the codename are encoded in the upstream filename (e.g.
# `20231109_raspi_4_bookworm.img.xz`), so each catalog entry
# carries its codename here. Earlier Bullseye builds remain
# downloadable but pi-bake skips them — there's no benefit over
# Bookworm for new bakes.
DEBIAN_BUILDS: dict[str, str] = {
    # date     -> codename (the all-models build; trixie has Pi 4 only)
    "20231111": "trixie",     # Pi 4 only (no raspi_1/2/3/5 builds)
    "20231109": "bookworm",   # full Pi 1/2/3/4 set
}
_DEBIAN_DATES_NEWEST_FIRST = tuple(
    sorted(DEBIAN_BUILDS.keys(), reverse=True)
)

DEBIAN = OSImage(
    name="debian",
    pretty="Debian",
    bake_backend="debian",
    versions=_DEBIAN_DATES_NEWEST_FIRST,
    # `stable`: 20231109 is the last "all 4 Pi models on bookworm"
    # build. The 20231111 trixie build is Pi-4-only and untested
    # by pi-bake. Bump when raspi.debian.net publishes a newer
    # tested set.
    stable_version="20231109",
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
        "Alpine for Pi 5. Run `pi-bake list-os-versions --os debian` "
        "for the menu."
    ),
)


def debian_url(version: str, rpi_n: str) -> str:
    """Build the raspi.debian.net tested-build URL.

    `version` is a YYYYMMDD date string in DEBIAN_BUILDS. Codename
    is looked up from the catalog (the upstream filename embeds
    it). Raises KeyError on unknown dates.
    """
    if version not in DEBIAN_BUILDS:
        raise KeyError(
            f"unknown debian version {version!r}; "
            f"known: {', '.join(_DEBIAN_DATES_NEWEST_FIRST)}"
        )
    codename = DEBIAN_BUILDS[version]
    return (
        f"https://raspi.debian.net/tested/"
        f"{version}_raspi_{rpi_n}_{codename}.img.xz"
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
    # specifics). Each catalog entry is `<release>-<respin>`; the
    # resolver derives `{minor_version}` (the release) from the
    # leading number. Bump on each Fedora release (~6mo cadence).
    versions=("43-1.6", "42-1.1"),
    # `stable`: 43-1.6 is current Fedora Server. No known userconf-
    # equivalent regression — Fedora's cloud-init NoCloud flow is
    # stable across releases.
    stable_version="43-1.6",
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
        "enhancement (see fedora.py docstring + ROADMAP). Run "
        "`pi-bake list-os-versions --os fedora` for the menu."
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

    `version=None` (or empty string) means "use latest known-good for
    this OS" -> versions[0]. `version="stable"` resolves to the
    OS's curated stable_version (or versions[0] if unset).
    `board_slug` (e.g. "pi-5") is used by URL templates that
    encode the Pi model in the filename (Debian's raspi.debian.net
    convention). Default empty for back-compat — Alpine ignores it.
    Returns `(OSImage, resolved_version, url)`.
    """
    os_ = get_os(os_name)
    if version is None or version == "":
        version = os_.latest()
    if version == "stable":
        version = os_.stable()
    # `latest` is special only for Raspbian — Pi OS publishes a
    # permanent-redirect endpoint that downloads.fetch() follows
    # via 30x. Every other backend has no upstream "latest" alias,
    # so the sentinel resolves to the catalog's newest entry
    # (which pi-bake's maintainers bump on each release).
    if version == "latest" and os_.bake_backend != "raspbian":
        version = os_.latest()
    if version not in os_.versions:
        # Allow operator to force a version not in the catalog — they
        # might know a newer point release exists. CLI surfaces a warning.
        pass

    download_version = version

    # Alpine `edge`: no RPi tarball exists on edge, but in ext4 mode
    # we don't need one — alpine_ext4.py bootstraps from upstream
    # apk repositories directly. We still return a URL for the
    # latest stable tarball so the field is populated (callers that
    # check it for `tarball` image_kind get a valid value); the
    # ext4 backend ignores the URL and points apk at edge repos
    # itself. The diskless backend never sees `edge` because
    # recipe.py validation rejects it.
    if os_.name == "alpine" and download_version == "edge":
        # Latest stable in the catalog tuple, skipping `edge` itself.
        stable_versions = tuple(
            v for v in os_.versions if v != "edge" and "." in v
        )
        download_version = stable_versions[0] if stable_versions else version

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
    # Backend-specific URL dispatch for dated catalogs.
    # Raspbian: `latest` -> permanent-redirect URL (template); any
    # other version -> raspbian_url() builds the dated path.
    if os_.bake_backend == "raspbian" and download_version != "latest":
        url = raspbian_url(download_version, url_arch)
    elif os_.bake_backend == "debian":
        url = debian_url(download_version, rpi_n)
    else:
        url = os_.url_template.format(
            version=download_version,
            minor_version=minor,
            arch=url_arch,
            board="?",   # only used by old templates; ignored otherwise
            rpi_n=rpi_n,
        )
    return os_, version, url
