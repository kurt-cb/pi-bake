"""apkfetch module — cheap (no-network) tests.

Bake-time apk-fetch's heavy paths (downloading apk-tools-static,
fetching real packages from upstream) are integration-tested by
hand against any example with `packages:` populated. The unit
tests here cover:

  - host arch detection
  - cached binary reuse semantics
  - error surfaces (unknown arch, missing tarball, missing cpio,
    apk fetch failures) so the bake fails loudly rather than
    silently regressing to "needs network on first boot"

The integration scenarios that DO touch the network are skipped
unless explicitly opted in via PI_BAKE_INTEGRATION=1.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pi_bake import apkfetch


# --------------------------------------------------------------------------- #
# host_arch                                                                    #
# --------------------------------------------------------------------------- #

def test_host_arch_returns_a_known_slug():
    """On any supported bake host, host_arch() returns one of the
    Alpine arch slugs apk-tools-static is built for."""
    arch = apkfetch.host_arch()
    assert arch in {"x86_64", "aarch64", "armv7", "x86"}


def test_host_arch_rejects_unknown(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "ppc64le")
    with pytest.raises(RuntimeError, match="unrecognized bake-host arch"):
        apkfetch.host_arch()


# --------------------------------------------------------------------------- #
# ensure_apk_static                                                            #
# --------------------------------------------------------------------------- #

def test_ensure_apk_static_returns_cached_if_present(tmp_path, monkeypatch):
    """If the binary is already on disk, ensure_apk_static() must NOT
    re-download or call tar. (Caching is the whole point of
    `~/.cache/pi-bake/apk-tools-static/`.)"""
    monkeypatch.setattr(apkfetch, "cache_dir", lambda: tmp_path)
    bin_dir = (
        tmp_path / "apk-tools-static"
        / f"{apkfetch.APK_STATIC_VERSION}-{apkfetch.host_arch()}"
    )
    bin_dir.mkdir(parents=True)
    bin_path = bin_dir / "apk.static"
    bin_path.write_bytes(b"#!/bin/sh\necho fake\n")
    bin_path.chmod(0o755)

    def _explode(*a, **kw):
        raise AssertionError("should not have downloaded")

    monkeypatch.setattr(apkfetch, "fetch", _explode)
    out = apkfetch.ensure_apk_static()
    assert out == bin_path


# --------------------------------------------------------------------------- #
# extract_initramfs_keys                                                       #
# --------------------------------------------------------------------------- #

def test_extract_initramfs_keys_missing_tarball(tmp_path):
    """Clear error when the extracted tarball directory has no
    boot/initramfs-rpi (operator pointed at the wrong dir)."""
    with pytest.raises(RuntimeError, match="initramfs-rpi not at"):
        apkfetch.extract_initramfs_keys(tmp_path, tmp_path / "out")


@pytest.mark.skipif(
    not (Path.home() / ".cache" / "pi-bake"
         / "alpine-rpi-3.21.4-aarch64.tar.gz").is_file(),
    reason="needs a cached Alpine RPi tarball; run a bake first",
)
def test_extract_initramfs_keys_pulls_alpine_pubkeys(tmp_path):
    """Real integration check: extract the tarball, then pull the
    apk pubkeys out of its initramfs. Skipped when there's no
    cached tarball to point at."""
    tarball = (Path.home() / ".cache" / "pi-bake"
               / "alpine-rpi-3.21.4-aarch64.tar.gz")
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    subprocess.run(
        ["tar", "-xzf", str(tarball), "-C", str(extracted)],
        check=True, capture_output=True,
    )
    keys_dir = apkfetch.extract_initramfs_keys(
        extracted, tmp_path / "keys_out",
    )
    assert keys_dir.is_dir()
    keys = sorted(p.name for p in keys_dir.glob("*.rsa.pub"))
    assert keys, f"no Alpine pubkeys extracted into {keys_dir}"
    # Alpine devs name them alpine-devel@lists.alpinelinux.org-XXXXXXXX.rsa.pub
    assert any("alpine-devel" in k for k in keys)


# --------------------------------------------------------------------------- #
# fetch_packages                                                               #
# --------------------------------------------------------------------------- #

def test_fetch_packages_surfaces_apk_failure(tmp_path):
    """When apk.static exits non-zero, fetch_packages must raise with
    the full stderr — silently falling through to "no extras staged"
    would regress the operator into the network-on-first-boot bug
    they opted into apk_fetch to avoid."""
    # Stand in a fake apk.static that always fails.
    fake_apk = tmp_path / "apk.static"
    fake_apk.write_text(
        "#!/bin/sh\necho 'simulated APKINDEX fetch failure' >&2\nexit 7\n"
    )
    fake_apk.chmod(0o755)

    keys_dir = tmp_path / "keys"
    keys_dir.mkdir()
    out_dir = tmp_path / "out"

    with pytest.raises(RuntimeError, match="bake-time apk-fetch failed"):
        apkfetch.fetch_packages(
            apk_static=fake_apk,
            target_arch="aarch64",
            alpine_branch="v3.21",
            packages=["avahi"],
            out_dir=out_dir,
            keys_dir=keys_dir,
        )


@pytest.mark.skipif(
    os.environ.get("PI_BAKE_INTEGRATION") != "1",
    reason="needs network + downloads apk-tools-static + fetches from upstream "
           "(set PI_BAKE_INTEGRATION=1 to opt in)",
)
def test_fetch_packages_real_upstream(tmp_path):
    """End-to-end integration: download apk-tools-static, extract
    Alpine pubkeys from a real tarball, fetch a tiny package
    (`tzdata`) cross-arch into out_dir. Opt-in only."""
    # Use the existing cache to avoid re-downloading the RPi tarball.
    from pi_bake.download import fetch as _fetch
    tarball = _fetch(
        "https://dl-cdn.alpinelinux.org/alpine/v3.21/releases/aarch64/"
        "alpine-rpi-3.21.4-aarch64.tar.gz"
    )
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    subprocess.run(
        ["tar", "-xzf", str(tarball), "-C", str(extracted)],
        check=True, capture_output=True,
    )
    keys_dir = apkfetch.extract_initramfs_keys(
        extracted, tmp_path / "keys",
    )
    apk_static = apkfetch.ensure_apk_static()
    fetched = apkfetch.fetch_packages(
        apk_static=apk_static,
        target_arch="aarch64",
        alpine_branch="v3.21",
        packages=["tzdata"],
        out_dir=tmp_path / "out",
        keys_dir=keys_dir,
    )
    assert any("tzdata" in f for f in fetched)
