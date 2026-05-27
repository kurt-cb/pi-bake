"""bake.build() dispatch tests — make sure os_mode routes to the
right backend without actually invoking the bake.

The bake itself needs sudo + network + significant time, so these
tests stub out the backend's `bake` function and just verify the
dispatcher selects + calls the expected module.
"""
from __future__ import annotations

import pytest

from pi_bake import bake as bake_mod
from pi_bake.config import NodeConfig


_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItesting test@host"


def _node() -> NodeConfig:
    return NodeConfig(hostname="pi-test", ssh_pubkey=_PUBKEY)


# --------------------------------------------------------------------------- #
# Dispatch routing                                                             #
# --------------------------------------------------------------------------- #


def test_alpine_default_dispatches_to_diskless_backend(monkeypatch, tmp_path):
    """os_mode='' (default) routes to pi_bake.alpine.bake."""
    called = {}

    def fake_alpine_bake(**kwargs):
        called["alpine"] = kwargs
        return tmp_path / "out.img.gz"

    import pi_bake.alpine
    monkeypatch.setattr(pi_bake.alpine, "bake", fake_alpine_bake)

    bake_mod.build(
        board="pi-5", os_name="alpine", version="3.21.4",
        node=_node(), out_path=tmp_path / "out.img.gz",
    )
    assert "alpine" in called
    # The diskless dispatch passes `url` (tarball URL) and
    # `alpine_branch` — ext4 dispatch does not.
    assert "url" in called["alpine"]
    assert "alpine_branch" in called["alpine"]


def test_alpine_ext4_dispatches_to_ext4_backend(monkeypatch, tmp_path):
    """os_mode='ext4' routes to pi_bake.alpine_ext4.bake."""
    called = {}

    def fake_ext4_bake(**kwargs):
        called["ext4"] = kwargs
        return tmp_path / "out.img.xz"

    import pi_bake.alpine_ext4
    monkeypatch.setattr(pi_bake.alpine_ext4, "bake", fake_ext4_bake)

    bake_mod.build(
        board="pi-5", os_name="alpine", version="3.21.4",
        node=_node(), out_path=tmp_path / "out.img.xz",
        os_mode="ext4",
    )
    assert "ext4" in called
    # ext4 dispatch passes `alpine_version` (not `alpine_branch`) +
    # `arch`.
    assert called["ext4"]["alpine_version"] == "3.21.4"
    assert called["ext4"]["arch"] == "aarch64"
    # ext4 dispatch does NOT pass `url` (ext4 bootstraps from
    # upstream repos directly, no tarball).
    assert "url" not in called["ext4"]


def test_alpine_edge_in_ext4_dispatches(monkeypatch, tmp_path):
    """version='edge' + os_mode='ext4' routes to ext4 backend with
    alpine_version='edge' so the backend writes edge repos."""
    called = {}

    def fake_ext4_bake(**kwargs):
        called["ext4"] = kwargs
        return tmp_path / "out.img.xz"

    import pi_bake.alpine_ext4
    monkeypatch.setattr(pi_bake.alpine_ext4, "bake", fake_ext4_bake)

    bake_mod.build(
        board="pi-5", os_name="alpine", version="edge",
        node=_node(), out_path=tmp_path / "out.img.xz",
        os_mode="ext4",
    )
    assert called["ext4"]["alpine_version"] == "edge"


def test_alpine_edge_in_diskless_rejected(tmp_path):
    """version='edge' + diskless mode raises before any backend
    is dispatched (CLI users who bypass Recipe get the same guard)."""
    with pytest.raises(ValueError, match="edge is not supported"):
        bake_mod.build(
            board="pi-5", os_name="alpine", version="edge",
            node=_node(), out_path=tmp_path / "out.img.gz",
        )


def test_os_mode_rejected_for_non_alpine(tmp_path):
    """os_mode is alpine-only at the bake.build() level too."""
    with pytest.raises(ValueError, match="os_mode is only meaningful"):
        bake_mod.build(
            board="pi-5", os_name="raspbian", version=None,
            node=_node(), out_path=tmp_path / "out.img.xz",
            os_mode="ext4",
        )


def test_unsupported_board_os_combo_rejected(tmp_path):
    """Pre-flight check before edge/os_mode validation."""
    with pytest.raises(ValueError, match="doesn't support"):
        bake_mod.build(
            board="pi-zero-w", os_name="raspbian", version=None,
            node=_node(), out_path=tmp_path / "out.img.xz",
        )


def test_alpine_pxe_dispatches_to_pxe_backend(monkeypatch, tmp_path):
    """os_mode='pxe' + pxe_server_url routes to pi_bake.alpine_pxe.bake."""
    called = {}

    def fake_pxe_bake(**kwargs):
        called["pxe"] = kwargs
        out = tmp_path / "pxe-tree"
        out.mkdir(exist_ok=True)
        return out

    import pi_bake.alpine_pxe
    monkeypatch.setattr(pi_bake.alpine_pxe, "bake", fake_pxe_bake)

    bake_mod.build(
        board="pi-5", os_name="alpine", version="3.21.4",
        node=_node(), out_path=tmp_path / "pxe-tree",
        os_mode="pxe", pxe_server_url="http://192.168.4.2/td-cm4",
    )
    assert "pxe" in called
    assert called["pxe"]["pxe_server_url"] == "http://192.168.4.2/td-cm4"
    # pxe dispatch DOES pass `url` (used to fetch the Alpine RPi
    # tarball to extract — same upstream URL the diskless backend uses).
    assert "url" in called["pxe"]
    # alpine_branch is needed for the alpine_repo URL in cmdline.
    assert called["pxe"]["alpine_branch"] == "v3.21"


def test_alpine_pxe_without_server_url_rejected(tmp_path):
    """bake.build() raises if os_mode=pxe but no pxe_server_url —
    catches CLI users who bypass Recipe-level validation."""
    with pytest.raises(ValueError, match="pxe_server_url"):
        bake_mod.build(
            board="pi-5", os_name="alpine", version="3.21.4",
            node=_node(), out_path=tmp_path / "pxe-tree",
            os_mode="pxe",  # no pxe_server_url
        )
