"""imgxz module tests — partition-image-baker scaffolding.

These tests cover the host-side helpers used by the Raspbian /
Debian / Fedora backends. They avoid the parts that need sudo
(losetup, mount) — those have to be exercised by an actual
bake. See `incus exec pi-bake` in the v0.3 bring-up notes.
"""
from __future__ import annotations

import lzma
import pytest

from pi_bake.imgxz import decompress_xz


# --------------------------------------------------------------------------- #
# decompress_xz — content-sniff (BUG fix from 2026-05 PXE bring-up)            #
# --------------------------------------------------------------------------- #

def _make_xz(content: bytes, path):
    """Write `content` xz-compressed at `path`."""
    with lzma.open(path, "wb", format=lzma.FORMAT_XZ) as f:
        f.write(content)


def test_decompress_xz_with_dot_xz_suffix(tmp_path):
    """The traditional happy path: input is `<name>.img.xz`, output
    is `<name>.img` in the supplied out_dir."""
    src = tmp_path / "test.img.xz"
    _make_xz(b"image-payload", src)
    out_dir = tmp_path / "raw"
    raw = decompress_xz(src, out_dir)
    assert raw.name == "test.img"
    assert raw.read_bytes() == b"image-payload"


def test_decompress_xz_without_dot_xz_suffix(tmp_path):
    """Pi OS's `_latest` redirect saves the downloaded file with
    NO `.xz` extension (e.g. `raspios_lite_arm64_latest`). The
    earlier `endswith('.xz')` check rejected these. Now we sniff
    the xz magic bytes and decompress regardless of suffix."""
    src = tmp_path / "raspios_lite_arm64_latest"
    _make_xz(b"pi-os-payload", src)
    out_dir = tmp_path / "raw"
    raw = decompress_xz(src, out_dir)
    # Without a `.xz` suffix to strip, decompress_xz appends `.img`
    # so downstream code can recognize the file type.
    assert raw.name == "raspios_lite_arm64_latest.img"
    assert raw.read_bytes() == b"pi-os-payload"


def test_decompress_xz_rejects_non_xz(tmp_path):
    """A file that's NOT actually xz-compressed gets a clear
    ValueError, not a confusing xz-tool error. Common cause:
    upstream Pi OS / Fedora published a redirected page that
    returns an HTML 404 page instead of the image (URL went
    stale)."""
    src = tmp_path / "definitely_not_xz"
    src.write_bytes(b"<!DOCTYPE html><html>404 Not Found</html>")
    with pytest.raises(ValueError, match="doesn't look like an xz file"):
        decompress_xz(src, tmp_path / "raw")


def test_decompress_xz_idempotent(tmp_path):
    """Re-running on an already-decompressed file returns the
    cached path without re-running xz."""
    src = tmp_path / "test.img.xz"
    _make_xz(b"original", src)
    out_dir = tmp_path / "raw"
    first = decompress_xz(src, out_dir)
    # Tamper with the cached output to prove the second call
    # doesn't overwrite it.
    first.write_bytes(b"tampered")
    second = decompress_xz(src, out_dir)
    assert second == first
    assert second.read_bytes() == b"tampered"   # not re-decompressed
