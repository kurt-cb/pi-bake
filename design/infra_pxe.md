# PXE infrastructure — reference setup

**Scope.** This doc describes the operator-bench infrastructure
needed to PXE-boot pi-bake'd images for hardware testing. It is
**not part of pi-bake itself** — pi-bake produces flashable
images; this doc covers how to serve those images over the
network for boot-without-an-SD-card testing.

The lab setup described here was used during pi-bake v0.3
development to verify the Raspbian / Debian / Fedora backends
on real Pi 5 / CM4 hardware. **Major rewrite 2026-05-26** after
the first end-to-end CM4 bring-up surfaced a long list of
incorrect assumptions baked into the initial design. The
current text reflects what actually works on real hardware.

**Audience.** Whoever sets up a PXE bench to test pi-bake images.
**Not** a how-to for operators consuming pi-bake images — they
just `dd` to SD and boot.

---

## §1  How Pi PXE boot actually works

The Pi 4 / CM4 / Pi 5 bootloader behaves quite differently from
the Pi 3 here. Most pi-bake testing targets the Pi 4 generation,
so the modern path is documented as primary.

### §1.1  Pi 4 / CM4 / Pi 5 path (modern; pi-bake's primary target)

```
power on
  ↓
Pi bootloader EEPROM (on-chip) starts. NO bootcode.bin fetch —
the equivalent runs entirely from EEPROM. Checks BOOT_ORDER;
when it hits the "network" nibble (2), continues:
  ↓
DHCP DISCOVER on eth0, vendor class:
   "PXEClient:Arch:00000:UNDI:002001"
   (yes, Arch:00000 — despite being ARM, the Pi firmware uses
    the legacy PC-BIOS arch code for PXE compat. Don't try to
    match "Arch:00011" — that's iPXE / EFI clients, not the
    Pi firmware.)
  ↓
dnsmasq DHCP responds:
   - IP lease
   - option 60 (vendor-class echo back) via pxe-service
   - option 66 (next-server) = TFTP server IP
   - option 67 (boot file) — IGNORED by Pi 4+ for prefix
     resolution (see below) — but dnsmasq still has to send
     SOMETHING valid here.
  ↓
Pi sends DHCPREQUEST + receives DHCPACK.

Pi firmware then TFTPs files. **It does NOT use option-67's
directory as the prefix.** Instead, it uses ITS OWN convention:
   - try  <serial-hex>/<file>    first
   - then <file>                 at TFTP root

Where <serial-hex> is the Pi's 8-character hex serial number
(e.g. "7614437e" for a CM4). The serial is on the SoC, baked at
manufacture; visible in /proc/cpuinfo's "Serial" line or via
`vcgencmd otp_dump | grep ^28:` on a booted Pi.

Files the firmware fetches (in order, each at the same prefix):
   start4.elf       (stage-2 firmware; mandatory)
   fixup4.dat       (memory map fixup; mandatory)
   config.txt       (boot config; mandatory)
   cmdline.txt      (kernel command line; mandatory)
   boot/vmlinuz-rpi (kernel — path from config.txt's `kernel=` line;
                     Alpine puts kernel in boot/, Pi OS uses kernel8.img
                     at root)
   boot/initramfs-rpi  (initramfs — path from config.txt)
   <board>.dtb      (device tree, e.g. bcm2711-rpi-cm4.dtb for CM4)
   overlays/<*>.dtbo (overlays referenced from config.txt)

It also probes for optional files (404s are normal, harmless):
   usercfg.txt, pieeprom.sig, recover4.elf, recovery.elf,
   bootcfg.txt, dt-blob.bin, armstub8-gic.bin
  ↓
Kernel boots from RAM. cmdline.txt's `root=` determines rootfs:
   - root=/dev/mmcblk0p2  → expects SD card present (defeats PXE)
   - root=/dev/nfs        → NFS-mount via nfsroot= argument
   - (anything else: kernel panic at mount root_fs)
```

### §1.2  Pi 3 path (legacy; if you're targeting old hardware)

