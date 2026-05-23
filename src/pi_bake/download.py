"""Image-archive download + cache + checksum verify.

Cache lives at `$XDG_CACHE_HOME/pi-bake/` (defaults to
`~/.cache/pi-bake/`). Re-baking the same `(os, version)` reuses
the cached copy.

Verification: if upstream provides a sha256 sidecar (Alpine does
at `<url>.sha256`), we fetch + check it. If not (some Raspbian
mirrors), we skip the check and surface a warning.
"""
from __future__ import annotations

import hashlib
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

LOG = logging.getLogger("pi_bake.download")


def cache_dir() -> Path:
    """Return the per-user cache directory, creating it if needed."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    d = Path(base) / "pi-bake"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cached_path(url: str) -> Path:
    """Where `url` would be cached on disk. Doesn't fetch."""
    name = url.rsplit("/", 1)[-1] or hashlib.sha256(url.encode()).hexdigest()
    return cache_dir() / name


def fetch(url: str, *, force: bool = False) -> Path:
    """Download `url` into the cache. Returns the cached path.

    Already-cached file is reused unless `force=True`. Checksum is
    verified against `<url>.sha256` when that sidecar exists; a mis-
    matched checksum re-downloads. Verbose logging at INFO level so
    operators see progress on big images.
    """
    dest = cached_path(url)
    if dest.is_file() and not force and _verify_if_possible(url, dest):
        LOG.info("cache hit: %s", dest)
        return dest

    LOG.info("downloading %s → %s", url, dest)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with urllib.request.urlopen(url) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length", 0))
            done = 0
            chunk = 1 << 20    # 1 MiB
            while True:
                buf = r.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if total:
                    pct = 100 * done // total
                    if done % (chunk * 16) == 0:
                        LOG.info("  %3d%% (%d / %d MB)",
                                 pct, done >> 20, total >> 20)
        tmp.replace(dest)
    except urllib.error.URLError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise RuntimeError(f"download failed: {url}: {e}") from e

    if not _verify_if_possible(url, dest):
        # The check did fire and failed (downloaded sidecar didn't match).
        raise RuntimeError(
            f"checksum mismatch for {dest}; retry with --force or check "
            f"the URL"
        )
    return dest


def _verify_if_possible(url: str, path: Path) -> bool:
    """True iff: (a) no sidecar is published (skip check, ok),
    or (b) sidecar exists AND content hashes match."""
    sidecar_url = url + ".sha256"
    try:
        with urllib.request.urlopen(sidecar_url, timeout=15) as r:
            expected = r.read().decode().split()[0].lower().strip()
    except urllib.error.URLError:
        LOG.warning("no .sha256 sidecar at %s — skipping verify", sidecar_url)
        return True
    if len(expected) != 64:
        LOG.warning("sidecar contents not a sha256 — skipping verify")
        return True
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for buf in iter(lambda: f.read(1 << 20), b""):
            h.update(buf)
    actual = h.hexdigest()
    if actual != expected:
        LOG.warning("sha256 mismatch (expected %s, got %s)", expected, actual)
        return False
    LOG.info("sha256 verified")
    return True
