# pi-bake — Claude session context

## Project mission

**pi-bake is a robust, "boot the first time" Raspberry Pi image
baker.** One `pi-bake build` invocation produces a flashable
`.img.gz`. Operator dd's it, powers the Pi, and within ~30
seconds the device is on the network and accepting SSH. No
manual `setup-alpine`, no rpi-imager pre-fill, no console
keyboard, no first-boot scripts that need internet and a clock.

Everything pi-bake does has to serve that mission. If a change
makes first-boot LESS reliable (or adds a condition the
operator can fail to meet), it doesn't belong.

## Project boundary — DO NOT conflate with downstream projects

pi-bake is downstream-agnostic. It bakes images for **any**
project that wants a flash-and-boot Pi (totaldns is one
consumer; not the only one). Common temptation when working in
parallel with a downstream project (e.g. totaldns hardware-lab
work): adding downstream-specific features to pi-bake because
"the lab needs it." **Don't.** Two failures from doing this:

- **2026-05-25: `pybake-pristine` + `fixit.sh` on FAT.** I tried
  to add an operator-specific recovery layer (the totaldns
  operator's `pybake-pristine` apkovl + `fixit.sh` console
  recovery script) to pi-bake's bake. Operator pushed back:
  "fixit.sh and pristine are for my use, not in pi-bake unless
  it is a generalized solution." Reverted. Recovery layer
  belongs in totaldns's deploy roles (`safe_reboot.py`) until
  pi-bake gets a designed-from-scratch general recovery feature.

If a downstream project needs a pi-bake feature, capture it in
**`feature_request.md`** at the repo root with enough context
that pi-bake can address it in its own focused session. Don't
just bolt it on.

## Current state (as of v0.2)

### What ships

- **Alpine baker** — fully working, no-root, mtools-based.
- **Raspbian / Debian backends** — stubbed with clear error
  pointing at the v0.2 roadmap.
- **YAML recipes** — `pi-bake build --config <yaml>`
  declarative input + `--to-yaml` to round-trip CLI flags into
  a normalized recipe + `--no-bake` to capture without baking.
  Strict load: unknown keys raise.
