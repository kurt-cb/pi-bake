"""alpine_pxe backend — unit tests that don't need network.

The full bake-and-deploy flow is operator-side; these tests cover
the pieces we can exercise locally:
  - cmdline.txt construction follows the 2026-05-27 gotchas
    (alpine_repo URL has no /arch suffix; ip=dhcp present; URLs
    point at pxe_server_url's base)
  - config.txt has kernel/initramfs paths under boot/
  - apkovl produced via alpine._write_apkovl gets the
    explicit_eth0_dhcp flag (verified via the apkovl machinery's
    /etc/network/interfaces output)
"""
from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from pi_bake import alpine, alpine_pxe
from pi_bake.config import NodeConfig


_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItesting test@host"


def _node() -> NodeConfig:
    return NodeConfig(hostname="td-cm4", ssh_pubkey=_PUBKEY)


# --------------------------------------------------------------------------- #
# alpine._write_apkovl gained `explicit_eth0_dhcp` — verify it writes the     #
# eth0 dhcp block the PXE backend will rely on.                                #
# --------------------------------------------------------------------------- #


def _extract_member(tarball: Path, member: str) -> str:
    """Read a single member out of an apkovl tar.gz and return text."""
    with tarfile.open(tarball, "r:gz") as tf:
        f = tf.extractfile(member)
        assert f is not None
        return f.read().decode()


def test_apkovl_dhcp_default_has_lo_only(tmp_path):
    """Existing diskless behavior: dhcp node => only `auto lo` in
    /etc/network/interfaces (dhcpcd manages eth0 as a daemon)."""
    out = tmp_path / "ovl.tar.gz"
    alpine._write_apkovl(out, _node())
    interfaces = _extract_member(out, "etc/network/interfaces")
    assert "auto lo" in interfaces
    assert "auto eth0" not in interfaces


def test_apkovl_explicit_eth0_dhcp_adds_eth0_block(tmp_path):
    """PXE mode: explicit_eth0_dhcp=True => `auto eth0 inet dhcp`
    block joins `auto lo`. Critical for pure-PXE because dhcpcd
    alone doesn't reliably bring up eth0 in time (proven hands-on
    2026-05-27)."""
    out = tmp_path / "ovl.tar.gz"
    alpine._write_apkovl(out, _node(), explicit_eth0_dhcp=True)
    interfaces = _extract_member(out, "etc/network/interfaces")
    assert "auto lo" in interfaces
    assert "auto eth0" in interfaces
    assert "iface eth0 inet dhcp" in interfaces


def test_apkovl_static_ip_unaffected_by_explicit_eth0_dhcp(tmp_path):
    """Static IP always wins — the explicit_eth0_dhcp flag is
    irrelevant when has_static_ip is true (static block already
    includes `auto eth0`)."""
    node = NodeConfig(
        hostname="td-cm4", ssh_pubkey=_PUBKEY,
        static_ipv4="192.168.4.51/24", gateway_ipv4="192.168.4.1",
    )
    out = tmp_path / "ovl.tar.gz"
    alpine._write_apkovl(out, node, explicit_eth0_dhcp=True)
    interfaces = _extract_member(out, "etc/network/interfaces")
    assert "iface eth0 inet static" in interfaces
    assert "192.168.4.51" in interfaces
    # No dhcp block should sneak in.
    assert "inet dhcp" not in interfaces


# --------------------------------------------------------------------------- #
# alpine_pxe.bake — cmdline.txt + config.txt construction.                     #
# Network parts (extracting upstream tarball + apk-fetch) are                  #
# integration tests; these stub the tarball extraction.                        #
# --------------------------------------------------------------------------- #


def _stub_tarball(tmp_path: Path) -> Path:
    """Build a minimal tarball that LOOKS like an Alpine RPi tarball
    (boot/vmlinuz-rpi + boot/initramfs-rpi + apks/<arch>/) so
    alpine_pxe.bake's tarfile extraction succeeds without us
    hitting the network."""
    src = tmp_path / "fake-tar-src"
    src.mkdir()
    (src / "boot").mkdir()
    (src / "boot" / "vmlinuz-rpi").write_bytes(b"fake-kernel")
    (src / "boot" / "initramfs-rpi").write_bytes(b"fake-initramfs")
    (src / "boot" / "modloop-rpi").write_bytes(b"fake-modloop")
    (src / "apks").mkdir()
    (src / "apks" / "aarch64").mkdir()
    (src / "bootcode.bin").write_bytes(b"fake-firmware")
    (src / "config.txt").write_text("# stock alpine config\n")
    (src / "cmdline.txt").write_text("# stock alpine cmdline\n")
    tarball = tmp_path / "alpine-rpi-stub.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        for item in sorted(src.rglob("*")):
            tf.add(item, arcname=str(item.relative_to(src)))
    return tarball


