"""pi-bake — generate flashable, headless Raspberry Pi images.

Bake `(board, os, version, hostname, ssh_pubkey, [wifi])` into a
single `.img.gz` operator dd's to an SD card. Boot the Pi → it
joins the network → operator can SSH in. No keyboard, no monitor,
no `setup-alpine`.

Public API (also surfaced via the `pi-bake` CLI):

    from pi_bake import NodeConfig, build, list_boards, list_oses
    out = build(
        board="pi-zero-2-w",
        os_name="alpine",
        version="3.21",
        node=NodeConfig(
            hostname="pi-radio-1",
            ssh_pubkey="ssh-ed25519 AAAA...",
            wifi_ssid="totaldns-lab",
            wifi_psk="secret",
        ),
        out_path="~/sdcards/pi-radio-1.img.gz",
    )

Designed to be agnostic of any specific downstream — totaldns,
home-server projects, anything that wants a flash-and-boot Pi.
"""
from importlib.metadata import PackageNotFoundError, version as _pkg_version

from pi_bake.boards import Board, BOARDS, list_boards
from pi_bake.config import NodeConfig
from pi_bake.oses import OSImage, OSES, list_oses, resolve_image
from pi_bake.bake import build, supports
from pi_bake.recipe import (
    NetworkSpec, OutputSpec, Recipe, WifiSpec,
    dump_recipe, load_recipe, recipe_to_node_config,
)

try:
    # Distribution name on PyPI is `py-pi-bake`; importlib.metadata
    # reads it from the installed dist-info, which setuptools_scm
    # populated from the git tag at build time.
    __version__ = _pkg_version("py-pi-bake")
except PackageNotFoundError:
    # Running from a checkout without `pip install -e .` — fine for
    # one-off tests, just don't claim a real version.
    __version__ = "0.0.0+unknown"

__all__ = [
    "Board", "BOARDS", "list_boards",
    "OSImage", "OSES", "list_oses", "resolve_image",
    "NodeConfig",
    "build", "supports",
    "Recipe", "NetworkSpec", "WifiSpec", "OutputSpec",
    "dump_recipe", "load_recipe", "recipe_to_node_config",
]
