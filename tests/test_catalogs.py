"""Board + OS catalog lookups and the (board, os) support matrix."""
from __future__ import annotations

import pytest

from pi_bake.bake import supports
from pi_bake.boards import BOARDS, get_board, list_boards
from pi_bake.oses import OSES, get_os, list_oses, resolve_image


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


def test_debian_supports_pi_4_and_5_only():
    assert supports("pi-4", "debian")
    assert supports("pi-5", "debian")
    assert not supports("pi-zero-2-w", "debian")


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
