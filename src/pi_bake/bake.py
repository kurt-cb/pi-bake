"""Top-level bake dispatcher.

Routes `(board, os)` to the right backend module + verifies the
combo is on the supported edge list before going anywhere near
the network. The CLI calls this; the Python API also exports it
via `pi_bake.build()`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from pi_bake.boards import get_board
from pi_bake.config import NodeConfig
from pi_bake.oses import get_os, resolve_image

LOG = logging.getLogger("pi_bake.bake")


def supports(board: str, os_name: str) -> bool:
    """True iff `os_name` is supported on `board` per the catalog."""
    try:
        b = get_board(board)
        o = get_os(os_name)
    except KeyError:
        return False
    return b.name in o.supports_boards


def build(
    *, board: str, os_name: str, version: str | None,
    node: NodeConfig, out_path: str | Path,
    image_size_mb: int | None = None,
    extra_packages: list[str] | None = None,
) -> Path:
    """Build an `.img.gz` for `(board, os, version, node)`.

    `version=None` → use the OS's latest known-good.
    `image_size_mb=None` → backend's default.
    `extra_packages` → list of apk package names appended to
        `/etc/apk/world` (Alpine only). Today these install on
        first boot via network when not in the stock /apks cache.

    Raises:
      - KeyError on unknown board/os.
      - ValueError if the (board, os) combo isn't supported.
      - NotImplementedError for backends not in v0.1.
    """
    b = get_board(board)
    o = get_os(os_name)
    if b.name not in o.supports_boards:
        raise ValueError(
            f"{o.pretty} ({o.name}) doesn't support {b.pretty} ({b.name}). "
            f"Supported boards for {o.name}: {sorted(o.supports_boards)}"
        )

    o, resolved_version, url = resolve_image(o.name, version, b.arch)

    LOG.info(
        "baking %s on %s using %s %s (url=%s)",
        node.hostname, b.name, o.name, resolved_version, url,
    )

    backend = o.bake_backend
    if backend == "alpine":
        from pi_bake import alpine
        # Alpine repositories branch: `edge` keeps the rolling label,
        # everything else gets the `v3.21`-style prefix.
        if resolved_version == "edge":
            alpine_branch = "edge"
        else:
            minor = ".".join(resolved_version.split(".")[:2])
            alpine_branch = f"v{minor}"
        kwargs = {
            "url": url, "node": node, "out_path": Path(out_path),
            "alpine_branch": alpine_branch,
            "extra_packages": extra_packages,
        }
        if image_size_mb is not None:
            kwargs["image_size_mb"] = image_size_mb
        return alpine.bake(**kwargs)
    elif backend == "raspbian":
        from pi_bake import raspbian
        return raspbian.bake(
            url=url, node=node, out_path=Path(out_path),
            image_size_mb=image_size_mb or 0,
        )
    else:
        raise RuntimeError(f"unknown bake backend {backend!r} in OS catalog")
