#!/usr/bin/env bash
# img-to-tftp.sh — extract a pi-bake'd image's boot files into
# a serial-keyed TFTP dir for Pi PXE serving.
#
# Usage:
#   sudo tools/img-to-tftp.sh <SERIAL> <image-file> [tftp-root]
#
# Examples:
#   sudo tools/img-to-tftp.sh 7614437e /tmp/pi-test-1.img.xz
#   sudo tools/img-to-tftp.sh 7614437e /tmp/td-pi5-1.img.gz /srv/tftp
#
# What it does:
#   1. Decompress (.img.gz | .img.xz | .img all handled).
#   2. Detect partitioned (Raspbian/Debian/Fedora) vs single-FAT
#      (Alpine RPi tarball-bake) and extract the boot partition.
#   3. Copy the boot file set into <tftp-root>/<serial>/
#      where <serial> is the Pi's 8-hex-char serial number
#      (e.g. 7614437e — verified via dnsmasq.log TFTP attempts
#      OR `vcgencmd otp_dump | grep ^28:` on a booted Pi).
#
# Why serial-keyed, not MAC-keyed (LESSON LEARNED — see
# design/infra_pxe.md):
#   Pi 4 / CM4 / Pi 5 bootloader IGNORES option-67's directory
#   prefix and uses its OWN convention: tries `<serial>/<file>`
#   first, then falls back to `<file>` at TFTP root. The MAC
#   you set in dnsmasq's `dhcp-boot=` is irrelevant for Pi 4+.
#   Serial-number is what the bootloader actually looks for.
#
#   Pi 3's bootloader DOES honor option 67, so MAC-keyed would
#   work there — but for Pi 4+ uniformity, this script targets
#   serial-keyed always.
#
# How to discover a Pi's serial:
#   1. Power on the Pi with no per-Pi TFTP dir populated.
#   2. dnsmasq.log will show TFTP fetch attempts like:
#        file /var/lib/tftpboot/7614437e/start4.elf not found
#      → the `7614437e` is the Pi's serial.
#   3. Or, on a booted Pi-OS Pi: `vcgencmd otp_dump | grep ^28:`
#      (the value after `:` is the serial in hex).
#
# See design/infra_pxe.md for the full PXE lab setup.

set -euo pipefail

usage() {
    sed -n 's/^# \?//;1,/^$/p' "$0" >&2
    exit 2
}

[[ $# -ge 2 ]] || usage

SERIAL="$1"
IMG="$2"
TFTP_ROOT="${3:-${TFTP_ROOT:-/var/lib/tftpboot}}"

# Validate serial format: 8 hex chars, case-insensitive (we normalize
# to lowercase for filesystem use). The Pi bootloader formats the
# serial as 8 lowercase hex chars — match that exactly.
if ! [[ "$SERIAL" =~ ^[0-9a-fA-F]{8}$ ]]; then
    echo "error: SERIAL must be 8 hex chars (e.g. 7614437e); got $SERIAL" >&2
    echo "       find it via dnsmasq.log TFTP attempts, or on a booted Pi:" >&2
    echo "         vcgencmd otp_dump | awk -F: '/^28:/{print \$2}'" >&2
    exit 2
fi
SERIAL=$(echo "$SERIAL" | tr '[:upper:]' '[:lower:]')

[[ -f "$IMG" ]] || { echo "error: image not found: $IMG" >&2; exit 2; }

# Need root for tftp dir writes + losetup/mount on partitioned images.
if [[ "$EUID" -ne 0 ]]; then
    echo "error: needs root (re-run with sudo)" >&2
    exit 2
fi

DEST="$TFTP_ROOT/$SERIAL"

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
# Single-FAT (Alpine RPi-style) is readable by mdir directly.
# Partitioned (Raspbian/Debian/Fedora) is not, fall through to losetup.
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

# Make TFTP-readable for the dnsmasq daemon. The PARENT dir
# (/var/lib/tftpboot) needs o+x too, but that's a one-time
# operator setup — flagged in design/infra_pxe.md §8.
chmod -R a+rX "$DEST"

# Quick sanity check. Pi 4 / CM4 / Pi 5 firmware reads
# start4.elf or start.elf from the bootloader EEPROM's first
# TFTP attempt (NOT bootcode.bin — that's the Pi 3 / older path).
echo "→ contents under $DEST:"
ls -la "$DEST" | head -20
echo
if [[ -f "$DEST/start4.elf" ]] || [[ -f "$DEST/start.elf" ]]; then
    echo "✓ start*.elf present (Pi 4 / CM4 / Pi 5 path)"
fi
if [[ -f "$DEST/bootcode.bin" ]]; then
    echo "✓ bootcode.bin present (Pi 3 / older path; ignored by Pi 4+)"
fi
if ! [[ -f "$DEST/start4.elf" || -f "$DEST/start.elf" ]]; then
    echo "⚠ no start*.elf — image will not PXE-boot on Pi 4/CM4/Pi 5." >&2
    echo "  Check that $IMG actually has a Pi boot partition." >&2
fi

echo
echo "Done. Next steps:"
echo "  1. Make sure /var/lib/tftpboot/ is daemon-traversable:"
echo "       ls -ld /var/lib/tftpboot/   # mode should end in r-x not r--"
echo "       sudo chmod o+x /var/lib/tftpboot   # if needed"
echo "  2. Optional — pin the Pi's IP via /etc/dnsmasq.d/pi-bake-pxe.conf:"
echo "       dhcp-host=<MAC>,set:pi-X,<IP>,pi-X,1h"
echo "       (NOTE: dhcp-boot= line NOT needed for Pi 4/CM4/Pi 5 — they"
echo "        ignore option-67 prefix and use $SERIAL/ regardless.)"
echo "  3. sudo systemctl reload dnsmasq"
echo "  4. Power-cycle the Pi. Tail /var/log/dnsmasq.log to watch."
