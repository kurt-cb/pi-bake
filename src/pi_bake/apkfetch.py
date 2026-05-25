"""Bake-time apk-fetch — air-gap appliance support (v0.2).

When `apk_fetch` is enabled, the Alpine baker pulls operator-declared
`packages:` + their full recursive dep tree from upstream Alpine
repos at BAKE time and stages the .apk files into the FAT image at
`/apks/<arch>/extras/`. On first boot, the Pi installs them via a
small `local.d` script running `apk add --no-network
--allow-untrusted /media/mmcblk0/apks/<arch>/extras/*.apk` — no
internet needed.

Net effect: a fully provisioned device after the first cold boot,
even on an air-gapped LAN. Removes the v0.0.9 "first boot needs
network for `packages:` extras" constraint.

Why this shape (vs. regenerating + signing APKINDEX.tar.gz)
-----------------------------------------------------------
The Alpine RPi diskless init copies `/etc/apk/keys/` from the
initramfs into `$sysroot/etc/apk/keys/` *after* the apkovl is
extracted, then runs `apk add --root $sysroot --no-network` against
`/etc/apk/world`. We could sign a regenerated APKINDEX with a
bake-time RSA key and bake the pubkey into the apkovl so init's
`apk add` finds + verifies our extras at INIT time (before sshd
starts).

For v0.2 we take the simpler `local.d --no-network --allow-untrusted`
path: same end-state ("device boots fully provisioned offline"),
~100 fewer lines, no signing / index-merge surface area. The cost
is a small late-boot window where the extras finish installing
*after* sshd is reachable — acceptable for the appliance use case
that motivated this. Init-time install is a future enhancement
(see ROADMAP.md).

Bake-time deps
--------------
- `apk-tools-static` — auto-downloaded into `~/.cache/pi-bake/` on
  first use. The .apk is an Alpine static binary; runs on any glibc
  or musl x86_64 / aarch64 host.
- `tar` (GNU; handles multi-stream gzip used by the .apk format).
- `cpio` (to extract the Alpine official pubkeys from the bake
  target's initramfs — those are needed to verify the upstream
  APKINDEX during the bake-time fetch).
- Network connectivity to dl-cdn.alpinelinux.org at bake time.
"""
from __future__ import annotations

import gzip
import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path

from pi_bake.download import cache_dir, fetch

LOG = logging.getLogger("pi_bake.apkfetch")

# Pinned apk-tools-static. Bumped manually when a newer release
# tests cleanly. Pinning > dynamic discovery: deterministic, works
# offline after first download, simpler. The package is available
# from main on every Alpine branch including edge.
APK_STATIC_VERSION = "2.14.6-r3"
APK_STATIC_BRANCH = "v3.21"

# Host (bake-machine) arch → Alpine arch slug. apk-tools-static is
# arch-specific (it's a binary). The bake produces images for any
# TARGET arch via `apk fetch --arch <target>`; only the host needs
# a native apk.static.
_HOST_TO_ALPINE_ARCH = {
    "x86_64": "x86_64",
    "aarch64": "aarch64",
    "armv7l": "armv7",
    "i686": "x86",
}


def host_arch() -> str:
    """Alpine arch slug for the bake host. Raises if unsupported."""
    m = platform.machine()
    if m not in _HOST_TO_ALPINE_ARCH:
        raise RuntimeError(
            f"unrecognized bake-host arch {m!r}; bake-time apk-fetch "
            f"needs an apk-tools-static binary built for this host. "
            f"Known: {sorted(_HOST_TO_ALPINE_ARCH)}. "
            f"File an issue if you need this added."
        )
    return _HOST_TO_ALPINE_ARCH[m]


def ensure_apk_static() -> Path:
    """Return path to a usable apk-tools-static binary for this host.

    First call downloads + extracts apk-tools-static into
    `~/.cache/pi-bake/apk-tools-static/<ver>-<arch>/apk.static` and
    chmods it executable. Subsequent calls return the cached path.
    """
    arch = host_arch()
    bin_dir = cache_dir() / "apk-tools-static" / f"{APK_STATIC_VERSION}-{arch}"
    bin_path = bin_dir / "apk.static"
    if bin_path.is_file() and os.access(bin_path, os.X_OK):
        return bin_path

    if shutil.which("tar") is None:
        raise RuntimeError(
            "tar not on PATH; required to extract apk-tools-static.apk"
        )
    url = (
        f"https://dl-cdn.alpinelinux.org/alpine/{APK_STATIC_BRANCH}/main/"
        f"{arch}/apk-tools-static-{APK_STATIC_VERSION}.apk"
    )
    LOG.info("apk-tools-static: fetching %s", url)
    apk_file = fetch(url)
    bin_dir.mkdir(parents=True, exist_ok=True)
    # .apk files are multi-stream gzip-tar (signature | control |
    # data). GNU tar reads multi-stream gzip transparently and the
    # binary lives in the data segment as sbin/apk.static.
    subprocess.run(
        ["tar", "-xzf", str(apk_file), "-C", str(bin_dir),
         "sbin/apk.static"],
        check=True, capture_output=True, text=True,
    )
    (bin_dir / "sbin" / "apk.static").rename(bin_path)
    (bin_dir / "sbin").rmdir()
    bin_path.chmod(0o755)
    return bin_path


