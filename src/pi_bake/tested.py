"""Loader for `tested_bakes.yaml` — pi-bake's hardware-test ledger.

Each entry records a real (board, os, os_mode, os_version)
combination that was bake-flash-boot tested on real Pi hardware
and confirmed SSH-reachable. `pi-bake list-os-versions` reads
this file and annotates each catalog row with a test status:

  supported          — there's a ledger entry for this combo
  supported/untested — pi-bake has code but no ledger entry
  unknown            — runtime fallback territory (only logged
                       at bake time as a WARNING)

The ledger is a source of truth orthogonal to the catalog: it
says "what has been proven to work end-to-end on real hardware,"
not "what pi-bake claims to support." A catalog entry can be
supported/untested for a long time before becoming supported.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class BakeRecord:
    """One row from tested_bakes.yaml.

    `version` is the os_version value passed at bake time —
    sentinel ("latest" / "stable") or concrete (e.g. "3.21.4").
    `resolved_to` (when set) records the concrete version the
    sentinel resolved to at test time; matters for `latest`
    which moves over time.
    """
    board: str
    os: str
    version: str
    tested_on: str
    tested_by: str
    recipe: str
    notes: str = ""
    os_mode: str = ""
    resolved_to: str = ""


def _default_ledger_path() -> Path:
    """Find tested_bakes.yaml at the repo root.

    Walk up from this file's location until we find a directory
    containing the ledger or hit the filesystem root. Lets the
    same install work from a checked-out repo, an editable
    install, and a pip-installed wheel (the latter ships
    tested_bakes.yaml alongside the package).
    """
    here = Path(__file__).resolve()
    for candidate in [here.parent, *here.parents]:
        ledger = candidate / "tested_bakes.yaml"
        if ledger.is_file():
            return ledger
    # Fallback: a path that will fail is_file() and yield an
    # empty ledger. Callers that depend on entries surface this
    # as "no rows" rather than crashing.
    return Path("tested_bakes.yaml")


def load_tested_bakes(path: Path | None = None) -> list[BakeRecord]:
    """Parse tested_bakes.yaml and return the list of BakeRecord.

    Missing file -> empty list (the dispatcher treats absence as
    "no test data available," which is honest). Malformed entries
    raise — we'd rather crash the CLI than silently miss a
    column that should have shown supported.
    """
    import yaml  # lazy: keep startup costless when ledger unused

    p = path or _default_ledger_path()
    if not p.is_file():
        return []
    with open(p) as f:
        data = yaml.safe_load(f) or {}
    entries = data.get("tested", [])
    out: list[BakeRecord] = []
    for e in entries:
        out.append(BakeRecord(**e))
    return out


def bake_status(
    os_name: str, version: str,
    bakes: Sequence[BakeRecord] | None = None,
) -> tuple[str, str]:
    """Return (status, recipe-or-empty) for (os, version).

    Status is `"supported"` if any bake in the ledger matches the
    (os, version) pair, otherwise `"supported/untested"`. The
    recipe string is the path to the first matching bake's recipe
    (empty when status is supported/untested).

    `unknown` status is intentionally NOT produced by this lookup
    — it only surfaces at runtime when a backend's dispatcher
    falls back to a different baker (see raspbian.py codename
    fallback). Catalog rows are always at least
    supported/untested because pi-bake has code for them.

    Match rules:
      - Exact (os, version) match wins.
      - Sentinel `latest` matches any ledger row whose
        resolved_to is set (the sentinel was tested under a
        snapshot of upstream — partial information; better than
        marking it untested).
    """
    if bakes is None:
        bakes = load_tested_bakes()
    for b in bakes:
        if b.os == os_name and b.version == version:
            return "supported", b.recipe
    if version == "latest":
        for b in bakes:
            if b.os == os_name and b.resolved_to:
                return "supported", b.recipe
    return "supported/untested", ""
