"""alpine_edge module tests — bake-time edge kernel upgrade.

Coverage focus: the `check_requirements()` skip-paths. Each
missing prereq must surface a clear `EdgeKernelSkipped` error
with actionable install hints — operators trying to use
`os_version: edge` should know exactly what to fix when the
upgrade is skipped.

NOT covered here: the actual chroot+qemu apk-upgrade run.
That needs root + qemu-user-static + binfmt_misc + network
+ a fully-extracted Alpine RPi tarball. Verified manually
when the operator runs an edge bake on a host that has those
deps (see alpine_edge.py docstring for the install path).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from pi_bake import alpine_edge
from pi_bake.alpine_edge import EdgeKernelSkipped, check_requirements


def test_skip_when_not_root(monkeypatch):
    """Most CI / dev bakes run as non-root. The error must tell
    the operator they need sudo + name the alternative (LXC)."""
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    with pytest.raises(EdgeKernelSkipped, match="requires root"):
        check_requirements()


def test_skip_when_qemu_static_missing(monkeypatch):
    """qemu-user-static install is the most common gap. The error
    must name the package + give per-distro install commands."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    # Real `which` finds chroot on every Linux; selectively block
    # qemu-aarch64-static.
    real_which = shutil.which
    def mock_which(name):
        return None if name == "qemu-aarch64-static" else real_which(name)
    monkeypatch.setattr(shutil, "which", mock_which)
    with pytest.raises(EdgeKernelSkipped, match="qemu-aarch64-static"):
        check_requirements()


def test_skip_when_qemu_static_missing_armhf(monkeypatch):
    """Pi Zero W (armhf) gets a different qemu binary name —
    confirm the error message reflects the target arch."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(shutil, "which",
                        lambda n: None if "qemu" in n else "/bin/foo")
    with pytest.raises(EdgeKernelSkipped, match="qemu-arm-static"):
        check_requirements(target_arch="arm")


def test_skip_when_binfmt_not_registered(monkeypatch, tmp_path):
    """Sometimes qemu-user-static is installed but binfmt_misc
    isn't auto-registered (depends on distro packaging — Debian
    needs `binfmt-support` separate from `qemu-user-static`).
    Error must say so + give the systemd-binfmt fix."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
    # Point binfmt_path at an empty tmp dir so the file doesn't exist.
    real_open = Path.open
    monkeypatch.setattr(
        Path, "is_file",
        lambda self: False if "binfmt_misc" in str(self) else True,
    )
    with pytest.raises(EdgeKernelSkipped, match="binfmt_misc not registered"):
        check_requirements()


def test_skip_when_binfmt_registered_but_disabled(monkeypatch, tmp_path):
    """binfmt_misc entries can be registered but disabled (e.g.
    after a sysctl or manual `echo 0 > .../qemu-aarch64`).
    Error must distinguish from 'not registered' + give the enable
    incantation."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
    # Stub Path.is_file to claim the binfmt file exists.
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    # Stub Path.read_text to return a binfmt entry WITHOUT "enabled".
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **kw: (
            "disabled\ninterpreter /usr/bin/qemu-aarch64-static\n"
            if "binfmt_misc" in str(self) else ""
        ),
    )
    with pytest.raises(EdgeKernelSkipped, match="not.*enabled"):
        check_requirements()


def test_passes_when_all_deps_present(monkeypatch):
    """Sanity: when everything's there, check_requirements is silent."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, *a, **kw: (
            "enabled\ninterpreter /usr/bin/qemu-aarch64-static\n"
            if "binfmt_misc" in str(self) else ""
        ),
    )
    # No exception
    check_requirements()


def test_constants_well_formed():
    """Catch typos in the edge repo URLs + package list."""
    assert alpine_edge.EDGE_REPO_MAIN.endswith("/edge/main")
    assert alpine_edge.EDGE_REPO_COMMUNITY.endswith("/edge/community")
    assert "linux-rpi" in alpine_edge.EDGE_UPGRADE_PACKAGES
    assert "linux-firmware-rpi" in alpine_edge.EDGE_UPGRADE_PACKAGES
    assert "mkinitfs" in alpine_edge.EDGE_UPGRADE_PACKAGES