Pi 3 bootloader is in an SPI flash + reads `bootcode.bin` from
the first boot source (SD or network). On PXE:
- Fetches `bootcode.bin` via TFTP from option-67's path
- THEN honors option-67's directory as the prefix for all subsequent
  files

So Pi 3 PXE actually works with MAC-keyed (or any) per-Pi
prefixes via dnsmasq's `dhcp-boot=` line. Pi 4+ doesn't.

**pi-bake's tooling targets Pi 4+ behavior.** Pi 3 PXE works as
a side effect (the serial-keyed dir + a `dhcp-boot=` pointing
at it would also work), but isn't a first-class target.

---

## §2  Minimal dnsmasq config

Tested against dnsmasq 2.92 on Fedora 41-44 hosts.
Path: `/etc/dnsmasq.d/pi-bake-pxe.conf`.

```ini
# /etc/dnsmasq.d/pi-bake-pxe.conf
# Pi-bake PXE/TFTP lab — replaces ISP DHCP on the LAN.
# Host IP: 192.168.4.2 (this machine, hardcoded static).
#
# Strict per-MAC IP pinning + Pi-PXE service offering. Unknown
# Pis still get an IP lease (so we can read their serial from
# TFTP attempts) but no boot file. They'll PXE-loop harmlessly
# until added.

# ─── Interface binding ───────────────────────────────────────────
# Match this to the actual LAN interface (ip -br link). Old habit
# of writing "eth0" silently breaks DHCP on modern Fedora hosts
# where the iface is enp0s3 / eno1 / wlp2s0 / etc.
interface=enp0s3
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

# ─── Pi PXE ──────────────────────────────────────────────────────
# Match Pi clients by the start of the vendor class. The Pi
# firmware sends "PXEClient:Arch:00000:UNDI:002001"; substring
# match "PXEClient:Arch:00000" covers all Pi 4+ generations.
# (Pi 3 bootcode.bin client may send a different string; add a
# Arch:00011 match if you also want to catch iPXE / EFI loaders.)
dhcp-vendorclass=set:rpi-any,PXEClient:Arch:00000
dhcp-vendorclass=set:rpi-any,PXEClient:Arch:00011

# REQUIRED for Pi 4+ PXE. Without this directive the Pi
# bootloader rejects the DHCPOFFER and re-DISCOVER-loops forever.
# pxe-service makes dnsmasq behave as a "real" PXE server (echoes
# back the vendor class + sends a PXE menu entry).
# Arch 0 corresponds to "PXEClient:Arch:00000" (Pi firmware
# uses the legacy PC-BIOS arch code).
pxe-service=tag:rpi-any,0,"Raspberry Pi Boot"

# ─── Per-Pi pinning (one block per Pi) ───────────────────────────
# Pi 4+ DOES NOT use the option-67 path prefix — the bootloader
# resolves TFTP paths by SERIAL NUMBER, not MAC. dnsmasq's job
# is just IP pinning + hostname. The dhcp-boot= line is NOT
# needed for Pi 4+ (kept for documentation only; harmless).
#
# Discover a Pi's serial from the dnsmasq.log TFTP attempts the
# first time it powers on (see §6).
#
# Example: CM4 with MAC 88:a2:9e:44:31:f3 and serial 7614437e
# dhcp-host=88:a2:9e:44:31:f3,set:cm4-1,192.168.4.51,pxe-test-cm4,1h
# # The dhcp-boot below is REQUIRED for Pi 3 (which honors option 67),
# # IGNORED by Pi 4/CM4/Pi 5 (which uses <serial>/<file> prefix).
# dhcp-boot=tag:cm4-1,7614437e/start4.elf
```

### §2.1  Kill the ISP DHCP first

`dhcp-authoritative` makes dnsmasq respond aggressively. If the
ISP router is still serving DHCP, you'll get a race. Disable it
in the router admin OR move the Pi + this host onto an isolated
switch.

### §2.2  Starting

```bash
sudo systemctl enable --now dnsmasq
sudo journalctl -u dnsmasq -f
# OR: sudo tail -f /var/log/dnsmasq.log
```

