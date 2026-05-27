# pi-bake

Generate flashable, headless Raspberry Pi images. Flash one
`.img.gz` per Pi, boot, SSH in. No `setup-alpine` interactive
walk, no `rpi-imager` GUI clicking through pre-fill, no console
on the Pi.

## What gets baked

Per node, from CLI flags or the Python API:

- **Hostname**       → `/etc/hostname`
- **SSH pubkey**     → `/root/.ssh/authorized_keys` (mode 0600) +
                       sshd `PasswordAuthentication no`
- **WiFi creds** (optional) → `/etc/wpa_supplicant/wpa_supplicant.conf`
  so the Pi auto-joins on first boot. Omit for wired-only.
- **Timezone, regulatory country** (sensible defaults)
- **First-boot script** that `apk add`s the small set of packages
  (openssh-server, iproute2, etc.), enables services, then
  self-disables.

That's it. No role-specific code, no totaldns, no platform lock-in.
Once the Pi is on the network, whatever orchestrator you use
(pyinfra, Ansible, plain SSH) takes over.

## Install

```
pip install pi-bake
```

System tools (one-time per dev machine):

```
# Fedora
sudo dnf install mtools dosfstools xz util-linux openssl tar cpio
# Debian / Ubuntu
sudo apt install mtools dosfstools xz-utils util-linux openssl tar cpio
# Alpine
apk add mtools dosfstools xz util-linux openssl tar cpio
```

Tooling by backend:

| Backend | Tools | Root? |
|---------|-------|-------|
| **Alpine** | mtools + dosfstools + tar + cpio + openssl + ssh-keygen | NO |
| **Raspbian / Debian / Fedora** | xz + losetup + mount + partprobe + lsblk + ssh-keygen | **YES** (losetup needs CAP_SYS_ADMIN) |

For the sudo-requiring backends, the typical operator setup is
either:

- **Run pi-bake inside a privileged LXC container** that gives it
  root without prompting your host. LXC setup is outside this
  project's scope (use whatever container/VM workflow you prefer);
  pi-bake just needs to be able to `losetup` + `mount`.
- **Add passwordless sudoers entries** for the specific commands
  pi-bake invokes (`losetup`, `mount`, `umount`, `partprobe`,
  `tee`, `chmod`, `chown`, `mkdir`, `sh -c "cat >>"`). Restrictive
  enough to be safe; broad enough to bake without prompting.

The Alpine baker doesn't need any of this — runs as a regular
user via mtools.

## Quick start

```
# What can we bake for what?
pi-bake list-boards
pi-bake list-os --board pi-zero-2-w

# Bake an Alpine image for a Pi Zero 2 W with WiFi creds.
pi-bake build \
  --board pi-zero-2-w \
  --os alpine \
  --hostname pi-radio-1 \
  --ssh-pubkey ~/.ssh/id_ed25519.pub \
  --wifi-ssid totaldns-lab \
  --wifi-psk secret \
  --out ~/sdcards/pi-radio-1.img.gz

# Flash. Replace mmcblk0 with your SD card's actual device.
# Local SD: zcat works directly.
zcat ~/sdcards/pi-radio-1.img.gz | sudo dd of=/dev/mmcblk0 bs=4M status=progress conv=fsync

# Remote SD via SSH (e.g. flashing a Pi already PXE-booted that
# you'll then swap to SD-boot): use iflag=fullblock so the remote
# dd accumulates SSH-delivered chunks into proper 4 MB writes.
# Without it, dd short-reads per TCP packet, writes 32 KB at a
# time, and the SD card erase-block thrash makes the flash 100x
# slower (520 KB/s vs full speed). Discovered hands-on 2026-05-27.
xzcat ~/sdcards/pi-radio-1.img.xz | \
  ssh root@<remote-pi> 'dd of=/dev/mmcblk0 bs=4M iflag=fullblock conv=fsync'

# Boot the Pi. Wait ~30s. Then:
ssh root@pi-radio-1.lan uptime
```

For wired-only nodes (eth0), omit the WiFi flags:

```
pi-bake build \
  --board pi-5 --os alpine --hostname boat \
  --ssh-pubkey ~/.ssh/id_ed25519.pub \
  --out ~/sdcards/boat.img.gz
```

## YAML recipes (v0.0.5+)

