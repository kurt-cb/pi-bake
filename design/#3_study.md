# #3 study — why isn't everything just in the baseline?

A study doc, not a design doc. Captures the architecture and
the decision space for ROADMAP item #3 ("Init-time install of
bake-staged extras (signed APKINDEX)") so the operator can
review it cold and decide what they want.

**Bottom line up front:** there is no technical reason
"everything" can't be in `/etc/apk/world` and install at init
time. The thing currently keeping operator-declared extras
*out* of /etc/apk/world is one specific missing piece — a
regenerated, signed `APKINDEX.tar.gz` on the FAT partition.
Item #3 is "add that piece." Once it's in, the artificial
"baseline vs extras" split disappears and everything is just
"the package set" installed at init.

---

## §1  What "baseline" means today

`/etc/apk/world` (in the apkovl) is a plain-text list of apk
package names, one per line. On first boot, Alpine's init does:

```sh
apk add --root $sysroot --no-network $(cat $sysroot/etc/apk/world)
```

That's real apk-tools, not a workaround. The `--no-network`
flag means it resolves package names against the LOCAL
repositories listed in `$sysroot/etc/apk/repositories` — for
us, that's `/media/mmcblk0/apks` (the FAT-resident cache).

For the add to succeed, every package in `world` needs:

  1. A matching `<pkg>-<ver>-<rN>.apk` file under
     `/media/mmcblk0/apks/<arch>/` (or its `extras/` subdir,
     once we extend the repos line — see §6).
  2. An entry in the matching `APKINDEX.tar.gz` for that
     directory.
  3. A valid signature on the APKINDEX, by a key apk trusts.

