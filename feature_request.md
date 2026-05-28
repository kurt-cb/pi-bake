# pi-bake feature requests + design gaps

A tracking log of **bugs encountered + design gaps surfaced
during actual pi-bake use.** Not a feature backlog — committed
features live in [ROADMAP.md](ROADMAP.md). This file is the
candidate pool the roadmap pulls from when something here
becomes worth scheduling.

When an entry here ships: move the description to ROADMAP.md
(marked ✅) + delete here. When an entry is found to be a real
bug (vs. a design gap), capture the fix in a regression test +
delete the entry — the CHANGELOG + commit history is the record.

When adding: write enough context for a future session (or
human) to address the request without needing the downstream
project that surfaced it. Pi-bake stays downstream-agnostic;
totaldns-specific naming or workflows don't belong here.

---

## Open gaps

### `cache_packages:` — stage .apks for offline `apk add` later

**From:** pi-bake session — 2026-05-27.

**The shape:** today `packages:` does two things at once —
(1) fetch the apk + recursive deps into FAT `/apks/<arch>/` at
bake time + sign the index, (2) add the package name to
`/etc/apk/world` so init's `apk add --no-network` installs it
at first boot. Operators sometimes want only the first half:
"have this apk available locally so I can `apk add <pkg>`
later, offline, without committing to running it at boot."

```yaml
packages:                        # installed at init (current behavior)
  - avahi
  - dbus

cache_packages:                  # cached only; not installed
  - strace                       # debug tool, install when needed
  - tcpdump
  - py3-pydbus                   # optional runtime helper
```