def extract_initramfs_keys(extracted_tarball: Path, out_dir: Path) -> Path:
    """Pull Alpine official pubkeys out of the RPi initramfs.

    `apk fetch --recursive` against upstream needs to verify the
    upstream APKINDEX signature, and apk-tools-static won't trust an
    index unless the matching pubkey is in its `--keys-dir`. The
    canonical source for the right pubkey set is the initramfs
    bundled with the same Alpine RPi tarball we're baking — match
    the build version exactly, never get out of sync with upstream
    key rotation.

    `extracted_tarball` — directory where the RPi tarball was
    extracted (contains `boot/initramfs-rpi`).
    `out_dir` — where to drop the extracted keys subtree.
    Returns the path to the `etc/apk/keys/` directory inside out_dir,
    ready to pass to apk.static as `--keys-dir`.
    """
    initramfs = extracted_tarball / "boot" / "initramfs-rpi"
    if not initramfs.is_file():
        raise RuntimeError(
            f"initramfs-rpi not at {initramfs} — expected from a "
            f"freshly-extracted Alpine RPi tarball"
        )
    if shutil.which("cpio") is None:
        raise RuntimeError(
            "cpio not on PATH; required to read the initramfs. "
            "Install: dnf install cpio  /  apt install cpio"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    # Initramfs is a gzipped cpio archive; pipe gunzip → cpio
    # extracting only etc/apk/keys/*.
    with gzip.open(initramfs, "rb") as gz:
        p = subprocess.Popen(
            ["cpio", "-idmu", "--quiet", "etc/apk/keys/*"],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=out_dir,
        )
        assert p.stdin is not None
        shutil.copyfileobj(gz, p.stdin)
        p.stdin.close()
        _, err = p.communicate()
        if p.returncode != 0:
            raise RuntimeError(
                f"cpio extract failed (rc={p.returncode}): "
                f"{err.decode(errors='replace')}"
            )
    keys_dir = out_dir / "etc" / "apk" / "keys"
    if not keys_dir.is_dir():
        raise RuntimeError(
            f"initramfs didn't contain etc/apk/keys/; got {out_dir}"
        )
    return keys_dir


def fetch_packages(
    *,
    apk_static: Path,
    target_arch: str,
    alpine_branch: str,
    packages: list[str],
    out_dir: Path,
    keys_dir: Path,
) -> list[str]:
    """Fetch `packages` + recursive deps into `out_dir` as .apk files.

    Pulls from Alpine main + community for `alpine_branch` (`v3.21`,
    `edge`, etc.). Cross-arch via apk.static's `--arch`. Verifies
    upstream signatures using `keys_dir`. Raises with full apk
    stderr on failure (no silent fallback — operator opted in to
    bake-time fetch, so a failure should surface, not regress to
    "needs network on first boot").

    Returns sorted list of .apk filenames now present in out_dir.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    repo_base = f"https://dl-cdn.alpinelinux.org/alpine/{alpine_branch}"
    cmd = [
        str(apk_static),
        "--arch", target_arch,
        "--keys-dir", str(keys_dir),
        "-X", f"{repo_base}/main",
        "-X", f"{repo_base}/community",
        "--no-cache",
        "fetch",
        "--recursive",
        "--output", str(out_dir),
        *packages,
    ]
    LOG.info("apk fetch: arch=%s branch=%s packages=%s",
             target_arch, alpine_branch, packages)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"bake-time apk-fetch failed (rc={r.returncode})\n"
            f"--- cmd ---\n{' '.join(cmd)}\n"
            f"--- stdout ---\n{r.stdout}\n"
            f"--- stderr ---\n{r.stderr}"
        )
    fetched = sorted(p.name for p in out_dir.glob("*.apk"))
    LOG.info("apk fetch: %d apk file(s) → %s",
             len(fetched), out_dir)
    return fetched
