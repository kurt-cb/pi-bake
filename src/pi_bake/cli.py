"""pi-bake CLI.

Subcommands:
  list-boards         — every supported Pi model
  list-os [--board B] — every OS we can bake (optionally filtered)
  build               — bake an .img.gz for one Pi (CLI flags or --config YAML)

Examples:
  pi-bake list-boards
  pi-bake list-os --board pi-zero-2-w

  # Flag-driven bake (familiar form).
  pi-bake build \\
    --board pi-zero-2-w --os alpine --version 3.21.4 \\
    --hostname pi-radio-1 --ssh-pubkey ~/.ssh/id_ed25519.pub \\
    --wifi-ssid totaldns-lab --wifi-psk secret \\
    --out ~/sdcards/pi-radio-1.img.gz

  # Save those flags as a reusable YAML recipe (no bake).
  pi-bake build <same-flags> --to-yaml ~/recipes/pi-radio-1.yaml --no-bake

  # Bake from the YAML recipe later (or anywhere).
  pi-bake build --config ~/recipes/pi-radio-1.yaml

The output `.img.gz` is flashable with:
  zcat ~/sdcards/pi-radio-1.img.gz | sudo dd of=/dev/mmcblk0 bs=4M status=progress

Annotated reference for every YAML field: pi-bake.example.yaml.
Tested-known-good recipes: examples/.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from pi_bake import __version__
from pi_bake.bake import build, supports
from pi_bake.boards import BOARDS, list_boards
from pi_bake.config import NodeConfig
from pi_bake.oses import list_oses
from pi_bake.recipe import (
    NetworkSpec, OutputSpec, Recipe, WifiSpec,
    dump_recipe, load_recipe, recipe_to_node_config,
)


# --------------------------------------------------------------------------- #
# Pretty printers                                                              #
# --------------------------------------------------------------------------- #

def _print_table(rows: list[dict], cols: list[str]) -> None:
    if not rows:
        print("(empty)")
        return
    widths = {
        c: max(len(c), max(len(str(r.get(c, "") or "")) for r in rows))
        for c in cols
    }
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "") or "").ljust(widths[c]) for c in cols))


# --------------------------------------------------------------------------- #
# Subcommand impls                                                             #
# --------------------------------------------------------------------------- #

def _cmd_list_boards(args: argparse.Namespace) -> int:
    rows = []
    for b in list_boards():
        supported_oses = sorted(
            o.name for o in list_oses(board=b.name)
        )
        rows.append({
            "board": b.name,
            "pretty": b.pretty,
            "arch": b.arch,
            "os_options": ", ".join(supported_oses) or "(none in catalog)",
            "notes": b.notes,
        })
    _print_table(rows, ["board", "pretty", "arch", "os_options"])
    print()
    for r in rows:
        if r["notes"]:
            print(f"  {r['board']}: {r['notes']}")
    return 0


def _cmd_list_os(args: argparse.Namespace) -> int:
    oses = list_oses(board=args.board)
    rows = [
        {
            "os": o.name,
            "pretty": o.pretty,
            "latest": o.latest(),
            "all_versions": ", ".join(o.versions),
            "backend": o.bake_backend,
            "boards": ", ".join(sorted(o.supports_boards)),
        }
        for o in oses
    ]
    _print_table(rows, ["os", "pretty", "latest", "backend", "boards"])
    print()
    for r in rows:
        os_obj = next(o for o in oses if o.name == r["os"])
        if os_obj.notes:
            print(f"  {r['os']}: {os_obj.notes}")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    # YAML-driven path: --config <yaml> reads everything from a
    # recipe file and ignores per-field flags. Operator can still
    # override --out / --no-bake / --to-yaml.
    if args.config:
        try:
            recipe = load_recipe(args.config)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        # Allow --out override for one-off retargeting without
        # editing the YAML.
        if args.out:
            recipe.output.path = args.out
        node, build_kwargs = recipe_to_node_config(recipe)
    else:
        # Flag-driven path: every input from argv. Required args
        # checked first so we fail before any I/O.
        if not args.board or not args.os_name or not args.hostname:
            print(
                "error: --board, --os, --hostname are required "
                "(or use --config <yaml>)",
                file=sys.stderr,
            )
            return 2
        pubkey = ""
        if args.ssh_pubkey:
            pubkey = Path(args.ssh_pubkey).expanduser().read_text().strip()
        elif args.ssh_pubkey_inline:
            pubkey = args.ssh_pubkey_inline.strip()
        else:
            print("error: --ssh-pubkey or --ssh-pubkey-inline is required",
                  file=sys.stderr)
            return 2

        extra_pubkeys: list[str] = []
        for path in args.extra_pubkey or []:
            extra_pubkeys.append(Path(path).expanduser().read_text().strip())

        if not args.out and not args.to_yaml:
            print(
                "error: --out is required unless --to-yaml is given "
                "(--to-yaml writes a recipe without baking)",
                file=sys.stderr,
            )
            return 2

        # Build a Recipe so --to-yaml can serialize it AND the
        # downstream NodeConfig is constructed via the same path
        # the YAML loader uses (no duplicate field plumbing).
        recipe = Recipe(
            hostname=args.hostname,
            board=args.board,
            os=args.os_name,
            os_version=args.version or "",
            timezone=args.timezone,
            ssh_pubkey=args.ssh_pubkey or pubkey,
            extra_pubkeys=list(args.extra_pubkey or []),
            network=NetworkSpec(
                mode="static" if args.static_v4 else "dhcp",
                address=args.static_v4 or "",
                gateway=args.gateway_v4 or "",
                send_hostname=args.dhcp_send_hostname,
            ),
            wifi=(
                WifiSpec(
                    ssid=args.wifi_ssid, psk=args.wifi_psk,
                    country=args.wifi_country,
                )
                if args.wifi_ssid else None
            ),
            packages=list(args.package or []),
            apk_fetch=args.apk_fetch,
            ssh_host_key=args.ssh_host_key or "",
            output=OutputSpec(
                # --to-yaml without --out: emit a sensible placeholder
                # so the dumped YAML is editable + visibly incomplete
                # (operator changes the dir; rerun bakes).
                path=args.out or f"~/sdcards/{args.hostname}.img.gz",
                image_size_mb=args.image_size_mb or 0,
            ),
        )
        # Build the node from the recipe so the pubkey-resolution
        # logic + validation lives in exactly one place.
        node, build_kwargs = recipe_to_node_config(recipe)

    # --to-yaml writes the recipe to disk regardless of which input
    # path got us here. Useful for round-tripping flag form to YAML.
    if args.to_yaml:
        text = dump_recipe(recipe)
        p = Path(args.to_yaml).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        print(f"wrote recipe → {p}")

    # --no-bake skips the actual bake (just serialize + exit).
    if args.no_bake:
        return 0

    if not supports(build_kwargs["board"], build_kwargs["os_name"]):
        print(
            f"error: {build_kwargs['os_name']!r} doesn't run on "
            f"{build_kwargs['board']!r}. "
            f"Run `pi-bake list-os --board {build_kwargs['board']}` to "
            f"see options.",
            file=sys.stderr,
        )
        return 2

    try:
        out = build(node=node, **build_kwargs)
    except NotImplementedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"wrote {out} ({out.stat().st_size >> 20} MB)")
    print(f"flash with:")
    print(f"  zcat {out} | sudo dd of=/dev/mmcblkN bs=4M status=progress")
    print(f"  (or rpi-imager → 'Use Custom Image')")
    return 0


# --------------------------------------------------------------------------- #
# Parser                                                                       #
# --------------------------------------------------------------------------- #

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pi-bake",
        description="Generate flashable, headless Raspberry Pi images.",
    )
    p.add_argument("--version", action="version",
                   version=f"pi-bake {__version__}")
    p.add_argument("--quiet", action="store_true",
                   help="log warnings only")
    sub = p.add_subparsers(dest="cmd", required=True)

    # list-boards
    sub.add_parser(
        "list-boards", help="every supported Pi model",
    ).set_defaults(func=_cmd_list_boards)

    # list-os
    p_los = sub.add_parser(
        "list-os", help="every OS we can bake (--board to filter)",
    )
    p_los.add_argument(
        "--board", help="filter to OSes supported on this board",
    )
    p_los.set_defaults(func=_cmd_list_os)

    # build
    p_b = sub.add_parser(
        "build",
        help="bake an .img.gz for one Pi (CLI flags or --config YAML)",
    )
    # Recipe-based path: --config wins over per-field flags. Not
    # marked required because --config is an alternative input.
    p_b.add_argument(
        "--config", metavar="YAML",
        help="read recipe from YAML file (see pi-bake.example.yaml). "
             "When set, per-field flags are ignored except --out.",
    )
    # Per-field flags: required iff --config is absent. We don't use
    # argparse's required=True here because --config makes them moot.
    p_b.add_argument("--board",
                     help=f"one of: {', '.join(b.name for b in BOARDS)}")
    p_b.add_argument("--os", dest="os_name",
                     help="alpine | raspbian | debian")
    p_b.add_argument("--version",
                     help="OS version (default: latest known-good; "
                          "Alpine: `edge` for newer drivers, e.g. iwlwifi)")
    p_b.add_argument("--hostname",
                     help="DNS-label-safe hostname")
    p_b.add_argument("--ssh-pubkey", metavar="PATH",
                     help="OpenSSH pubkey file to install into "
                          "/root/.ssh/authorized_keys")
    p_b.add_argument("--ssh-pubkey-inline", metavar="STR",
                     help="alternative: pubkey as a single inline string")
    p_b.add_argument("--extra-pubkey", action="append",
                     help="additional pubkey file (repeatable)")
    p_b.add_argument("--wifi-ssid",
                     help="bootstrap WiFi SSID (omit for wired-only)")
    p_b.add_argument("--wifi-psk",
                     help="bootstrap WiFi PSK (omit for wired-only)")
    p_b.add_argument("--wifi-country", default="US",
                     help="regulatory domain (default: US)")
    p_b.add_argument("--static-v4", dest="static_v4", metavar="CIDR",
                     help="eth0 static IPv4 (e.g. 192.168.4.111/24); "
                          "if set, --gateway-v4 must be too. Omit for DHCP.")
    p_b.add_argument("--gateway-v4", dest="gateway_v4",
                     help="eth0 IPv4 default gateway (required with --static-v4)")
    p_b.add_argument("--timezone", default="UTC",
                     help="default: UTC")
    p_b.add_argument(
        "--package", action="append",
        help="extra apk package to install on first boot (repeatable). "
             "Not in stock RPi /apks cache → needs network on first boot "
             "(bake-time cache enrichment is a v0.3 ROADMAP item).",
    )
    p_b.add_argument(
        "--ssh-host-key", metavar="PATH",
        help="bake this OpenSSH private key (and matching <PATH>.pub) "
             "into /etc/ssh/ssh_host_<type>_key{,.pub} so the Pi's SSH "
             "identity is stable across rebuilds — no known_hosts "
             "'IDENTIFICATION HAS CHANGED' warnings. Omit to let pi-bake "
             "auto-generate a fresh ed25519 pair per bake.",
    )
    p_b.add_argument(
        "--apk-fetch", action="store_true", default=False,
        help="bake-time apk-fetch: download --package extras + their "
             "recursive deps from upstream Alpine repos at BAKE time and "
             "stage them in the FAT image. First-boot install runs offline "
             "(no internet needed on the Pi). Required for air-gapped "
             "appliances. Bake host needs network + tar + cpio.",
    )
    p_b.add_argument(
        "--no-dhcp-hostname",
        dest="dhcp_send_hostname",
        action="store_false",
        default=True,
        help="DON'T send DHCP option 12 (hostname) on lease "
             "DISCOVER/REQUEST. Default: send. Set this to bake an "
             "intentional test fixture for DHCP-server-side hostname "
             "recovery via mDNS / synthesized placeholder.",
    )
    p_b.add_argument("--image-size-mb", type=int,
                     help="FAT32 image size in MB (default: backend's default)")
    p_b.add_argument("--out",
                     help="output .img.gz path (required unless --to-yaml)")
    p_b.add_argument(
        "--to-yaml", metavar="PATH",
        help="serialize the (CLI flags OR --config) recipe to PATH as "
             "an annotated YAML file. Combine with --no-bake to ONLY "
             "write the recipe.",
    )
    p_b.add_argument(
        "--no-bake", action="store_true",
        help="don't actually bake — useful with --to-yaml to capture "
             "a recipe without producing an image.",
    )
    p_b.set_defaults(func=_cmd_build)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(main())
