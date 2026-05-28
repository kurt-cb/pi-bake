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

## Real-hardware lessons (informational)

Lessons learned while shipping v0.0.x → v0.2 on real Pi
hardware are captured in [`CLAUDE.md`](CLAUDE.md) under
"Critical lessons from real-hardware deployment." Anything that
cost real debug time has a regression test; the lessons doc
exists so we don't re-relearn them.
