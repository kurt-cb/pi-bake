"""Top-level bake dispatcher.

Routes `(board, os)` to the right backend module + verifies the
combo is supported before going anywhere near the network. The
CLI calls this; the Python API also exports it via
`pi_bake.build()`.
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
    os_mode: str = "",
    pxe_server_url: str = "",
    apk_fetch: bool = True,  # DEPRECATED — always-on; kept for back-compat
) -> Path:
    """Build an `.img.gz` for `(board, os, version, node)`.

    `version=None` → use the OS's latest known-good.
    `image_size_mb=None` → backend's default.
    `extra_packages` → list of apk package names. When set,
        pi-bake fetches them + recursive deps at bake time,
        regenerates + signs the FAT-resident APKINDEX, and adds
        them to `/etc/apk/world` so init installs them at init
        time. Offline first boot guaranteed.
    `os_mode` → image layout for Alpine. Empty / "diskless" =
        default tarball-based no-root bake. "ext4" = sys-mode
        Alpine on a real partitioned image (FAT /boot + ext4 /),
        requires sudo. Ignored for non-Alpine backends. Edge
        kernel selection (`version="edge"`) requires ext4 — the
        diskless backend rejects it at recipe-load time.

    `apk_fetch` is a DEPRECATED no-op; always-on whenever
    `extra_packages` is non-empty. Kept in the signature for
    backward compatibility with old call sites; will be removed
    once nobody references it.

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

    # Same guard the recipe loader applies — catches CLI users
    # who bypass Recipe (--os alpine --version edge --os-mode diskless).
    if o.name == "alpine" and version == "edge":
        effective_mode = os_mode or "diskless"
        if effective_mode != "ext4":
            raise ValueError(
                "Alpine edge is not supported in diskless mode "
                "(Alpine upstream ships no RPi edge tarball, and "
                "diskless's modloop-on-FAT makes post-boot kernel "
                "upgrade require a manual ritual). "
                "Pass --os-mode ext4 for edge kernel support."
            )

    if os_mode and o.name != "alpine":
        raise ValueError(
            f"os_mode is only meaningful for os: alpine; "
            f"got os={o.name!r} with os_mode={os_mode!r}"
        )

    o, resolved_version, url = resolve_image(
        o.name, version, b.arch, board_slug=b.name,
    )

    LOG.info(
        "baking %s on %s using %s %s (url=%s)",
        node.hostname, b.name, o.name, resolved_version, url,
    )

    backend = o.bake_backend
    if backend == "alpine":
        if os_mode == "ext4":
            from pi_bake import alpine_ext4
            return alpine_ext4.bake(
                node=node, out_path=Path(out_path),
                arch=b.arch,
                alpine_version=resolved_version,
                extra_packages=extra_packages,
                image_size_mb=image_size_mb,
            )
        if os_mode == "pxe":
            if not pxe_server_url:
                raise ValueError(
                    "os_mode: pxe requires pxe_server_url (the lab HTTP "
                    "base URL). Set it in the recipe under `pxe.server_url:` "
                    "or pass --pxe-server-url on the CLI."
                )
            from pi_bake import alpine_pxe
            minor = ".".join(resolved_version.split(".")[:2])
            alpine_branch = f"v{minor}"
            return alpine_pxe.bake(
                url=url, node=node, out_path=Path(out_path),
                alpine_branch=alpine_branch,
                extra_packages=extra_packages,
                arch=b.arch,
                pxe_server_url=pxe_server_url,
            )
        # diskless (default) — original alpine backend, RPi tarball.
        # Edge selection was already rejected at recipe load for
        # diskless, so resolved_version here is a stable point release.
        from pi_bake import alpine
        minor = ".".join(resolved_version.split(".")[:2])
        alpine_branch = f"v{minor}"
        kwargs = {
            "url": url, "node": node, "out_path": Path(out_path),
            "alpine_branch": alpine_branch,
            "extra_packages": extra_packages,
            "arch": b.arch,
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
    elif backend == "debian":
        from pi_bake import debian
        return debian.bake(
            url=url, node=node, out_path=Path(out_path),
            image_size_mb=image_size_mb or 0,
        )
    elif backend == "fedora":
        from pi_bake import fedora
        return fedora.bake(
            url=url, node=node, out_path=Path(out_path),
            image_size_mb=image_size_mb or 0,
        )
    else:
        raise RuntimeError(f"unknown bake backend {backend!r} in OS catalog")
