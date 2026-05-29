"""Board + OS catalog lookups and the (board, os) support matrix."""
from __future__ import annotations

import pytest

from pi_bake.bake import supports
from pi_bake.boards import BOARDS, get_board, list_boards
from pi_bake.oses import (
    DEBIAN_BUILDS, OSES, RASPBIAN_BUILDS,
    debian_url, get_os, list_oses, raspbian_url, resolve_image,
)


def test_every_board_has_some_os_support():
    """Every board in the catalog must be reachable by at least one
    OS — otherwise it shouldn't be in the list."""
    for b in list_boards():
        oses_for_board = list_oses(board=b.name)
        assert oses_for_board, f"board {b.name!r} has no OS support"


def test_board_alias_resolves():
    """`pi5` should resolve to `pi-5`."""
    assert get_board("pi5").name == "pi-5"
    assert get_board("pi-5").name == "pi-5"
    assert get_board("PI-5").name == "pi-5"


def test_unknown_board_raises():
    with pytest.raises(KeyError, match="unknown board"):
        get_board("not-a-pi")


def test_os_synonym_resolves():
    """`rpi-os` and `raspberry-pi-os` should both resolve to `raspbian`."""
    assert get_os("raspbian").name == "raspbian"
    assert get_os("rpi-os").name == "raspbian"
    assert get_os("raspberry-pi-os").name == "raspbian"


def test_alpine_supports_pi_zero_aarch64():
    assert supports("pi-zero-2-w", "alpine")
    assert supports("pi-5", "alpine")     # 3.21+ does
    assert supports("pi-zero-w", "alpine")  # armhf


def test_raspbian_does_not_support_pi_zero_w():
    """Original Pi Zero W is armv6 only — Raspberry Pi OS arm64 won't run."""
    assert not supports("pi-zero-w", "raspbian")


def test_debian_supports_pi_3_and_4_only():
    """raspi.debian.net has tested builds for Pi 1/2/3/4 on
    bookworm but no Pi 5 tested image (2026-05). pi-bake's
    catalog reflects that gap."""
    assert supports("pi-3", "debian")
    assert supports("pi-4", "debian")
    assert not supports("pi-5", "debian")
    assert not supports("pi-zero-2-w", "debian")


def test_fedora_supports_pi_4_and_5_only():
    """Fedora's generic aarch64 image targets Pi 4 + Pi 5 (need
    arm-image-installer for the Pi-bootloader shim; pi-bake's
    Fedora backend produces the configured rootfs but caveat
    applies — see fedora.py module docstring)."""
    assert supports("pi-4", "fedora")
    assert supports("pi-5", "fedora")
    assert not supports("pi-3", "fedora")
    assert not supports("pi-zero-2-w", "fedora")


def test_fedora_in_os_catalog():
    """Fedora must be discoverable via the catalog API."""
    os_ = get_os("fedora")
    assert os_.bake_backend == "fedora"
    assert os_.image_kind == "img_xz"
    assert "Server-Host-Generic" in os_.url_template
    # Latest hardcoded version is current Fedora (Fedora 43 at the
    # time of writing). Bump in oses.py when newer Fedora ships;
    # this just ensures we have SOMETHING in versions.
    assert os_.latest()


def test_resolve_image_debian_uses_board_slug():
    """Debian URL encodes the Pi model number; resolve_image
    must consume the board_slug arg to fill it in."""
    _, _, url_pi4 = resolve_image(
        "debian", None, "aarch64", board_slug="pi-4",
    )
    _, _, url_pi3 = resolve_image(
        "debian", None, "aarch64", board_slug="pi-3",
    )
    assert "raspi_4" in url_pi4
    assert "raspi_3" in url_pi3
    assert "raspi_5" not in url_pi4


def test_resolve_image_fedora_uses_major_for_minor_version():
    """Fedora URLs interpolate the major release (`43`) into the
    path even though `version` carries the full point-release
    (`43-1.6`). minor_version derivation needs the Fedora-specific
    split on `-`."""
    _, _, url = resolve_image("fedora", "43-1.6", "aarch64")
    assert "/releases/43/" in url
    assert "Fedora-Server-Host-Generic-43-1.6.aarch64.raw.xz" in url


def test_resolve_image_uses_latest_when_version_omitted():
    os_, version, url = resolve_image("alpine", None, "aarch64")
    assert version == os_.latest()
    assert "alpine-rpi-" in url
    assert "aarch64" in url


def test_resolve_image_substitutes_arch():
    """Raspbian URL uses `arm64`, not `aarch64`."""
    _, _, url = resolve_image("raspbian", None, "aarch64")
    assert "arm64" in url
    assert "aarch64" not in url


def test_resolve_image_uses_minor_for_alpine():
    """Alpine URL needs both 3.21 (minor) and 3.21.4 (full) interpolated."""
    _, _, url = resolve_image("alpine", "3.21.4", "aarch64")
    assert "/v3.21/" in url
    assert "alpine-rpi-3.21.4-aarch64.tar.gz" in url


