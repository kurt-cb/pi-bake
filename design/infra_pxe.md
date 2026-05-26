# PXE infrastructure — reference setup

**Scope.** This doc describes the operator-bench infrastructure
needed to PXE-boot pi-bake'd images for hardware testing. It is
**not part of pi-bake itself** — pi-bake produces flashable
images; this doc covers how to serve those images over the
network for boot-without-an-SD-card testing.

The lab setup described here was used during pi-bake v0.3
development to verify the Raspbian / Debian / Fedora backends
on real Pi 5 hardware. Captured here as reference so the next
lab build doesn't have to rediscover it.

**Audience.** Whoever sets up a PXE bench to test pi-bake images.
**Not** a how-to for operators consuming pi-bake images — they
just `dd` to SD and boot.

---

## §1  How Pi PXE boot works

```
power on
  ↓
Pi bootloader EEPROM checks BOOT_ORDER
  → finds "network" enabled (BOOT_ORDER nibble 2)
  ↓
DHCP DISCOVER broadcast on eth0
   vendor class: "PXEClient:Arch:00011:Raspberry Pi Boot"
  ↓
dnsmasq DHCP responds:
   - IP lease (from configured range)
   - option 66 (next-server)  = TFTP server IP
   - option 67 (boot file)    = <mac>/bootcode.bin
  ↓
Pi TFTPs the boot file. The bootloader uses the DIRECTORY
portion of the boot file path as a prefix for all subsequent
fetches (start4.elf, fixup4.dat, config.txt, cmdline.txt,
kernel8.img, *.dtb, overlays/*). So pointing option 67 at
"<mac>/bootcode.bin" auto-isolates each Pi's file set under
its own MAC-keyed subdir.
  ↓
Kernel boots, runs cmdline.txt's "init=" or default systemd.
Rootfs is whatever cmdline.txt's "root=" points at (NFS for
diskless, /dev/mmcblk0p2 if booting SD, etc.).
```

Key property: **the bootloader does not re-request a boot file
per stage.** It fetches `<prefix>/bootcode.bin` once and uses
the prefix for everything else. So per-MAC isolation only needs
the option-67 trick — no extra config beyond that.

---

## §2  Minimal dnsmasq config

Tested against dnsmasq 2.x on Fedora 41 + Debian 12 hosts.
Path: `/etc/dnsmasq.d/pi-bake-pxe.conf`.

