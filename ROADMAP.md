# pi-bake roadmap

## ✅ Landed since v0.0.4

### YAML recipes are the primary interface (v0.0.5)

`pi-bake build --config <yaml>` reads a strict-validated YAML
recipe and bakes the image. `--to-yaml <path>` round-trips a
CLI invocation (or a `--config` load) into a normalized
annotated YAML file the operator can version-control. `--no-bake`
skips the actual bake — useful with `--to-yaml` to capture or
validate a recipe without producing an image.

Shape (full annotated reference: `pi-bake.example.yaml`):

```yaml
hostname: td-pi5-1
board: pi-5
os: alpine
os_version: edge                   # optional; defaults to latest known-good
ssh_pubkey: ~/.ssh/totaldns-adhoc.pub
network:
  mode: dhcp                        # or "static" with address+gateway
  send_hostname: true
wifi:                               # optional — omit for wired-only
  ssid: totaldns-lab
  psk: secret
packages:                           # extras appended to /etc/apk/world
  - avahi
  - linux-firmware-intel
output:
  path: ~/sdcards/td-pi5-1.img.gz
```

Strict load: unknown top-level keys + unknown sub-keys raise
with the operator-facing field name (so a typo like
`network: {addres: ...}` fails loudly with `unknown key
['addres']`, not a silent bake).

Tested recipes shipped under `examples/`:
- `pi-zero-2-w-wifi-station.yaml`
- `pi-5-wired-dhcp.yaml`
- `pi-5-be200-edge.yaml` (Alpine edge for iwlwifi)
- `pi-zero-w-armhf.yaml`

### Alpine `edge` OS version (v0.0.5)

`os_version: edge` (or `--version edge`) uses the latest stable
Alpine RPi tarball for the bootloader/FAT layout but points
`/etc/apk/repositories` at `edge`. Post-boot `apk upgrade`
rolls kernel + drivers + firmware forward to edge versions.

Motivation: stable 3.21's `linux-rpi-6.12.13` modloop strips the
entire `wireless/intel/` subtree (no `iwlwifi.ko`) despite
`bcm2711_defconfig` having `CONFIG_IWLWIFI=m`. Edge's
`linux-rpi-6.12.85` ships it, plus `linux-firmware-intel`
carries the BE200 firmware blobs (`iwlwifi-gl-c0-fm-c0-*` +
`iwlwifi-sc-a0-fm-c0-*`). See `§30 fw-be200` in totaldns'
FEATURES-TODO for the full decision tree.

Trade-off: edge is rolling. Reproducibility weaker. For an
appliance NEEDING an edge-only kernel feature, unavoidable;
pin the snapshot date at bake time when possible.


## v0.3 — top of list (next focused work)

### "pibakehub" — community recipe registry (operator idea 2026-05-25)

Like DockerHub but for Pi hardware. Plug a HAT or device into a
Pi, and:

```
pi-bake build --pibakehub waveshare-poe-m2-bekey-hat --board pi-5 \
              --hostname my-be200-host
```

would compose the right recipe automatically — pulling a curated
YAML from the registry that knows the HAT's required dtoverlay
entries, kernel modules, firmware packages, and any sysfs
quirks. Operator fills in just hostname/ssh-key/output.

**Why it matters:** the value of a HAT or USB radio is locked
behind the operator's tribal knowledge of which `dtoverlay=`
line + which kernel module + which firmware blob makes it work.
Encoding that as a community recipe makes hardware
plug-and-play.

**Implementation sketch:**

- Registry shape: a git repo (github.com/pi-bake/recipes) of
  `<vendor>/<product>.yaml` fragments + a top-level index.
  Fragment shape: any sub-tree of the full recipe schema (the
  `packages:` / config.txt overlay / kernel-module list pieces
  needed for that hardware), composable into a full recipe.
- `pi-bake build --pibakehub <slug>` fetches the fragment +
  merges into the operator's base recipe (or a default).
- Multiple `--pibakehub` flags compose multiple HATs.
- Local cache at `~/.cache/pi-bake/pibakehub/` so air-gapped
  bakes work once the recipe is pulled.
- Submission flow: operator who gets a HAT working contributes
  the recipe back via PR. Each merged recipe gets CI-tested by
  baking + booting in a Pi emulator (qemu-system-aarch64 for
  the boot smoke; physical-hardware verification is a separate
  community process).

**Why deferred to v0.3+:** depends on the bake-time apk-fetch
work below — recipes that pull firmware/kernel-module packages
need air-gap support to be useful. Also needs the
config.txt/HAT-overlay machinery (also v0.3, below).