### §2.3  Firewall (Fedora)

Default Fedora `FedoraWorkstation` zone blocks DHCP server (only
allows `dhcpv6-client`). Add `dhcp` + `tftp` services to the
zone the LAN interface is in:

```bash
ZONE=$(sudo firewall-cmd --get-zone-of-interface=enp0s3)
sudo firewall-cmd --zone=$ZONE --add-service=dhcp --add-service=tftp --permanent
sudo firewall-cmd --reload
```

Without this, tcpdump sees the Pi's DHCPDISCOVER on the wire
but dnsmasq logs nothing — firewalld drops the broadcast before
dnsmasq's socket sees it.

### §2.4  TFTP directory permissions

The TFTP daemon (running as the `dnsmasq` user) needs `o+x` on
`/var/lib/tftpboot` to traverse it:

```bash
sudo chmod o+x /var/lib/tftpboot       # mode -> 0755 or 0775
ls -ld /var/lib/tftpboot/              # confirm trailing "r-x" not "r--"
```

Without this, dnsmasq logs `failed sending ...` or `cannot access`
messages (the wording is misleading — looks like a network failure
but is actually a permission denial on the directory traversal).

---

## §3  TFTP tree layout (serial-keyed)

```
/var/lib/tftpboot/                          ← TFTP root
└── <pi-serial-hex>/                        ← per-Pi subdir (8 hex chars)
    ├── start4.elf  start.elf  start4cd.elf
    ├── fixup4.dat  fixup.dat
    ├── config.txt
    ├── cmdline.txt
    ├── boot/                               ← Alpine kernel layout
    │   ├── vmlinuz-rpi
    │   ├── initramfs-rpi
    │   └── modloop-rpi
    ├── kernel8.img                         ← Pi OS / Debian kernel layout
    ├── bcm2711-rpi-cm4.dtb                 ← Pi 4 / CM4
    ├── bcm2712-rpi-5-b.dtb                 ← Pi 5
    └── overlays/
        └── *.dtbo
```

The Pi bootloader prefixes every TFTP fetch with
`<serial>/`, so total isolation between Pis on the same TFTP
server comes for free — no shared content under TFTP root.

### §3.1  Pi serial number — where to find it

- **From dnsmasq.log** (preferred; works for unknown new Pis):
  Power-cycle the Pi with no per-Pi subdir populated. dnsmasq
  will log `cannot access /var/lib/tftpboot/<SERIAL>/start4.elf`
  (or `file ... not found`). The `<SERIAL>` is the Pi's
  8-character hex serial.
- **From a Pi-booted Pi:** `vcgencmd otp_dump | awk -F: '/^28:/{print $2}'`
  (value is hex serial, 8 chars after leading zeros).
- **From `/proc/cpuinfo`:** `cat /proc/cpuinfo | grep ^Serial`
  (note: the cpuinfo serial is 16 hex chars but only the LAST 8
  are what the PXE bootloader uses).

### §3.2  Populating from a baked image

Use `tools/img-to-tftp.sh`:

```bash
# After baking an image:
pi-bake build --config recipe.yaml
# Extract its boot partition into the Pi's serial-keyed TFTP dir:
sudo tools/img-to-tftp.sh 7614437e ~/sdcards/pxe-test-cm4.img.gz
```

The script handles `.img.gz` (Alpine), `.img.xz` (Raspbian /
Debian / Fedora), and raw `.img`. For partitioned images it
losetup-mounts the boot partition and rsyncs; for Alpine's
single-FAT image it `mcopy`s everything.

---

## §4  Pi 5 EEPROM setup

Pi 5 EEPROM defaults to SD-first. Most CM4s already have
network in BOOT_ORDER by default (verified empirically on
CM4Lite + Waveshare CM4-to-Pi4 adapter, 2026-05). To verify
or change:

```bash
# On a Pi booted from SD with Pi OS:
sudo rpi-eeprom-config --edit
# Set or add:
BOOT_ORDER=0xf142
# Save + reboot.
```

`BOOT_ORDER` nibbles (read right-to-left, first attempted first):