```ini
# /etc/dnsmasq.d/pi-bake-pxe.conf
# Pi-bake PXE/TFTP lab — replaces ISP DHCP on the LAN
# Host IP: 192.168.4.2 (this machine, hardcoded static)
#
# Strict per-MAC PXE: only Pis we've explicitly registered get a
# boot file. Unknown Pis still get an IP lease (so we can read
# their MAC from the log + add a block below) but no boot file —
# they'll PXE-timeout safely instead of grabbing some other Pi's
# image.

# ─── Interface binding ───────────────────────────────────────────
interface=eth0
bind-interfaces
listen-address=127.0.0.1,192.168.4.2
no-dhcp-interface=lo

# ─── DNS forwarding ──────────────────────────────────────────────
no-resolv
no-poll
server=1.1.1.1
server=8.8.8.8
domain-needed
bogus-priv
cache-size=1000

# ─── DHCP for the LAN ────────────────────────────────────────────
dhcp-range=192.168.4.50,192.168.4.99,255.255.255.0,12h
dhcp-option=option:router,192.168.4.1
dhcp-option=option:dns-server,192.168.4.2
dhcp-option=option:domain-name,lab
dhcp-authoritative

log-dhcp
log-facility=/var/log/dnsmasq.log

# ─── TFTP ────────────────────────────────────────────────────────
enable-tftp
tftp-root=/var/lib/tftpboot
tftp-no-blocksize   # some Pi bootloaders dislike RFC 2348 blksize

# ─── Pi PXE: tag identification only (no global boot file) ───────
# Tag any client identifying as a Raspberry Pi bootloader so we
# can see them in the log. Crucially: NO `dhcp-boot=tag:rpi-any`
# line — unknown Pis get an IP lease and that's it. They'll PXE-
# timeout safely without grabbing some other Pi's image.
dhcp-vendorclass=set:rpi-any,PXEClient:Arch:00000:Raspberry Pi Boot
dhcp-vendorclass=set:rpi-any,PXEClient:Arch:00011:Raspberry Pi Boot

# Pi-vendor option (some bootloaders need this echoed back)
dhcp-option-force=tag:rpi-any,vendor:PXEClient,6,2b

# ─── Per-Pi registered blocks ────────────────────────────────────
# Each Pi we want to PXE-boot needs TWO lines:
#   1. dhcp-host: MAC → static IP + hostname + per-Pi tag
#   2. dhcp-boot: per-Pi tag → MAC-dashed boot file path
#
# The TFTP path uses the same MAC formatted with DASHES (filesystem-
# friendlier than colons). The Pi bootloader uses the directory
# component of the boot file as the prefix for ALL subsequent
# fetches (start4.elf, fixup4.dat, kernel8.img, *.dtb, overlays/*)
# — so we only need to point at <mac>/bootcode.bin and the rest
# is automatic.
#
# Add one pair of lines per Pi. Find the MAC from log-dhcp output
# the first time a new Pi attempts to PXE-boot.

# Pi #1 — pi-test-raspbian
# dhcp-host=aa:bb:cc:dd:ee:ff,set:pi1,192.168.4.51,pi-test-raspbian,1h
# dhcp-boot=tag:pi1,aa-bb-cc-dd-ee-ff/bootcode.bin

# Pi #2 — pi-test-debian
# dhcp-host=11:22:33:44:55:66,set:pi2,192.168.4.52,pi-test-debian,1h
# dhcp-boot=tag:pi2,11-22-33-44-55-66/bootcode.bin

# Pi #3 — pi-test-fedora
# dhcp-host=99:88:77:66:55:44,set:pi3,192.168.4.53,pi-test-fedora,1h
# dhcp-boot=tag:pi3,99-88-77-66-55-44/bootcode.bin
```

### Kill the ISP DHCP first

`dhcp-authoritative` means dnsmasq will respond aggressively. If
the ISP router is still serving DHCP, you'll get a race. Either:

- Disable DHCP in the router's admin (most consumer routers have
  a toggle).
- Or move the Pi + this host onto an isolated switch.