If ANY of those is missing for ANY package in world, the entire
transaction fails. No partial installs. (This is CLAUDE.md
lesson #1 — we lost a half-day to learning it in v0.0.8.)

That all-or-nothing rule is WHY pi-bake today restricts
`/etc/apk/world` to packages we KNOW are in the stock Alpine
RPi cache (the 101 `.apk` files shipped in the tarball). Adding
`avahi` to world without doing the rest of the work would brick
the boot.

---

## §2  Where the signing trust comes from

apk-tools verifies the APKINDEX signature using whatever public
keys are in its `--keys-dir`. For init's `apk add --root
$sysroot ...`, the keys-dir defaults to `$sysroot/etc/apk/keys/`.

`$sysroot/etc/apk/keys/` gets populated by two paths during
init, in this order:

  1. **Apkovl extraction.** Anything we put at
     `etc/apk/keys/<name>.rsa.pub` in the apkovl tarball lands
     here.
  2. **Initramfs copy.** The init script's line ~936 does
     `cp -a /etc/apk/keys $sysroot/etc/apk` — initramfs ships
     Alpine's official devel keys (5 .pub files, one per active
     signing key), and they get merged in.

`cp -a` doesn't overwrite files that exist with different
names. So our `pi-bake-<random>.rsa.pub` from the apkovl
coexists peacefully with alpine-devel's keys. Both are trusted
when init runs apk add.

We have verified this by reading the init script directly
(it's in `boot/initramfs-rpi` in the Alpine RPi tarball, gzipped
cpio, ~1100 lines of POSIX shell).

---

## §3  What's actually in the stock /apks/<arch>/

The Alpine RPi 3.21.4 tarball ships 101 `.apk` files in
`/apks/aarch64/` and a matching `APKINDEX.tar.gz` signed by
alpine-devel. The 101 cover:

  - alpine-base + baselayout
  - busybox + busybox-mdev-openrc
  - openssh-server + openssh-sftp-server + openssh-client-default
  - dhcpcd + dhcpcd-openrc
  - chrony + chrony-openrc
  - wpa_supplicant + ifupdown-ng-wifi + iw
  - linux-rpi (kernel) + alpine-conf + raspberrypi-bootloader
  - apk-tools, openrc, ca-certificates-bundle, etc.

It does NOT cover:

  - avahi, dbus, py3-pydbus
  - linux-firmware-* (none of brcm, intel, lenovo, etc.)
  - anything user-domain (vim, htop, tcpdump, python3-can, …)

So as long as the operator's recipe stays inside the 101 .apks,
`/etc/apk/world` can list them all directly and init's apk add
just works. The moment the recipe wants something outside that
set, we need to add the .apk file to the cache AND extend the
APKINDEX AND have signing/trust line up.

---

## §4  What v0.2's `apk_fetch: true` does today

The shipped v0.2 air-gap path does TWO of the three things
needed:

  ✓ Fetches the operator's extras + all recursive deps into
    `/apks/<arch>/extras/` on the FAT partition.
  ✓ Knows the .apks are real and verified — they came from
    upstream Alpine repos, signed by alpine-devel, verified by
    apk-tools-static at bake time using the same alpine-devel
    keys (extracted from the bake target's own initramfs).

  ✗ Does NOT regenerate the local APKINDEX.tar.gz to list the
    new files.
  ✗ Does NOT add the operator's extras to `/etc/apk/world`.

The workaround for those two gaps is the
`/etc/local.d/install-extras.start` script written into the
apkovl. It runs LATE in boot (after the `default` runlevel
starts most services), and does:

```sh
apk add --no-network --allow-untrusted \
    /media/mmcblk0/apks/*/extras/*.apk
```

Two key things in that command:

  1. **Files-as-arguments.** Passing `.apk` PATHS to `apk add`
     bypasses repository lookup. apk reads each file's metadata
     directly, builds the dep graph, installs them.
  2. **`--allow-untrusted`.** Because we passed files, apk
     would normally check each file's SIGNATURE against trusted
     keys. The extras .apks ARE signed by alpine-devel (we
     fetched them from upstream), but `--allow-untrusted` says
     "I don't care about per-file signatures, install anyway."
     Needed because the index for `extras/` doesn't exist, and
     apk's verification path doesn't work without one.

Net result: same end state (everything installed, offline), but
the install moment is post-sshd instead of at-init.

---

## §5  Why the "baseline vs extras" split exists at all

There IS no architectural baseline-vs-extras distinction. The
split is purely a workaround for "we haven't done #3 yet." It's
a label pi-bake uses internally to mean:

  - **baseline** = "stuff init's apk add can handle today
    without us doing index regen + signing"
  - **extras**   = "stuff that needs the index regen + signing
    work we punted on"

That's the whole story. Once #3 lands, the distinction
disappears: there's just "the package set the operator
declared," and it all goes in `/etc/apk/world`.

---

## §6  What #3 ACTUALLY does (in detail)

Three new bake-time steps, after the existing `apk_fetch: true`
fetch phase:

### §6.1  Regenerate the index

Run `apk index` over BOTH stock and extras .apks to produce a
single new unsigned `APKINDEX.tar.gz`:

```sh
apk-tools-static index --rewrite-arch aarch64 \
    -o APKINDEX.unsigned.tar.gz \
    /apks/aarch64/*.apk /apks/aarch64/extras/*.apk
```

The result is a small tar of two files: `APKINDEX` (plain-text
metadata for every .apk) and optionally `DESCRIPTION`.

### §6.2  Sign the index

apk-tools doesn't sign indexes itself. `abuild-sign` (a small
shell script from the abuild package) does. We replicate its
~20 lines in Python so we don't need abuild on the bake host:

  1. Generate a 2048-bit RSA keypair via `openssl genrsa` +
     `openssl rsa -pubout`. Per-bake, ephemeral.
  2. Hash the unsigned tar.gz with SHA-1 and sign with the
     private key:
     ```sh
     openssl dgst -sha1 -sign privkey.pem \
                  -out sig.bin APKINDEX.unsigned.tar.gz
     ```
  3. Build a one-file tar containing `.SIGN.RSA.pi-bake-<hex>.rsa.pub`
     with `sig.bin` as the body.
  4. gzip the signature tar.
  5. Concatenate signature.tar.gz + APKINDEX.unsigned.tar.gz →
     `APKINDEX.tar.gz`. (gzip is a multi-stream format; tar
     reads concatenated streams natively.)

The result is a properly-signed APKINDEX.tar.gz that apk-tools
verifies against the matching pubkey.

### §6.3  Bake the pubkey into the apkovl

Add `etc/apk/keys/pi-bake-<hex>.rsa.pub` to the apkovl tarball.
Per §2 above, init's `cp -a` will preserve our key alongside
Alpine's devel keys when populating `$sysroot/etc/apk/keys/`.

### §6.4  Move extras into /etc/apk/world + delete the script

Operator-declared extras + their recursive deps get added to
`/etc/apk/world` in the apkovl. The `/etc/local.d/install-extras.start`
script and its `local` runlevel symlink are NOT generated.

### §6.5  Decide the directory layout

Two options:

  (a) **One flat cache.** Put extras files in `/apks/<arch>/`
      directly (not in `extras/`). Single repository, single
      index. Slightly tidier on the Pi side.

  (b) **Keep /extras/ separate.** Keep extras in `/apks/<arch>/extras/`
      and add an extra repository line to `/etc/apk/repositories`:
      ```
      /media/mmcblk0/apks
      /media/mmcblk0/apks/aarch64/extras
      http://dl-cdn.alpinelinux.org/alpine/edge/main
      http://dl-cdn.alpinelinux.org/alpine/edge/community
      ```
      Two indexes (the stock one signed by alpine-devel, the
      extras one signed by us). Lets us NOT regenerate the
      stock index — only generate ours from scratch over the
      extras dir. Less code, narrower blast radius.

(b) is simpler and probably correct. Documented here so the
choice is reviewable.

---

## §7  Boot-time impact

### Today (v0.2 shipped)

```
init's apk add --no-network <baseline-only world>
   ↳ baseline installs (sshd, dhcpcd, chrony, etc.)
sshd starts
chronyd starts
dhcpcd brings up network
🟢 Pi is SSH-reachable                          ← here
local runlevel runs
   ↳ install-extras.start
     ↳ apk add --no-network --allow-untrusted /apks/*/extras/*.apk
     ↳ avahi, dbus, firmware install (~5-15s later)
```

### After #3

```
init's apk add --no-network <full world incl. extras>
   ↳ baseline + extras all install (avahi, dbus, firmware loaded)
sshd starts
chronyd starts
dhcpcd brings up network
🟢 Pi is SSH-reachable + fully provisioned      ← here
(no local.d script, no late install)
```

Operator-visible difference: zero, except for the timing. By
the time pyinfra reaches the Pi, drivers/firmware/services that
came in via extras are already running.

---

## §8  Cases where the timing difference matters

For most appliances, "extras install 5-15 seconds after sshd"
is invisible. Cases where it isn't:

  - **Firmware required by early-boot drivers.** iwlwifi loads
    its firmware blob from `/lib/firmware/` when the module
    initializes. If the firmware .apk hasn't installed yet, the
    radio is dead until the next reboot. Today the operator
    works around this with a `rmmod iwlwifi && modprobe iwlwifi`
    after install-extras.done lands.
  - **Services that depend on extras.** A pyinfra deploy that
    expects `avahi-daemon` to be running can race the install
    script. Operator works around with `wait_for_pyinfra:
    avahi.service` or a sleep.
  - **First-boot smoke tests in CI.** A test that boots the Pi
    + runs an inventory needs to either wait for install-extras.done
    or risk false negatives on "is foo installed yet."

#3 removes all three classes of issue. Whether that's worth
the implementation effort is the operator call.

---

## §9  Alternatives considered + rejected

### §9.1  Pass `--allow-untrusted` to init's apk add

Would require modifying the init script in the initramfs
(`boot/initramfs-rpi` — a gzipped cpio). Invasive: we'd be
diverging from upstream's init for every bake, and an init
change between Alpine RPi versions could break us silently.
**Rejected.**

### §9.2  Sign with an Alpine-trusted key

Alpine's devel signing keys are private to the Alpine project.
We can't sign with them. **Rejected — not available to us.**

### §9.3  Vendor a permanent pi-bake signing key

Generate one signing key, distribute the pubkey in pi-bake's
source, sign every bake's APKINDEX with the same private key.
Stable trust across all pi-bake users. **Rejected — the private
key would have to ship with pi-bake to be useful, defeating
the point of signing.** (Anyone can re-sign anything with a
key everyone has access to.)

### §9.4  Per-bake ephemeral signing key (chosen for #3)

Generate a fresh RSA keypair per bake. Private key lives only
in bake-host RAM; pubkey goes into that ONE bake's apkovl. Each
device trusts only its own bake's pubkey. **Selected.** Minor
downside: each `pi-bake build` invocation produces a different
pubkey, but that's fine because the pubkey is per-image and the
image is per-device.

### §9.5  Keep the v0.2 local.d --allow-untrusted path

The shipped path. **Kept as the fallback** when operator hasn't
opted in. #3 doesn't remove v0.2's path; it adds a better one.

---

## §10  What the operator-facing recipe looks like

NO CHANGE. Today:

```yaml
packages:
  - avahi
  - dbus
  - linux-firmware-intel
apk_fetch: true
```

After #3 (same recipe):

```yaml
packages:
  - avahi
  - dbus
  - linux-firmware-intel
apk_fetch: true
```

The schema doesn't grow. Internally pi-bake just stops
generating the local.d script and starts generating the signed
index instead.

If the operator wants the v0.2 behavior (e.g. for compatibility
with custom local.d hooks they wrote), we could add an opt-out
later — `apk_install_when: local_d` vs `apk_install_when: init`
— but that's noise to add only if real demand surfaces.

---

## §11  Implementation cost estimate

Code:

  - APKINDEX regen via apk-tools-static (already cached) ~30 LOC
  - openssl-based signature ~40 LOC
  - Multi-stream gzip tar concat ~20 LOC
  - Apkovl key + world updates ~30 LOC
  - Plumbing through bake.py + alpine.py ~50 LOC
  - Tests ~100 LOC

Total ~270 LOC of which ~70 is signing/index plumbing that's
fiddly but well-understood (abuild-sign is the reference).

Bake-host deps added:

  - openssl (already on every Linux dev box; pi-bake already
    uses it indirectly for SSH host key auto-gen via ssh-keygen)

No new Python deps. Stays inside the "stdlib + PyYAML" constraint
the project values.

Bake time impact:

  - +~3-5 seconds for index regen + signature generation
  - No change in image size of any consequence (APKINDEX is
    small, pubkey is ~400 bytes)

---

## §12  What to verify before merging #3

Real-hardware tests:

  1. Bake a recipe with `packages: [linux-firmware-intel]` +
     `apk_fetch: true`. Flash, boot, SSH in.
  2. Verify `apk info -e linux-firmware-intel` returns OK
     IMMEDIATELY after sshd reachable (no "not installed yet"
     race window).
  3. Verify the iwlwifi early-boot scenario: BE200 in PCIe,
     `dmesg | grep iwlwifi` shows successful firmware load on
     first try (no rmmod/modprobe cycle needed).
  4. Verify `apk verify` against any installed package returns
     clean (signing chain works end-to-end).

Tests-doc artifacts to add to CLAUDE.md "Critical lessons":

  - APKINDEX format being multi-stream gzip — easy to corrupt
    if cat'ed wrong.
  - Signature filename inside the .SIGN tar must match the
    pubkey filename in /etc/apk/keys/ exactly.

---

## §13  Open questions for the operator review

  1. Are you happy with per-bake ephemeral signing keys, or do
     you want a knob to use a stable per-operator key (like
     `ssh_host_key:` does)? The latter would let an operator
     re-bake the same recipe + get a byte-identical image —
     useful for reproducibility CI.

  2. Is the §6.5 "two-repo" approach acceptable, or do you
     prefer one flat cache? Both work; two-repo is simpler.

  3. Do you want #3 to REMOVE the v0.2 local.d path, or keep
     both as user-selectable? Default behavior should be #3
     once it's stable; v0.2 as fallback only if real demand.

  4. Is the implementation cost (~270 LOC, ~3-5s bake time, no
     new deps) the right time to spend, vs. spending it on #7
     (--pibakehub wiring — now unblocked by #6) or #9/#10/#11
     (multi-OS backend)?

---

## §14  What `apk add --no-network` actually does

A really good question that the rest of this doc has been
hand-waving past. "Just set the overlay" is close — but apk
does more than file extraction. Let's walk through what runs
when init does `apk add --root $sysroot --no-network openssh-server`.

### §14.1  The steps apk takes

For each package being installed:

  1. **Solve.** Read each .apk's metadata (`.PKGINFO`), build
     the dependency graph, decide install order. Already-cached
     so no network.
  2. **Extract.** Open the .apk (gzipped tar), extract files
     into the root with their packaged perms/uid/gid.
  3. **Update DB.** Record the package in
     `/var/lib/apk/installed` (the apk database) so subsequent
     `apk info` / `apk del` know it's there. Update
     `/etc/apk/world` to canonicalize the operator's request.
  4. **Run install scripts.** If the .apk ships a `.pre-install`
     / `.post-install` / `.pre-upgrade` / `.post-upgrade` /
     `.pre-deinstall` / `.post-deinstall` hook, apk `chroot
     $sysroot` and runs it. For first-boot install, only
     `pre-install` + `post-install` fire (no upgrade path).
  5. **Fire triggers.** If installing this package satisfies
     a trigger declared by ANOTHER package's `.trigger` script,
     apk runs that trigger. (Triggers fire when files appear in
     a watched directory — `ldconfig`, `update-mime-database`,
     etc. use this.)

So no, it's not just "set the overlay." Real scripts run.

### §14.2  Examples of what those scripts do (from real apks)

  - `openssh-server.post-install`: creates `/etc/ssh/` dir,
    chmods. Does NOT regenerate host keys (that's a separate
    init.d service `sshd-keygen`).
  - `alpine-baselayout.post-install`: creates `/proc`, `/sys`,
    `/dev` mount points; populates `/var/{log,cache,…}`; sets
    perms on `/tmp`.
  - `busybox.post-install`: creates `/var/spool/cron`, sets
    ownership.
  - `dhcpcd.post-install`: creates `/var/lib/dhcpcd/`,
    `/var/run/dhcpcd/` with the right owner.
  - `chrony.post-install`: creates the `chrony` user/group,
    chowns `/var/lib/chrony/`.
  - `ca-certificates.trigger`: regenerates
    `/etc/ssl/certs/ca-certificates.crt` from the loose pem
    files in `/usr/share/ca-certificates/`.
  - `alpine-conf.trigger`: nothing critical — mostly just
    re-runs `setup-*` plumbing.

These are all FILESYSTEM operations: mkdir, chown, write a
small config, run a shell command. None of them need a running
init system / dbus / network.

### §14.3  Are there hooks that DON'T work during init?

In theory yes. Hypothetically a hook that did
`systemctl enable foo.service` would fail (systemd isn't
running). A hook that did `dbus-send` to a live bus would
fail. A hook that needed network would fail.

In practice on Alpine: hooks are written with the assumption
they might run on a chroot/installroot, so they avoid live-
system dependencies. OpenRC's service registration is
filesystem-based — `rc-update add foo default` just creates
`/etc/runlevels/default/foo` as a symlink to `/etc/init.d/foo`.
No init system needed. So the standard set of hooks works
identically whether apk add runs at init time (in initramfs)
or post-boot (in the live sysroot).

### §14.4  Does this differ between baseline and extras?

NO. Same mechanism. If avahi's `.post-install` does `mkdir -p
/var/run/avahi-daemon && chown avahi /var/run/avahi-daemon`,
that runs identically whether init's apk add installs it (at
init time) or `local.d/install-extras.start`'s apk add does
(post-sshd).

The local.d path doesn't change which scripts run or what they
do. It only changes WHEN. Same hooks, same effects, same
filesystem mutations.

### §14.5  The one corner case: hooks that need NETWORK

If some weird extra .apk's post-install needed to reach the
internet (e.g. to download a model file), it would fail at
init (no network up yet — init is pre-runlevel) AND it would
fail in local.d if the operator is air-gapped. So that .apk
just isn't compatible with offline first boot, no matter when
apk add runs.

None of the packages in the v0.2 / pibakehub-pilot set have
this property. We'd notice fast if a future one did — apk add
returns non-zero on failed scripts and pi-bake's local.d
script logs that.

### §14.6  Why this confirms the "no reason not to baseline"
        thesis

The user's intuition — "it should just set the overlay, and
that's it" — was nearly right, and the residual "but what
about scripts?" concern dissolves on inspection:

  - Yes, apk runs scripts.
  - The scripts work the same at init time as they do post-boot.
  - There's no class of "init-only-breaks-here" hooks that
    affect realistic extra packages.

So when #3 lands and extras go into baseline:

  - Same .apks
  - Same scripts
  - Same filesystem mutations
  - Just earlier in boot

---

## §15  TL;DR

There's no architectural reason "everything" can't be in
`/etc/apk/world` today. The thing preventing it is one specific
missing piece (a regenerated, bake-signed APKINDEX.tar.gz on the
FAT partition). #3 is "add that piece." Once done, the
internal-only "baseline vs extras" split goes away, init's
`apk add` installs the full operator-declared set at init time,
and `/etc/local.d/install-extras.start` becomes dead code.

Cost: ~270 LOC, ~3-5s extra bake time, no new Python deps,
openssl on PATH. Operator-facing recipe doesn't change. Boot
becomes faster and cleaner; firmware/driver early-boot races go
away.
