"""Raspberry Pi board catalog.

One `Board` per officially-supported model. Keep this list short +
honest — a board belongs here when at least one OS we know how to
bake images for ACTUALLY runs on it. "Supported" lives on the
(board, os) edges in `oses.py`, not on the board itself.

`arch` matches what the upstream OS image archives use, so the
download URL templates can interpolate it directly.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Board:
    name: str          # short slug, e.g. "pi-5"
    pretty: str        # human label, e.g. "Raspberry Pi 5"
    arch: str          # "aarch64" | "armhf" — what the OS image label uses
    notes: str = ""    # quirks worth flagging to the operator

    def __str__(self) -> str:
        return f"{self.name} — {self.pretty} ({self.arch})"


# Order: most-current first. CLI `list-boards` renders in this order.
# Compute Module variants share the same SoC + Pi OS image as their
# consumer-Pi family member (CM3 = BCM2837 / Pi 3 family, CM4 =
# BCM2711 / Pi 4 family, CM5 = BCM2712 / Pi 5 family). Pi OS .img.xz
# carries firmware + kernels + DTBs for every Pi, and the bootloader
# auto-selects the right ones at boot — so for pi-bake's image bakes
# the CM entries are functionally equivalent to their consumer
# counterparts. They're listed separately for catalog clarity:
# operators picking what they actually have rather than coercing
# to pi-4 / pi-5.
BOARDS: tuple[Board, ...] = (
    Board(
        "pi-5", "Raspberry Pi 5", "aarch64",
        notes="64-bit only. Alpine 3.21+ has experimental support; "
              "Raspberry Pi OS Lite (Debian Bookworm) is the safe default.",
    ),
    Board(
        "pi-cm5", "Raspberry Pi Compute Module 5", "aarch64",
        notes="Same BCM2712 SoC as Pi 5; Pi OS bakes for `pi-5` work "
              "identically (universal image, bootloader auto-selects "
              "kernel_2712.img + bcm2712-rpi-cm5-*.dtb). Has eMMC "
              "(no SD slot) — typical lab use is PXE/NFS root or USB.",
    ),
    Board(
        "pi-4", "Raspberry Pi 4 Model B", "aarch64",
        notes="64-bit recommended; 32-bit still works for the ≤2 GB models.",
    ),
    Board(
        "pi-cm4", "Raspberry Pi Compute Module 4", "aarch64",
        notes="Same BCM2711 SoC as Pi 4; Pi OS bakes for `pi-4` work "
              "identically (universal image, bootloader auto-selects "
              "kernel8.img + bcm2711-rpi-cm4.dtb). Has eMMC variants "
              "+ SD-card-less variants — typical lab use is PXE/NFS "
              "root. mPCIe + dual-USB-2 daughterboards common.",
    ),
    Board(
        "pi-zero-2-w", "Raspberry Pi Zero 2 W", "aarch64",
        notes="Quad-core Cortex-A53; the snappy small WiFi-only Pi. "
              "Alpine aarch64 is the typical choice for IoT / station roles.",
    ),
    Board(
        "pi-3", "Raspberry Pi 3 Model B+", "aarch64",
        notes="Mostly legacy; Pi 4 / Pi 5 are the buy-it-today choices.",
    ),
    Board(
        "pi-cm3", "Raspberry Pi Compute Module 3", "aarch64",
        notes="Same BCM2837 SoC as Pi 3; Pi OS bakes for `pi-3` work "
              "identically. CM3/CM3+ variants share this entry.",
    ),
    Board(
        "pi-zero-w", "Raspberry Pi Zero W (original)", "armhf",
        notes="32-bit ARMv6 ONLY. Alpine `armhf` image. Pi Zero 2 W is the "
              "successor for new deployments.",
    ),
)

_BY_NAME: dict[str, Board] = {b.name: b for b in BOARDS}


def list_boards() -> list[Board]:
    """Return every supported board, in display order."""
    return list(BOARDS)


def get_board(name: str) -> Board:
    """Look up a board by slug. Raises KeyError on miss.

    Aliases: `pi5` / `pi-5` / `rpi5` all resolve to `pi-5`.
    """
    norm = name.strip().lower().replace("rpi", "pi").replace(" ", "-")
    # Common shorthand: "pi5" → "pi-5".
    if norm not in _BY_NAME and "-" not in norm and norm.startswith("pi"):
        try_dashed = f"pi-{norm[2:]}"
        if try_dashed in _BY_NAME:
            return _BY_NAME[try_dashed]
    if norm in _BY_NAME:
        return _BY_NAME[norm]
    raise KeyError(
        f"unknown board {name!r}; known: {sorted(_BY_NAME)}"
    )