def test_alpine_edge_in_catalog_resolves_to_stable_url():
    """`edge` is in the ALPINE.versions tuple so resolve_image
    accepts it, but the URL falls back to the latest stable RPi
    tarball — Alpine ships no edge tarball, and the ext4 backend
    doesn't use the URL anyway (it bootstraps via apk-tools-static
    against upstream repos). The returned version stays 'edge' so
    downstream code can branch on it."""
    os_, version, url = resolve_image("alpine", "edge", "aarch64")
    assert version == "edge"
    assert "edge" not in url
    assert "alpine-rpi-3." in url


def test_alpine_edge_present_in_catalog():
    alpine = get_os("alpine")
    assert "edge" in alpine.versions
    # latest() returns versions[0] which must be a stable point
    # release — `edge` is never the default.
    assert alpine.latest() != "edge"
    assert "." in alpine.latest()


# ----- `stable` sentinel + dated catalog support (v0.4) -----


def test_every_os_has_a_stable_version():
    """`stable` is the recommended on-ramp for new bakes — every
    OS in the catalog must resolve `stable` to a concrete version."""
    for o in OSES:
        assert o.stable(), f"{o.name} has no stable version"
        # stable_version (when explicit) or versions[0] (fallback) —
        # either way must be a real entry in the catalog.
        assert o.stable() in o.versions


def test_stable_sentinel_resolves_per_os():
    """`stable` -> the OS's curated known-good version."""
    _, v, _ = resolve_image("raspbian", "stable", "aarch64", "pi-5")
    assert v == "2025-05-13"
    _, v, _ = resolve_image("alpine", "stable", "aarch64")
    assert v == "3.21.4"
    _, v, _ = resolve_image("debian", "stable", "aarch64", "pi-4")
    assert v == "20231109"
    _, v, _ = resolve_image("fedora", "stable", "aarch64")
    assert v == "43-1.6"


def test_raspbian_latest_uses_permanent_redirect():
    """For Raspbian, `latest` is THE sentinel that follows Pi OS's
    permanent-redirect endpoint (the rest is interpolation)."""
    _, v, url = resolve_image("raspbian", "latest", "aarch64", "pi-5")
    assert v == "latest"
    assert url == (
        "https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
    )


def test_non_raspbian_latest_resolves_to_catalog_newest():
    """Alpine / Debian / Fedora have no upstream `latest` alias —
    the sentinel falls back to versions[0]."""
    _, v, _ = resolve_image("alpine", "latest", "aarch64")
    assert v == "3.21.4"
    _, v, _ = resolve_image("debian", "latest", "aarch64", "pi-4")
    assert v == "20231111"
    _, v, _ = resolve_image("fedora", "latest", "aarch64")
    assert v == "43-1.6"


def test_raspbian_dated_url_includes_codename():
    """Each Raspbian dated build has a codename (trixie/bookworm)
    that gets baked into the filename — operator picks the date,
    raspbian_url() looks up the codename."""
    url = raspbian_url("2025-05-13", "arm64")
    assert "raspios_lite_arm64-2025-05-13" in url
    assert "2025-05-13-raspios-bookworm-arm64-lite.img.xz" in url
    url = raspbian_url("2026-04-21", "arm64")
    assert "2026-04-21-raspios-trixie-arm64-lite.img.xz" in url


def test_raspbian_dated_url_handles_off_by_one_file_date():
    """Some Pi OS directory dates differ from the file's build
    date by one day (release-pipeline quirk). The catalog encodes
    both so the URL hits a real file."""
    # 2025-10-02 directory contains 2025-10-01-built file.
    url = raspbian_url("2025-10-02", "arm64")
    assert "raspios_lite_arm64-2025-10-02/" in url   # directory
    assert "2025-10-01-raspios-trixie" in url        # file date


def test_raspbian_unknown_version_raises_with_menu():
    """Operator-friendly error: lists known versions."""
    with pytest.raises(KeyError, match="unknown raspbian version"):
        raspbian_url("9999-99-99", "arm64")


def test_debian_dated_url_includes_codename():
    """Debian's tested-build filename embeds the codename;
    debian_url() looks it up from DEBIAN_BUILDS."""
    url = debian_url("20231109", "4")
    assert "20231109_raspi_4_bookworm.img.xz" in url
    url = debian_url("20231111", "4")
    assert "20231111_raspi_4_trixie.img.xz" in url


def test_debian_unknown_version_raises():
    with pytest.raises(KeyError, match="unknown debian version"):
        debian_url("19990101", "4")


def test_raspbian_versions_catalog_starts_with_latest_sentinel():
    """Order matters: the `latest` sentinel must be versions[0] so
    `latest()` returns it (Raspbian's special-case behavior)."""
    raspbian = get_os("raspbian")
    assert raspbian.versions[0] == "latest"
    assert raspbian.latest() == "latest"


def test_catalog_dated_builds_are_well_formed():
    """Every catalog entry round-trips through its URL builder."""
    for date in RASPBIAN_BUILDS:
        url = raspbian_url(date, "arm64")
        codename, file_date = RASPBIAN_BUILDS[date]
        assert codename in url
        assert file_date in url
    for date in DEBIAN_BUILDS:
        url = debian_url(date, "4")
        assert DEBIAN_BUILDS[date] in url
