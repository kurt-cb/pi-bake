# CHANGELOG

Release notes for pi-bake. Generated from annotated git
tags via `./scripts/release-notes.sh`. To add notes for
a new release, tag the commit with
`git tag -a vX.Y.Z -m "..."` and re-run this script.

## v0.3.2 — 2026-05-27

## v0.3.1 — 2026-05-27

## v0.3.0 — 2026-05-26

v0.2.1-v0.2.6 built a chroot+qemu-user-static+binfmt_misc pipeline to
upgrade the Alpine RPi tarball's kernel to edge at bake time
(motivated by Intel BE200 iwlwifi being absent from stable 3.21's
linux-rpi-6.12.13 modloop). ~370 lines of fragile infrastructure
serving a single HAT, breaking once per iteration.

Operator call 2026-05-26: drop BE200, evaluate AX210 (same iwlwifi
driver family, present in stable Alpine's modloop) as a clean
replacement. That obviates the entire edge path.

This commit removes:
- src/pi_bake/alpine_edge.py + tests/test_alpine_edge.py (the
  chroot+qemu+modloop infrastructure — gone with the reset to f5727d6)
- `edge` from oses.py's Alpine versions tuple + the resolve_image()
  URL special-case
- bake.py's `if resolved_version == "edge"` branch
- The pi-5-be200-edge.yaml example + intel/be200 pibakehub fragment
- The "Alpine edge" entry from ROADMAP (marked ❌ dead-ended,
  pointing readers at the preservation branch)
- All edge/BE200 mentions from CLAUDE.md, README.md,
  pi-bake.example.yaml, pibakehub-pilot/README.md, and the example
  recipe comments

The v0.2.1-v0.2.6 work is preserved on branch kurt-cb/edge-mistake
for archaeology. Don't resurrect the chroot/qemu/modloop work — if a
future hardware item needs an Alpine kernel newer than stable ships,
wait for the next point release.

Tests: 109 passed (down from 121 — removed 10 alpine_edge tests + 1
recipe-edge-passthrough test + 1 example-file parse test).

## v0.2.6 — 2026-05-26

v0.2.5 left /boot/modloop-rpi as the stable minirootfs's squashfs even
after `apk add linux-rpi` installed edge modules into
/lib/modules/<edge-ver>/. Result: edge kernel boots, but module loading
silently falls back to whatever was in the stale modloop — iwlwifi
still missing on a BE200.

Root cause: Alpine's `linux-rpi` apk install hook unpacks modules +
runs depmod, but does NOT regenerate modloop. That's a release-tooling
step (scripts/mkimg.rpi.sh → mkmodloop-boot) that runs at tarball-build
time, not at package-install time.

Fix in alpine_edge.py: after the apk transaction, if
/boot/modloop-rpi is missing OR predates the new modules,
_generate_modloop_rpi() apk-adds squashfs-tools in the chroot,
stages /lib/modules (+ /lib/firmware when present) into a temp tree,
and mksquashfs's it to /boot/modloop-rpi with -comp xz to match
Alpine's release modloop format.

Layout of the squashfs root mirrors Alpine release modloop:
  modules/<kver>/...
  firmware/...
init mounts modloop at /.modloop and symlinks /lib/modules →
/.modloop/modules, so the modules/ root entry (not lib/modules/) is
correct.

## v0.2.5 — 2026-05-26

## v0.2.4 — 2026-05-26

**v0.2.3: minirootfs extract uses filter="tar" not "data"**

v0.2.2's minirootfs chroot setup hit AbsoluteLinkError on
Python 3.12+:

  tarfile.AbsoluteLinkError: './usr/bin/yes' is a link to an
  absolute path

Cause: Python 3.12 added safety filters to tarfile.extractall().
The `data` filter (most strict) rejects absolute symlinks. That
breaks alpine-minirootfs extraction because busybox is wired up
via absolute symlinks throughout (/bin/cat, /bin/sh, /usr/bin/yes,
... → /bin/busybox).

Fix: use the `tar` filter (next-strictest after `data`). Allows
absolute symlinks but still blocks path traversal + absolute file
paths. Safe because the minirootfs is sha256-verified against
Alpine's signed .sha256 sidecar.

Caught by the operator on the first v0.2.2 edge bake attempt.

(The alpine.py extract of the RPi tarball keeps filter="data" —
that tarball doesn't have absolute symlinks and the operator's
bakes work with the stricter filter.)

No new tests — the failing code path is the tarfile.extractall()
call itself, which Python's standard library covers; the fix is
just choosing the right filter constant. Hardware test will
catch any new edge cases.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## v0.2.3 — 2026-05-26

**v0.2.2: edge kernel upgrade chroots minirootfs (not the FAT tarball)**

v0.2.1's alpine_edge.upgrade_to_edge_kernel() chroot'd into the
extracted alpine-rpi-*.tar.gz tree. That tree is a FAT-layout
bundle (bootcode.bin, start4.elf, /apks/<arch>/*.apk, etc.) —
NOT a chroot-able rootfs. chroot would fail immediately:

  error: chroot apk update failed (rc=127):
  stderr: chroot: failed to run command '/sbin/apk':
          No such file or directory

Caught by the operator on the first real-hardware edge bake
attempt. Filed in feature_request.md the same day; this is the fix.

New flow (matches the operator's proposed shape):

  1. Derive alpine-minirootfs URL from the RPi tarball URL
     (same {branch, arch, version} path; just swap the filename
     prefix from `alpine-rpi-` to `alpine-minirootfs-`).
  2. Download via pi_bake.download.fetch() — caches + sha256-verifies
     against the .sha256 sidecar Alpine publishes.
  3. Extract minirootfs into workdir/edge-chroot/ — SEPARATE from
     the RPi tarball tree. The minirootfs IS the actual busybox +
     musl + apk-tools rootfs (~3.8 MB), chroot-ready.
  4. Copy qemu-<arch>-static, /etc/resolv.conf into chroot; bind
     /proc /sys /dev.
  5. Point chroot's /etc/apk/repositories at edge main + community.
  6. `apk update` + `apk upgrade --available --latest` (avoids
     cross-version dep skew between stable minirootfs and edge
     linux-rpi).
  7. `apk add linux-rpi linux-firmware-rpi mkinitfs` — install
     hook regenerates /boot/{vmlinuz-rpi, initramfs-rpi,
     modloop-rpi} inside the chroot.
  8. Copy those 3 boot artefacts FROM chroot/boot/ TO
     extracted_root/boot/, replacing the stable versions.
     modloop-rpi IS the squashfs of /lib/modules/<edge-ver>/,
     so this single copy covers the entire module set — no
     separate /lib/modules sync needed.

Cleanup: unmount binds in reverse order, workdir cleanup is the
caller's responsibility (alpine.bake passes its TemporaryDirectory).

Signature changed:
  before: upgrade_to_edge_kernel(extracted_root, target_arch="aarch64")
  after:  upgrade_to_edge_kernel(extracted_root, *, rpi_tarball_url,
                                  workdir, target_arch="aarch64")

alpine.bake() updated to pass `url` + `td` (its TemporaryDirectory).

3 new tests in test_alpine_edge.py for minirootfs_url() derivation
(aarch64, armhf, rejects non-RPi URLs). The original 7 skip-path
tests still pass.

Tests: 121 passing (+3).

NOT YET VERIFIED on real hardware. v0.2.1's verification path
also wasn't run (operator's first run hit this bug at line 1).
Now ready for the operator's retry — same recipe + sudo + the
qemu-user-static + binfmt_misc deps that v0.2.1 documented.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## v0.2.2 — 2026-05-26

v0.2.1's alpine_edge.upgrade_to_edge_kernel() chroot'd into the
extracted RPi tarball — which is FAT-layout, no /sbin/apk — and
failed immediately. v0.2.2 downloads alpine-minirootfs separately,
chroots THAT, copies the edge boot artefacts (vmlinuz-rpi,
initramfs-rpi, modloop-rpi) into the RPi tarball tree.

Same operator-facing UX as v0.2.1: os_version: edge + bake-host
deps (qemu-user-static + binfmt_misc + sudo) → actually-edge
kernel in the sealed image. If deps missing, same clear-warning
fallback to stable kernel.

3 new tests for minirootfs_url derivation. 121 passing total.

## v0.2.1 — 2026-05-26

  * Bug 1: os_version: edge now actually delivers an edge kernel
    via bake-time chroot+qemu-user-static (alpine_edge.py).
    Requires qemu-user-static + binfmt_misc + sudo on bake host;
    skipped with a clear warning if missing. (a5cff99)

  * Bug 2: static IP reinforcement tests against current code —
    the operator's "empty interfaces" report couldn't reproduce
    on current master, suggesting their bake came from pre-#3
    pi-bake. Added 4 belt-and-suspenders tests so the next
    regression gets caught fast. (f5727d6)

  * Bug 4: board-aware LBU_MEDIA. Pi Zero W's FAT mounts at
    /media/mmcblk0p1 (partition 1 of a partitioned image), not
    /media/mmcblk0 like Pi 3/4/5/Zero 2 W. With the wrong
    LBU_MEDIA, `lbu commit` silently wrote to nowhere — operator's
    deploy state never persisted across reboot on Pi Zero W
    diskless deploys. New NodeConfig.board field + _LBU_MEDIA_BY_BOARD
    lookup. (f5727d6)

Tests: 118 passing (+11 from v0.2.0).

PXE bench infra also landed in this release (commits 6718148,
be1b9c3, 414d6bc): dnsmasq config + tools/img-to-tftp.sh +
end-to-end CM4 PXE verification + design/infra_pxe.md.

Also fixed: pi-bake's raspbian backend choking on Pi OS's
`raspios_lite_arm64_latest` permanent redirect URL (no .xz
suffix on the cached filename → decompress_xz rejected it).
Now content-sniffs xz magic bytes instead of trusting the
extension. (9a84d9e)

## v0.2.0 — 2026-05-25

Highlights since v0.0.9:

* Pre-baked SSH host keys (commit c9c6367)
  ssh_host_key: <path> YAML field / --ssh-host-key CLI flag bakes
  the operator's keypair into /etc/ssh/ssh_host_<type>_key{,.pub}.
  Auto-generated ed25519 fallback. No more known_hosts churn.

* Bake-time apk-fetch (commit c9c6367)
  apk_fetch: true / --apk-fetch pulls operator packages + all
  recursive deps from upstream Alpine at bake time via apk-tools-
  static. Stages .apks in FAT; first-boot script installs offline.
  Pi never needs internet on first boot.

* pibakehub v1 design + pilot (commits 5020064 + 3367743)
  design/pibakehub-v1.md frozen-design doc with stable §N.M.K.J
  numbering. 7 scraped Waveshare HAT fragments + 1 verified
  intel/be200 fragment in pibakehub-pilot/. Composition prototype
  in tools/pibakehub_compose.py. Not wired into pi-bake build yet
  (v0.3+); informs the eventual --pibakehub implementation.

87 tests passing (63 → 87, +24 new). Integration tests for
bake-time apk-fetch gated behind PI_BAKE_INTEGRATION=1; all 7
pass against real upstream.

Verified end-to-end: pi-5-be200-edge.yaml with apk_fetch: true
produces a 211 MB image with 63 .apk files (5 declared packages +
recursive deps) in ~60 s.

## v0.0.9 — 2026-05-25

Found on real hardware: a freshly-baked v0.0.8 image with
`packages: [avahi, dbus, linux-firmware-intel, ...]` came up
with no DHCP — eth0 fell back to 169.x APIPA. Root cause: Alpine
init's first-boot does

    apk add --root $sysroot --no-network $world

reading /etc/apk/world. With --no-network, apk uses ONLY the
local /apks cache. The stock RPi tarball /apks cache carries
~150 packages — sshd, dhcpcd, chrony, wpa_supplicant — but NOT
avahi, dbus, linux-firmware-intel, etc. When the operator's
extras land in /etc/apk/world, apk can't resolve them with
--no-network, and the ENTIRE TRANSACTION FAILS WHOLESALE.
Result: dhcpcd uninstalled → no DHCP → 169.x APIPA → device
unreachable.

Fix: split package lists.
  - /etc/apk/world stays baseline-only (everything in stock
    cache). First-boot apk add --no-network succeeds.
  - /etc/local.d/install-extras.start: a POSIX shell script
    that waits for dhcpcd to bring up a default route (60s
    timeout), then `apk update && apk add <extras>` ONLINE.
    Idempotent — touches /var/lib/pi-bake/install-extras.done
    on success so re-boots are no-ops.
  - `local` runlevel auto-added when extras present (or when
    wifi is on, for the existing power-save-off script).

Trade-off: extras still need network on first boot. That's the
same constraint as v0.0.8 expected, but now it doesn't take
DHCP down with it. Air-gap deployment (every package in /apks
at bake time) remains the v0.3 ROADMAP goal — see "Bake-time
apk-fetch" section.

Tests: 2 new regression tests in test_apkovl.py
(test_extra_packages_NOT_in_world, test_install_extras_script_
runs_apk_add). 63 total tests pass.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## v0.0.8 — 2026-05-25

Alpine `edge` OS version
  Stable 3.21's linux-rpi-6.12.13 modloop omits the entire
  wireless/intel/ subtree (no iwlwifi.ko) despite bcm2711_defconfig
  having CONFIG_IWLWIFI=m; edge's linux-rpi-6.12.85 has it, plus
  linux-firmware-intel ships BE200 firmware (iwlwifi-gl-c0-fm-c0-*).
  Alpine doesn't publish an edge RPi tarball, so bakes fetch the
  latest stable tarball for FAT/bootloader layout and write
  /etc/apk/repositories pointing at edge — post-boot `apk upgrade`
  rolls the kernel + drivers + firmware forward.

YAML recipe (--config + --to-yaml)
  `pi-bake build --config <yaml>` reads a strict-validated recipe
  and bakes. `--to-yaml <path>` round-trips CLI flags (or a
  --config load) into a canonical annotated YAML. `--no-bake`
  serializes without baking. Strict load: unknown top-level or
  sub-keys raise with the operator-facing field name.

  Schema covers hostname, board, os, os_version, ssh_pubkey,
  extra_pubkeys, network (dhcp/static), wifi (optional), packages
  (extras appended to /etc/apk/world), output (path + image_size_mb).
  Pubkey strings auto-resolve as paths (~ / / ./) or inline keys.

  Annotated reference: pi-bake.example.yaml (dnsmasq-style, every
  field commented). Tested recipes: examples/{pi-zero-2-w-wifi-
  station,pi-5-wired-dhcp,pi-5-be200-edge,pi-zero-w-armhf}.yaml.

  PyYAML added as runtime dep.

Pi Zero W (BCM43438) WiFi power-save fix
  When wifi is enabled, bake /etc/local.d/wlan-power-save-off.start
  + add `local` to default runlevel. Script runs
  `iw dev wlan0 set power_save off` after wpa_supplicant comes
  up. Fixes the BCM43438's aggressive PS that keeps L2 associated
  while dropping ARP/L3 traffic — chipset looks "up" but isn't
  reachable. Harmless no-op on cards that handle PS properly.

Tests: 25 new test_recipe.py cases (load/dump/round-trip/examples);
61 total tests pass.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>

## v0.0.7 — 2026-05-23

**cli: --no-dhcp-hostname for intentional "missing option 12" test fixtures**

Default behavior unchanged (dhcpcd sends DHCP option 12 from
/etc/hostname — landed in d20b63e). New `--no-dhcp-hostname` CLI
flag (and `NodeConfig.dhcp_send_hostname=False`) bakes the
`hostname` directive in /etc/dhcpcd.conf as a comment instead, so
the device intentionally doesn't advertise its name via DHCP. The
DHCP server then falls back to mDNS lookup or to a synthesized
placeholder.

Use case: exercise DHCP-server-side hostname recovery paths (e.g.
totaldns §5.6 fuse — joining mDNS-discovered hostname into a lease
whose DHCP option 12 was missing). One bake produces a fixture
device; multi-vendor IoT noise is approximated without needing
the real fleet.

Future: add `--name-via {dhcp,mdns,both,none}` once bake-time
avahi fetch lands (pi-bake v0.2 ROADMAP item) — `mdns`/`both`
will then ALSO bake avahi-daemon into the image, not just toggle
the dhcpcd directive.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.0.6 — 2026-05-23

**alpine: bake `hostname` directive into /etc/dhcpcd.conf**

dhcpcd's default config DOES NOT advertise the system hostname in
DHCP option 12 — even though /etc/hostname is set. Stock dhcpcd-
openrc ships /etc/dhcpcd.conf with `option hostname` commented
out, so DHCP DISCOVER/REQUEST go out with `req_name=None` and the
DHCP server has no idea what to call the device.

Found on real hardware: a baked-with-pi-bake Pi Zero 2W
(hostname=td-pi0-1) joined a totaldns-served network and totaldns
logged the lease against the synthesized fallback `unknown-5e1bd9`
instead of `td-pi0-1`. avahi-discovered name was correct (so the
operator could still find the box via `td-pi0-1.local`), but the
DHCP record and the avahi record never get joined back at the
server (separate totaldns-side gap captured in FEATURES-TODO).

Fix: write our own /etc/dhcpcd.conf with `hostname` (bare, no
arg → uses /etc/hostname) plus the standard dhcpcd 10.x options
(duid, persistent, rapid_commit, classless_static_routes, etc.).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.0.5 — 2026-05-23

**Changed workflow name**

## v0.0.4 — 2026-05-23

Fixes from real-hardware Pi 5 deployment:
- .default_boot_services marker so modloop loads kernel modules
  (root cause of all prior DHCP failures — no af_packet without it)
- LBU_MEDIA=mmcblk0 so `lbu commit` works without args
- sshd_config: drop UsePAM (Alpine openssh has no PAM), use
  KbdInteractiveAuthentication
- openssh-sftp-server in /etc/apk/world (modern scp + pyinfra need it)
- dhcpcd over busybox udhcpc (works on Pi 5 macb driver)
- No firstboot script: packages install from local /apks cache at
  boot via /etc/apk/world (no network, no clock dependency)

## v0.0.2 — 2026-05-23

**roadmap: avahi=v0.2 bake-fetch, dhcpcd choice settled, apkovl backup lesson**

- v0.2 entry: bake-time apk fetch for avahi/dbus/linux-firmware-*
  (the bits not in stock cache), to enable .local discovery without
  giving up the no-firstboot-script property.
- Mark DHCP-client question resolved: dhcpcd (in stock cache, no
  fetch, works on Pi 5 macb where udhcpc hangs). Removes the
  earlier "switch to dhclient" wishlist item.
- Capture the apkovl-backup-name lesson learned the hard way: any
  file matching *.apkovl.tar.gz on FAT root confuses Alpine's
  bootloader. Backups must be off-FAT or renamed away from the
  pattern entirely.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

## v0.0.1 — 2026-05-23

**static IP + time sync + WiFi firmware + RTC-less boot survival**

Live-debug of the user's first Pi 5 deployment found three gaps:

1. **Pi 5 + busybox 1.37 udhcpc on Alpine 3.21 hangs**. We saw
   "udhcpc: started" then nothing — the macb driver / BPF socket
   path doesn't play nice here. Workaround until upstream fix:
   support a baked static IP so DHCP isn't on the critical path.

   - NodeConfig: new `static_ipv4` ("a.b.c.d/N") + `gateway_ipv4`
     fields. Validation: both-or-neither, CIDR form required.
   - Alpine baker: when set, /etc/network/interfaces uses
     `iface eth0 inet static` w/ address+netmask+gateway. Also
     writes /etc/resolv.conf (DHCP would've filled it; static
     doesn't).
   - CLI: --static-v4 + --gateway-v4 flags through to NodeConfig.

2. **Pi has no RTC; clock starts at 1970**. openssh + apk's TLS
   verify both complain about wildly-skewed dates.

   - Firstboot script: HTTP-Date header pull from
     dl-cdn.alpinelinux.org BEFORE apk update; sets a usable
     clock so subsequent TLS works.
   - apk add chrony + enable chronyd in default runlevel for
     ongoing NTP. Real fix arrives in ~30s after boot.

3. **WiFi firmware missing**. The user's Pi 5 has Intel BE200 +
   built-in BCM43455 — neither shows up without firmware blobs.

   - Firstboot: apk add wifi-firmware (meta — pulls in
     linux-firmware-brcm + linux-firmware-iwlwifi etc).

Tests still 28/28 passing. End-to-end smoke bake verified:
interfaces uses static when --static-v4 set, firstboot script
contains chrony + wifi-firmware + the HTTP Date hack.

Follow-up TODO (not in this commit): user_config.txt edits on
the FAT partition (e.g. PCIe overlays for the BE200 HAT). That
needs a FAT-write path beyond just apkovl drop — separate work.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>

