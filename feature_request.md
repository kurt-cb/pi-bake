# pi-bake feature requests

Captured from downstream projects (totaldns + others) while
working in those projects' contexts. **Each entry is a candidate
for a focused pi-bake session** — don't bolt these on as
side-effects of unrelated work.

When adding: write enough context for a future session (or human)
to address the request without needing the downstream project.
When implementing: move the entry to the v0.X section of
[ROADMAP.md](ROADMAP.md) + delete here.

## Open requests

### "pibakehub" — community recipe registry

**From:** totaldns operator — 2026-05-25.

**The shape:** Like DockerHub but for Pi hardware. Plug a HAT
or device into a Pi, and:

```
pi-bake build --pibakehub waveshare-poe-m2-bekey-hat \
              --board pi-5 \
              --hostname my-be200-host
```

would compose the right recipe automatically — pulling a
curated YAML from the registry that knows the HAT's required
`dtoverlay=` entries, kernel modules, firmware packages, and
any sysfs quirks. Operator fills in just hostname / SSH key /
output path.

**Why it matters:** the value of a HAT or USB radio is locked
behind the operator's tribal knowledge of which `dtoverlay=`
line + which kernel module + which firmware blob makes it
work. Encoding that as a community recipe makes hardware
plug-and-play.

**Implementation sketch:**
- Registry shape: a git repo (`github.com/pi-bake/recipes`) of
  `<vendor>/<product>.yaml` fragments + a top-level index.
  Fragment shape: any sub-tree of the full recipe schema
  (`packages:` / config.txt overlay / kernel-module list)
  composable into a full recipe.
- `pi-bake build --pibakehub <slug>` fetches the fragment +
  merges into the operator's base recipe (or a default).
- Multiple `--pibakehub` flags compose multiple HATs.
- Local cache at `~/.cache/pi-bake/pibakehub/` so air-gapped
  bakes work once recipes are pulled.
- Submission flow: operator who gets a HAT working contributes
  the recipe back via PR. Each merged recipe gets CI-tested by
  baking + booting in a Pi emulator
  (qemu-system-aarch64 for the boot smoke; physical-hardware
  verification is a separate community process).

**Depends on:** bake-time apk-fetch (above) — recipes that
pull firmware/kernel-module packages need air-gap support to
be useful. Also depends on HAT-overlay machinery
(config.txt edits — also v0.3, see ROADMAP.md).

**Tracking:** in [ROADMAP.md](ROADMAP.md) under v0.3.

### Generalized recovery layer (factory-reset apkovl + console script)

**From:** totaldns hardware-lab — 2026-05-25.

**Status:** considered + rejected for v0.0.9 because the
totaldns operator's specific shape (`pybake-pristine` apkovl +
`fixit.sh` console script) is downstream-specific naming + a
specific recovery workflow. Adding it as-is would conflate
pi-bake with the totaldns lab's particular needs.

**For pi-bake to adopt this generally:**

1. Schema in YAML for opt-in: `recovery: { enabled: true,
   script: optional/custom.sh }`.
2. Always-correct default file names (`pi-bake-factory.apkovl.tar.gz`
   for the frozen baseline; `recover.sh` for the console
   script).
3. Documentation in `pi-bake.example.yaml` of when to enable
   (most appliances) vs leave off (test fixtures that
   intentionally want no recovery to validate failure paths).
4. Tests covering the recovery files land on FAT + the script
   does what it says (uses pi-bake-factory.apkovl.tar.gz +
   reboots).