Otherwise the Pi may get an IP from the wrong server and
silently fail PXE (the wrong server doesn't set option 67).

### Starting

```bash
sudo systemctl enable --now dnsmasq
sudo journalctl -u dnsmasq -f
# OR: sudo tail -f /var/log/dnsmasq.log
```

---

## §3  TFTP tree layout

```
/var/lib/tftpboot/
├── aa-bb-cc-dd-ee-ff/                ← Pi #1 (MAC dashed, lowercase)
│   ├── bootcode.bin
│   ├── start4.elf  start.elf  start4cd.elf
│   ├── fixup4.dat  fixup.dat
│   ├── config.txt
│   ├── cmdline.txt
│   ├── kernel8.img
│   ├── bcm2712-rpi-5-b.dtb           ← Pi 5
│   ├── bcm2711-rpi-4-b.dtb           ← Pi 4
│   └── overlays/
│       └── *.dtbo
├── 11-22-33-44-55-66/                ← Pi #2 (different image set)
│   └── …
└── 99-88-77-66-55-44/                ← Pi #3
    └── …
```

Each Pi sees only its own subtree. The Pi bootloader prefixes
every TFTP fetch with the directory portion of the option-67 boot
file path, so total isolation comes for free once the option-67
path includes the MAC dir.

### Populating from a baked image

Use the helper script at `tools/img-to-tftp.sh` (in the pi-bake
repo):

```bash
# Bake an image first
pi-bake build --board pi-5 --os raspbian \
    --hostname pi-test-1 \
    --ssh-pubkey ~/.ssh/k.pub \
    --out /tmp/pi-test-1.img.xz

# Extract its boot partition into the MAC-keyed TFTP dir
sudo tools/img-to-tftp.sh aa:bb:cc:dd:ee:ff /tmp/pi-test-1.img.xz
```

The script handles `.img.gz` (Alpine), `.img.xz` (Raspbian /
Debian / Fedora), and raw `.img`. For partitioned images it
losetup-mounts the boot partition and rsyncs the contents; for
Alpine's single-FAT it `mcopy`s everything.

---

## §4  Pi 5 EEPROM setup (one-time, before PXE works)

Pi 5 EEPROM defaults to SD-first. Enable network boot:

```bash
# On a Pi 5 booted from SD with Pi OS or Alpine:
sudo rpi-eeprom-config --edit
# Set or add:
BOOT_ORDER=0xf241
# Save + reboot.
```

`BOOT_ORDER` nibbles (read right-to-left, first attempted first):

```
0xf  2  4  1
 │   │  │  └─ try 1 = SD card
 │   │  └──── try 4 = USB-MSD
 │   └─────── try 2 = network (PXE)
 └────────── restart from the right
```

So `0xf241` means: SD → USB → network → restart. To make PXE
the **first** attempt, swap: `0xf142` (network → SD → USB →
restart). Both work; first-attempt-PXE is slower at boot when
no PXE server is reachable but cleaner for the lab.

EEPROM is sticky — once set, every cold boot of that Pi tries
in that order forever.

### Pi 4

`BOOT_ORDER` works the same on Pi 4 (latest EEPROM). Older Pi 4
firmware may not honor network boot reliably; update via
`sudo rpi-eeprom-update -a` first.

---

## §5  CM4 specifics — Waveshare CM4-to-Pi4 adapter

For CM4-based bench Pis using the [Waveshare CM4-to-Pi4
adapter](https://www.waveshare.com/product/raspberry-pi/boards-kits/compute-module-4-4s-cat/cm4-to-pi4-adapter.htm),
the workflow is simpler than rpiboot. The adapter provides a
microSD slot + Pi 4 form factor for the CM4, so most of the
Pi 4 / Pi 5 PXE story applies directly.

### Bench setup variants

**CM4Lite (no eMMC):** Boots from SD card directly. No eMMC
boot order to worry about. Flash a pi-bake'd `.img.xz` to the
SD card; the CM4 boots from it on power-up. PXE is governed by
the SD card's `config.txt` + the bootloader EEPROM (same as
Pi 4).

**CM4 with eMMC:** eMMC normally has the boot priority. The
Waveshare adapter exposes a "disable eMMC" jumper — when set,
the CM4 ignores eMMC and falls through to the SD card slot. For
the bench, leave that jumper in "disable eMMC" so the bench SD
card always wins. (If you want eMMC-stored boot config to be
the source of truth — production deployment, not bench — leave
the jumper in "enable eMMC" and flash the EEPROM config via
rpiboot + the standard CM4 setup process.)

The Waveshare adapter's eMMC-disable jumper is labelled "EMMC"
on the silkscreen, near the USB-OTG port. Default position is
ENABLED (eMMC takes priority); flip to DISABLED for bench PXE
work.

### PXE flow on CM4 with Waveshare adapter

1. Flash a pi-bake'd `.img.xz` to a microSD card.
2. Insert the microSD into the Waveshare adapter's slot.
3. Set the eMMC-disable jumper to DISABLED (if CM4 has eMMC).
4. Power on.
5. CM4 bootloader reads `/config.txt` from SD card.
6. If `config.txt` doesn't have `program_usb_boot_mode=1` set
   AND the EEPROM has network boot enabled (BOOT_ORDER), PXE
   kicks in.

### CM4 wireless / Bluetooth caveat

The CM4 + Waveshare adapter exposes the CM4's onboard BCM43455
WiFi/BT. Same chip as Pi 4. Use `linux-firmware-brcm` in
`packages:` for Alpine bakes. For Raspbian/Debian/Fedora the
firmware ships in their default rootfs already.

### PCIe on the Waveshare adapter

The adapter does NOT expose CM4's PCIe lane (it's a Pi 4 form-
factor passthrough, no PCIe HAT slot). If you need PCIe HATs
(e.g. M.2 + BE200 setup), use a different carrier such as the
official CM4 IO board or a HAT-equipped carrier.

