# pi-bake roadmap

## v0.2 — top of list

### Bake-time package fetch (avahi + firmware)
Alpine RPi's stock /apks cache ships sshd, dhcpcd, chrony,
wpa_supplicant — enough for "real sshd + an IP". But .local
discovery via avahi-daemon, dbus, and WiFi firmware blobs
(linux-firmware-brcm for Pi-built-in BCM43455, linux-firmware-intel
for BE200) are NOT in the stock cache.

Plan: at bake time, after extracting the upstream tarball, do a
local `apk fetch` against the upstream Alpine mirror to drop
extra .apk files into `apks/aarch64/` and regenerate the
APKINDEX.tar.gz. Then add those packages to /etc/apk/world so
the diskless init's `apk add --no-network` picks them up from the
now-enriched local cache on first boot. No podman, no chroot —
just `apk fetch --no-cache --recursive --output ...` on the bake
host, which works as a regular user with the apk-tools package.

The 936f233 baseline (sshd + dhcpcd + chrony) is sufficient for
pyinfra take-over. Avahi is a discoverability nice-to-have, not a
blocker.

### Interactive mode + YAML recipes (UX)
The `pi-bake build` CLI is now flag-heavy enough that nobody will
remember it. Add an interactive walk-through:

```
pi-bake build --interactive
  ? Which board? (pi-5, pi-zero-2-w, …)        > pi-5
  ? Which OS?    (alpine 3.21.4, raspbian, …)  > alpine 3.21.4
  ? Hostname?                                   > td-pi5-1
  ? SSH pubkey path? (Tab to browse)           > ~/.ssh/totaldns-adhoc.pub
  ? WiFi? [y/N]                                 > N
  ? Static IP? [y/N]                            > y
    ? CIDR?                                     > 192.168.4.111/24
    ? Gateway?                                  > 192.168.4.1
  ? HATs / expansion? (multi-select)
      [x] Intel BE200 M.2 WiFi 7 (PCIe HAT)
      [ ] PoE+ HAT (Pi 5)
      [ ] Sense HAT
      [ ] Adafruit 2.8" PiTFT 320x240
  ? Save recipe to YAML? [td-pi5-1.yaml]
  ? Bake now? [Y/n]
```

The HAT picker drives config.txt edits (dtoverlay=, dtparam=).

### YAML recipe in/out
- `pi-bake build --from-yaml pi-1.yaml` — bake from a saved recipe.
- `pi-bake build --to-yaml pi-1.yaml ...` — write the recipe without baking.
- `pi-bake build --interactive --to-yaml ...` — walk-through saves.

Means batch deployments are: edit pi-1.yaml, pi-2.yaml, pi-3.yaml,
then `for y in *.yaml; do pi-bake build --from-yaml $y; done`.

### HAT catalog + config.txt overlays (FAT writes)
Currently we only write apkovl. To support HATs (PCIe BE200, Sense
HAT, displays, etc.) we need to write `/uboot/usercfg.txt` (the
Pi-canonical override file) or append to `/config.txt`. Each HAT
catalog entry knows its required dtoverlay= line(s).

### Raspbian backend
Documented v0.2 target — losetup-based, sudo prompted. Pi 4 / Pi 5
Raspbian deployments still go via rpi-imager pre-fill until this lands.

### Dynamic version discovery
Pull the live Alpine + Raspbian version indexes instead of the hard-
coded `versions` tuples. CLI flag `--refresh-versions` to update.

## v0.1 retrospective (what we learned during real deployment)

- udhcpc on Pi 5 + busybox 1.37 + Alpine 3.21 hangs. Static IP
  baked-in path now standard (commit 3a2efbd).
- Pi has no RTC. Without time, apk TLS verify rejects everything.
  HTTP-Date header hack + chrony in firstboot (commit 3a2efbd).
- WiFi firmware needs explicit linux-firmware-{brcm,intel}, not a
  meta `wifi-firmware` package (doesn't exist on Alpine).
- /etc/apk/repositories must be set to upstream main + community in
  the apkovl — local FAT cache only covers the bootstrap set.
- FAT partition mounts read-only by default. lbu commit handles the
  remount cycle; external apkovl pushes need explicit `mount -o
  remount,rw /media/mmcblk0` first.
- Backup the apkovl before overwriting:
  `cp .../td-pi5-1.apkovl.tar.gz{,.bak}` enables console rollback
  without re-flashing.

### DHCP client choice — RESOLVED (commit 936f233)
We picked dhcpcd over both busybox udhcpc and ISC dhclient. dhcpcd
ships pre-built in the stock Alpine RPi /apks cache (no bake-time
fetch needed), runs as a daemon watching all interfaces, sends DHCP
option 12 from /etc/hostname automatically, and doesn't share
udhcpc's Pi 5 + macb-driver hang. dhclient would have worked too
but isn't in the stock cache so it'd need a fetch dance.

### Backup convention — APKOVL FILENAME LESSON
The Alpine RPi bootloader globs `*.apkovl.tar.gz` on the FAT root.
A backup at `td-pi5-1.apkovl.tar.gz.bak` was still glob-matched
(or close enough to confuse the loader) and bricked boot until
the user renamed it to `.failed`. Going forward: NEVER leave a
file matching the apkovl pattern on the FAT root. Either keep
backups OUTSIDE the FAT or use a name that has no `.apkovl.tar.gz`
anywhere in it.