**Downstream workaround in the meantime:** totaldns has the
recovery layer in its `safe_reboot.py` deploy role (which
writes the operator's specific `fixit.sh` to FAT + uses
operator's `pybake-pristine`). That's the right place for now —
it stays out of pi-bake.

**When to land:** when at least two downstream projects ask
for it (so the abstraction is informed by more than one
workflow).

### Alpine `edge` as a documented OS selection

**From:** pi-bake session — 2026-05-27. Captures lessons from
the v0.2.1–v0.2.6 dead-end (preserved on branch
`kurt-cb/edge-mistake`) so a future session doesn't relearn
them.

**Status:** dropped in v0.3.0 (commit `1f873ce`). Re-adding
the lightweight form (no chroot/qemu/modloop rebuild — that
path stays dead) is on the table, but the **better answer**
revealed by this session is [[alpine-ext4-sys-mode-backend]]:
edge selection only delivers the edge kernel cleanly in
sys-mode Alpine, because diskless's modloop-on-FAT is the
root cause of the post-boot ritual. See "Sequencing" below.

**Why operators ask for it:** newer kernel / drivers / firmware
than what stable Alpine's `linux-rpi` apk carries. Concrete
recent example: Intel BE200 (Wi-Fi 7) iwlwifi support landed in
the edge kernel before any stable point release shipped it.

**The fundamental problem:** Alpine's edge branch does NOT
publish an RPi release tarball (`alpine-rpi-*.tar.gz`). Stable
branches do. So any pi-bake "edge" support has to use a stable
tarball for the **boot layout on FAT** (kernel image, modloop
squashfs, firmware blobs, initramfs) and somehow get edge bits
on top. Two attempts, both flawed:

1. **Lightweight (v0.0.8 → v0.2.0):** stable tarball for FAT
   boot layout, `/etc/apk/repositories` pointed at edge.
   Failure modes:
   - Device boots the **stable** kernel — operator-visible
     mismatch with "I selected edge."
   - Getting the edge kernel onto the device requires
     post-boot: network reachable, `apk upgrade` succeeds,
     `lbu commit`, reboot. None of that is automatic, and
     air-gap is impossible.
   - Even after `apk upgrade`, modloop on FAT is the stable
     squashfs that init mounts pre-rootfs. Until modloop is
     regenerated for the new kernel version, modules from
     `/lib/modules/<edge-ver>/` don't load — the very drivers
     the operator wanted (e.g. BE200 iwlwifi) are unavailable
     until at least one reboot, *if* modloop regenerated
     correctly.

2. **Heavy (v0.2.1–v0.2.6, dead-end):** chroot + qemu-user-static
   + binfmt_misc at bake time, do the kernel upgrade + modloop
   regeneration on the bake host so the image boots edge
   directly. ~370 lines of fragile infrastructure (minirootfs
   download, chroot extract, qemu binary copy, bind mounts,
   `apk` in chroot, modloop squashfs regen). Broke once per
   iteration on undocumented Alpine release-tooling quirks.
   Operator call 2026-05-26: drop the entire pipeline; drop
   BE200; evaluate AX210 (same iwlwifi driver family, already
   in stable Alpine's modloop). See CLAUDE.md "Dead-end" section.

**Recommended default response when a hardware item needs a
newer-than-stable kernel:** wait for the next Alpine point
release. Don't rebuild the kernel inside a bake-host chroot.

**Decision (2026-05-27):** **diskless mode does NOT support
edge.** Alpine upstream chose not to ship an RPi release
tarball on edge; pi-bake reflects that decision rather than
papering over it with a documented post-boot ritual. Edge
selection works only in [[alpine-ext4-sys-mode-backend]]
mode, where Alpine's normal `apk upgrade` does the job
cleanly.

**Implementation when [[alpine-ext4-sys-mode-backend]] lands:**

1. `recipe.py` validation: if `os: alpine` + `os_version: edge`
   + (`os_mode: diskless` or unset), raise a clear ValueError
   at load time: *"Alpine edge is not supported in diskless
   mode (Alpine upstream ships no RPi edge tarball, and
   diskless's modloop-on-FAT makes post-boot kernel upgrade
   require a manual ritual). Use `os_mode: ext4` for edge
   kernel support."*
2. In ext4 mode, edge is just a repo URL — the bootstrap
   points `/etc/apk/repositories` at edge, the kernel
   installs to a real `/boot`, modules install to a real
   `/lib/modules/<ver>/`. No special-case code needed
   beyond the URL selection.
3. Tests:
   - `os: alpine` + `os_version: edge` + default mode →
     ValueError at recipe load.
   - `os: alpine` + `os_version: edge` + `os_mode: ext4` →
     bake produces an image whose `/etc/apk/repositories`
     points at edge.

**Cross-refs:**
- [[cache_packages]] is the right answer when stable's
  kernel already has the right module but a firmware blob
  or userland apk isn't in the stock cache — no kernel
  upgrade needed.
- For hardware that genuinely needs an Alpine kernel newer
  than stable ships, use `os_mode: ext4` + `os_version: edge`.
  Or use the raspbian backend (Pi Foundation's kernel +
  matching userland, no Alpine version-skew problem).

### Alpine ext4 (sys-mode) backend

**From:** pi-bake session — 2026-05-27. Surfaced when working
through why [[alpine-edge-as-a-documented-os-selection]] is
ergonomically miserable: the diskless constraint (apkovl +
modloop + FAT-everything) is what makes kernel upgrade hard,
not anything fundamental about Alpine. Sys-mode Alpine on
ext4 has none of those problems.

**The shape:** a new bake backend (`bake_backend: "alpine_ext4"`
on a new OSImage entry, or a flag on the existing ALPINE
entry — TBD) that produces a partitioned image with FAT
`/boot` + ext4 `/`, same shape as the Raspbian/Debian/Fedora
backends.

**Why it matters:**
- **Kernel upgrade works normally.** `apk upgrade linux-rpi`
  writes to a real `/boot` and a real `/lib/modules/<ver>/`;
  next boot picks them up. No modloop dance, no `lbu commit`,
  no FAT manipulation ritual.
- **Edge selection becomes honest.** Operator picks edge,
  reboots, `apk upgrade` post-boot actually delivers the edge
  kernel — the BE200-class problem goes from "documented
  caveat operator must work around" to "works as expected."
- **[[cache_packages]] becomes a complete air-gap upgrade
  story** — including kernel — because there's no
  FAT-modloop layer to regenerate.
- **A/B watchdog boot** (see mynotes.txt) is feasible —
  swap between two ext4 root partitions via `cmdline.txt`,
  bootloader-style. Diskless mode makes this awkward; sys
  mode makes it natural.
- **Removes the user-friction concerns** that motivated
  [[overlay-based-persistence-layer]] — operators get a
  normal RW root they understand, not an "appliance mode"
  with surprising semantics.

**Implementation sketch:**

Same shape as [src/pi_bake/raspbian.py](src/pi_bake/raspbian.py):

1. losetup-based — REQUIRES SUDO (or privileged LXC).
2. Create empty .img file, partition (FAT32 boot + ext4 root),
   `mkfs.vfat` + `mkfs.ext4`.
3. Bootstrap Alpine into the ext4 root: `apk.static
   --root <mnt> --initdb --arch <arch> --repository
   http://dl-cdn.alpinelinux.org/alpine/v<ver>/main add
   alpine-base linux-rpi linux-firmware-brcm openrc …`.
   apk-tools-static is already used by [[cache_packages]]
   machinery so this isn't new infrastructure.
4. Drop Pi firmware blobs (start*.elf, bootcode.bin, fixup*.dat,
   bcm*.dtb, overlays/) into `/boot`. These come from the
   `raspberrypi-bootloader` apk or by copying from the
   diskless tarball.
5. Write `/boot/cmdline.txt` pointing root at the ext4
   partition by PARTUUID.
6. Write `/boot/config.txt` (Pi standard, optionally with
   recipe's `config_txt:` additions).
7. Write `/etc/fstab`, `/etc/hostname`, `/etc/network/...`,
   `/etc/ssh/...`, etc. — same recipe fields as today's
   Alpine backend, just landing in real config files in the
   ext4 root rather than the apkovl tarball.
8. Set root password / SSH keys / wifi / static IP — same
   recipe schema.
9. Unmount, detach loop, xz-compress to `.img.gz` (or
   `.img.xz` to match other backends — TBD).

Expected size: ~300–400 LOC, in
`src/pi_bake/alpine_ext4.py`, with substantial overlap to
both `alpine.py` (the recipe → config-file mapping) and
`raspbian.py` (the losetup + partition + chroot dance).

**Trade-offs vs. existing Alpine diskless backend:**
- ❌ Requires sudo (Alpine diskless's no-root bake is the
  outlier in pi-bake — the new backend joins
  raspbian/debian/fedora in the "needs root or LXC" camp).
- ❌ Larger flashed image (real fs vs. tmpfs+apkovl).
  Probably +200–400 MB depending on packages.
- ❌ Slower cold boot vs. tmpfs warm-start (real fs init).
- ❌ lbu / apkovl are irrelevant — different operator mental
  model for "where does my config live."
- ❌ Another backend to test + keep current with Alpine
  upstream changes.
- ✅ Kernel upgrade ergonomics dramatically better.
- ✅ Edge selection actually delivers the edge kernel.
- ✅ Cleaner story for operators who push back on appliance
  semantics ("I want to be able to apt-style install stuff
  later" — see [[overlay-based-persistence-layer]] for the
  even-friendlier middle ground).

**Recipe-level UX:**

```yaml
os: alpine
os_mode: ext4        # new — defaults to "diskless" for back-compat
os_version: edge     # works cleanly with ext4; warns/recommends ext4
                     # when paired with the default diskless mode
```

Diskless stays the default for now (back-compat + no-sudo
bake remains attractive for the "just want a fast appliance"
case). When operator selects `os_version: edge` against
diskless, pi-bake emits a clear warning recommending ext4 —
see [[alpine-edge-as-a-documented-os-selection]] for the
warning text.

**Tests to add:**
- Bake produces a partitioned image with FAT boot + ext4
  root, both populated with the expected files.
- Recipe fields (hostname, ssh_key, wifi, static_ip,
  packages, cache_packages) end up in the right real
  config files in the ext4 root.
- `os_version: edge` + `os_mode: ext4` produces an image
  whose `/etc/apk/repositories` points at edge — no
  warning emitted.
- `os_version: edge` + `os_mode: diskless` (or default)
  emits the "consider ext4" warning at recipe-load time.

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

**Open questions to resolve before this becomes a v0.X
roadmap item:**
- Does [[alpine-ext4-sys-mode-backend]] already cover enough
  of the "I want a friendly RW Alpine" case that this
  feature is redundant? (Probably yes for first-time users;
  no for operators who want factory-reset semantics.)
- Per-OS hook proliferation vs. one generic initramfs hook —
  which is the right shape?
- Partition layout: separate overlay partition (clean,
  resizeable, but more bake-time complexity) vs.
  file-backed overlay on a FAT/ext4 partition (simpler,
  resize-headache).

**Cross-refs:**
- [[alpine-ext4-sys-mode-backend]] — the simpler answer for
  most user-friendliness pushback.
- [[operator-selectable-fat-partition-size]] — if overlay
  upper layer lives on FAT, this matters more.
- mynotes.txt "A/B boot with watchdog" — overlay is a
  different shape of layered boot; could compose with A/B
  or replace it depending on use case.

### Operator-selectable FAT partition size

**From:** pi-bake session — 2026-05-27.

**The shape:** `fat_size_mb: 1024` in YAML (or
`--fat-size-mb 1024` on the CLI). Pi-bake resizes the FAT
partition at bake time so [[cache_packages]] and other
operator-staged FAT contents fit without overflowing.

**Why it matters:** today the FAT partition is whatever size
the upstream tarball/image happens to ship with. Once
operators start staging cached apks, custom config blobs,
A/B boot images (see mynotes.txt), or anything else under
[[operator-controlled-fat-contents]], they hit an opaque
"image won't bake / device won't boot from a too-small FAT"
wall. Making FAT size an explicit recipe field surfaces the
trade-off (boot speed + flash time vs. on-device staging
space) and makes "this 200-pkg cache_packages list doesn't
fit" a recipe-load-time error, not a bake-time crash.

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

**Related themes from mynotes.txt** (these belong in their
own feature_request entries, not bolted into this one):
- Package version pinning + frozen-set rebuilds + CVE/patch
  rollback paths.
- Operator-supplied files + custom sshd_config + per-image
  ssh keys + bundled python scripts.
- A/B boot with watchdog rollback (a major feature on its
  own — sub-images, last-known-good selection, sanity-driven
  fallback).
- Bootloader / secure boot / smart-card code signing.

These all want operator-controlled FAT contents; FAT-size
selection is the foundation.

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

### `os_mode: pxe` — bake a PXE-ready tree instead of an SD image

**From:** pi-bake session — 2026-05-27, surfaced during the
[[alpine-ext4-sys-mode-backend]] hardware-bring-up loop when
SD boot failed and the PXE recovery path was found to be
fragile (Alpine's stock `initramfs-rpi` has no network driver,
so pure-PXE-without-an-SD requires rebuilding initramfs + a
side HTTP server + four cmdline parameters).

**The shape:** new mode `os_mode: pxe`. Output isn't an
`.img.xz`; it's a TFTP-ready directory tree:

```
out/<hostname>-tftp/
├── bootcode.bin, fixup4.dat, start4.elf, ...   # Pi firmware
├── bcm*.dtb, overlays/                          # DTBs
├── config.txt                                   # kernel + initramfs paths
├── cmdline.txt                                  # ip=dhcp + apkovl=URL + alpine_repo=URL
├── boot/
│   ├── vmlinuz-rpi
│   ├── initramfs-rpi                            # rebuilt with `network` feature
│   └── modloop-rpi
├── apks/<arch>/                                 # apk cache (signed APKINDEX)
└── <hostname>.apkovl.tar.gz                     # apkovl (hostname/ssh/network/etc.)
```

Operator drops this into their TFTP root keyed by Pi serial or
MAC. Pi PXE-boots, initramfs brings up network, fetches
apkovl + modloop over HTTP, system comes up. No SD card needed.

**Why it matters:** the lab/recovery story for any Pi-bake
deployment. When an SD goes bad, when an EEPROM update bricks
boot, when you want to bring a CM4 up without touching the
hardware — PXE is the answer, and it should be a first-class
output of `pi-bake build`, not an ad-hoc lab fix.

**Confirmed working shape (from 2026-05-27 hands-on PXE bring-up):**

The end-to-end PXE boot WORKS without rebuilding initramfs — Alpine
v3.21+ already ships a `linux-rpi` kernel with `CONFIG_BCMGENET=y`
built in, so the Pi NIC comes up without needing any kernel module
loaded from initramfs. The stock `initramfs-rpi` already has all
the network-fetch logic in its init script (`apkovl=URL`,
`alpine_repo=URL`, `is_url()` accepts http/https/ftp, busybox `wget`
applet available). So the actual work pi-bake has to do is:

1. **Lay out files on disk** in the per-Pi TFTP subtree structure
   the Pi firmware expects (MAC-keyed dir or serial-keyed symlink).
2. **Write the right cmdline.txt** with `ip=dhcp` + `apkovl=URL`
   + `alpine_repo=URL` + `modules=loop,squashfs,sd-mod,usb-storage`.
3. **Bake the apkovl** with the operator's recipe (hostname, ssh
   keys, network, packages) — same logic as the existing diskless
   backend's `_write_apkovl`.
4. **Bundle the apks cache + signed APKINDEX** with the operator's
   `packages:` + recursive deps — same as `cache_packages` work.

No initramfs rebuild needed. No qemu-user-static. Concrete proof:
[/var/lib/tftpboot/88-a2-9e-44-31-f3](/var/lib/tftpboot/88-a2-9e-44-31-f3)
on the lab host plus the cmdline.txt at that path are what got the
CM4 to PXE-boot Alpine without an SD card.

**Implementation sketch (Alpine variant):**

1. New `os_mode: pxe` value. Validates: `os: alpine` only for v0.4;
   `os: raspbian` is the sibling [[raspbian-pxe-recovery]] entry.
2. New backend module `src/pi_bake/alpine_pxe.py` reuses the
   diskless backend's apkovl-generation logic but writes
   straight to an output directory instead of a FAT image:
   - Fetch the Alpine RPi tarball (same URL as diskless).
   - Extract Pi firmware blobs + DTBs + overlays + boot/* (kernel,
     initramfs, modloop, System.map, config) to the output dir.
   - Generate apkovl using `alpine._write_apkovl`.
   - Run bake-time apk-fetch (apkfetch.py) for any `packages:` +
     dep closure, drop into `apks/<arch>/`, regenerate + sign the
     APKINDEX (same as today's diskless backend's #3 init-time
     install path).
   - Write `config.txt` (`kernel=boot/vmlinuz-rpi` +
     `initramfs boot/initramfs-rpi` + `arm_64bit=1` + operator
     overlays).
   - Write `cmdline.txt` (`ip=dhcp` +
     `apkovl=http://{server}/{path}/<host>.apkovl.tar.gz` +
     `alpine_repo=http://{server}/{path}/apks/<arch>` +
     `modules=loop,squashfs,sd-mod,usb-storage` +
     `console=tty1 console=serial0,115200`).
   - Output layout (matches the working hand-rolled lab tree):
     ```
     out/<hostname>-tftp/
     ├── bootcode.bin, start4.elf, fixup4.dat, ...
     ├── bcm*.dtb, overlays/
     ├── boot/
     │   ├── vmlinuz-rpi
     │   ├── initramfs-rpi
     │   └── modloop-rpi
     ├── apks/<arch>/
     │   ├── APKINDEX.tar.gz  (signed)
     │   └── *.apk (baseline + operator extras + deps)
     ├── <hostname>.apkovl.tar.gz
     ├── config.txt
     └── cmdline.txt
     ```
3. New recipe fields:
   - `pxe.server_url`: base URL for HTTP fetches in cmdline.txt
     (e.g. `http://192.168.4.2/<host>`). pi-bake substitutes
     this into the cmdline templates.
   - `output.path` is a DIRECTORY for pxe mode, not a file.
4. Operator follows [ngnix_setup.md](ngnix_setup.md) on the lab
   host. They copy the baked tree into `/var/lib/tftpboot/<mac>/`
   (or `/var/lib/tftpboot/<serial>/` with the symlink-to-mac
   pattern Pi firmware uses on CM4). nginx serves the same tree
   for the HTTP fetches in cmdline.txt.

**No longer open questions (resolved 2026-05-27):**
- ~~Cross-arch initramfs rebuild~~ — not needed; stock initramfs
  works because `linux-rpi` has `BCMGENET=y` (kernel-builtin NIC).
- ~~mkinitfs feature flag confusion~~ — not needed.

**Still-open questions:**
- TFTP tree layout: pi-bake outputs a single per-host tree;
  operator decides whether to deploy it under MAC-keyed or
  serial-keyed path. Document the Pi firmware lookup order in
  README. Maybe `output.tftp_subdir:` field lets operator pick.
- modloop signing: today the apkovl's apk add at first boot
  installs modloop-rpi from the apks repo, so modloop is verified
  via the pre-baked pubkey set. Confirm this still works in PXE
  mode (no SD-resident modloop signature pubkey needed).

**Gotchas discovered during the 2026-05-27 hands-on PXE boot
(must be baked into the pxe backend so they're not learned again):**

1. **`alpine_repo` URL must NOT include the arch suffix.** apk
   appends `/<arch>/APKINDEX.tar.gz` automatically. So:
   - WRONG: `alpine_repo=http://host/path/apks/aarch64`
     → fetches `apks/aarch64/aarch64/APKINDEX.tar.gz` (404)
   - RIGHT: `alpine_repo=http://host/path/apks`
     → fetches `apks/aarch64/APKINDEX.tar.gz` (200)
2. **apkovl's `/etc/network/interfaces` MUST include an `auto
   eth0` block.** The default diskless apkovl
   ([alpine.py:_write_apkovl](src/pi_bake/alpine.py)) writes
   only `lo` by default; PXE mode needs the eth0 stanza explicit
   so the `networking` service brings up the NIC. dhcpcd
   auto-detects but only fires reliably when eth0 is already up.
3. **modloop will fail at first boot in pure PXE mode** because
   there's no SD-resident `boot/modloop-rpi`. This is non-fatal
   IF the apkovl includes `linux-rpi` in its world (apk add at
   init time then puts modloop-rpi in the sysroot for next time —
   though in pure PXE "next time" still has the same problem
   because sysroot is tmpfs). For first-boot, kernel-builtin
   drivers (`BCMGENET=y`, `MMC_BLOCK=y`, `EXT4_FS=y`) cover
   everything we need; modloop-only-modules (squashfs, less
   common stuff) are unavailable but unblocked.
   - **Cleaner long-term fix**: pi-bake's pxe backend should pack
     modloop-rpi into the initramfs (concat to cpio.gz, place
     at `/lib/modules/.modloop/modloop-rpi`), and patch the
     init script to recognize it. Adds ~33 MB to the initramfs
     but produces a fully-functional system on first boot. Worth
     it for a recovery image.

**Cross-refs:**
- [[alpine-ext4-sys-mode-backend]] — sibling backend; PXE
  shares the recipe schema (hostname, ssh, network,
  packages, cache_packages) but different output shape.
- [[raspbian-pxe-recovery]] — known-good alternative for
  recovery-only PXE, doesn't need the Alpine-specific
  initramfs rebuild.

### `os: raspbian` + `os_mode: pxe` — known-good recovery PXE

**From:** pi-bake session — 2026-05-27, paired with
[[os_mode-pxe]] for the harder-to-net-boot Alpine.

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
- [[os_mode-pxe]] — sibling Alpine variant; same recipe
  schema, different network protocol.
- The CM4 EEPROM flash workflow that motivated this entry
  is itself a downstream-of-pi-bake concern (`rpi-eeprom`
  runs on the device, not at bake time). pi-bake provides
  the boot environment; the operator scripts the EEPROM
  update inside it.

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
- [[ab-slot-boot]] — once direct-to-device exists, A/B slot
  writes become natural (partition the target, write to the
  inactive slot).
- [[iflag-fullblock-documented]] — the SSH-stream flash
  workaround we'd no longer need.

### A/B slot boot + atomic upgrade

**From:** pi-bake session — 2026-05-27. mynotes.txt has the
original framing ("Two OS boot with watchdog scaffold"); this
entry is the more concrete design now that we have ext4 sys-
mode working.

**The shape:** image layout with TWO root-fs slots (`/dev/
mmcblk0p2` and `/dev/mmcblk0p3` or similar). Bootloader picks
which slot to boot via `cmdline.txt` updated at upgrade time.
Watchdog reverts to the other slot if first-boot sanity check
fails.

```
SD card:
  p1: FAT /boot      (256 MB) — kernel + initramfs +
                                  config.txt + cmdline.txt +
                                  ab-state.txt
  p2: ext4 root-A    (1.7 GB)
  p3: ext4 root-B    (1.7 GB)
  p4: ext4 data      (rest)   — persistent across slot swaps
```

**Why it matters:** the "I upgraded my Pi and now it doesn't
boot" experience is the worst possible failure mode for an
appliance. A/B + watchdog makes upgrades reversible — the new
slot fails, the watchdog reboots to the old slot, operator
investigates from a working system.

**Recipe shape:**

```yaml
os: alpine
os_mode: ext4
partition:
  layout: ab            # vs. single (current default)
  boot_size_mb: 256
  root_size_mb: 1700    # per slot
  data: fill            # remaining space
ab:
  active_slot: a        # which slot the bake populates +
                        # boots first
  watchdog:
    enabled: true
    sanity_check: /usr/local/bin/first-boot-ok
    revert_after_sec: 120   # if check doesn't pass in 2 min,
                            # revert
```

**Upgrade workflow** (post-bake, on the running Pi):
- Operator uploads new .img.xz to /data
- A `pi-bake-ab-upgrade <image.img.xz>` tool (shipped in the
  baked image) writes the new image to the INACTIVE slot
- Updates `/boot/ab-state.txt` to flag new slot as
  "pending-verify"
- Reboots
- Bootloader (read by initramfs hook) picks the pending-verify
  slot
- On successful boot + sanity-check passing, ab-state moves to
  "active"; old slot becomes "previous"
- If sanity fails or watchdog fires, ab-state reverts to
  previous

**Implementation sketch:**
- Partition layout: extend `_partition_image()` in
  `alpine_ext4.py` to support 4-partition layout.
- Slot-write at bake time: write the apkovl + bootstrap into
  the recipe's `active_slot`. The other slot is empty.
- Initramfs hook: small shell script that reads
  `/boot/ab-state.txt` and edits `cmdline.txt`'s `root=`
  on the fly before kernel exec. (Alternative: dual `root=`
  via `kernel-cmdline` builder.)
- Watchdog: openrc service that runs the sanity check; on
  failure, calls `reboot` (which the initramfs sees as a
  failed boot and reverts). Or a kernel watchdog timer
  (more reliable) configured via /etc/conf.d/.
- New baked tool: `/usr/local/bin/pi-bake-ab-upgrade` that
  does the slot dance.

**Why ext4 backend first, then this:** A/B needs the
partitioned-image shape we just built in v0.3.1.
Diskless-mode A/B is harder (apkovl + modloop on FAT make
slot rotation messy). ext4 cleanly extends to 4-partition.

**Cross-refs:**
- [[direct-to-device-flash]] — natural consumer of A/B mode
  (the upgrade tool writes to the inactive slot's block
  partition directly).
- mynotes.txt "Two OS boot with watchdog scaffold" — original
  framing of this feature.

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
  Raspbian — works with our raspbian backend) and the dd command
  with `/dev/sdX` instead of `/dev/mmcblkN`.
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
- [[direct-to-device-flash]] — when `output.path: /dev/sdX` lands,
  flashing to USB becomes a single pi-bake invocation.
- [[ab-slot-boot]] — A/B slot writes pair naturally with USB
  storage (more space, faster writes for the inactive slot).

### HAT catalog + config.txt overlays

**From:** general — needed for any Pi with PCIe HAT, sense
HAT, displays, etc.

**Already in ROADMAP** under "v0.2 — HAT catalog + config.txt
overlays". Currently pi-bake only writes the apkovl, not
`/uboot/usercfg.txt` or `/config.txt`. HAT-bound boards
(BE200 on PCIe, Adafruit PiTFT, Sense HAT) need
`dtoverlay=` / `dtparam=` edits to enable the bus / device
tree node.

**Schema sketch:**
```yaml
hat:
  - waveshare-poe-m2-e
  - sense-hat
```
Each `hat:` entry resolves to a catalog entry knowing the
required `dtoverlay=` / `dtparam=` lines, which get appended
to `/uboot/usercfg.txt` on FAT.

**Depends on:** [pibakehub](#pibakehub--community-recipe-registry)
for the catalog source.