```
0xf  1  4  2
 │   │  │  └─ try 2 = network (PXE)
 │   │  └──── try 4 = USB-MSD
 │   └─────── try 1 = SD card
 └────────── restart from the right
```

For PXE-first: `0xf142` (network → SD → USB → loop). Or
network-only (no SD fallback): `0xf2`.

**Alpine does NOT ship `rpi-eeprom`** (no package in main /
community as of Alpine 3.21). If you're running Alpine on the
Pi and need to change BOOT_ORDER, the path is: flash Raspbian
once (it has `rpi-eeprom-config` baseline), edit + reboot —
the EEPROM update sticks. Then flash back to whatever you
wanted.

---

## §5  CM4 specifics — Waveshare CM4-to-Pi4 adapter

For CM4-based bench Pis using the [Waveshare CM4-to-Pi4
adapter](https://www.waveshare.com/product/raspberry-pi/boards-kits/compute-module-4-4s-cat/cm4-to-pi4-adapter.htm),
the workflow is simpler than rpiboot. The adapter provides a
microSD slot + Pi 4 form factor for the CM4, so most of the
Pi 4 PXE story applies directly.

### §5.1  CM4 boot priorities

- **CM4Lite (no eMMC):** boots from SD card OR network (via
  PXE) directly. No jumpers to set.
- **CM4 with eMMC:** if the eMMC contains a valid bootloader,
  it takes priority. The Waveshare adapter has an `nRPIBOOT` /
  `EMMC-DISABLE` jumper near the USB-OTG port — set to DISABLED
  to force SD/PXE-only boot.

### §5.2  CM4 default BOOT_ORDER

Most CM4s (verified 2026-05) ship with network in BOOT_ORDER
out of the box — so PXE Just Works without an EEPROM edit. If
PXE isn't reaching dnsmasq.log on a fresh CM4, the EEPROM might
be the older `0x1` or `0xf41` (no network); see §4 for the
update path.

### §5.3  CM4 wireless / Bluetooth

Built-in BCM43455 WiFi/BT (same chip as Pi 4). Use
`linux-firmware-brcm` in `packages:` for Alpine; ships in
Raspbian/Debian/Fedora rootfs by default.

### §5.4  PCIe

The Waveshare CM4-to-Pi4 adapter does NOT expose CM4's PCIe lane
(it's a Pi 4 form-factor passthrough, no HAT slot). For PCIe
HATs (e.g. BE200 in M.2), use a different carrier such as the
official CM4 IO board.

---

## §6  Discovery workflow for an unknown Pi

The first time a new Pi powers on, you don't know its serial or
MAC. Two-step discovery from the dnsmasq log:

```bash
sudo tail -f /var/log/dnsmasq.log
```

Power on the Pi. Watch for two pieces of info:

1. **MAC** (from DHCPDISCOVER):
   ```
   dnsmasq-dhcp[…]: DHCPDISCOVER(enp0s3) 88:a2:9e:44:31:f3
   dnsmasq-dhcp[…]:    vendor class: PXEClient:Arch:00000:UNDI:002001
   ```
2. **Serial** (from TFTP attempts, after DHCPACK):
   ```
   dnsmasq-tftp[…]: file /var/lib/tftpboot/7614437e/start4.elf not found
   ```

With both, you can:

```bash
# 1. Populate the serial-keyed TFTP dir from a baked image:
sudo tools/img-to-tftp.sh 7614437e ~/sdcards/<host>.img.gz

# 2. (Optional) Pin the Pi's IP via dnsmasq:
sudo tee -a /etc/dnsmasq.d/pi-bake-pxe.conf <<EOF

# Pi #X (CM4, serial 7614437e)
dhcp-host=88:a2:9e:44:31:f3,set:pi-X,192.168.4.51,pi-X,1h
EOF
sudo systemctl reload dnsmasq

# 3. Power-cycle the Pi. Should boot the served image.
```

---

## §7  End-to-end test flow

Given a fresh bench (dnsmasq running, ISP DHCP off, firewall
allows dhcp + tftp, /var/lib/tftpboot has o+x):

```bash
# 1. Bake an image
pi-bake build \
    --board pi-4 --os alpine \
    --hostname pi-test-1 \
    --ssh-pubkey ~/.ssh/k.pub \
    --out /tmp/pi-test-1.img.gz

# 2. Power on the Pi to discover its MAC + serial
sudo tail -f /var/log/dnsmasq.log
# (note MAC + serial — see §6, then Ctrl-C)

# 3. Add per-Pi IP pin to dnsmasq + reload
sudo $EDITOR /etc/dnsmasq.d/pi-bake-pxe.conf
sudo systemctl reload dnsmasq

# 4. Populate the serial-keyed TFTP dir
sudo tools/img-to-tftp.sh <SERIAL> /tmp/pi-test-1.img.gz

# 5. Power-cycle the Pi
# (watch dnsmasq.log — should see DHCP cycle complete + TFTP
#  fetches of start4.elf, fixup4.dat, config.txt, cmdline.txt,
#  kernel, dtb, etc.)
```

Note: **The Pi will get the kernel via PXE, but Alpine needs
a rootfs source.** Without SD card AND without NFS/HTTP-served
apkovl, kernel boots to a console-only busybox shell. See §11.

---

## §8  Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| dnsmasq starts fine but `ss -ulnp` shows no `:67` for the pi-bake dnsmasq PID | `interface=` in conf names a NIC that doesn't exist on this host (e.g. `eth0` vs actual `enp0s3`); `bind-interfaces` silently drops DHCP serving | `ip -br link` to find the real LAN iface name; set `interface=<actual>` in conf; restart dnsmasq; confirm `ss -ulnp` now lists `:67` |
| tcpdump sees Pi's DHCPDISCOVER on the wire but dnsmasq logs nothing (even with `log-dhcp`) | Firewalld is dropping the broadcast pre-dnsmasq. Fedora default `FedoraWorkstation` zone allows `dhcpv6-client` (client) but NOT `dhcp` (server) | `sudo firewall-cmd --zone=<zone> --add-service=dhcp --permanent` (also `tftp` if not already), then `sudo firewall-cmd --reload`. Verify: `firewall-cmd --info-zone=<zone> \| grep services` shows `dhcp` |
| Pi sends DHCPDISCOVER → dnsmasq sends DHCPOFFER → Pi never DHCPREQUESTs, just re-DISCOVERS with same XID | Missing `pxe-service` directive in dnsmasq conf. Pi 4+ bootloader rejects offers that don't include a PXE service declaration | Add `pxe-service=tag:rpi-any,0,"Raspberry Pi Boot"` to conf, restart dnsmasq |
| Pi's `tags:` in dnsmasq.log don't include `rpi-any` even though it's clearly a Pi (vendor class shows PXEClient) | `dhcp-vendorclass` substring match is too restrictive. Pi sends `PXEClient:Arch:00000:UNDI:002001`; matching against `:Raspberry Pi Boot` fails | Loosen the match to a stable prefix: `dhcp-vendorclass=set:rpi-any,PXEClient:Arch:00000` |
| TFTP fetches go to wrong paths: `/var/lib/tftpboot/<file>` and `/var/lib/tftpboot/<SERIAL>/<file>`, not the MAC-keyed path you set in dhcp-boot | **Pi 4 / CM4 / Pi 5 bootloader ignores option-67's directory prefix.** Uses its own convention: `<serial-hex>/<file>` then fallback to `<file>` at root | Populate `/var/lib/tftpboot/<SERIAL>/` instead of `/var/lib/tftpboot/<MAC-dashed>/`. Use `tools/img-to-tftp.sh <SERIAL> <image>` (script takes serial, not MAC, as of 2026-05) |
| dnsmasq logs `failed sending /var/lib/tftpboot/<serial>/start4.elf` or `cannot access ...` for files that DO exist | Permission denied on directory traversal — TFTP daemon (running as `dnsmasq` user) lacks `o+x` on `/var/lib/tftpboot`. Wording in dnsmasq is misleading ("failed sending" looks like network) | `sudo chmod o+x /var/lib/tftpboot` (mode → 0755 or 0775). Verify trailing `r-x` not `r--` in `ls -ld /var/lib/tftpboot/` |
| Optional file 404s for `usercfg.txt`, `pieeprom.sig`, `recover4.elf`, `recovery.elf`, `bootcfg.txt`, `dt-blob.bin`, `armstub8-gic.bin` | Pi firmware probes for these; absence is normal for a stock pi-bake image | None — ignore the 404s. Real failure is if `start4.elf`, `fixup4.dat`, `config.txt`, `cmdline.txt`, kernel, or DTB 404s |
| `failed sending /var/lib/tftpboot/<serial>/<file>` followed shortly by `sent ...` for the same file | TFTP is UDP; firmware retransmit handles transient packet loss/timing | None — log noise, not a failure |
| `dhcpcd` on the Pi fails with APIPA after PXE boot | Race with ISP DHCP server (still active) | Confirm ISP router DHCP is OFF; check dnsmasq.log shows DHCPACK |
| Pi boots fine but can't SSH in | Pi got a different IP than pinned (probably a stale lease) | Force fresh lease on Pi side OR confirm `dhcp-host` MAC matches what Pi actually sends; `sudo systemctl restart dnsmasq` (not `reload`) is sometimes required for new dhcp-host entries to take effect for already-leased clients |
| `losetup` errors in `tools/img-to-tftp.sh` from inside an LXC container | Container lacks CAP_SYS_ADMIN OR doesn't have loop devices passed through. Loop control is a GLOBAL kernel resource, not namespaceable | Run on the host, OR use a privileged container with `/dev/loop-control` device passthrough |

---

## §9  Lab-only — not for production / downstream integration

This dnsmasq setup is bench-test infrastructure. It replaces the
LAN's existing DHCP for the duration of a PXE test pass, then
gets shut down. Production DHCP/DNS belongs in a downstream tool
(whatever DNS/DHCP appliance the operator actually ships), not
in pi-bake's design dir.

If a downstream project wants to integrate PXE serving into its
own DHCP/DNS appliance, the mechanics here (vendor-class tagging,
serial-keyed TFTP-tree layout, pxe-service directive, firewalld
service entries) transfer cleanly — but the integration work
happens IN the downstream project, not in pi-bake.

---

## §10  References

- Raspberry Pi network boot:
  https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#network-boot
- Pi 4 bootloader source (TFTP prefix behavior):
  https://github.com/raspberrypi/rpi-eeprom
- dnsmasq man page: `man 8 dnsmasq` — keywords `dhcp-host`,
  `dhcp-boot`, `dhcp-vendorclass`, `pxe-service`, `enable-tftp`
- Waveshare CM4-to-Pi4 adapter:
  https://www.waveshare.com/product/raspberry-pi/boards-kits/compute-module-4-4s-cat/cm4-to-pi4-adapter.htm

---

## §11  Rootfs delivery (what's MISSING from this doc)

PXE-boot of the kernel is now verified end-to-end. To get a
USABLE Pi (not just a kernel that boots to console), you need
the rootfs delivered somehow:

| OS | SD card present | NFS-rootfs | Alpine apkovl over HTTP |
|----|----------------|------------|-------------------------|
| Alpine | ✓ works (PXE kernel + SD apkovl) | not natively supported | requires pi-bake feature work |
| Raspbian | ✓ works (hybrid) | ✓ standard pattern | n/a |
| Debian | ✓ works (hybrid) | ✓ standard pattern | n/a |
| Fedora | ✓ works (hybrid) | ✓ standard pattern | n/a |

**Not yet documented:**

- NFS-rootfs setup on the dnsmasq host (`nfs-utils` install, `/etc/exports` config, firewall services)
- `tools/img-to-nfs.sh` (sibling to `tools/img-to-tftp.sh`) for extracting the rootfs partition of an `.img.xz` into the NFS export dir
- Pi-bake feature for PXE-friendly Alpine bakes (apkovl over HTTP, repositories pointing at HTTP)

These are next-session work, captured here so the gap is explicit.
