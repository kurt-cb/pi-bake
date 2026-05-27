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


# --------------------------------------------------------------------------- #
# read_partition_layout — sfdisk -d parser                                     #
# --------------------------------------------------------------------------- #


def _fake_sfdisk_output(monkeypatch, stdout_text):
    """Monkey-patch imgxz._sudo to return a CompletedProcess with
    the supplied stdout. Lets us exercise the parser without
    actually running sfdisk."""
    import subprocess
    import pi_bake.imgxz as imgxz_mod

    def fake_sudo(*args, capture=True):
        return subprocess.CompletedProcess(
            args=list(args), returncode=0,
            stdout=stdout_text, stderr="",
        )

    monkeypatch.setattr(imgxz_mod, "_sudo", fake_sudo)


def test_read_partition_layout_real_sfdisk_format(monkeypatch, tmp_path):
    """Real sfdisk -d output has padded numbers + space before the
    colon. Both must parse cleanly. (Regression test for the
    parser bug that swallowed every partition on first attempt.)"""
    from pi_bake.imgxz import read_partition_layout

    sample = (
        "label: dos\n"
        "label-id: 0xd50ebe3f\n"
        "device: /tmp/x.img\n"
        "unit: sectors\n"
        "sector-size: 512\n"
        "\n"
        "/tmp/x.img1 : start=        2048, size=       10240, type=c\n"
        "/tmp/x.img2 : start=       12288, size=       20480, type=83\n"
    )
    _fake_sfdisk_output(monkeypatch, sample)
    parts = read_partition_layout(tmp_path / "any.img")
    assert parts == [
        (1, 2048 * 512, 10240 * 512),
        (2, 12288 * 512, 20480 * 512),
    ]


def test_read_partition_layout_handles_bootable_flag(monkeypatch, tmp_path):
    """`bootable` is a bare token (no =value), must not confuse parser."""
    from pi_bake.imgxz import read_partition_layout

    sample = (
        "label: dos\n"
        "device: /tmp/x.img\n"
        "unit: sectors\n"
        "\n"
        "/tmp/x.img1 : start=2048, size=524288, type=c, bootable\n"
        "/tmp/x.img2 : start=526336, size=3670016, type=83\n"
    )
    _fake_sfdisk_output(monkeypatch, sample)
    parts = read_partition_layout(tmp_path / "any.img")
    assert len(parts) == 2
    assert parts[0] == (1, 2048 * 512, 524288 * 512)


def test_read_partition_layout_empty_table(monkeypatch, tmp_path):
    """No partition lines → empty result, no crash."""
    from pi_bake.imgxz import read_partition_layout

    sample = "label: dos\ndevice: /tmp/x.img\nunit: sectors\n\n"
    _fake_sfdisk_output(monkeypatch, sample)
    assert read_partition_layout(tmp_path / "any.img") == []


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