Operators bake the same image from a YAML recipe. Better for
multi-node deployments + version control:

```
pi-bake build --config ~/recipes/pi-5.yaml
```

Already have a working CLI invocation? Round-trip it to YAML:

```
pi-bake build \
  --board pi-5 --os alpine --hostname boat \
  --ssh-pubkey ~/.ssh/id_ed25519.pub \
  --out ~/sdcards/boat.img.gz \
  --to-yaml ~/recipes/boat.yaml \
  --no-bake
```

`--no-bake` skips the actual image build — useful when you only
want to capture the recipe. Drop it to bake AND save the YAML.

References:
- [`pi-bake.example.yaml`](pi-bake.example.yaml) — annotated reference
  for every field, dnsmasq-style. Read once, never go hunting for
  options again.
- [`examples/`](examples/) — minimal, tested recipes for common
  shapes (wifi-station, wired AP, CAN/RS485 HAT).

Validate a recipe without baking:

```
pi-bake build --config recipe.yaml --to-yaml /tmp/normalized.yaml --no-bake
```

This runs the strict validator (unknown keys are an error) and
writes a canonical normalized YAML — surfaces schema errors
instantly while you're editing.

## Supported boards × OSes

| Board          | Alpine | Raspbian | Debian | Fedora |
|----------------|--------|----------|--------|--------|
| Pi Zero W      | ✓ (armhf) | ✗ (32-bit ARMv6 not packaged) | ✗ | ✗ |
| Pi Zero 2 W    | ✓        | ✓ (32-bit ARMv7 / arm64) | ✗ | ✗ |
| Pi 3           | ✓        | ✓        | ✓      | ✗ |
| Pi 4           | ✓        | ✓        | ✓      | 🟡 |
| Pi 5           | ✓ (3.21+) | ✓        | ✗ (raspi.debian.net has no Pi 5 build) | 🟡 |

🟡 = Fedora bake produces a configured Fedora rootfs but the
upstream Fedora ARM image isn't Pi-specific — operator must run
`arm-image-installer --target=rpi4|rpi5` on the output to inject
Pi firmware before flashing. Pi-bootloader-shim is a future
pi-bake feature (see ROADMAP.md).

Run `pi-bake list-os --board <b>` for the current matrix.

## Status

All four backends (Alpine, Raspbian, Debian, Fedora) are
working as of v0.3. Alpine is the no-root, mtools-based path
(easiest to run from any Linux host). Raspbian / Debian / Fedora
use losetup + mount and require sudo (typically via LXC — see
above).

## Python API

The CLI is a thin wrapper around `pi_bake.build()`:

```python
from pi_bake import build, NodeConfig

build(
    board="pi-zero-2-w",
    os_name="alpine",
    version=None,                       # latest known-good
    node=NodeConfig(
        hostname="pi-radio-1",
        ssh_pubkey=open(".../id_ed25519.pub").read(),
        wifi_ssid="totaldns-lab",
        wifi_psk="secret",
    ),
    out_path="~/sdcards/pi-radio-1.img.gz",
)
```

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the numbered backlog + state
table. Highlights:

- **Dynamic OS version discovery.** Pull
  `https://dl-cdn.alpinelinux.org/alpine/` and the rpi downloads
  index instead of the hardcoded `versions` tuples in the catalog.
- **v0.3 — multi-image batch.** `pi-bake build-many topology.json`
  emits an `.img.gz` per node entry in one go.
- **v0.3 — static IP option.** Today we DHCP from first reachable
  iface; static-IP-only deployments need a `--static-v4` flag.
- **v0.3 — encrypted overlay** for the rootfs (LUKS).

## Why does this exist

Three reasons to bake images instead of using
`rpi-imager` pre-fill or hand-running `setup-alpine`:

1. **Per-node config in a script.** Topology files (any shape —
   JSON, YAML, a hand-written shell script) drive `pi-bake build`
   in a loop. Lab grows from 1 Pi to 20 without 20 separate
   keyboard sessions.
2. **Reproducible.** Same inputs → same `.img.gz`. Re-flash a
   replacement SD card identically; clone a node by changing the
   hostname.
3. **Alpine RPi has no `rpi-imager` pre-fill equivalent.** This is
   the only convenient headless flow for the Pi Zero W / 2 W
   family running Alpine.

## License

MIT — see `LICENSE`.
