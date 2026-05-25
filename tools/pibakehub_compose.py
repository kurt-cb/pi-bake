#!/usr/bin/env python3
"""pibakehub composition prototype — loads fragments and merges
them into an operator's base pi-bake recipe per the rules in
design/pibakehub-v1.md §4.

This is the PILOT (design/pibakehub-v1.md §10.2): NOT wired into
`pi-bake build`, no install entry point, no test suite beyond a
small smoke check. The point is to exercise the §3 fragment
schema + §4 merge rules end-to-end on real scraped data and
surface anything the design overlooked.

Usage:
    tools/pibakehub_compose.py \\
        --base examples/pi-5-wired-dhcp.yaml \\
        --pibakehub waveshare/m2-hat-plus \\
        --pibakehub intel/be200 \\
        --root pibakehub-pilot/

Output: the merged recipe as YAML on stdout, plus any §6.2
"untested fragment" warnings and §4.4 conflict errors on
stderr.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


# --------------------------------------------------------------------------- #
# Fragment loader — §3 schema validation                                       #
# --------------------------------------------------------------------------- #

# Required + optional fragment fields (subset of §3 schema we
# actually use in this prototype).
_FRAGMENT_REQUIRED = ("schema_version", "vendor", "slug",
                      "display_name", "manufacturer_url",
                      "description")
_FRAGMENT_OPTIONAL = ("requires", "config_txt", "packages",
                      "modules", "local_d_scripts", "caveats",
                      "see_also", "tags", "provenance",
                      "verified_on")
_FRAGMENT_ALLOWED = set(_FRAGMENT_REQUIRED) | set(_FRAGMENT_OPTIONAL)


def load_fragment(root: Path, slug: str) -> dict[str, Any]:
    """Load <root>/<vendor>/<product>/fragment.yaml.

    `slug` is `<vendor>/<product>`, e.g. `waveshare/m2-hat-plus`.
    """
    if "/" not in slug or slug.count("/") != 1:
        raise ValueError(
            f"fragment slug must be <vendor>/<product>; got {slug!r}"
        )
    path = root / slug / "fragment.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"no fragment at {path} for slug {slug!r}"
        )
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")

    unknown = set(data) - _FRAGMENT_ALLOWED
    if unknown:
        raise ValueError(
            f"{path}: unknown fragment fields {sorted(unknown)}; "
            f"allowed: {sorted(_FRAGMENT_ALLOWED)}"
        )
    missing = set(_FRAGMENT_REQUIRED) - set(data)
    if missing:
        raise ValueError(
            f"{path}: missing required fields {sorted(missing)}"
        )
    if data["schema_version"] != 1:
        raise ValueError(
            f"{path}: schema_version must be 1 (got "
            f"{data['schema_version']!r}); pibakehub v2+ not "
            f"implemented in this prototype"
        )
    declared = f"{data['vendor']}/{data['slug']}"
    if declared != slug:
        raise ValueError(
            f"{path}: vendor/slug fields ({declared!r}) don't "
            f"match the file location ({slug!r})"
        )
    return data


# --------------------------------------------------------------------------- #
# requires:* checks — §3.2 + §4.4                                              #
# --------------------------------------------------------------------------- #

def _check_requires(
    fragment: dict[str, Any],
    base: dict[str, Any],
    all_slugs: list[str],
) -> list[str]:
    """Return a list of human-readable failure messages (empty
    list = ok)."""
    fails: list[str] = []
    req = fragment.get("requires") or {}
    slug = f"{fragment['vendor']}/{fragment['slug']}"

    if "oneof_boards" in req:
        if base.get("board") not in req["oneof_boards"]:
            fails.append(
                f"  - {slug}.requires.oneof_boards = "
                f"{req['oneof_boards']!r}\n"
                f"    base recipe board = {base.get('board')!r}"
            )

    if "os" in req:
        if base.get("os") != req["os"]:
            fails.append(
                f"  - {slug}.requires.os = {req['os']!r}\n"
                f"    base recipe os = {base.get('os')!r}"
            )

    if "os_version_min" in req:
        if not _os_version_satisfies(base.get("os_version", ""),
                                     req["os_version_min"]):
            fails.append(
                f"  - {slug}.requires.os_version_min = "
                f"{req['os_version_min']!r}\n"
                f"    base recipe os_version = "
                f"{base.get('os_version') or '(default/latest)'!r}"
            )

    for clash in req.get("conflicts_with") or []:
        if clash in all_slugs:
            fails.append(
                f"  - {slug}.requires.conflicts_with includes "
                f"{clash!r}, but it's also on the CLI"
            )
    return fails


# §3.2.1 ordering for Alpine.
_ALPINE_VERSION_ORDER = ("3.19", "3.20", "3.21", "3.22", "edge")


def _os_version_satisfies(version: str, minimum: str) -> bool:
    """True if `version` >= `minimum` per the §3.2.1 ordering."""
    def _rank(v: str) -> int:
        # Strip patch level: 3.21.4 → 3.21
        norm = ".".join(v.split(".")[:2]) if "." in v else v
        try:
            return _ALPINE_VERSION_ORDER.index(norm)
        except ValueError:
            return -1   # unknown → fails the comparison
    return _rank(version) >= _rank(minimum)


# --------------------------------------------------------------------------- #
# Composition — §4                                                             #
# --------------------------------------------------------------------------- #

def compose(
    base: dict[str, Any],
    fragments: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Apply each fragment to `base` per §4 merge rules.

    Returns (merged_recipe, warnings, errors). Empty `errors`
    means the merge completed; callers decide whether to act on
    `warnings`.
    """
    merged = dict(base)
    warnings: list[str] = []
    errors: list[str] = []
    seen_local_d: dict[str, str] = {}   # name → slug providing it
    all_slugs = [f"{f['vendor']}/{f['slug']}" for f in fragments]

    for frag in fragments:
        slug = f"{frag['vendor']}/{frag['slug']}"

        # requires:* — §4.4
        fails = _check_requires(frag, merged, all_slugs)
        if fails:
            errors.append(
                f"composition error from {slug}:\n"
                + "\n".join(fails)
            )
            continue   # don't apply a failing fragment

        # packages:* — §4.2, dedupe append
        if frag.get("packages"):
            existing = list(merged.get("packages") or [])
            for p in frag["packages"]:
                if p not in existing:
                    existing.append(p)
            merged["packages"] = existing

        # modules:* — §4.2, dedupe append
        if frag.get("modules"):
            existing = list(merged.get("modules") or [])
            for m in frag["modules"]:
                if m not in existing:
                    existing.append(m)
            merged["modules"] = existing

        # config_txt:* — §4.2, dedupe append by exact line
        if frag.get("config_txt"):
            existing = list(merged.get("config_txt") or [])
            for line in frag["config_txt"]:
                if line not in existing:
                    existing.append(line)
            merged["config_txt"] = existing

        # local_d_scripts:* — §4.4 hard error on duplicate name
        for script in frag.get("local_d_scripts") or []:
            name = script.get("name")
            if not name:
                errors.append(
                    f"{slug}.local_d_scripts entry missing 'name'"
                )
                continue
            if name in seen_local_d:
                errors.append(
                    f"local_d_scripts conflict on {name!r}: "
                    f"both {seen_local_d[name]} and {slug} "
                    f"declare it"
                )
                continue
            seen_local_d[name] = slug
            merged.setdefault("local_d_scripts", []).append(script)

        # §6.2 untested warning
        if not _has_verified_record(frag, merged, all_slugs):
            warnings.append(_untested_warning(frag, merged, all_slugs))

    return merged, warnings, errors


