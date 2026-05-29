"""tested_bakes.yaml loader + (os, version) -> status lookup."""
from __future__ import annotations

import pytest

from pi_bake.tested import (
    BakeRecord,
    _default_ledger_path,
    load_tested_bakes,
    bake_status,
)


# ---------- ledger loading ----------


def test_ledger_path_resolves_to_repo_root():
    """The default ledger path walks up from the package directory
    until it finds tested_bakes.yaml. In dev (editable install),
    that's the repo root."""
    p = _default_ledger_path()
    assert p.name == "tested_bakes.yaml"


def test_load_tested_bakes_returns_alpine_entries():
    """Initial ledger ships with Alpine entries (hardware-validated
    on v0.3.x). Bumping these is fine; this test just verifies
    the loader reads them."""
    bakes = load_tested_bakes()
    alpine = [b for b in bakes if b.os == "alpine"]
    assert alpine, "expected Alpine entries in tested_bakes.yaml"


def test_load_tested_bakes_missing_file_returns_empty(tmp_path):
    """If the ledger isn't found, the loader returns []. The CLI
    then treats every row as supported/untested — honest about
    the absence of test data."""
    assert load_tested_bakes(tmp_path / "nope.yaml") == []


def test_load_tested_bakes_validates_required_fields(tmp_path):
    """Malformed entries SHOULD crash the load — silent miss
    would mean a row that should have shown supported gets
    marked untested. tested_bakes.yaml is operator-edited; we
    want loud feedback when an entry is broken."""
    p = tmp_path / "bad.yaml"
    p.write_text(
        "tested:\n"
        "  - board: pi-5\n"   # missing os, version, etc.
        "    notes: incomplete\n"
    )
    with pytest.raises(TypeError):  # dataclass __init__ blowup
        load_tested_bakes(p)


def test_tested_bake_has_optional_os_mode_field():
    """`os_mode:` is Alpine-only (diskless / ext4 / pxe). Other
    backends omit it. The dataclass default is empty string."""
    b = BakeRecord(
        board="pi-5", os="raspbian", version="2025-05-13",
        tested_on="2026-05-29", tested_by="op",
        recipe="examples/r.yaml",
    )
    assert b.os_mode == ""
    assert b.resolved_to == ""


# ---------- (os, version) -> status lookup ----------


_FIXTURE = [
    BakeRecord(
        board="pi-5", os="alpine", os_mode="diskless",
        version="3.21.4", tested_on="2026-05-27",
        tested_by="op", recipe="examples/a.yaml",
    ),
    BakeRecord(
        board="pi-5", os="alpine", os_mode="ext4",
        version="edge", tested_on="2026-05-27",
        tested_by="op", recipe="examples/a-edge.yaml",
    ),
    BakeRecord(
        board="pi-5", os="raspbian", version="latest",
        resolved_to="2026-04-21",
        tested_on="2026-05-29", tested_by="op",
        recipe="examples/r.yaml",
    ),
]


def test_status_exact_match_returns_supported():
    status, recipe = bake_status("alpine", "3.21.4", _FIXTURE)
    assert status == "supported"
    assert recipe == "examples/a.yaml"


def test_status_unknown_combo_returns_supported_untested():
    """pi-bake has the code path (Debian backend is in the
    catalog), but no operator has validated it on hardware.
    Honest label."""
    status, recipe = bake_status("debian", "20231109", _FIXTURE)
    assert status == "supported/untested"
    assert recipe == ""


def test_status_latest_sentinel_matches_resolved_to_entry():
    """`latest` is a moving sentinel. If any ledger row recorded
    its resolved_to, we'll claim 'supported' (partial info, but
    better than always-untested for a sentinel that has been
    validated under a specific snapshot)."""
    status, recipe = bake_status("raspbian", "latest", _FIXTURE)
    assert status == "supported"
    assert recipe == "examples/r.yaml"


def test_status_latest_sentinel_unmatched_returns_untested():
    status, recipe = bake_status("alpine", "latest", _FIXTURE)
    assert status == "supported/untested"
    assert recipe == ""


def test_status_default_loads_real_ledger():
    """When no ledger arg is passed, fall back to the on-disk
    file. Sanity check: Alpine 3.21.4 is in the real ledger."""
    status, _ = bake_status("alpine", "3.21.4")
    assert status == "supported"
