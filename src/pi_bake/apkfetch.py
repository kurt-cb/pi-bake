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
import secrets
import shutil
import subprocess
import tarfile
from pathlib import Path

from pi_bake.download import cache_dir, fetch

LOG = logging.getLogger("pi_bake.apkfetch")

# Pinned apk-tools-static. Bumped manually when a newer release
# tests cleanly. Pinning > dynamic discovery: deterministic, works
# offline after first download, simpler. The package is available
# from main on every supported Alpine branch.
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

    Pulls from Alpine main + community for `alpine_branch` (e.g.
    `v3.21`). Cross-arch via apk.static's `--arch`. Verifies
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


# --------------------------------------------------------------------------- #
# Signing key + APKINDEX regen (init-time install — see design/#3_study.md)   #
# --------------------------------------------------------------------------- #


def make_signing_key(workdir: Path) -> tuple[Path, Path, str]:
    """Generate a fresh RSA-2048 signing keypair for the APKINDEX.

    The pi-bake convention is `pi-bake-<8hex>.rsa.pub` for the
    public key filename — matches Alpine's
    `<email>-<8hex>.rsa.pub` shape. The matching private key sits
    alongside it in `workdir` with the `.pub` stripped.

    Returns `(privkey_path, pubkey_path, pubkey_filename)`. The
    pubkey filename is what gets referenced inside the signed
    APKINDEX's `.SIGN.RSA.<name>` member and what we drop into
    the apkovl at `/etc/apk/keys/<name>` so init's `apk add`
    trusts our signature.
    """
    if shutil.which("openssl") is None:
        raise RuntimeError(
            "openssl not on PATH; required for APKINDEX signing. "
            "Install: dnf install openssl  /  apt install openssl"
        )
    workdir.mkdir(parents=True, exist_ok=True)
    pubkey_name = f"pi-bake-{secrets.token_hex(4)}.rsa.pub"
    pubkey = workdir / pubkey_name
    privkey = workdir / pubkey_name[:-4]   # strip ".pub"
    subprocess.run(
        ["openssl", "genrsa", "-out", str(privkey), "2048"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["openssl", "rsa", "-in", str(privkey), "-pubout",
         "-out", str(pubkey)],
        check=True, capture_output=True, text=True,
    )
    privkey.chmod(0o600)
    LOG.info("APKINDEX signing key: %s", pubkey_name)
    return privkey, pubkey, pubkey_name


def regen_signed_index(
    *,
    apk_static: Path,
    apks_dir: Path,
    privkey: Path,
    pubkey_name: str,
) -> None:
    """Regenerate `apks_dir/APKINDEX.tar.gz` over every .apk in
    `apks_dir`, signed by `privkey` and referencing `pubkey_name`.

    Replaces the existing (alpine-devel-signed) index with a
    pi-bake-signed one. apk-tools verifies APKINDEX signatures
    against whatever pubkeys are in `--keys-dir`; the matching
    pubkey lands in the apkovl's `/etc/apk/keys/` so init's
    `apk add --root $sysroot` accepts our index.

    The .apk files themselves are unchanged — they retain their
    upstream alpine-devel-signed bodies. apk-tools verifies each
    file's INTERNAL signature against alpine-devel keys (already
    in $sysroot/etc/apk/keys via init's `cp -a` from initramfs).
    Our key only signs the INDEX listing.

    Signed APKINDEX.tar.gz format (per apk-tools / abuild-sign):
        gzip-stream-1: tar containing one file
                        `.SIGN.RSA256.<pubkey_name>` whose body
                        is the openssl RSA-SHA256 signature of
                        gzip-stream-2. (apk-tools also accepts
                        `.SIGN.RSA.<name>` for legacy RSA-SHA1
                        signatures, but modern OpenSSL 3.x
                        refuses to produce SHA-1 RSA signatures
                        by default — security policy. SHA-256
                        is fine; apk-tools 2.6+ supports it.)
        gzip-stream-2: tar containing `APKINDEX` (plain-text
                        package metadata).
        Concatenated as multi-stream gzip (GNU tar reads this
        transparently).
    """
    if shutil.which("openssl") is None:
        raise RuntimeError("openssl not on PATH; can't sign index")

    apk_files = sorted(p for p in apks_dir.iterdir()
                       if p.suffix == ".apk")
    if not apk_files:
        raise RuntimeError(f"no .apk files in {apks_dir}")

    # Step 1. Build the unsigned index. apk index reads each
    # .apk's metadata and tries to verify each file's embedded
    # signature; we pass --allow-untrusted because Alpine ships
    # .apks signed with RSA-SHA1 and modern apk-tools-static
    # (built against new OpenSSL) refuses to verify SHA-1 RSA
    # signatures. This is safe: we're not installing anything
    # here, just building an INDEX. The .apks' alpine-devel
    # signatures DO get verified later — by init's apk add on
    # the Pi, which runs the older apk-tools bundled in the
    # Alpine RPi initramfs (that does trust SHA-1 RSA for
    # legacy compat). --no-warnings suppresses "no description"
    # chatter for our extras.
    unsigned = apks_dir / ".APKINDEX.unsigned.tar.gz"
    cmd = [str(apk_static), "index", "--no-warnings",
           "--allow-untrusted",
           "-o", str(unsigned)] + [str(p) for p in apk_files]
    LOG.info("apk index: %d .apks → %s",
             len(apk_files), unsigned.name)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"apk index failed (rc={r.returncode}):\n"
            f"--- stdout ---\n{r.stdout}\n"
            f"--- stderr ---\n{r.stderr}"
        )

    # Step 2. RSA-SHA256 sign the unsigned tar.gz with openssl.
    # SHA-256, not SHA-1 — modern OpenSSL 3.x refuses RSA-SHA1.
    # apk-tools 2.6+ accepts .SIGN.RSA256.<name> for SHA-256.
    sig_bin = apks_dir / ".APKINDEX.sig"
    subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(privkey),
         "-out", str(sig_bin), str(unsigned)],
        check=True, capture_output=True, text=True,
    )

    # Step 3. Wrap the signature in a one-file tar (member name
    # ".SIGN.RSA256.<pubkey_name>") then gzip it. Member metadata
    # set to deterministic values so the resulting bytes are
    # reproducible.
    sig_blob = sig_bin.read_bytes()
    sig_tar_gz = apks_dir / ".APKINDEX.sig.tar.gz"
    # gzip.GzipFile (not open()) lets us pass mtime=0 for
    # reproducible signature stream bytes across bakes of the
    # same recipe.
    with open(sig_tar_gz, "wb") as raw:
        gz = gzip.GzipFile(filename="", fileobj=raw, mode="wb",
                           compresslevel=9, mtime=0)
        with tarfile.open(fileobj=gz, mode="w|") as tf:
            member = tarfile.TarInfo(name=f".SIGN.RSA256.{pubkey_name}")
            member.size = len(sig_blob)
            member.mode = 0o644
            member.uid = 0
            member.gid = 0
            member.mtime = 0
            import io as _io
            tf.addfile(member, _io.BytesIO(sig_blob))
        gz.close()

    # Step 4. Concatenate signature stream + unsigned index →
    # signed APKINDEX.tar.gz (overwrites whatever was there).
    signed = apks_dir / "APKINDEX.tar.gz"
    with open(signed, "wb") as out:
        for piece in (sig_tar_gz, unsigned):
            out.write(piece.read_bytes())

    # Tidy intermediates.
    for f in (unsigned, sig_bin, sig_tar_gz):
        f.unlink()
    LOG.info("APKINDEX signed: %s (%d bytes)",
             signed, signed.stat().st_size)