def test_bake_writes_correct_cmdline(tmp_path, monkeypatch):
    """cmdline.txt has all four critical bits:
      - ip=dhcp
      - apkovl=<server>/<host>.apkovl.tar.gz
      - alpine_repo=<server>/apks (NO trailing /arch — gotcha #1)
      - modules + console
    """
    # Stub `fetch()` to return our fake tarball instead of hitting
    # dl-cdn.alpinelinux.org.
    fake_tarball = _stub_tarball(tmp_path)
    monkeypatch.setattr(
        "pi_bake.alpine_pxe.fetch", lambda url: fake_tarball,
    )

    out_dir = tmp_path / "tftp-out"
    alpine_pxe.bake(
        url="https://example/alpine.tar.gz",
        node=_node(),
        out_path=out_dir,
        pxe_server_url="http://192.168.4.2/td-cm4/",  # trailing slash
        alpine_branch="v3.21",
    )
    cmdline = (out_dir / "cmdline.txt").read_text()
    assert "ip=dhcp" in cmdline
    assert "apkovl=http://192.168.4.2/td-cm4/td-cm4.apkovl.tar.gz" in cmdline
    # GOTCHA #1 — alpine_repo must NOT include the arch suffix.
    # The server URL had a trailing slash; we strip it.
    assert "alpine_repo=http://192.168.4.2/td-cm4/apks" in cmdline
    assert "alpine_repo=http://192.168.4.2/td-cm4/apks/aarch64" not in cmdline
    # Console flags so operator can plug into UART for triage.
    assert "console=tty1" in cmdline
    assert "console=serial0,115200" in cmdline
    # No `quiet` — we WANT kernel boot messages visible.
    assert "quiet" not in cmdline


def test_bake_writes_correct_config_txt(tmp_path, monkeypatch):
    """config.txt has kernel + initramfs paths under boot/,
    arm_64bit=1, enable_uart=1, and include usercfg.txt."""
    fake_tarball = _stub_tarball(tmp_path)
    monkeypatch.setattr(
        "pi_bake.alpine_pxe.fetch", lambda url: fake_tarball,
    )
    out_dir = tmp_path / "tftp-out"
    alpine_pxe.bake(
        url="https://example/alpine.tar.gz",
        node=_node(),
        out_path=out_dir,
        pxe_server_url="http://192.168.4.2/td-cm4",
    )
    config = (out_dir / "config.txt").read_text()
    assert "arm_64bit=1" in config
    assert "enable_uart=1" in config
    assert "kernel=boot/vmlinuz-rpi" in config
    assert "initramfs boot/initramfs-rpi" in config
    assert "include usercfg.txt" in config


def test_bake_creates_usercfg_txt_with_operator_overlays(tmp_path, monkeypatch):
    """Operator's config_txt: lines land in usercfg.txt — same
    pattern as the diskless backend (config.txt unchanged, additions
    layer cleanly via include)."""
    fake_tarball = _stub_tarball(tmp_path)
    monkeypatch.setattr(
        "pi_bake.alpine_pxe.fetch", lambda url: fake_tarball,
    )
    node = NodeConfig(
        hostname="td-cm4", ssh_pubkey=_PUBKEY,
        config_txt=["dtparam=pciex1", "dtoverlay=mcp2515-can0,oscillator=12000000"],
    )
    out_dir = tmp_path / "tftp-out"
    alpine_pxe.bake(
        url="https://example/alpine.tar.gz",
        node=node, out_path=out_dir,
        pxe_server_url="http://lab/td-cm4",
    )
    usercfg = (out_dir / "usercfg.txt").read_text()
    assert "dtparam=pciex1" in usercfg
    assert "dtoverlay=mcp2515-can0,oscillator=12000000" in usercfg


def test_bake_writes_apkovl_with_explicit_eth0_dhcp(tmp_path, monkeypatch):
    """The pxe backend MUST pass explicit_eth0_dhcp=True to
    _write_apkovl. Verify by inspecting the apkovl's interfaces."""
    fake_tarball = _stub_tarball(tmp_path)
    monkeypatch.setattr(
        "pi_bake.alpine_pxe.fetch", lambda url: fake_tarball,
    )
    out_dir = tmp_path / "tftp-out"
    alpine_pxe.bake(
        url="https://example/alpine.tar.gz",
        node=_node(), out_path=out_dir,
        pxe_server_url="http://lab/td-cm4",
    )
    apkovl = out_dir / "td-cm4.apkovl.tar.gz"
    assert apkovl.is_file()
    interfaces = _extract_member(apkovl, "etc/network/interfaces")
    assert "auto eth0" in interfaces, (
        "pxe backend must opt apkovl into explicit_eth0_dhcp — see "
        "feature_request.md gotcha #2"
    )
    assert "iface eth0 inet dhcp" in interfaces