### Bake-time apk-fetch (air-gap appliance support)

**Why it matters:** v0.0.5 honors `packages:` by appending them
to `/etc/apk/world`. On first boot, init's `apk add` installs
them — from the upstream Alpine repo, requiring the Pi to have
working network. For an **air-gapped appliance** ("the device
will never be on the internet"), the package + its recursive
deps must be in the bake-time `/media/mmcblk0/apks/` cache so
init's `apk add --no-network` path finds them locally.

**Plan:**

1. Auto-download `apk-tools-static` (small static binary) into
   `~/.cache/pi-bake/apk.static/` on first bake-with-packages.
   Works on any Linux host without requiring `apk-tools` system
   install. Reused on subsequent bakes.

2. `_extra_apks_fetch()` in `alpine.py`:
   - `apk.static --arch <target> --root <stage> fetch --recursive
     -X <repo-url> <pkg...>`
   - Writes `.apk` files into `<stage>/var/cache/apk/`
   - Move into FAT image's `/apks/<arch>/`

3. Regenerate (or augment) the local APKINDEX so init's
   `apk add` can resolve packages from the cache. Sign with a
   bake-time-generated key whose pubkey gets baked into
   `/etc/apk/keys/` in the apkovl.

4. Honor `os_version: edge` here too: fetch from edge repos
   when the recipe asks for edge, so the cache covers the
   edge kernel/firmware bundle.

**Why deferred to v0.3:** the YAML schema + CLI lands cleanly
without it. Bake-time fetch is a meaty implementation
(APKINDEX format + signing + cross-arch). Better to ship the
declarative interface now, then add the air-gap implementation
as a focused PR that doesn't reshape the YAML schema.

### Honor `runlevels:` + `fat_files:` from YAML

Schema docs already imply both; implementation deferred until
operators actually ask for them. Today operators get the same
effect via the post-boot pyinfra deploy chain (which is more
flexible). Skip until there's a real use case.

### Pre-baked SSH host keys (no host-key-change warnings)
Every reflash regenerates `/etc/ssh/ssh_host_*_key{,.pub}` so the
operator's `~/.ssh/known_hosts` flags the rebuilt Pi as "REMOTE HOST
IDENTIFICATION HAS CHANGED". Annoying; also breaks pyinfra runs
without `-o StrictHostKeyChecking=no`.

macmpi/alpine-linux-headless-bootstrap solves this by placing
`ssh_host_*_key{,.pub}` files alongside the apkovl on the FAT
partition; on first boot the apkovl restore copies them into
`/etc/ssh/` with the right perms (600/644). pi-bake should
either:

  1. Generate a per-hostname keypair at bake time and bake it
     into the apkovl (`/etc/ssh/ssh_host_ed25519_key{,.pub}`),
     OR
  2. Accept a `--ssh-host-key PATH` CLI flag pointing at an
     existing keypair (so reflashes can reuse the same identity).

Probably (2) — operator-controlled, easy to back up, no surprises.

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

### Interactive mode (UX layer over YAML)
Now that YAML recipes are the primary input, a `--interactive`
walk-through would be the operator-friendly third entry point
(alongside flags + `--config`). Wizard collects answers, writes
a YAML via the same `dump_recipe()`, optionally bakes:

```
pi-bake build --interactive
  ? Which board? (pi-5, pi-zero-2-w, …)        > pi-5
  ? Which OS? (alpine, raspbian, …)            > alpine
  ? OS version (latest | edge | 3.21.4)        > edge
  ? Hostname?                                   > td-pi5-1
  ? SSH pubkey path? (Tab to browse)           > ~/.ssh/totaldns-adhoc.pub
  ? WiFi? [y/N]                                 > N
  ? Static IP? [y/N]                            > y
    ? CIDR?                                     > 192.168.4.111/24
    ? Gateway?                                  > 192.168.4.1
  ? Extra packages? (comma-sep)                 > avahi, dbus, linux-firmware-intel
  ? HATs / expansion? (multi-select)
      [x] Intel BE200 M.2 WiFi 7 (PCIe HAT)
      [ ] PoE+ HAT (Pi 5)
      [ ] Sense HAT
      [ ] Adafruit 2.8" PiTFT 320x240
  ? Save recipe to YAML? [td-pi5-1.yaml]
  ? Bake now? [Y/n]
```

The HAT picker drives `config.txt` edits (dtoverlay=, dtparam=)
once HAT catalog work below lands.

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
