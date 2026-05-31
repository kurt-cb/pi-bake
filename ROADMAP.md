# pi-bake roadmap

Every shipped + planned capability, numbered. Pick what's next
from the table; the body has the rationale per item. Release
versions get assigned at tag time, not here.

| #  | State | Item |
|---:|:-----:|:-----|
|  1 |  ✅   | [YAML recipes (`--config <yaml>` + `--to-yaml`)](#1-yaml-recipes) |
|  2 |  ❌   | [~~Alpine `edge` OS version~~ (dead-ended; see CLAUDE.md)](#2-alpine-edge-os-version-dead-ended) |
|  3 |  ✅   | [Pre-baked SSH host keys](#3-pre-baked-ssh-host-keys) |
|  4 |  ✅   | [Bake-time apk-fetch (offline first boot — init-time install)](#4-bake-time-apk-fetch-offline-first-boot) |
|  5 |  ✅   | [pibakehub v1 frozen design + 8-fragment pilot](#5-pibakehub-v1-frozen-design--pilot) |
|  6 |  ✅   | [HAT overlays + `/etc/modules` (FAT writes)](#6-hat-overlays--etcmodules-fat-writes) |
|  7 |  🔴   | [`--pibakehub` wired into `pi-bake build`](#7---pibakehub-wired-into-pi-bake-build) |
|  8 |  ✅   | [Init-time install of bake-staged extras (signed APKINDEX)](#8-init-time-install-of-bake-staged-extras-signed-apkindex) |
|  9 |  ✅   | [Raspbian backend (`.img.xz`, losetup)](#9-raspbian-backend) |
| 10 |  🟡   | [Fedora ARM backend (cloud-init NoCloud; Pi-bootloader-shim TBD)](#10-fedora-backend) |
| 11 |  ✅   | [Debian (community Pi images) backend](#11-debian-backend) |
| 12 |  ⬜   | [Honor `runlevels:` + `fat_files:` from YAML](#12-honor-runlevels--fat_files-from-yaml) |
| 13 |  ⬜   | [Interactive mode (`--interactive` wizard)](#13-interactive-mode) |
| 14 |  ⬜   | [Dynamic upstream version discovery](#14-dynamic-version-discovery) |
| 15 |  ⏸   | [Generalized recovery layer (waits for 2nd downstream asker)](#15-generalized-recovery-layer) |
| 16 |  ⬜   | [Operator-controlled FAT contents (extended backups, scratch)](#16-operator-controlled-fat-contents) |
| 17 |  ⬜   | [A/B rootfs with watchdog auto-revert](#17-ab-rootfs-with-watchdog-auto-revert) |
| 18 |  ⏸   | [Secure boot (verified boot chain — opt-in for stronger threat models)](#18-secure-boot) |
| 19 |  ✅   | [Alpine ext4 (sys-mode) backend (`os_mode: ext4`)](#19-alpine-ext4-sys-mode-backend) |
| 20 |  ✅   | [Alpine PXE backend (`os_mode: pxe`)](#20-alpine-pxe-backend) |
| 21 |  ✅   | [Deterministic SSH host keys from a seed (`ssh_host_key: usehost` / `seed:...`)](#21-deterministic-ssh-host-keys-from-a-seed-ssh_host_key-usehost--seed) |
| 22 |  ✅   | [`os_version:` selection across all backends (`stable` / `latest` / dated)](#22-os_version-selection-across-all-backends-stable--latest--dated) |
| 23 |  ✅   | [Raspbian `firstrun.sh` first-boot mechanism (replaces marker race)](#23-raspbian-firstrunsh-first-boot-mechanism-replaces-marker-race) |
| 24 |  ✅   | [Raspbian backend: per-codename baker classes](#24-raspbian-backend-per-codename-baker-classes) |
| 25 |  ⬜   | [EEPROM rescue SD image (`pi-bake rescue`)](#25-eeprom-rescue-sd-image-pi-bake-rescue) |
| 26 |  ⬜   | [Display info-screen (HDMI / SPI / I2C)](#26-display-info-screen-hdmi--spi--i2c) |
| 27 |  ✅   | [Raspbian PXE backend (`os_mode: pxe` for Raspbian)](#27-raspbian-pxe-backend-os_mode-pxe-for-raspbian) |

**State key:** ✅ shipped · 🟡 partial (code in, hardware verification or extra step pending) · 🚧 in flight · 🔴 blocked (on another item) · ⬜ not started · ⏸ deferred (won't pick up without more signal) · ❌ dead-ended (deliberately abandoned)

---

## 1. YAML recipes
**✅ shipped**

`pi-bake build --config <yaml>` reads a strict-validated YAML
recipe and bakes the image. `--to-yaml <path>` round-trips a CLI
invocation (or a `--config` load) into a normalized annotated
YAML file the operator can version-control. `--no-bake` skips
the actual bake — useful with `--to-yaml` to capture or validate
a recipe without producing an image.

Shape (full annotated reference: [`pi-bake.example.yaml`](pi-bake.example.yaml)):

```yaml
hostname: td-pi5-1
board: pi-5
os: alpine
os_version: 3.21.4                 # optional; defaults to latest known-good
ssh_pubkey: ~/.ssh/totaldns-adhoc.pub
network:
  mode: dhcp                       # or "static" with address+gateway
  send_hostname: true
wifi:                              # optional — omit for wired-only
  ssid: totaldns-lab
  psk: secret
packages:                          # extras (see #4 for offline-install path)
  - avahi
  - dbus
output:
  path: ~/sdcards/td-pi5-1.img.gz
```

Strict load: unknown top-level keys + unknown sub-keys raise
with the operator-facing field name. A typo like
`network: {addres: ...}` fails loudly with
`unknown key ['addres']`, not a silent bake.

Tested recipes shipped under [`examples/`](examples/):
- `pi-zero-2-w-wifi-station.yaml`
- `pi-5-wired-dhcp.yaml`
- `pi-zero-w-armhf.yaml`
- `pi-5-can-rs485.yaml`

---

## 2. Alpine `edge` OS version (dead-ended)
**❌ dead-ended 2026-05-26 — preserved on branch `kurt-cb/edge-mistake` (tags v0.2.1–v0.2.6)**

The v0.0.8 design wrote edge repos into `/etc/apk/repositories`
and assumed post-boot `apk upgrade` would roll the kernel
forward. It didn't — FAT and modloop are read-only at runtime,
the linux-rpi install hooks don't fire outside `setup-alpine`,
and the running Pi has no mkinitfs. v0.2.1–v0.2.6 tried to do
the upgrade at bake time via chroot + qemu-user-static +
binfmt_misc + manual modloop regeneration. That's ~370 lines of
fragile infrastructure that broke once per release.

Motivation was a single HAT (Intel BE200 iwlwifi missing from
stable 3.21's linux-rpi-6.12.13 modloop). Operator call: drop
BE200, evaluate AX210 (same iwlwifi driver family, present in
stable's modloop). One HAT does not justify a kernel-rebuild
pipeline inside the baker.

If a future hardware item needs an Alpine kernel newer than
stable ships, wait for the next Alpine point release. Don't
resurrect the chroot/qemu/modloop work. The dead-end branch
preserves the source for archaeology.

---

## 3. Pre-baked SSH host keys
**✅ shipped**

`ssh_host_key: <path>` (or `--ssh-host-key PATH` on the CLI)
bakes an operator-managed OpenSSH private key + matching
`<path>.pub` into `/etc/ssh/ssh_host_<type>_key{,.pub}` so the
Pi's SSH identity stays constant across rebuilds. No more
"REMOTE HOST IDENTIFICATION HAS CHANGED" warnings, no
`-o StrictHostKeyChecking=no` needed in pyinfra runs.

When unset, pi-bake auto-generates a fresh ed25519 host keypair
at bake time and embeds it — at least stable across reflashes of
the same `.img.gz` (instead of regenerated by sshd at first
boot). Key type is detected from the pubkey's first word
(ssh-ed25519 / ssh-rsa / ecdsa-sha2-*).

---

## 4. Bake-time apk-fetch (offline first boot — init-time install)
**✅ shipped** (originally landed v0.2 with a `local.d` post-sshd
installer; reworked by #8 to install at init time via a signed
APKINDEX, removing the late-boot script entirely)

Operator declares `packages:` in their recipe. Pi-bake fetches
the named packages + all recursive deps from upstream Alpine at
BAKE time using apk-tools-static (auto-downloaded into
`~/.cache/pi-bake/`). The `.apk` files land in the FAT image at
`/media/mmcblk0/apks/<arch>/` (flat, alongside the stock cache);
the APKINDEX gets regenerated and signed with a fresh per-bake
RSA key; the matching pubkey is baked into the apkovl's
`/etc/apk/keys/`. The packages also go into `/etc/apk/world` so
init's `apk add --no-network` installs everything in one
transaction at INIT TIME — same code path as the baseline. By
the time sshd starts, the Pi is fully provisioned.

Always-on whenever `packages:` is non-empty. The `apk_fetch:`
YAML field is a DEPRECATED no-op kept in the schema so old
recipes don't fail-load; the CLI `--apk-fetch` flag was removed.

Bake-host needs: `tar`, `cpio`, `openssl`, and network access
to dl-cdn.alpinelinux.org on the first run (apk-tools-static is
then cached for subsequent bakes).

---

## 5. pibakehub v1 frozen design + pilot
**✅ shipped (design + pilot artifacts) · implementation = #7**

DockerHub-style registry of Pi-HAT / USB-radio / M.2-card recipe
fragments. Operators compose multiple `--pibakehub
<vendor>/<slug>` flags into one bake and get a `.img.gz` that
boots and works with that specific hardware stack — no manual
`dtoverlay=` hunting in vendor wikis. Each fragment carries
scraped-from-manufacturer provenance, manufacturer doc links,
and operator-contributed `verified_on:` records (board × OS ×
OS-version × co-installed-components matrix).

Shipped artifacts:
- [`design/pibakehub-v1.md`](design/pibakehub-v1.md) — frozen v1
  design with stable §N.M.K.J numbering for forever-references.
- [`pibakehub-pilot/`](pibakehub-pilot/) — 7 scraped Waveshare
  HAT fragments.
- [`tools/pibakehub_compose.py`](tools/pibakehub_compose.py) —
  ~350-LOC composition prototype: loads fragments, validates §3
  schema, composes per §4, surfaces §6.2 untested warnings,
  supports §6.3 `--strict`.

The prototype is NOT wired into `pi-bake build` — that's #7.

---

## 6. HAT overlays + `/etc/modules` (FAT writes)
**✅ shipped**

Two new top-level Recipe fields:

```yaml
config_txt:                          # appended to /boot/usercfg.txt on FAT
  - dtparam=spi=on
  - dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25,spimaxfrequency=2000000
modules:                             # written to /etc/modules in apkovl
  - mcp251x
  - can_dev
```

Stock Alpine RPi `config.txt` already ends with `include
usercfg.txt`, so pi-bake creates that file fresh with the
operator's lines + a generated header comment. The shipped
`config.txt` is never touched.

`/etc/modules` lands in the apkovl, world-readable, in declared
order — important for cards with load-order dependencies (e.g.
spi-bcm2835 before mcp251x).

CLI: `--config-txt LINE` (repeatable) + `--module NAME`
(repeatable).

See `examples/pi-5-can-rs485.yaml` for a working HAT recipe
(Waveshare RS485 CAN HAT — SPI overlay + MCP2515 module + RS485
UART).

Unblocks #7. Lands as first-class Recipe fields so operators
with HATs can use them directly without waiting for the pibakehub
registry.

---

## 7. `--pibakehub` wired into `pi-bake build`
**🔴 blocked on #6**

Once `config_txt:` + `modules:` are honored (#6), wiring
`--pibakehub <vendor>/<slug>` into `pi-bake build` is mostly
plumbing:

- Adopt [`tools/pibakehub_compose.py`](tools/pibakehub_compose.py)
  as a real module under `src/pi_bake/`.
- Add `pibakehub:` top-level YAML field per
  [`design/pibakehub-v1.md`](design/pibakehub-v1.md) §7.3.
- Local cache at `~/.cache/pi-bake/pibakehub/` (git clone of the
  registry repo, §8 of the design).
- `pi-bake pibakehub list / info / update / diff` subcommands
  (§7.2 of the design).

Until then, the pilot script + fragment YAMLs document the
design but don't reach the SD card.

---

## 8. Init-time install of bake-staged extras (signed APKINDEX)
**✅ shipped — collapses the baseline-vs-extras split**

Originally a two-paths design (#4 used a `local.d` post-sshd
script with `--allow-untrusted`); the user pushed back that
there's no good reason to have two paths, and #8 collapses
them. After #8, operator-declared `packages:` get the same
init-time install path as the baseline. Result: one code path,
one install moment, no `/etc/local.d/install-extras.start`.

Bake-time steps when `packages:` is non-empty:

1. Fetch extras + all recursive deps from upstream Alpine
   into `/apks/<arch>/` (flat, alongside the stock cache —
   no `extras/` subdir).
2. Regenerate `APKINDEX.tar.gz` over the union of stock + new
   `.apk` files via `apk index --allow-untrusted` (the
   "--allow-untrusted" skips per-file SHA-1 signature verify,
   which modern apk-tools-static rejects; init's apk on the
   Pi uses the older bundled apk-tools that does trust the
   alpine-devel SHA-1 signatures, so .apks are properly
   verified at install time).
3. Sign the regenerated index with a fresh per-bake RSA-2048
   key via `openssl dgst -sha256 -sign`. Member name
   `.SIGN.RSA256.pi-bake-<8hex>.rsa.pub` (RSA-SHA256, not
   the legacy RSA-SHA1 — modern OpenSSL rejects SHA-1 RSA;
   apk-tools 2.6+ accepts SHA-256).
4. Bake the matching pubkey into the apkovl at
   `/etc/apk/keys/pi-bake-<8hex>.rsa.pub`. Init's `cp -a
   /etc/apk/keys $sysroot/etc/apk` (line ~936 of
   `boot/initramfs-rpi/init`) merges this with Alpine's
   official devel keys from initramfs.
5. Operator extras go into `/etc/apk/world` alongside the
   baseline. `/etc/local.d/install-extras.start` is no longer
   generated (deleted entirely from `_write_apkovl`).

End-to-end verified: bake with `packages:` populated produces
a ~211 MB image with:
- 163-package signed APKINDEX (100 stock + 63 extras incl.
  deps)
- `etc/apk/world` listing all 14 packages (baseline + extras)
- `etc/apk/keys/pi-bake-<hex>.rsa.pub` in the apkovl
- No `etc/local.d/install-extras.start`

See `design/#3_study.md` for the full architecture writeup
that informed this implementation.

**FAT size is NOT a constraint here.** pi-bake creates the FAT
from scratch via `mformat` and the operator controls its size
through `output.image_size_mb`. The "stock cache is small"
phrasing in earlier notes only described what Alpine *ships*
upstream (~100 packages); pi-bake can put as many .apks in
`/apks/<arch>/` as fits in the operator-chosen FAT. See also
#16 for operator-managed FAT contents beyond .apks.

---

## 9. Raspbian backend
**✅ shipped**

Fetches `https://downloads.raspberrypi.com/raspios_lite_arm64_latest`
(permanent redirect to the current dated build — likely trixie
as of 2026), xz -d's it into a tempdir, losetup -fP's the raw
.img, mounts both partitions, and edits:

- **boot (vfat):** `/ssh` marker, `/userconf.txt`
  (`pi:<sha-512-crypted-random-pass>`), `/wpa_supplicant.conf`
  when wifi is set, `/usercfg.txt` + `config.txt` include line
  when `config_txt:` is set.
- **rootfs (ext4):** `/etc/hostname`, `/etc/hosts`,
  `/home/pi/.ssh/authorized_keys` (chown 1000:1000),
  `/root/.ssh/authorized_keys`, pre-baked `/etc/ssh/ssh_host_*`
  when `ssh_host_key:` set, `/etc/dhcpcd.conf` static IP block,
  `/etc/modules`.

Then unmount + losetup -d + xz the modified .img → `.img.xz`.

The `pi` user's password is a sha-512 crypt of a random
throwaway secret — Bookworm+ requires SOME password set in
userconf.txt before sshd will allow logins, but pi-bake's
sshd config disables password auth so only the operator's
pubkey matters.

**Sudo required** for `losetup -fP` + `mount` + per-file writes
inside the mounted partitions. Pi-bake shells out to `sudo`;
operators typically run inside a privileged LXC container or
with sudoers entries allowing `losetup` + `mount` + `umount` +
`tee` + `chmod` + `chown` + `mkdir` + `sh -c "cat >>"` without
password. README documents the requirement.

---

## 10. Fedora backend
**🟡 partial — code shipped, Pi-bootloader-shim still operator-side**

Fetches Fedora Server Host-Generic aarch64 (e.g.
`Fedora-Server-Host-Generic-43-1.6.aarch64.raw.xz`), mounts the
EFI + rootfs partitions, and writes cloud-init NoCloud data
(`/user-data` + `/meta-data` on the EFI partition). The
user-data is YAML cloud-config setting hostname, the default
`fedora` user with sudo + operator SSH pubkey, optional wifi
via NetworkManager keyfile, optional static IP.

**Caveat:** Fedora's aarch64 image is a generic ARM build — no
Pi-specific bootloader. The image pi-bake produces is a valid
configured Fedora rootfs but doesn't directly boot on a Pi. The
operator must run `arm-image-installer --target=rpi4` (or rpi5)
on the output to inject Pi firmware before flashing. See
`fedora.py` module docstring for the workflow.

Pi-bootloader-shim is a future enhancement — would fit under
#16 (operator-controlled FAT contents: inject extra files into
the boot partition at bake time, in this case the Pi firmware
from `raspberrypi/firmware` GitHub).

Same sudo + LXC story as #9.

---

## 11. Debian backend
**✅ shipped**

Community Pi-on-Debian images from
[`raspi.debian.net`](https://raspi.debian.net/) — Pi-specific
firmware is bundled, so the produced image boots directly on
the Pi (unlike Fedora).

Default catalog version: `20231109` (the most recent build
covering Pi 1/2/3/4 on bookworm). raspi.debian.net's tested-
build cadence is slow; bump the catalog version when a newer
one lands. **No Pi 5 tested image yet** at raspi.debian.net
(2026-05) — `supports_boards` in the catalog reflects that.

Per-bake edits (same imgxz.py scaffolding as #9):

- **boot (vfat):** `/usercfg.txt` + `config.txt` include line
  when `config_txt:` is set.
- **rootfs (ext4):** `/etc/hostname`, `/etc/hosts`,
  `/root/.ssh/authorized_keys` (no `pi` user on Debian; root
  is the default account), `/etc/ssh/sshd_config.d/00-pi-bake.conf`
  (disables password auth + permits root pubkey login),
  pre-baked `/etc/ssh/ssh_host_*` when `ssh_host_key:` set,
  `/etc/wpa_supplicant/wpa_supplicant.conf` + interfaces.d
  drop-in when wifi is set, `/etc/network/interfaces.d/eth0`
  static block when `static_ipv4:` is set, `/etc/modules`.

Same sudo + LXC requirement as #9.

---

## 12. Honor `runlevels:` + `fat_files:` from YAML
**⬜ not started**

Schema docs already imply both; implementation deferred until
operators actually ask. Today operators get the same effect via
the post-boot pyinfra deploy chain (more flexible). Skip until
there's a real use case.

---

## 13. Interactive mode
**⬜ not started**

A `--interactive` walk-through as a third entry point alongside
flags + `--config`. Wizard collects answers, writes YAML via the
same `dump_recipe()`, optionally bakes:

```
pi-bake build --interactive
  ? Which board? (pi-5, pi-zero-2-w, …)        > pi-5
  ? Which OS? (alpine, raspbian, …)            > alpine
  ? OS version (latest | 3.21.4)               > latest
  ? Hostname?                                   > td-pi5-1
  ? SSH pubkey path? (Tab to browse)           > ~/.ssh/totaldns-adhoc.pub
  ? WiFi? [y/N]                                 > N
  ? Static IP? [y/N]                            > y
    ? CIDR?                                     > 192.168.4.111/24
    ? Gateway?                                  > 192.168.4.1
  ? Extra packages? (comma-sep)                 > avahi, dbus
  ? HATs / expansion? (multi-select)
      [x] Waveshare M.2 HAT+ (PCIe slot)
      [ ] PoE+ HAT (Pi 5)
      [ ] Sense HAT
      [ ] Adafruit 2.8" PiTFT 320x240
  ? Save recipe to YAML? [td-pi5-1.yaml]
  ? Bake now? [Y/n]
```

HAT picker drives `--pibakehub` selection (depends on #7).

---

## 14. Dynamic version discovery
**⬜ not started**

Pull the live Alpine + Raspbian + Fedora + Debian version
indexes instead of the hard-coded `versions` tuples in
[`src/pi_bake/oses.py`](src/pi_bake/oses.py). CLI flag
`--refresh-versions` to update on demand; bakes use whatever's
in the catalog by default.

---

## 15. Generalized recovery layer
**⏸ deferred — waits for 2nd downstream asker**

Originally requested as `pybake-pristine` + `fixit.sh` for the
totaldns hardware lab; rejected for v0.0.9 because the specific
shape was downstream-specific. The general design — opt-in
recovery apkovl + console script written into FAT — needs at
least one more downstream project asking for it before the
abstraction is informed by more than one workflow.

Totaldns workaround in the meantime: recovery layer lives in
their `safe_reboot.py` deploy role.

---

## 16. Operator-controlled FAT contents
**⬜ not started**

pi-bake creates the FAT image at whatever size the operator
specifies (`output.image_size_mb`), then fills it with the
Alpine tarball + apkovl + (when `apk_fetch: true`) the staged
.apks/extras/. Everything else on the FAT is operator-
inaccessible at bake time.

The FAT is `vfat`, world-readable on the Pi at
`/media/mmcblk0/`, world-writable with a `mount -o remount,rw`.
Operators with a 4 GB+ SD card have plenty of room for things
beyond the bake-time set:

- **Extended backups.** `lbu` rotates 3 apkovls today
  (`BACKUP_LIMIT=3` per #2's pre-existing config). With a
  larger FAT, the operator could bump that to 30, or keep
  hand-rolled snapshot tarballs alongside.
- **Recovery payloads.** Frozen "factory" apkovl + a console
  fixit script (see #15 for the generalized form). With a big
  FAT there's no question whether they fit.
- **Pre-staged operator data.** Config templates, firmware
  blobs not in upstream Alpine, on-device asset bundles.

Schema sketch:

```yaml
output:
  path: ~/sdcards/td-pi5-1.img.gz
  image_size_mb: 4096
  fat_files:                          # NEW
    - source: ~/operator/recovery/factory.apkovl.tar.gz.frozen
      dest: /recovery/factory.apkovl.tar.gz.frozen
    - source: ~/operator/templates/
      dest: /templates/               # whole tree
```

#12 already lists `fat_files:` as a schema-implied-but-not-
honored field; #16 is the explicit feature with a concrete use
case (operator-managed FAT contents at any size).

---

## 17. A/B rootfs with watchdog auto-revert
**⬜ not started**

The motivation isn't "two slots" for its own sake — it's the
**watchdog auto-revert** that an A/B layout enables. When a
deploy ships a broken image, today's pi-bake operator finds
out via a brick: SSH stops working, no way back without
physical SD access. With A/B + watchdog the deploy reboots
into the new slot, fails its sanity check, the watchdog
reverts to the previous slot, and the operator finds out via
"the new image rolled back" instead of "I need to drive to
the lab."

### Image shape

Per #16's flexible FAT sizing, the recipe opts into a
4-partition layout:

```yaml
os: alpine                            # or raspbian / debian
os_mode: ext4                         # A/B is sys-mode only —
                                      # diskless's modloop-on-FAT
                                      # doesn't admit slot rotation
partition:
  layout: ab                          # default `single` keeps
                                      # current behavior
  boot_size_mb: 256
  root_size_mb: 1700                  # PER slot
  data: fill                          # remaining space
ab:
  active_slot: a                      # which slot pi-bake
                                      # populates as active.
                                      # B gets the same content
                                      # at prep time (so the
                                      # first watchdog revert
                                      # always lands on a
                                      # known-good slot).
  watchdog:
    enabled: true
    sanity_check: /usr/local/bin/first-boot-ok
    revert_after_sec: 120
```

On-disk layout:
- p1: FAT `/boot` (256 MB) — Pi firmware + kernel + initramfs +
  DTBs + `cmdline.txt` + `ab-state.txt`
- p2: ext4 `root-A` (`root_size_mb`)
- p3: ext4 `root-B` (same size)
- p4: ext4 `data` (fill) — persists across slot swaps:
  ssh host keys (avoid the apkovl-style regeneration), operator
  data, var/log if you want it across the boundary

### Two pi-bake outputs

1. **Prep image** (full disk, default `pi-bake build` output) —
   what you flash once. Partitioned, both root slots populated
   identically, FAT seeded with first-boot ab-state.
2. **Update image** (partition-only, `output.mode: partition`)
   — what an OTA push or remote redeploy uses. Just the active
   slot's ext4 + a tiny manifest. Operator's upgrade tool dd's
   this to the inactive partition; pi-bake never touches the
   prep image's partition table or the OTHER slot.

This "pi-bake knows it's writing a partition image, not a disk
image" mode is the implementation knob that distinguishes the
two artifacts. Same recipe, same hostname/keys/packages — just
a different output shape.

### Runtime pieces (have to ship with the prep image)

A/B is not just "write to the other partition." For the
watchdog + revert to be self-contained:

1. **`/boot/ab-state.txt`** — readable by initramfs (busybox sh).
   Fields:
   - `active: a` (slot booting right now)
   - `pending: b` (slot waiting to be promoted)
   - `last-good: a` (revert target)
   - `failed-count: N` (watchdog tries before giving up)
2. **Initramfs hook** that reads `ab-state.txt` + swaps the
   `root=PARTUUID=...` on the kernel cmdline based on state.
   Pi-bake adds this to the apkovl/image at bake time.
3. **`pi-bake-ab-upgrade`** CLI tool on the Pi
   (`/usr/local/bin/pi-bake-ab-upgrade /path/to/update.img.xz`):
   verifies the image (sha256 + maybe operator signature),
   writes to the inactive partition, sets `ab-state` to
   `pending`, reboots. Idempotent + crash-safe (writes
   ab-state last).
4. **Watchdog service** — openrc sysinit-time service that
   runs the sanity check from the recipe + flips ab-state from
   `pending` to `active` on success. On timeout / failure:
   increments `failed-count`, reverts to `last-good` slot,
   reboots. After N failures the slot is "bad" and the system
   alerts (rsyslog → operator's collector).
5. **Persistent state via p4** — moving `/var/lib/<app>` and
   ssh host keys onto p4's data partition (symlinks from the
   sysroot) so an OTA doesn't lose them.

### Why this entry doesn't ship right now

Real engineering effort — ~1000 LOC across new backend module
(4-partition layout extension to `alpine_ext4.py`), apkovl
shipping the hook + tool + service, and integration tests that
fake a watchdog revert. Not blocked on anything; just a
self-contained chunk of work that hasn't been picked up.

When picked up: implement against `os_mode: ext4` first (the
sys-mode constraint above), then evaluate whether raspbian
backend should grow A/B too (#9 is losetup-based; would need
similar partition-table extension).

**Pairs with [[direct-to-device-flash]]** in feature_request.md
— once `output.path: /dev/sdX` lands, the OTA flow on the Pi
is `pi-bake-ab-upgrade` writing directly to `/dev/mmcblk0p3`
(or p2) with the same safety guards.

---

## 18. Secure boot (verified boot chain)
**⏸ deferred — opt-in for stronger threat models**

Extends #17's watchdog-revert story with cryptographic
verification of each boot stage. Builds the trust chain
operators with physical-tamper threat models actually need:

> "An attacker who briefly has access to the SD card can NOT
> swap the rootfs (or kernel, or initramfs) for one of their
> own. Even if the apkovl is signed, attacker can flash a
> different rootfs that ignores the apkovl entirely."

#17's apkovl-init A/B doesn't defend against that. The kernel
+ initramfs on FAT are unsigned; replacing them replaces the
entire trust chain.

### Three paths, only one worth taking

| Path | Slot-switch by | Crypto verifies | Trade-off |
|---|---|---|---|
| A: Pi firmware + apkovl-init (#17) | initramfs hook | EEPROM-level only (Pi 4+/CM4 SECURE_BOOT verifies boot.img) | Slot switching is rootfs-only; verified-boot ends at boot.img. Lowest complexity. |
| **B: Pi firmware + U-Boot + kernel** | **U-Boot bootenv** | **EEPROM verifies U-Boot; U-Boot verifies FIT-signed kernel/initramfs/dtb** | **+1 component, +1-2 sec boot. The verified-boot story that actually works on Pi. This entry's recommendation.** |
| C: Pi firmware + U-Boot + GRUB + kernel | GRUB | GRUB+shim signature chain | Only earns its keep if you share infra with an x86 fleet that uses GRUB. Skip on Pi-only labs. |

### Path B implementation sketch

- **Pi EEPROM secure boot:** operator fuses their RSA-4096
  signing key into the OTP (one-time). Pi firmware only loads
  a signed `boot.img` (kernel + initramfs + DTBs as a tarball).
  Pi-bake produces a signed `boot.img` at bake time using a
  `secure_boot.sign_with: ~/.keys/fleet-key.pem` recipe field.
- **U-Boot (the actual boot logic):** pi-bake includes a
  prebuilt U-Boot binary in the FAT (Pi firmware loads U-Boot
  as the "kernel" via `kernel=u-boot.bin` in config.txt). U-Boot
  reads `/uboot/boot.scr.uimg` (boot script — fit-verified by
  U-Boot's compiled-in DTB pubkey), drives A/B via U-Boot's
  `bootenv` + `bootcount` + `upgrade_available` (battle-tested
  in Toradex / Variscite industrial Pi deployments), then loads
  the FIT image for the chosen slot.
- **FIT-signed kernel/initramfs/dtb per slot:** each slot's
  rootfs ships with its own FIT image (`/boot/slot-A.itb`,
  `/boot/slot-B.itb`). pi-bake signs both at bake time using a
  fleet boot-signing key (same key, two FIT outputs).

### Pi-bake's existing trust model — two key tiers

The current per-bake APKINDEX key (per `design/security_notes.txt`'s
description of #8) is great for compartmentalized apk-install
trust but doesn't extend to boot-time naturally:

- Pi EEPROM secure boot needs a STABLE operator key (OTP-fused;
  can't rotate per-bake)
- U-Boot FIT verified boot uses a key compiled into U-Boot's
  DTB (same constraint — stable per fleet)

So secure-boot operators run TWO keys:

1. **Per-bake APKINDEX key** (existing #8) — short-lived,
   signs the apk repo at bake time, dies with the bake process.
2. **Long-lived fleet boot-signing key** (NEW for #18) —
   signs boot.img (Pi EEPROM path) or FIT image (U-Boot path).
   Stays in the operator's HSM / smart card / `~/.keys/`.
   One key per Pi fleet, not per Pi.

### Why deferred

- Real engineering: building U-Boot, FIT image tooling,
  bootenv management, signed-image-at-bake-time pipeline.
  Probably 1500+ LOC + a new dependency (`u-boot-tools` on the
  bake host).
- Today's downstream projects (totaldns) have not asked for
  this. The watchdog-revert motivation (#17) covers the
  dominant failure mode; secure boot covers the secondary
  "attacker with physical access" mode.
- Pi Foundation EEPROM secure boot is still maturing — the
  feature set in 2026 is more flexible than 2023's first
  cut. Picking up U-Boot integration now might miss
  future Pi-firmware-native features.

When picked up:
- Add `secure_boot:` recipe block (sign-with key path,
  enforcement level, optionally `bootloader: u-boot`).
- Build the U-Boot binary at bake time OR cache prebuilt
  binaries per board/version like apk-tools-static.
- Sign boot.img / FIT images via openssl, matching the
  existing apkfetch.py signing pattern.

---

## 19. Alpine ext4 (sys-mode) backend
**✅ shipped — `os_mode: ext4`**

Companion to the original Alpine diskless backend. Produces a
partitioned `.img.xz` (FAT `/boot` + ext4 `/`) instead of the
v0.0+ apkovl + modloop-on-FAT shape. Same recipe schema; new
`os_mode: ext4` field on the Recipe + bake.py dispatcher. Requires
SUDO (losetup + mkfs), joining the sudo'd backends (#9 / #10 / #11).

Cross-arch bootstrap via apk-tools-static with `--no-scripts` —
deferred post-install hooks fire on first boot via the
`/etc/init.d/pi-bake-firstboot-fix` one-shot openrc service
that runs `apk fix --no-network`. Busybox's `--install -s` hook
that creates `/sbin/init` (and ~300 other applet symlinks)
can't run cross-arch from the bake host, so pi-bake replicates
it at bake time by reading `/etc/busybox-paths.d/busybox` and
creating each symlink itself (would otherwise kernel-panic on
SD boot — caught + fixed during 2026-05-27 hardware bring-up).

The ONLY backend that supports `os_version: edge` — diskless
+ edge raises at recipe-load because Alpine ships no RPi edge
tarball and modloop-on-FAT makes post-boot kernel upgrade an
awkward ritual we won't paper over. ext4's `apk upgrade
linux-rpi` works normally.

Bake-host extras vs. diskless:
- losetup, mount, sfdisk, mkfs.vfat, mkfs.ext4 (joining the
  sudo'd backend set)
- otherwise reuses apkfetch.py + imgxz.py from existing
  backends

Container-friendly extensions to imgxz.py landed with this
backend so the LXC/Incus bake-host workflow works:
- `_sudo()` skips the `sudo` prefix when euid==0 (no sudo
  package needed in containers SSH'd into as root)
- per-partition offset-based loop attach (sidesteps incus
  cgroup-BPF blocking major 259 for partition device nodes
  while allowing major 7 for loop devices — every device
  pi-bake touches is now major 7)
- stderr surfaced in RuntimeError on _sudo failure (was
  swallowed in CalledProcessError → opaque "rc=1" errors)
- root-aware write_file / append_file (open directly when
  root; pipe through `sudo tee` only when not)

Validated end-to-end on a CM4 2026-05-27: SD boot reaches
login, networking comes up, sshd accepts connections,
hostname matches recipe.

Outputs `.img.xz` (not `.img.gz` like diskless mode).

See `src/pi_bake/alpine_ext4.py`, `examples/pi-5-alpine-ext4.yaml`.

---

## 20. Alpine PXE backend
**✅ shipped — `os_mode: pxe`**

The "lab recovery" backend. Output is a per-host directory
(NOT a flashable image) the operator drops into their lab's
TFTP root at `/var/lib/tftpboot/<cm4-mac>/`. The same tree gets
served over HTTP via the lab nginx (see `ngnix_setup.md`). Pi
PXE-boots the kernel + initramfs via TFTP, then the
initramfs's init script wget-fetches `apkovl=URL` +
`alpine_repo=URL` over HTTP — no SD card, no NFS, no NBD,
no rebuilt initramfs.

`linux-rpi`'s `CONFIG_BCMGENET=y` is built-in, so the stock
Alpine RPi initramfs already supports pure-network boot
without rebuild. No qemu-user-static; no module rebuilds; no
chroot.

Recipe gains a `pxe.server_url:` field — the lab HTTP base
URL that becomes the prefix for `apkovl=URL` +
`alpine_repo=URL` in cmdline.txt. Required when
`os_mode: pxe`.

Recipe-level gotchas baked into the backend (so future
operators don't relearn them):

1. `alpine_repo` URL must NOT end with the arch suffix — apk
   appends `/<arch>/APKINDEX.tar.gz` itself. The backend
   writes `<server>/apks` and lets apk do the rest.
2. apkovl gets an `auto eth0 inet dhcp` block in
   `/etc/network/interfaces` (via the
   `explicit_eth0_dhcp=True` kwarg the pxe backend passes to
   `alpine._write_apkovl`). Default diskless apkovl writes
   `lo` only and relies on dhcpcd-as-daemon; in pure-PXE
   timing dhcpcd alone doesn't bring up eth0 reliably.
3. After tarball extraction, pi-bake chmods the output tree
   to world-readable (`a+r` files, `a+rX` dirs). The Alpine
   RPi tarball ships some files at mode 0o600 (notably
   `boot/initramfs-rpi`) and Python's `data` extraction
   filter preserves that. dnsmasq-tftp + nginx both run as
   non-operator users on the lab host and can't read
   600-mode files → TFTP "failed sending" + HTTP 403 →
   kernel panic with no initramfs.

Companion lab-side doc: `ngnix_setup.md` covers the nginx +
firewalld + SELinux setup the operator needs on the lab host.

Validated end-to-end on a CM4 2026-05-27: PXE boot fetches
kernel + initramfs via TFTP, initramfs DHCPs + wgets apkovl,
apk install pulls 118 .apks via HTTP including signed
APKINDEX, switch_root + openrc starts networking + sshd +
dhcpcd + chronyd, system reachable as hostname `smoke-pxe`.

A "flush of closed file" bug in the apkfetch path
(Python 3.12 specific — manual `p.stdin.close()` before
`p.communicate()` raises on 3.12 but no-ops on 3.14) was
caught by the totaldns operator's first real-use bake +
fixed with a regression test that uses `ast.walk` to detect
the bug pattern.

See `src/pi_bake/alpine_pxe.py`, `examples/pi-cm4-alpine-pxe.yaml`,
`ngnix_setup.md`.

---

## 21. Deterministic SSH host keys from a seed (`ssh_host_key: usehost / seed:...`)
**✅ shipped**

The existing `ssh_host_key: <path>` (v0.2) requires the operator
to manage a per-host keypair file. For lab + CI bakes where
the goal is "stable known_hosts across reflashes" with no
ceremony, v0.4 adds two sentinel forms:

- `ssh_host_key: usehost` — derive ed25519 deterministically
  from the hostname (SHA-256 KDF).
- `ssh_host_key: seed:<string>` — derive ed25519 from a
  literal seed string. Use for HA pairs that share an identity.

Implementation: SHA-256(salt + seed_input) → 32-byte ed25519
seed → PKCS#8 PEM wrapper (sshd reads natively). ssh-keygen -y
derives the public key. No new Python deps (stdlib + the
system ssh-keygen that pi-bake already requires).

**Security caveat — labs only.** The derived key is predictable
from public info (hostname or a string committed to a recipe
in version control). pi-bake emits a runtime WARNING at bake
time whenever a sentinel form is used. For
production / WAN-exposed devices, use the file-path form with
a per-host keypair generated from `/dev/urandom`.

KDF salt is versioned (`pi-bake-host-key-v1`); a future change
to the derivation would bump it to v2 and rotate every
previously baked deterministic key.

See `src/pi_bake/host_keys.py`, `tests/test_host_keys.py`,
`pi-bake.example.yaml` (annotated reference).

---

## 22. `os_version:` selection across all backends (`stable` / `latest` / dated)
**✅ shipped**

v0.3 left `os_version:` as a thin pass-through that mostly
followed each backend's permanent-latest redirect. v0.4 adds
two sentinels and a per-OS dated catalog so operators can pin
to a known-good upstream build:

- `os_version: latest` — bleeding-edge upstream. For Raspbian
  this is Pi OS's permanent-redirect endpoint (whatever
  upstream cuts next); for Alpine / Debian / Fedora it
  resolves to the catalog's newest entry.
- `os_version: stable` — pi-bake's curated known-good pick.
  May lag `latest` deliberately to dodge upstream
  regressions. For Raspbian this is `2025-05-13` (last
  Bookworm — sidesteps Trixie's userconf-pi nologin default,
  see #23). For Alpine `3.21.4`, Debian `20231109`, Fedora
  `43-1.6`.
- `os_version: <date>` (Raspbian/Debian) or
  `os_version: <version>` (Alpine/Fedora) — explicit pin.

New CLI: `pi-bake list-os-versions [--os NAME]` prints every
selectable version per OS with codename (where applicable)
and what each sentinel resolves to.

Catalog covers the full upstream menus pi-bake's bake tooling
can actually reach: Raspbian 2023-12-06 → 2026-04-21 (11
builds, Bookworm + Trixie); Debian 20231109 + 20231111; Alpine
3.19–3.21 + edge; Fedora 42-1.1 + 43-1.6. Catalog is bumped
by hand on each pi-bake release until #14 (dynamic version
discovery) ships.

See `src/pi_bake/oses.py` (catalog + URL builders),
`tests/test_catalogs.py` (sentinel + dated-URL tests),
`pi-bake.example.yaml`.

---

## 23. Raspbian `firstrun.sh` first-boot mechanism (replaces marker race)
**✅ shipped**

The legacy `/boot/firmware/ssh` + `/boot/firmware/userconf.txt`
marker scheme had a regression on Pi OS Trixie: `userconf-pi`
creates the `pi` user with `/usr/sbin/nologin` shell. SSH
key auth succeeds, then login is immediately rejected
("This account is currently not available"). Bookworm worked;
Trixie didn't — and the operator-visible symptom is "boots,
gets to login prompt, no SSH, no password." Confirmed root
cause 2026-05-28 by inspecting a failed-bake SD card on the
lab CM4 PXE-Alpine.

The fix: write `/firstrun.sh` to FAT root and append
`systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot
systemd.unit=kernel-command-line.target` to `cmdline.txt`.
The script runs once before multi-user.target activates (so
userconf-pi doesn't race), creates the pi user with
`useradd -s /bin/bash`, force-sets the shell with
`usermod -s /bin/bash pi` (the load-bearing line), installs
authorized_keys, enables sshd, deletes the legacy markers
so userconf-pi doesn't fight us on the next boot, strips
its own systemd.run hooks from cmdline.txt, then exits 0 →
systemd reboots → normal Pi OS boot.

The `/ssh` + `/userconf.txt` markers are still written as a
fallback for the case firstrun.sh fails to run (cmdline.txt
corruption, etc.).

See `src/pi_bake/raspbian.py::_firstrun_sh` + `_patch_cmdline_txt`,
`tests/test_raspbian_firstrun.py`.

---

## 24. Raspbian backend: per-codename baker classes
**✅ shipped**

Pi OS evolves: Bullseye → Bookworm → Trixie → (whatever's next).
Each release tends to slip in one or two surprises that break a
headless bake — userconf-pi's shell default flipped from
`/bin/bash` to `/usr/sbin/nologin` in Trixie, etc. The pattern
that fits this trajectory is per-codename baker subclasses
sharing a common base:

```
_RaspbianBakerBase                  — version-agnostic logic
├── _RaspbianBookwormBaker          — codename = "bookworm"
└── _RaspbianTrixieBaker            — codename = "trixie"
```

Today both subclasses are empty — every v0.4 fix (firstrun.sh,
regenerate_ssh_host_keys mask, userconfig autologin removal)
works identically on Bookworm and Trixie because firstrun.sh
sidesteps Pi-OS-specific mechanisms entirely. When a future
fix applies to only one codename, it lives in that codename's
class: the override has one obvious home, no `if codename ==
"trixie"` branches scattered through the code.

The module-level `bake()` is a dispatcher: it parses the
codename from the upstream URL (Pi OS names files
`<date>-raspios-<codename>-<arch>-lite.img.xz`, stable since
Buster) and routes to the matching baker instance.

**Unknown-codename fallback strategy.** A Debian-codename
chronology (`jessie / stretch / buster / bullseye / bookworm /
trixie / forky / duke`) lets the dispatcher reason about
unknown codenames by chronological position:

- Newer-than-newest (e.g. a future Forky URL pi-bake hasn't
  shipped a baker for yet) → fall **forward** to the newest
  baker (Trixie today) + **WARNING**: "UNTESTED COMBINATION,
  expect first-boot quirks the firstrun.sh script may not
  handle."
- Older-than-oldest (e.g. a legacy Bullseye URL) → fall
  **backward** to the oldest baker (Bookworm today) + same
  loud warning.
- In between (pi-bake skipped a release) → walk backward to
  the nearest supported neighbor (conservative direction).
- Unknown name entirely → newest baker + warning.
- No codename in URL (e.g. `_latest` redirect) → newest baker
  silently (operator's recipe is asking for upstream-current,
  warning would be noise).

Per-codename example recipes that operators can copy-paste:

- `examples/pi-5-raspbian-bookworm.yaml` — `os_version: stable`
  pins to 2025-05-13 (last Bookworm). Recommended for lab
  baseline + production.
- `examples/pi-5-raspbian-trixie.yaml` — `os_version: latest`
  follows Pi OS's permanent redirect (Trixie today). pi-bake's
  firstrun.sh handles the userconf-pi nologin override
  automatically.

See `src/pi_bake/raspbian.py`,
`tests/test_raspbian_firstrun.py`,
`examples/pi-5-raspbian-{bookworm,trixie}.yaml`.

---

## 25. EEPROM rescue SD image (`pi-bake rescue`)
**📋 planned — design only**

Pi 4 / CM4 / Pi 5 boards boot from an EEPROM that occasionally
needs a clean reflash (after a bad update, a power cut during
firmware write, an accidental EEPROM corruption from a hack
attempt). Raspberry Pi publishes a rescue mechanism: boot the
Pi with a special FAT-formatted SD containing `recovery.bin` +
`pieeprom.bin` + `pieeprom.sig`, and the boot loader reflashes
itself from those files.

rpi-imager has this built-in under "Misc utility images →
Bootloader". pi-bake could ship the same as a subcommand:

```
pi-bake rescue --board pi-5 --out rescue.img.gz
pi-bake rescue --board pi-4 --out rescue.img.gz   # also CM4
```

Source files come from
[github.com/raspberrypi/rpi-eeprom](https://github.com/raspberrypi/rpi-eeprom)'s
release tarballs:
- `firmware-2711/` — Pi 4 + CM4
- `firmware-2712/` — Pi 5

Implementation sketch (~250 LOC + new module):

1. New `src/pi_bake/eeprom.py`:
   - download `rpi-eeprom-<release>.tar.gz` from GitHub releases
   - extract `firmware-27{11,12}/{recovery.bin,pieeprom.bin,pieeprom.sig}`
   - assemble a small FAT image (mtools, like Alpine baker)
   - copy the rescue files into FAT root
   - return `.img.gz`

2. New `oses.py` entry for the rpi-eeprom catalog —
   independent of Pi OS versions.

3. New CLI subcommand `rescue` with `--board`, `--out`,
   optional `--rpi-eeprom-version` (default: latest).

4. Tests: catalog lookup, URL build, FAT assembly with stub
   files.

Hardware validation required: actually flash + boot on a Pi
with an intentionally-bricked EEPROM to confirm the rescue
sequence triggers. That part can't be unit-tested.

Deferred from v0.6.0 because (a) the bake-time download from
GitHub releases is a new external dep that needs care, and
(b) hardware validation needs a specific test rig.

---

## 26. Display info-screen (HDMI / SPI / I2C)
**📋 planned — design only**

A baked-in helper that surfaces login / network info on an
attached display so the operator can see hostname + IP without
needing a console. Tier list by display type:

**Tier 1 (easy): HDMI.** Customize `/etc/issue` so the login
banner on tty1 shows hostname + eth0 IP + wlan0 IP + SSH
fingerprint. Bash escape sequences (`\4{eth0}`, `\n`) cover
most of it; pi-bake bakes a templated `/etc/issue`. ~30 LOC.
No service, no rendering, no driver questions. Works on any
Pi with HDMI attached.

**Tier 2 (medium): SPI TFT (ILI9341 / ST7789 / HX8357).**
fbtft kernel driver exposes the panel as /dev/fb1; a small
Python service renders text. Per-display dtoverlay needed
(`tft35a` for 3.5", `pitft28-resistive` for Adafruit 2.8",
etc.). ~150 LOC + systemd unit + dtoverlay registry. Schema:

```yaml
display:
  type: spi-fbtft
  dtoverlay: tft35a       # operator picks the right one
  rotate: 90              # optional
  fields: [hostname, eth_ip, wifi_ssid, wifi_ip, ssh_fp]
```

**Tier 3 (harder): I2C OLED (SSD1306 0.96").** Tiny char-only
display; needs the luma.oled python library + a per-update
script. ~100 LOC + systemd unit. Schema similar to tier 2:

```yaml
display:
  type: i2c-ssd1306
  i2c_address: 0x3c
  width: 128
  height: 32
  fields: [hostname, eth_ip]
```

Common across all tiers: a `pi-bake error-info <code>`
subcommand that prints recovery instructions (operator pulls
SD, boots a recovery image, etc.) when the live display shows
an error code. Out of scope for the display feature itself
— that's a separate CLI utility.

**Suggested order**: ship tier 1 (HDMI /etc/issue) as a
quick win, then SPI as a follow-up with one tested dtoverlay
(`tft35a` is the most common in lab use), then I2C if
demand surfaces.

Deferred from v0.6.0 because tier 2 + 3 each need hardware
test on a specific display panel, and we don't have one to
validate against.

---

## 27. Raspbian PXE backend (`os_mode: pxe` for Raspbian)
**✅ shipped (v0.6.5)**

Implemented at [`src/pi_bake/raspbian_pxe.py`](src/pi_bake/raspbian_pxe.py); the v0.7.0 NFS-root spec below is now the live behavior of `pi-bake build --config examples/pi-cm4-raspbian-pxe.yaml`. The 2026-05-30 CM4 hand-validation entry in [`tested_bakes.yaml`](tested_bakes.yaml) is what each module-level transform (service-mask list, config.txt strip, fstab PARTUUID strip, ssh.socket activation, getty@tty1 enable, default.target=multi-user, pre-baked SSH host keys, hostname baked direct) was derived from. Standing up the NFS server itself remains out of pi-bake's scope.

Alpine PXE shipped in v0.3.2 (ROADMAP #20) because the stock
Alpine RPi initramfs natively understands `apkovl=URL` +
`alpine_repo=URL` kernel cmdline params and wgets them at
boot. Pi OS has no equivalent — its initramfs expects to
mount root from a local-disk `PARTUUID=...`. The standard
fix for that is NFS-rootfs boot (Raspberry Pi Foundation
publishes the canonical tutorial).

### Pi-bake's contract

Pi-bake's job stops at the bake artifact + the cmdline.txt
the Pi needs. **Standing up an NFS server is the operator's
domain** (out of pi-bake's scope — every lab does it
differently: bare-metal, LXC container, NAS appliance,
existing infrastructure). Pi-bake just needs to know **where**
to push the rootfs and **what** to put in the Pi's
`nfsroot=` cmdline.

The bake produces:

1. **TFTP tree** at `output.path/` containing kernel +
   initramfs + DTBs + cmdline.txt (with `root=/dev/nfs
   nfsroot=<server>:<path>,vers=3,proto=tcp,...` filled in
   from the recipe).
2. **Rootfs tarball** at `output.path/<hostname>.rootfs.tar.gz`
   (preserves ownership in tar metadata; no fakeroot/sudo
   needed at bake time).
3. **Push helper** (stdout output) — pi-bake prints the exact
   `scp` + `ssh exec` commands the operator should run to
   ship the tarball to their NFS server and extract it.
   Operator runs them; pi-bake stays out of the SSH/firewall
   plumbing.

Recipe sketch:

```yaml
hostname: td-cm4-1
board: pi-5
os: raspbian
os_mode: pxe
pxe:
  # Where the Pi will mount its rootfs from (becomes nfsroot=
  # in cmdline.txt). Format: host[:port]:path
  nfs_server: 192.168.4.2:8801:/td-cm4-1
  # Where pi-bake should push the rootfs tarball (operator's
  # SSH endpoint with key auth; can be a localhost container,
  # a remote NAS, anything reachable). Optional — if omitted,
  # pi-bake just prints push instructions without acting.
  nfs_push: root@192.168.4.2:22
  # Extra mount options appended to nfsroot=
  nfs_mount_options: vers=3,proto=tcp,mountport=8803,nolock
output:
  path: out/td-cm4-1-pxe/   # directory output (TFTP tree + tarball)
```

### Operator's domain (not pi-bake)

How the operator stands up their NFS server is up to them.
Common patterns:

- LXC/incus container (one of the cleaner self-contained
  options — `unfs3` userspace NFS in an unprivileged Alpine
  container works fine, no kernel-module deps)
- bare-metal `nfs-kernel-server` on the lab host
- existing NAS appliance with NFS exports enabled
- Kubernetes NFS provisioner

Pi-bake doesn't care which. The contract is the SSH endpoint
in `pxe.nfs_push` + the mount endpoint in `pxe.nfs_server`.

### Hardware validation

Same lab setup as Alpine PXE (CM4 with EEPROM netboot
priority) plus an NFS server somewhere reachable. Add a row
to `tested_bakes.yaml` once a fresh bake boots end-to-end.

### A/B NFS export scheme (for redeploys)

End-to-end live testing 2026-05-30 surfaced a footgun: modifying
the NFS export tree while the Pi is actively using it causes
stale file handle errors that may panic the kernel. The
analogue of A/B partitioning on SD (ROADMAP #17) for NFS is:

```
/srv/nfs/pi-bake/<host>-a/   ← currently mounted (read-only-ish)
/srv/nfs/pi-bake/<host>-b/   ← prepare new bake here
/srv/nfs/pi-bake/<host>/     ← symlink, atomically swapped at deploy
```

On next reboot, the symlink-swap takes effect; the formerly-
active slot becomes inactive and safely modifiable for the
next bake. Pi-bake's deploy helper would do `incus exec
nfs-pi-bake -- swap-slots <host>` rather than direct rsync into
the active dir. v0.7.0 raspbian_pxe.py should implement this
from the start.

### Service-mask list (what Pi OS needs disabled for NFS-root)

Empirically verified on a CM4 hardware boot 2026-05-30. v0.7.0
raspbian_pxe.py must bake the following into the rootfs:

  - `regenerate_ssh_host_keys.service` — would wipe pre-baked
    keys on first boot
  - `init_resize2fs_once.service` — tries to resize a partition
    that doesn't exist
  - `NetworkManager.service` + `NetworkManager-wait-online.service`
    — would fight kernel's `ip=dhcp` over eth0 and break NFS
    mount
  - `dhcpcd.service` — same conflict reason
  - `dphys-swapfile.service` — tries to create swap file on
    slow NFS-mounted /
  - `rpi-eeprom-update.service` — does firmware-mailbox probes
    that fail in early boot
  - `userconfig.service` — interactive first-boot wizard;
    prompts for new username on tty1 and stalls boot
  - `userconf-pi.service` (if present) — silent variant of the
    same
  - `sshswitch.service` — Pi OS wrapper for ssh.service;
    redundant when ssh.service is enabled directly
  - `udisks2.service` — graphical-target leftover; not needed
    on Lite NFS-root

Also: ssh on Bookworm is **socket-activated** — must enable
`ssh.socket` (in `/etc/systemd/system/sockets.target.wants/`)
not just `ssh.service`. The service alone won't start if
nothing pokes the socket.

Also: delete `/etc/ssh/sshd_config.d/rename_user.conf` — it
references `/usr/share/userconf-pi/sshd_banner` which our
mask of userconf-pi may break. sshd refuses to start when
Banner points at missing file.

Also: persistent journal needs **BOTH**
`/var/log/journal/` directory present **AND**
`/etc/systemd/journald.conf.d/persistent.conf` with
`[Journal]\nStorage=persistent`. Directory alone isn't
enough on Bookworm. Required so the next boot's failures are
diagnosable from the NFS server side.

Also:

  - **`default.target` symlink override** to
    `/lib/systemd/system/multi-user.target` — Pi OS Lite
    Bookworm defaults to graphical.target which pulls in
    udisks2 + other unneeded services
  - **Pre-generate SSH host keys** via `ssh-keygen -A` (since
    regenerate service is masked)
  - **Enable `getty@tty1.service`** — Pi OS Lite has it
    disabled by default (the userconfig autologin was supposed
    to take its slot)
  - **Pre-create `/var/log/journal/`** — turn on persistent
    journal so future boot failures are diagnosable from the
    server side

### config.txt strip

Stock Pi OS config.txt enables GPU / camera / display
auto-detect which triggers Pi-firmware-mailbox property calls
that fail or hang on a netbooting CM4. v0.7.0 raspbian_pxe.py
should overwrite config.txt with bare minimum:

```
arm_64bit=1
auto_initramfs=0
enable_uart=1
```

### cmdline.txt template

Stripped of `init=`, `root=PARTUUID=`, `rootfstype=ext4`,
`quiet`. Has `ip=dhcp root=/dev/nfs nfsroot=<recipe-nfs_server>`
with mount options matching the NFS server (`vers=3,proto=tcp,
port=<nfs_port>,mountport=<mountd_port>,nolock` when going
through incus proxy on custom ports).

### Customization model (firstrun.sh vs direct)

The firstrun.sh approach (used for SD bakes since v0.4) is
**not viable for NFS-root** because:

- firstrun.sh's self-cleanup deletes `/boot/firmware/firstrun.sh`
  from the NFS-rootfs after first boot ✓
- but it CAN'T edit the **TFTP-served cmdline.txt** (a separate
  file on the lab host's TFTP root)
- result: every subsequent boot re-triggers
  `systemd.run=…firstrun.sh` which now doesn't exist → unit
  creation fails → `systemd.run_failure_action=poweroff` →
  CM4 powers off in a loop

For NFS-root, customization (hostname, user, SSH keys, etc.)
must be baked **directly into the rootfs at bake time** —
modify `/etc/passwd` / `/etc/shadow` / `/etc/hostname` /
`/home/<user>/.ssh/authorized_keys` in place. No first-boot
script needed.

See `src/pi_bake/alpine_pxe.py` for the parallel TFTP-tree
structure to mirror.

---

## Real-hardware lessons (informational)

Lessons learned while shipping v0.0.x → v0.2 on real Pi
hardware are captured in [`CLAUDE.md`](CLAUDE.md) under
"Critical lessons from real-hardware deployment." Anything that
cost real debug time has a regression test; the lessons doc
exists so we don't re-relearn them.