def test_bake_extracts_tarball_into_output_dir(tmp_path, monkeypatch):
    """Output dir gets the tarball contents (kernel, initramfs,
    firmware blobs) — these are what TFTP serves to PXE clients."""
    fake_tarball = _stub_tarball(tmp_path)
    monkeypatch.setattr(
        "pi_bake.alpine_pxe.fetch", lambda url: fake_tarball,
    )
    out_dir = tmp_path / "tftp-out"
    alpine_pxe.bake(
        url="https://example/alpine.tar.gz",
        node=_node(), out_path=out_dir,
        pxe_server_url="http://lab/td-cm4",
    )
    assert (out_dir / "boot" / "vmlinuz-rpi").is_file()
    assert (out_dir / "boot" / "initramfs-rpi").is_file()
    assert (out_dir / "boot" / "modloop-rpi").is_file()
    assert (out_dir / "bootcode.bin").is_file()
    assert (out_dir / "apks" / "aarch64").is_dir()


def test_bake_normalizes_perms_to_world_readable(tmp_path, monkeypatch):
    """Regression — 2026-05-27 PXE boot failure. The Alpine RPi
    tarball ships some files at mode 0o600 (notably
    boot/initramfs-rpi). Python's `data` extraction filter
    preserves recorded modes, so the baked tree had 600-mode
    files owned by the operator. dnsmasq-tftp + nginx (which
    run as non-operator users on the lab host) couldn't read
    them → TFTP "failed sending" + HTTP 403 + kernel panic
    because the initramfs never reached the Pi.

    Fix: chmod the whole output tree to world-readable
    (a+r on files, a+rX on dirs)."""
    # Build a tarball where one file has restrictive 0o600 mode.
    src = tmp_path / "src"
    src.mkdir()
    (src / "boot").mkdir()
    secret = src / "boot" / "initramfs-rpi"
    secret.write_bytes(b"fake-initramfs")
    secret.chmod(0o600)
    (src / "apks").mkdir()
    (src / "apks" / "aarch64").mkdir()
    (src / "bootcode.bin").write_bytes(b"fake-firmware")
    tarball = tmp_path / "alpine-stub.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        for item in sorted(src.rglob("*")):
            tf.add(item, arcname=str(item.relative_to(src)))
    monkeypatch.setattr("pi_bake.alpine_pxe.fetch", lambda url: tarball)

    out_dir = tmp_path / "out"
    alpine_pxe.bake(
        url="https://example/alpine.tar.gz",
        node=_node(), out_path=out_dir,
        pxe_server_url="http://lab/td-cm4",
    )
    # The 600-mode file from the tarball MUST be world-readable
    # in the output tree, else dnsmasq-tftp + nginx can't serve it.
    out_initramfs = out_dir / "boot" / "initramfs-rpi"
    mode = out_initramfs.stat().st_mode & 0o777
    assert mode & 0o044, (
        f"initramfs perms {oct(mode)} not world-readable — "
        f"dnsmasq-tftp/nginx will get permission-denied"
    )


def test_bake_strips_server_url_trailing_slash(tmp_path, monkeypatch):
    """pxe_server_url with trailing slash gets normalized so cmdline
    templates concat predictably (no double-slashes)."""
    fake_tarball = _stub_tarball(tmp_path)
    monkeypatch.setattr(
        "pi_bake.alpine_pxe.fetch", lambda url: fake_tarball,
    )
    out_dir = tmp_path / "tftp-out"
    alpine_pxe.bake(
        url="https://example/alpine.tar.gz",
        node=_node(), out_path=out_dir,
        pxe_server_url="http://lab/td-cm4///",
    )
    cmdline = (out_dir / "cmdline.txt").read_text()
    # No double-slash anywhere in the URLs (other than after http:).
    assert "http://lab/td-cm4///" not in cmdline
    assert "http://lab/td-cm4//" not in cmdline
    assert "apkovl=http://lab/td-cm4/td-cm4.apkovl.tar.gz" in cmdline
    assert "alpine_repo=http://lab/td-cm4/apks" in cmdline
