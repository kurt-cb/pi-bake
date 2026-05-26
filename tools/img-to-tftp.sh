#!/usr/bin/env bash
# img-to-tftp.sh — extract a pi-bake'd image's boot files into
# a MAC-keyed TFTP dir for PXE serving.
#
# Usage:
#   sudo tools/img-to-tftp.sh <MAC> <image-file> [tftp-root]
#
# Examples:
#   sudo tools/img-to-tftp.sh aa:bb:cc:dd:ee:ff /tmp/pi-test-1.img.xz
#   sudo tools/img-to-tftp.sh aa:bb:cc:dd:ee:ff /tmp/td-pi5-1.img.gz /srv/tftp
#
# What it does:
#   1. Decompress (.img.gz | .img.xz | .img all handled).
#   2. Detect partitioned (Raspbian/Debian/Fedora) vs single-FAT
#      (Alpine RPi tarball-bake) and extract the boot partition.
#   3. Copy the boot file set into <tftp-root>/<mac-dashed>/
#      where <mac-dashed> is the MAC with colons replaced by
#      dashes, lowercase (filesystem-friendlier than colons).
#
# The Pi bootloader uses the directory portion of the option-67
# boot file path as a prefix for all subsequent TFTP fetches
# (start4.elf, fixup4.dat, kernel8.img, *.dtb, overlays/*). So
# the bootcode.bin lives at <tftp-root>/<mac-dashed>/bootcode.bin
# and the dnsmasq `dhcp-boot=tag:<pi>,<mac-dashed>/bootcode.bin`
# line steers the bootloader to that subtree.
#
# See design/infra_pxe.md for the full PXE lab setup.

set -euo pipefail

usage() {
    sed -n 's/^# \?//;1,/^$/p' "$0" >&2
    exit 2
}

[[ $# -ge 2 ]] || usage

MAC="$1"
IMG="$2"
TFTP_ROOT="${3:-${TFTP_ROOT:-/var/lib/tftpboot}}"

# Validate MAC format (six colon-separated hex pairs, case-insensitive)
if ! [[ "$MAC" =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]; then
    echo "error: MAC must be aa:bb:cc:dd:ee:ff format; got $MAC" >&2
    exit 2
fi

[[ -f "$IMG" ]] || { echo "error: image not found: $IMG" >&2; exit 2; }

# Need root for tftp dir writes + losetup/mount on partitioned images.
if [[ "$EUID" -ne 0 ]]; then
    echo "error: needs root (re-run with sudo)" >&2
    exit 2
fi

# Normalize MAC for filesystem use.
MAC_DASHED=$(echo "$MAC" | tr ':' '-' | tr '[:upper:]' '[:lower:]')
DEST="$TFTP_ROOT/$MAC_DASHED"

# Pick decompressor by extension.
case "$IMG" in
    *.img.gz)  DECOMP=(zcat);;
    *.img.xz)  DECOMP=(xz -d -c);;
    *.img)     DECOMP=(cat);;
    *.raw.xz)  DECOMP=(xz -d -c);;
    *.raw)     DECOMP=(cat);;
    *)
        echo "error: unsupported image extension: $IMG" >&2
        echo "       want .img.gz, .img.xz, .raw.xz, .img, or .raw" >&2
        exit 2
        ;;
esac

# Tools we need.
for tool in mcopy losetup mount umount rsync file; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: $tool not on PATH" >&2
        exit 2
    }
done

TMP=$(mktemp -d -t pi-bake-tftp-XXXXXX)
trap 'rm -rf "$TMP" 2>/dev/null; [[ -n "${LOOP:-}" ]] && losetup -d "$LOOP" 2>/dev/null || true' EXIT

RAW="$TMP/raw.img"
echo "→ decompress $IMG → $RAW"
"${DECOMP[@]}" "$IMG" > "$RAW"

# Detect partitioned vs single-FAT.
# Partitioned images report "DOS/MBR boot sector" with partitions.
# Single-FAT (Alpine RPi-style) reports as "DOS/MBR boot sector" too,
# so we check by trying mdir first (single-FAT) then fall through
# to losetup (partitioned).
mkdir -p "$DEST"
echo "→ tftp dest: $DEST"

if mdir -i "$RAW" :: >/dev/null 2>&1; then
    # Single-FAT image (Alpine RPi tarball-bake).
    echo "→ single-FAT image (Alpine RPi shape) — mcopy"
    rm -rf "${DEST:?}"/*
    mcopy -i "$RAW" -s "::/*" "$DEST/"
else
    # Partitioned image (Raspbian/Debian/Fedora). losetup the
    # whole thing + mount partition 1 (the boot/vfat partition).
    echo "→ partitioned image — losetup + mount p1"
    LOOP=$(losetup -fP --show "$RAW")
    BOOT_PART="${LOOP}p1"

    # Wait a beat for partprobe to settle.
    sleep 0.5

    if [[ ! -b "$BOOT_PART" ]]; then
        echo "error: $BOOT_PART not found after losetup" >&2
        losetup -d "$LOOP"
        exit 3
    fi

    MNT="$TMP/boot"
    mkdir "$MNT"
    mount -o ro "$BOOT_PART" "$MNT"
    rsync -a --delete "$MNT/" "$DEST/"
    umount "$MNT"
    losetup -d "$LOOP"
    unset LOOP
fi

# Make TFTP-readable for the dnsmasq user.
chmod -R a+rX "$DEST"

# Quick sanity check — Pi PXE needs bootcode.bin OR start4.elf at
# minimum. Both are normal Pi-firmware file names.
echo "→ contents under $DEST:"
ls -la "$DEST" | head -20
echo
if [[ -f "$DEST/bootcode.bin" ]]; then
    echo "✓ bootcode.bin present (Pi 4 / older Pi 5 bootloader)"
fi
if [[ -f "$DEST/start4.elf" ]] || [[ -f "$DEST/start.elf" ]]; then
    echo "✓ start*.elf present"
fi
if ! [[ -f "$DEST/bootcode.bin" || -f "$DEST/start4.elf" || -f "$DEST/start.elf" ]]; then
    echo "⚠ no bootcode.bin / start*.elf — image may not PXE-boot." >&2
    echo "  Check that $IMG actually has a Pi boot partition." >&2
fi

echo
echo "Done. Next steps:"
echo "  1. Make sure /etc/dnsmasq.d/pi-bake-pxe.conf has:"
echo "       dhcp-host=$MAC,set:pi-X,<IP>,pi-X,1h"
echo "       dhcp-boot=tag:pi-X,$MAC_DASHED/bootcode.bin"
echo "  2. sudo systemctl reload dnsmasq"
echo "  3. Power-cycle the Pi. Tail /var/log/dnsmasq.log to watch."