---

## §6  Discovery workflow for unknown Pi MACs

A new Pi powered on for the first time has no entry in the
dnsmasq config — its DHCP request gets logged but no boot file
is served. Use the log to read the MAC, then add a block.

```bash
# Watch the log:
sudo tail -f /var/log/dnsmasq.log

# Power on the new Pi. Watch for:
#   dnsmasq-dhcp[1234]: DHCPDISCOVER(eth0) aa:bb:cc:dd:ee:ff
#   dnsmasq-dhcp[1234]:    vendor class: PXEClient:Arch:00011:Raspberry Pi Boot
#   dnsmasq-dhcp[1234]:    tags: rpi-any
#   dnsmasq-dhcp[1234]: DHCPOFFER(eth0) 192.168.4.50 aa:bb:cc:dd:ee:ff
#   (Pi retries a few times, then gives up — no boot file offered)
#
# Copy the MAC. Then add to /etc/dnsmasq.d/pi-bake-pxe.conf:
#   dhcp-host=aa:bb:cc:dd:ee:ff,set:pi-new,192.168.4.51,pi-new,1h
#   dhcp-boot=tag:pi-new,aa-bb-cc-dd-ee-ff/bootcode.bin
#
# Populate the TFTP dir:
#   sudo tools/img-to-tftp.sh aa:bb:cc:dd:ee:ff /tmp/pi-new.img.xz
#
# Reload + power-cycle the Pi:
sudo systemctl reload dnsmasq
```

---

## §7  End-to-end test flow

Given a fresh bench (dnsmasq running, ISP DHCP off):

```bash
# 1. Bake an image
pi-bake build \
    --board pi-5 --os raspbian \
    --hostname pi-test-1 \
    --ssh-pubkey ~/.ssh/k.pub \
    --out /tmp/pi-test-1.img.xz

# 2. Power on the Pi for the first time to discover its MAC
sudo tail -f /var/log/dnsmasq.log
# (note the MAC, e.g. aa:bb:cc:dd:ee:ff, then Ctrl-C the tail)

# 3. Add per-Pi config block to dnsmasq + reload
sudo $EDITOR /etc/dnsmasq.d/pi-bake-pxe.conf
# add:
#   dhcp-host=aa:bb:cc:dd:ee:ff,set:pi-test-1,192.168.4.51,pi-test-1,1h
#   dhcp-boot=tag:pi-test-1,aa-bb-cc-dd-ee-ff/bootcode.bin
sudo systemctl reload dnsmasq

# 4. Populate the MAC-keyed TFTP dir from the baked image
sudo tools/img-to-tftp.sh aa:bb:cc:dd:ee:ff /tmp/pi-test-1.img.xz

# 5. Power-cycle the Pi
# (watch dnsmasq.log — should see DHCPACK with boot file, then
#  TFTP fetches of bootcode.bin → start4.elf → kernel → DTB)

# 6. Wait ~30s for boot. SSH in.
ssh root@192.168.4.51        # Debian / Alpine
ssh pi@192.168.4.51          # Raspbian
ssh fedora@192.168.4.51      # Fedora
```

---