- **Alpine `edge` OS version** — for hardware that needs newer
  drivers than stable ships (e.g. Intel BE200 iwlwifi missing
  from stable 3.21's linux-rpi-6.12.13 modloop; present in
  edge's 6.12.85). Bakes use latest stable RPi tarball for
  bootloader/FAT layout + write `edge` repos into
  /etc/apk/repositories.
- **Pi Zero W BCM43438 power-save fix** — auto-baked
  `/etc/local.d/wlan-power-save-off.start` when wifi is on.
- **Bake-time apk-fetch + init-time install (v0.2 + #3)** —
  Operator declares `packages:` in their recipe; pi-bake pulls
  every entry + recursive deps from upstream Alpine at BAKE
  time via apk-tools-static (auto-downloaded to
  `~/.cache/pi-bake/`), drops the .apks into FAT at
  `/apks/<arch>/` (flat layout alongside the stock cache),
  regenerates `APKINDEX.tar.gz` and signs it with a fresh
  per-bake RSA-2048 key (RSA-SHA256), and bakes the matching
  pubkey into the apkovl at `/etc/apk/keys/pi-bake-<hex>.rsa.pub`.
  Operator extras land in `/etc/apk/world` so init installs them
  AT INIT TIME alongside the baseline — one transaction, no
  late-boot `local.d` script. By the time sshd is reachable,
  the Pi is fully provisioned. See `alpine.py` + `apkfetch.py`
  + `design/#3_study.md`.

  Always-on whenever `packages:` is non-empty. The `apk_fetch:`
  YAML field is a DEPRECATED no-op (silently accepted for old
  recipes); the `--apk-fetch` CLI flag is gone.
- **Pre-baked SSH host keys (v0.2)** — `ssh_host_key: <path>`
  in YAML / `--ssh-host-key PATH` on CLI bakes the operator's
  ed25519/rsa/ecdsa pair (private from PATH, public from
  `<path>.pub`) into `/etc/ssh/ssh_host_<type>_key{,.pub}` so
  the Pi's SSH identity stays stable across rebuilds — no
  `known_hosts` churn. When unset, pi-bake auto-generates a
  fresh ed25519 pair at bake time (stable across reflashes of
  the same `.img.gz`, changes per `pi-bake build`).
- **Annotated reference** at `pi-bake.example.yaml` (every
  field documented; dnsmasq-style).
- **Tested examples** under `examples/` for the common shapes.

### Known constraints

- **`--pibakehub` not yet wired into the CLI** — pibakehub
  fragments exist in `pibakehub-pilot/` and a composition
  prototype lives at `tools/pibakehub_compose.py`, but the
  build path doesn't consume them yet. ROADMAP item #7.
- **No Raspbian/Fedora/Debian backend** — Alpine only at the
  moment. ROADMAP items #9/#10/#11.

## Critical lessons from real-hardware deployment

Each one cost real debug time. Don't relearn them.

### 1. `apk add --no-network` fails WHOLESALE on missing packages

**The lesson:** Alpine RPi's init runs

    apk add --root $sysroot --no-network $world

reading `/etc/apk/world`. With `--no-network`, apk uses ONLY
the local `/apks/<arch>/` cache. The stock RPi tarball /apks
cache ships ~150 baseline packages (sshd, dhcpcd, chrony,
wpa_supplicant, etc.) but does NOT ship avahi, dbus, py3-pydbus,
linux-firmware-intel, etc.

**If ANY package in /etc/apk/world isn't in the cache, the
ENTIRE transaction fails — including dhcpcd.** Result: no DHCP,
no sshd, 169.x APIPA, unreachable device.

**Pi-bake's solution (since #3):**

- Operator extras + recursive deps are fetched at BAKE time
  into `/apks/<arch>/` (flat, alongside stock cache).
- `APKINDEX.tar.gz` is regenerated to list everything + signed
  with a per-bake RSA-SHA256 key.
- The matching pubkey lands in the apkovl's `/etc/apk/keys/`.
- ALL packages (baseline + extras) go into `/etc/apk/world` —
  init's `apk add --no-network` installs everything in one
  transaction at INIT TIME.

By the time sshd starts, the Pi is fully provisioned — no
late-boot `local.d` script, no two-paths design. The
wholesale-fail rule is satisfied because every world entry is
in the cache AND in the (signed) index.
- Self-disables on success via `/var/lib/pi-bake/install-extras.done`.

Trade-off: extras still need network on first boot. v0.3
bake-time apk-fetch (populating /apks at bake time) is what
makes the device truly air-gappable.

**Regression tests:** `test_extra_packages_NOT_in_world`,
`test_install_extras_script_runs_apk_add` in `tests/test_apkovl.py`.

### 2. `/etc/.default_boot_services` marker — modloop cascade

**The lesson:** Alpine RPi's /init has

    if [ -f "$sysroot/etc/.default_boot_services" -o ! -f "$ovl" ]; then
        rc_add modloop sysinit
        rc_add modules boot
        ...

When an apkovl IS present (always true for us), the marker MUST
also be present, or the modloop service never runs →
`/lib/modules` stays empty → `af_packet` never loads → every
DHCP client dies with "Address family not supported by protocol".

**Pi-bake bakes the empty `etc/.default_boot_services` marker
always.** Don't remove this.

### 3. Alpine openssh is built WITHOUT PAM

**The lesson:** `UsePAM yes` in `/etc/ssh/sshd_config` makes
Alpine's sshd refuse to start with "Bad configuration option".
Same for the deprecated `ChallengeResponseAuthentication`
directive in OpenSSH 8.7+.

**Pi-bake's sshd_config:** no `UsePAM`, uses
`KbdInteractiveAuthentication no` (the modern spelling).

**Regression test:** `test_sshd_config_omits_unsupported_directives`.

### 4. dhcpcd doesn't send DHCP option 12 by default

**The lesson:** stock `/etc/dhcpcd.conf` has `hostname`
commented out. Without it, the upstream DHCP server / totaldns
sees `req_name=None` and synthesizes a placeholder (`unknown-<6mac>`)
instead of the operator-chosen hostname.

**Pi-bake bakes its own `/etc/dhcpcd.conf` with the bare
`hostname` directive** (uses /etc/hostname at request time).
The `--no-dhcp-hostname` flag intentionally bakes a "broken"
test fixture for exercising DHCP-server-side hostname recovery.

### 5. `openssh-server` package doesn't ship `sftp-server`

**The lesson:** modern scp (OpenSSH 9.0+) defaults to the SFTP
protocol. `openssh-server` alone has no sftp-server binary → scp
fails with "subsystem request failed", pyinfra `files.put` fails.

**Pi-bake adds `openssh-sftp-server` to baseline /etc/apk/world.**

### 6. `lbu` no-ops without `LBU_MEDIA`

**The lesson:** stock `/etc/lbu/lbu.conf` ships with `LBU_MEDIA`
commented out. Without it (and without an explicit
`lbu commit mmcblk0` arg), `lbu commit` and even `lbu status`
just print usage.

**Pi-bake bakes `LBU_MEDIA="mmcblk0"` + `BACKUP_LIMIT=3`
(rotation; up to 3 apkovl backups before dropping oldest).**

### 7. `dhcpcd` over `udhcpc` on Pi 5

**The lesson:** udhcpc 1.37 + Alpine 3.21 + Pi 5's macb driver
hangs silently with "Address family not supported by protocol".
dhcpcd 10.x in the stock cache works reliably.

**Pi-bake uses dhcpcd. Don't switch to udhcpc.**

### 8. Pi Zero W BCM43438 aggressive power-save

**The lesson:** BCM43438 keeps L2 associated while dropping
ARP/L3 traffic when its idle PS kicks in. Looks "up" but
unreachable from peers.

**Pi-bake (v0.0.8+) writes `/etc/local.d/wlan-power-save-off.start`
when wifi is on.** Harmless no-op on radios that handle PS
properly. Always-applied when wifi is enabled in the recipe.

### 9. Backup apkovls must not match `*.apkovl.tar.gz` glob

**The lesson:** the RPi bootloader globs `*.apkovl.tar.gz` on
FAT root. A backup at `td-pi5-1.apkovl.tar.gz.bak` was still
glob-matched (or close enough to confuse the loader) and
bricked boot. The user had to power-cycle + manually rename
the backup file.

**Pi-bake uses `BACKUP_LIMIT=3` for lbu's native rotation — backups
get named `<host>.apkovl.tar.gz.0` / `.1` / `.2`, which the
bootloader glob ignores.** Don't ever name a file ending in
`.apkovl.tar.gz` (even with extra suffix) on FAT.

## Architecture (small + boring on purpose)

```
src/pi_bake/
├── __init__.py     # public API exports
├── boards.py       # Pi board catalog (Board dataclass + BOARDS tuple)
├── oses.py         # OS catalog (OSImage dataclass + ALPINE/RASPBIAN/DEBIAN)
│                   # resolve_image() does URL templating + edge special-case
├── config.py       # NodeConfig — per-Pi inputs that go into the bake
├── recipe.py       # YAML recipe (Recipe dataclass + load/dump/round-trip)
├── alpine.py       # Alpine baker — _write_apkovl + bake() + mtools helpers
├── apkfetch.py     # Bake-time apk-fetch (air-gap) — apk-tools-static
│                   # download + cross-arch fetch + initramfs key extraction
├── raspbian.py     # Raspbian/Debian baker (stub for v0.2)
├── bake.py         # Top-level dispatch — picks backend by OS
├── download.py     # URL fetch + cache + sha256 verify
└── cli.py          # argparse + subcommands (list-boards/list-os/build)

tests/
├── test_apkovl.py   # apkovl tarball shape (apkovl tests go here)
├── test_apkfetch.py # apk-tools-static download + initramfs key
│                    # extract + cross-arch fetch (integration tests
│                    # gated behind PI_BAKE_INTEGRATION=1)
├── test_catalogs.py
├── test_config.py
└── test_recipe.py   # YAML round-trip + schema validation
```

**Hot file is `alpine.py`.** Most lessons above land changes
here. `apkfetch.py` is the second hot file (v0.2+).

**Design constraint:** stdlib + PyYAML at the Python level.
System deps: `mtools` + `dosfstools` always; `tar` + `cpio`
when `apk_fetch: true`; `ssh-keygen` for SSH host key auto-gen.
No root needed for Alpine baker. Raspbian backend (v0.2) will
need sudo for losetup.

## Testing

```
cd ~/pi-bake
python3 -m pytest -q              # ~85 unit tests, <1s
PI_BAKE_INTEGRATION=1 pytest -q   # +network/upstream apk fetch
```

Every "lesson" above has a regression test in `tests/test_apkovl.py`
or `tests/test_recipe.py`. **Always add a test when you fix a
real-hardware bug** — re-relearning is expensive.

## How to verify a behavior change touches the right thing

When making a non-trivial change to the apkovl:

1. Bake against a real example:
   ```
   pi-bake build --config examples/pi-5-be200-edge.yaml
   ```
2. Extract the apkovl + inspect:
   ```
   cd /tmp && zcat ~/sdcards/td-pi5-1.img.gz > x.img
   mcopy -i x.img ::/td-pi5-1.apkovl.tar.gz .
   tar -tzf td-pi5-1.apkovl.tar.gz
   tar -xzOf td-pi5-1.apkovl.tar.gz etc/apk/world
   tar -xzOf td-pi5-1.apkovl.tar.gz etc/local.d/install-extras.start
   ```
3. Reason about the boot flow:
   - apkovl extracts to sysroot
   - init's `cp -a /etc/apk/keys $sysroot/etc/apk` (line ~936 of
     `boot/initramfs-rpi/init`) merges Alpine devel pubkeys with
     anything the apkovl provided at `etc/apk/keys/`
   - init's `apk add --root $sysroot --no-network $world` — must
     succeed; world is baseline-only by design (see lesson 1)
   - runlevels start: networking → sshd → chronyd → dhcpcd (→ local)
   - `local` runs `/etc/local.d/*.start`:
     - `install-extras.start` — online apk add (default) OR offline
       `apk add --no-network --allow-untrusted` against bake-staged
       .apks (when `apk_fetch: true`)
     - `wlan-power-save-off.start` (wifi only)
   - sshd reachable + dhcpcd has lease

If any step would fail, the apkovl is wrong.

4. For apk_fetch bakes, verify the FAT staging too:
   ```
   mdir -i x.img ::/apks/aarch64/extras 2>&1 | head -20
   ```
   Operator extras (+ recursive deps) should all be there.

## Release flow

- Version is dynamic from latest git tag via setuptools_scm.
- `git tag v0.0.N && git push --tags` triggers
  `.github/workflows/workflow.yml` which builds + publishes via
  PyPI Trusted Publishing (OIDC, no secrets).
- Commits are atomic + descriptive — see existing log for style.

## Commits to read for context

- `599ad07` — original fix for DHCP / lbu / sftp / PAM (the
  half-dozen first-boot bugs).
- `936f233` — switch to dhcpcd from udhcpc.
- `e42596c` (v0.0.8) — Alpine edge + YAML recipe + power-save off.
- `c4ac2c1` (v0.0.9) — apk-add wholesale failure fix.

## See also

- [README.md](README.md) — operator-facing docs.
- [ROADMAP.md](ROADMAP.md) — landed + planned.
- [pi-bake.example.yaml](pi-bake.example.yaml) — annotated reference.
- [feature_request.md](feature_request.md) — incoming feature
  requests from downstream projects, ready for a focused
  pi-bake session.