**Why it matters:**
- **Air-gapped runtime install** — operator can SSH into the
  Pi later and `apk add tcpdump` without internet, because the
  .apk is already on FAT and the signing key is in
  `/etc/apk/keys/`. The post-boot `/etc/apk/repositories`
  already lists `/media/mmcblk0/apks` first (see
  [src/pi_bake/alpine.py:433](src/pi_bake/alpine.py#L433)),
  so no extra wiring needed.
- **Diagnostic kits** — bake the image with debug tools
  available but not running. Keeps the boot footprint small;
  the tools materialize only if the operator needs them.
- **Optional drivers / firmware** — ship a kernel module's apk
  in the cache without forcing it loaded at boot (e.g. ship
  `linux-firmware-intel` only on a fraction of devices).

**Implementation sketch:**
- Recipe schema gets a new top-level list
  `cache_packages: [str, ...]` (parallel to `packages:`).
- In `alpine.bake()`, build the apk-fetch input as
  `union(packages, cache_packages)` — one signed index, one
  dep-resolution pass, no duplicates in `/apks/<arch>/`.
- ONLY `packages:` entries go into `/etc/apk/world`. The
  cache-only set is in the index + on FAT but not in world,
  so init's `apk add --no-network $world` doesn't install
  them.
- Dedup rule: if a package appears in both lists, `packages:`
  wins (install). Recipe `__post_init__` should warn so the
  operator notices the redundancy.

**FAT-size caveat (must surface in docs):** every cached .apk
counts against FAT image size. A 200-pkg cache can balloon a
Pi image past 2 GB and overflow the default FAT partition.
README and pi-bake.example.yaml should call this out next to
the `cache_packages:` block. Pairs with
[[operator-selectable-fat-partition-size]] — the right way
to make room for a large cache is to enlarge FAT explicitly,
not to discover the overflow at bake time.

**Out of scope for v1 — explicit non-features:**
- **No "groups" / "all" sentinel.** Alpine has no dnf-style
  install groups. Closest analogs are meta-packages
  (`alpine-sdk`, `xfce4`, `gnome`) — operator lists the
  meta-pkg by name and apk-fetch's recursive-dep resolver
  pulls the closure. An `all` sentinel is a foot-gun for image
  size and not worth the surface area.
- **No "cache by repo" knob** (e.g. "everything in
  `community/`"). Same image-size foot-gun, deferred until a
  concrete use case shows up.

**Tests to add:**
- `cache_packages:` entries end up in `/apks/<arch>/` with the
  regenerated signed `APKINDEX.tar.gz`.
- `cache_packages:` entries do NOT appear in `/etc/apk/world`.
- `packages:` + `cache_packages:` deps resolve without
  duplicate .apks on FAT.
- A package listed in both yields the install path (and
  ideally a warning at recipe-load time).

### Operator-selectable FAT partition size

**From:** pi-bake session — 2026-05-27.

**The shape:** `fat_size_mb: 1024` in YAML (or
`--fat-size-mb 1024` on the CLI). Pi-bake resizes the FAT
partition at bake time so [[cache_packages]] and other
operator-staged FAT contents fit without overflowing.

**Why it matters:** today the FAT partition is whatever size
the upstream tarball/image happens to ship with. Once
operators start staging cached apks, custom config blobs,
A/B boot images (ROADMAP #17), or anything else under
ROADMAP #16, they hit an opaque "image won't bake / device
won't boot from a too-small FAT" wall. Making FAT size an
explicit recipe field surfaces the trade-off (boot speed +
flash time vs. on-device staging space) and makes "this
200-pkg cache_packages list doesn't fit" a recipe-load-time
error, not a bake-time crash.

**Implementation sketch:**
- Recipe field `fat_size_mb: int | None` (default None =
  "use the upstream default").
- Alpine baker: trivial — pi-bake already builds the FAT
  image with mtools; just size the image larger before
  populating.
- Raspbian / Debian / Fedora backends: harder — they're
  losetup + partition-resize on an existing partitioned
  image. Either `parted resizepart` + `resize2fs`-equivalent
  for FAT, or "round up to the next size class and emit a
  warning if operator asked for less than the upstream
  default."
- Recipe-load validation: if cache_packages estimated size
  > fat_size_mb - (boot artifacts size), refuse to bake with
  a clear error.

**Related themes from mynotes.txt** (each could become its own
entry once concretely surfaced in use):
- Package version pinning + frozen-set rebuilds + CVE/patch
  rollback paths.
- Operator-supplied files + custom sshd_config + per-image
  ssh keys + bundled python scripts.

### Overlay-based persistence layer (RO base + RW overlay)

**From:** pi-bake session — 2026-05-27.

**Status:** design exploration. May belong as a pi-bake
recipe option or as a separate project — TBD once the shape
clarifies. Captured here so it's not lost.

**The pushback that motivates it:** Alpine diskless mode's
"appliance feel" (immutable FAT + apkovl for config + no
post-flash `apk add`) trades user-friendliness for clean
factory-reset semantics. That trade is fine for a fixed-
function appliance but feels overcomplicated for the broader
pi-bake audience, who reasonably expect "I flashed this Pi,
now I can install stuff on it." The all-or-nothing choice
between "diskless appliance" and "fully writable sys mode"
is the friction; a middle ground would be valuable.

**The shape:** overlayfs union mount at boot.

- **Lower layer (RO):** the baked base image (squashfs or
  ext4-mounted-readonly). Pristine. Survives.
- **Upper layer (RW):** an overlay partition (or file-backed
  loop) that captures all post-flash changes. `apk add`,
  config edits, user data — all land here.
- **Result:** operator sees a normal RW root. Factory reset
  = wipe the upper layer. Best of both worlds.

**Why this is potentially OS-agnostic:** overlayfs is a
Linux kernel feature (3.18+), available on every modern
distro pi-bake might bake. The setup is the same shape
regardless of base distro:

1. Initramfs mounts the RO base.
2. Initramfs mounts the writable overlay partition.
3. Initramfs unions them via `mount -t overlay …`.
4. switch_root into the union.

Concretely: Ubuntu has Casper / overlayroot for this pattern.
Fedora has live-rootfs. Debian has live-boot. Each is
distro-specific but they all do the same kernel trick.
Pi-bake could either lean on the per-distro packages or
ship a small generic initramfs hook.

**Why this might be a separate project:** the bake-time work
(producing a flashable image) is what pi-bake does today.
The runtime overlay machinery (initramfs hook that unions
layers at boot) is upstream-of-pi-bake — it's runtime, not
build-time. Pi-bake could *enable* overlay mode in a recipe
(`persistence: overlay` opting into bundling the right
initramfs hook + partition layout), but the hook itself
could reasonably live in its own project, packaged as an
apk / deb / rpm that pi-bake just installs into the base
image.

**Open questions:**
- Does ROADMAP #19 (alpine ext4 sys-mode) already cover
  enough of the "I want a friendly RW Alpine" case that this
  feature is redundant? (Probably yes for first-time users;
  no for operators who want factory-reset semantics.)
- Per-OS hook proliferation vs. one generic initramfs hook —
  which is the right shape?
- Partition layout: separate overlay partition (clean,
  resizeable, but more bake-time complexity) vs.
  file-backed overlay on a FAT/ext4 partition (simpler,
  resize-headache).

### Direct-to-device flash + safety guards

**From:** pi-bake session — 2026-05-27. Surfaced during the
ext4 hardware-bring-up loop after spending an hour staring at a
2 GB image getting dd'd onto an SD via SSH at 500 KB/s. The
end result was the same as if pi-bake had been able to write
straight to a block device.

**The shape:** `output.path:` accepts a `/dev/...` block device,
not just a file path. pi-bake handles xz-decompression inline
and dd's directly to the device, replacing the operator
workflow:

```
xzcat out/image.img.xz | sudo dd of=/dev/mmcblk0 bs=4M ...
```

with:

```yaml
output:
  path: /dev/mmcblk0       # block device, not a file
  overwrite: confirm       # see safety guards below
```

**Why it matters:**
- One less manual step + xz-decompression-fopped-into-bash quirk
  to teach every operator.
- Pi-bake already knows the image bytes — passing through xz
  + ssh + dd was always lossy. Direct write removes 2 layers
  of stream wrangling.
- Lets pi-bake apply guards we can't apply from a shell pipe
  (see safety below).
- For ext4 mode, this would replace the `losetup + xz + dd`
  dance the operator has to do at the end with a single
  pi-bake invocation that ends with the SD card ready.

**Safety guards (the part that makes this not-terrifying):**

1. **Filesystem-detection refusal.** Before writing, pi-bake
   reads the first few sectors of the target. If it sees a
   recognizable filesystem (FAT, ext4, btrfs, NTFS, …) AND
   the recipe has `overwrite: forbid` (default), pi-bake
   refuses + tells operator what's there. `overwrite:
   confirm` re-prompts the operator interactively. `overwrite:
   yes` proceeds without prompt (CI / scripted bakes).
2. **Mount detection.** Refuse if the target (or any
   partition of it) is currently mounted anywhere. Catches
   the "I aimed at /dev/sda by accident and it's my root
   disk" foot-gun.
3. **Size sanity.** Refuse if the image is bigger than the
   target device. Refuse if the target is suspiciously
   large (>~512 GB suggests it's not an SD — likely a
   data drive).
4. **Removable-only by default.** Read /sys/block/<dev>/
   removable; default to refusing fixed disks. `--allow-fixed`
   opt-in for unusual setups.
5. **Confirmation prompt.** Interactive default: print target
   device info (size, model, current FS) and require operator
   to type the model string back before proceeding.

**Implementation sketch:**
- New `OutputSpec.overwrite: str = "forbid"` field
  (forbid / confirm / yes).
- New module `src/pi_bake/devflash.py` with the safety
  checks + the write loop (using `iflag=fullblock` semantics
  but in Python — read full blocks, write full blocks).
- bake.py detects when `output.path` is a block device
  (stat() + S_ISBLK) vs a file path and dispatches:
  file path → existing pipeline → produce .img.xz at path
  block device → existing pipeline → devflash to device

**Cross-refs:**
- ROADMAP #17 (A/B slot boot) — once direct-to-device exists,
  A/B OTA writes become natural (target the inactive slot's
  partition directly).
- [[usb-boot-prep]] — the operator workflow speedup pairs
  with USB-boot to fully sidestep SD-card slowness.

### `os: raspbian` + `os_mode: pxe` — known-good recovery PXE

**From:** pi-bake session — 2026-05-27, paired with the
Alpine PXE backend (ROADMAP #20) for the harder-to-net-boot
operators who want Pi Foundation's documented PXE path
instead.

**Why this is separate from the Alpine PXE variant:** Pi
Foundation publishes a documented, well-tested PXE/NFS-root
setup for Raspbian / Pi OS. Setup is older, more battle-tested,
and well-served by upstream tooling (`rpi-imager` has a
network-boot prep mode, `rpi-eeprom` is the Pi-side tool that
sets boot order). For a pi-bake operator who needs PXE
RECOVERY (not "this is my primary boot path"), Raspbian PXE is
the lower-risk move.

**The shape:** `os: raspbian` + `os_mode: pxe`. Output is a
TFTP root + an NFS-served rootfs:

```
out/<hostname>-tftp/                # TFTP root
├── bootcode.bin, start.elf, ...
├── kernel*.img, *.dtb, overlays/
├── cmdline.txt                     # root=/dev/nfs nfsroot=<ip>:/srv/<hostname>/rootfs
└── config.txt

out/<hostname>-nfs/                 # NFS-served rootfs
├── (full Raspbian Lite rootfs)
├── etc/fstab → mount root rw via NFS
├── home/pi/.ssh/authorized_keys    # baked from recipe
├── etc/hostname
└── etc/dhcpcd.conf                 # static IP if recipe specifies
```

**Why it matters:**
- **Recovery path** for a hosed CM4 — boot Raspbian over PXE,
  run `rpi-eeprom-config --apply` or arm-image-installer to
  fix the device, then re-image.
- **rpi-eeprom is Raspbian-only.** Alpine doesn't ship it.
  CM4 EEPROM updates require Raspbian.
- **Pi Foundation has battle-tested tooling.** Use what works.

**Implementation sketch:**
- Fetch the Raspbian Lite .img.xz (already in the catalog as
  `os: raspbian`).
- Mount it (losetup, as the existing raspbian.py does).
- Copy /boot/* contents to `out/<host>-tftp/`.
- Rsync rootfs to `out/<host>-nfs/`.
- Edit `out/<host>-tftp/cmdline.txt` to use
  `root=/dev/nfs nfsroot=<server>:/srv/<host>/rootfs ip=dhcp`.
- Bake recipe customization (hostname, ssh key,
  authorized_keys, dhcpcd static IP) into the NFS rootfs.
- README documents the NFS server side: `apt install nfs-kernel-server`,
  /etc/exports entry, `exportfs -ra`.

**Open questions:**
- Distinct output paths: `output.path` + new
  `output.nfs_path`? Or single base path + auto-derived
  subdirs?
- Whether to bundle a one-shot NFS server hint (likely not —
  NFS server setup varies enough by distro that the README
  is the right place).

**Cross-refs:**
- ROADMAP #20 (Alpine PXE) — sibling. Same recipe schema,
  different network protocol.
- The CM4 EEPROM flash workflow that motivated this entry
  is itself a downstream-of-pi-bake concern (`rpi-eeprom`
  runs on the device, not at bake time). pi-bake provides
  the boot environment; the operator scripts the EEPROM
  update inside it.

### USB-boot prep: BOOT_ORDER + bake docs

**From:** pi-bake session — 2026-05-28. Surfaced after the
hardware validation loop, watching dd-to-SD on the CM4 trickle
at 0.5 MB/s while the same SD card + same `.img.xz` flashed at
16.3 MB/s on a laptop USB reader. CM4's SD path is the
bottleneck (likely the IO board's SD signal integrity forcing
the controller down to a legacy mode); the SD card itself is
spec'd correctly (V20 = ≥20 MB/s sustained).

**The shape:** USB boot dodges the slow SD path entirely. Pi-bake's
output (an `.img.xz`) is already flashable to any block device, so
this isn't a new backend — it's a documentation + workflow story:

1. Operator wires `BOOT_ORDER` in the Pi's EEPROM to prefer USB
   (`0xf41` = SD → USB → SD → ... repeating; `0xf14` = USB-first).
2. Operator dd's the same baked image to a USB SSD/HDD instead
   of an SD card.
3. Pi boots from USB at ~35 MB/s read (USB 2.0 on CM4) — 70× faster
   than the slow SD path. Pi 4/5 with USB 3.0 → ~400 MB/s.

**Why this matters:** the iteration loop "bake → flash → boot →
debug" eats 5-10 minutes per cycle when SD takes ~2 minutes to
flash and the Pi boots slowly. USB-boot cuts the flash to ~10
seconds and the boot to faster (sequential reads matter for
modloop + apk install). For development this is a 5× workflow
speedup.

**What pi-bake should provide:**

- **README / docs:** a "use USB instead of SD" section with the
  one-time EEPROM-config step using `rpi-eeprom-config` (needs
  Raspbian — works with the raspbian backend, ROADMAP #9) and
  the dd command with `/dev/sdX` instead of `/dev/mmcblkN`.
- **Optional baked-in EEPROM update:** when `os: raspbian` is
  selected, pi-bake could bake a recipe field like
  `eeprom: { boot_order: usb-first }` that drops an
  `rpi-eeprom-config` snippet + first-boot service onto the
  image to apply at first boot. Useful for shipping
  ready-to-go USB-boot SD cards (operator flashes SD ONCE,
  Pi auto-updates EEPROM on first boot, subsequent boots are
  USB).
- **Catalog mention:** `oses.py` notes could call out which
  boards support USB boot natively (Pi 4 / Pi 5 / CM4 with
  recent EEPROM) and which don't (Pi Zero series).

**Cross-refs:**
- [[direct-to-device-flash]] — when `output.path: /dev/sdX`
  lands, flashing to USB becomes a single pi-bake invocation.
- ROADMAP #17 (A/B slot boot) — A/B writes pair naturally with
  USB storage (more space, faster writes for the inactive
  slot).