## §8  Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Pi DHCPs but no TFTP fetches | option 67 not delivered | Check `dhcp-boot=tag:…` line; ensure the per-Pi tag set in `dhcp-host` matches the tag in `dhcp-boot` |
| TFTP fetches start but fail at `start4.elf` | Bootloader prefix isn't honoring option-67's directory | Verify the boot file path in option 67 is `<mac-dashed>/bootcode.bin` (with the directory component, not just `bootcode.bin`) |
| `dhcpcd` on the Pi fails with APIPA | Race with ISP DHCP server | Confirm ISP router DHCP is OFF; check `sudo tail -f /var/log/dnsmasq.log` shows DHCPACK |
| Pi boots fine but can't SSH in | Pi got a different IP than expected | Check the `dhcp-host` IP matches what the Pi is using; force a fresh lease via power-cycle |
| Unknown DHCP request from non-Pi devices | Lab LAN has other clients | Acceptable — dnsmasq will lease them from the range; only Pis tagged `rpi-any` get the PXE-specific options |
| `tftp-no-blocksize` warning at dnsmasq startup | Old dnsmasq version | Upgrade dnsmasq, or remove that line (its absence is harmless on most Pi bootloaders) |
| `losetup` errors in `tools/img-to-tftp.sh` | LXC container doesn't have CAP_SYS_ADMIN | Run the script on the host, or grant the LXC the right capability |
| `interface=eth0` in conf, dnsmasq starts fine but `ss -ulnp` shows no `:67` listener | dnsmasq's `bind-interfaces` requires the named interface to exist; if the actual LAN NIC is `enp0s3` / `eno1` / etc., DHCP is silently skipped | `ip -br link` to find the real LAN iface name; set `interface=<actual>` in the conf; `systemctl restart dnsmasq`; confirm `ss -ulnp` now lists `:67` |
| tcpdump sees Pi's DHCPDISCOVER on `enp0s3` but dnsmasq logs nothing (even with `log-dhcp`); `ss -ulnp` confirms dnsmasq IS bound to UDP 67 on the right iface | **Firewalld** is dropping the broadcast before it reaches dnsmasq. Default Fedora `FedoraWorkstation` zone allows `dhcpv6-client` (host AS DHCP client) but NOT `dhcp` (host AS DHCP server). Same for the trailing `iifname "enp0s3" reject` catch-all rule. | `sudo firewall-cmd --zone=<zone> --add-service=dhcp --permanent` (also `--add-service=tftp` if not already), then `sudo firewall-cmd --reload`. Verify: `sudo firewall-cmd --info-zone=<zone> \| grep services` shows `dhcp`. Verified on Fedora Workstation 41–43. |

---

## §9  Lab-only — not for production / downstream integration

This dnsmasq setup is bench-test infrastructure. It replaces the
LAN's existing DHCP for the duration of a PXE test pass, then
gets shut down. **Do not bake operator/customer deployment
config into this setup** — production DHCP/DNS belongs in a
downstream tool (whatever DNS/DHCP appliance the operator
actually ships), not in pi-bake's design dir.

If a downstream project (totaldns or any other) wants to
integrate PXE serving into its own DHCP/DNS appliance, the
mechanics here (vendor-class tagging, MAC-keyed boot file
paths, TFTP-tree layout) transfer cleanly — but the integration
work happens IN the downstream project, not in pi-bake.

---

## §10  References

- Raspberry Pi network boot:
  https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#network-boot
- Pi 4 bootloader source (option 67 prefix behavior):
  https://github.com/raspberrypi/rpi-eeprom
- dnsmasq man page: `man 8 dnsmasq` (look for `dhcp-host`,
  `dhcp-boot`, `dhcp-vendorclass`, `enable-tftp`)
- Waveshare CM4-to-Pi4 adapter:
  https://www.waveshare.com/product/raspberry-pi/boards-kits/compute-module-4-4s-cat/cm4-to-pi4-adapter.htm
