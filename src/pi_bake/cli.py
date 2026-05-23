"""pi-bake CLI.

Subcommands:
  list-boards         — every supported Pi model
  list-os [--board B] — every OS we can bake (optionally filtered)
  build               — bake an .img.gz for one Pi

Examples:
  pi-bake list-boards
  pi-bake list-os --board pi-zero-2-w
  pi-bake build \\
    --board pi-zero-2-w --os alpine --version 3.21.4 \\
    --hostname pi-radio-1 --ssh-pubkey ~/.ssh/id_ed25519.pub \\
    --wifi-ssid totaldns-lab --wifi-psk secret \\
    --out ~/sdcards/pi-radio-1.img.gz

The output `.img.gz` is flashable with:
  zcat ~/sdcards/pi-radio-1.img.gz | sudo dd of=/dev/mmcblk0 bs=4M status=progress
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
    if not supports(args.board, args.os_name):
        print(
            f"error: {args.os_name!r} doesn't run on {args.board!r}. "
            f"Run `pi-bake list-os --board {args.board}` to see options.",
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

    node = NodeConfig(
        hostname=args.hostname,
        ssh_pubkey=pubkey,
        wifi_ssid=args.wifi_ssid or "",
        wifi_psk=args.wifi_psk or "",
        wifi_country=args.wifi_country,
        timezone=args.timezone,
        extra_pubkeys=extra_pubkeys,
    )

    try:
        out = build(
            board=args.board,
            os_name=args.os_name,
            version=args.version,
            node=node,
            out_path=args.out,
            image_size_mb=args.image_size_mb,
        )
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
    p_b = sub.add_parser("build", help="bake an .img.gz for one Pi")
    p_b.add_argument("--board", required=True,
                     help=f"one of: {', '.join(b.name for b in BOARDS)}")
    p_b.add_argument("--os", dest="os_name", required=True,
                     help="alpine | raspbian | debian")
    p_b.add_argument("--version",
                     help="OS version (default: latest known-good)")
    p_b.add_argument("--hostname", required=True,
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
    p_b.add_argument("--timezone", default="UTC",
                     help="default: UTC")
    p_b.add_argument("--image-size-mb", type=int,
                     help="FAT32 image size in MB (default: backend's default)")
    p_b.add_argument("--out", required=True,
                     help="output .img.gz path")
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