def _has_verified_record(
    fragment: dict[str, Any],
    base: dict[str, Any],
    all_slugs: list[str],
) -> bool:
    """§5.2 matching. True iff fragment has a verified_on entry
    matching the bake's board × os × os_version × components."""
    other_slugs = {s for s in all_slugs
                   if s != f"{fragment['vendor']}/{fragment['slug']}"}
    for record in fragment.get("verified_on") or []:
        if record.get("board") != base.get("board"):
            continue
        if record.get("os") != base.get("os"):
            continue
        if record.get("os_version") != base.get("os_version", ""):
            continue
        required_present = set(record.get("components_present") or [])
        forbidden = set(record.get("components_absent") or [])
        if not required_present.issubset(other_slugs):
            continue
        if forbidden & other_slugs:
            continue
        return True
    return False


def _untested_warning(
    fragment: dict[str, Any],
    base: dict[str, Any],
    all_slugs: list[str],
) -> str:
    slug = f"{fragment['vendor']}/{fragment['slug']}"
    parts = [
        f"⚠ {slug} has no verified_on record matching:",
        f"    board={base.get('board')!r}  "
        f"os={base.get('os')!r}  "
        f"os_version={base.get('os_version', '')!r}",
        f"    co-fragments={[s for s in all_slugs if s != slug]!r}",
    ]
    prov = fragment.get("provenance") or {}
    if prov.get("scraped_from"):
        parts.append(
            f"  This fragment was auto-scraped from "
            f"{prov['scraped_from']}\n"
            f"  ({prov.get('scraped_date', 'date unknown')})."
        )
    parts.append(
        "  Verify the bake, then send a PR adding a verified_on "
        "record."
    )
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# CLI entry                                                                    #
# --------------------------------------------------------------------------- #

def main() -> int:
    p = argparse.ArgumentParser(
        prog="pibakehub_compose.py",
        description="Compose pibakehub fragments into a pi-bake "
                    "recipe (prototype).",
    )
    p.add_argument("--base", required=True, type=Path,
                   help="pi-bake recipe YAML (the operator's base)")
    p.add_argument("--pibakehub", action="append", default=[],
                   metavar="VENDOR/SLUG",
                   help="fragment to compose (repeatable, "
                        "left-to-right priority)")
    p.add_argument("--root", default=Path("pibakehub-pilot"),
                   type=Path,
                   help="pibakehub registry root (default: "
                        "./pibakehub-pilot/)")
    p.add_argument("--strict", action="store_true",
                   help="promote untested warnings to errors "
                        "(§6.3)")
    args = p.parse_args()

    with open(args.base) as f:
        base = yaml.safe_load(f)
    if not isinstance(base, dict):
        print(f"error: {args.base} doesn't parse as a mapping",
              file=sys.stderr)
        return 1

    fragments = []
    for slug in args.pibakehub:
        try:
            fragments.append(load_fragment(args.root, slug))
        except (FileNotFoundError, ValueError) as e:
            print(f"error loading {slug}: {e}", file=sys.stderr)
            return 1

    merged, warnings, errors = compose(base, fragments)

    for w in warnings:
        print(w, file=sys.stderr)
        print(file=sys.stderr)
    for e in errors:
        print(e, file=sys.stderr)
        print(file=sys.stderr)

    if errors:
        return 2
    if warnings and args.strict:
        print("--strict: failing on untested fragment warnings",
              file=sys.stderr)
        return 3

    # Print merged recipe.
    yaml.safe_dump(merged, sys.stdout, sort_keys=False,
                   default_flow_style=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
