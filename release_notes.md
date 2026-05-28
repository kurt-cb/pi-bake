# pi-bake release notes

Concise per-version index of what shipped. For full notes per
tag (commit-by-commit detail), see [CHANGELOG.md](CHANGELOG.md).
For the plan + the "why" behind each feature, see
[ROADMAP.md](ROADMAP.md).

Add a new entry here on every `git tag -a vX.Y.Z` — see
[CLAUDE.md](CLAUDE.md#release-flow) for the tag-time
checklist.

## Quick reference

| Version | Date | Headline |
|---|---|---|
| [v0.3.3](#v033--2026-05-27--bugfix) | 2026-05-27 | Bugfix: alpine_pxe + `packages:` crash on Python 3.12 |
| [v0.3.2](#v032--2026-05-27--alpine-pxe-backend) | 2026-05-27 | Alpine PXE backend (`os_mode: pxe`) + ext4 boot fix |
| [v0.3.1](#v031--2026-05-27--alpine-ext4-sys-mode-backend) | 2026-05-27 | Alpine ext4 sys-mode backend (`os_mode: ext4`) |
| [v0.3.0](#v030--2026-05-26--back-out) | 2026-05-26 | Dropped Alpine `edge` (chroot+qemu dead-end revert) |

Older: v0.2.x and earlier in [CHANGELOG.md](CHANGELOG.md).

---

## v0.3.3 — 2026-05-27 — bugfix

Fixed `os_mode: pxe` + `packages:` crash on Alpine 3.22 / Python
3.12 bake hosts (`flush of closed file` ValueError). Manual
`p.stdin.close()` before `p.communicate()` raises on 3.12 but
silently no-ops on 3.14, which masked the bug from local dev.

Docs: `scripts/release-notes.sh` regenerates [CHANGELOG.md](CHANGELOG.md)
from annotated tag messages. `iflag=fullblock` documented in
[README.md](README.md) as the canonical SSH-streamed flash pattern.

## v0.3.2 — 2026-05-27 — Alpine PXE backend

- **`os_mode: pxe`** — Alpine PXE (TFTP+HTTP) boot tree for
  network-boot recovery. Output is a per-host directory the
  operator drops into the lab's TFTP root.
  → [ROADMAP #20](ROADMAP.md#20-alpine-pxe-backend),
    [examples/pi-cm4-alpine-pxe.yaml](examples/pi-cm4-alpine-pxe.yaml),
    [ngnix_setup.md](ngnix_setup.md) for the lab-side nginx prereq.
- **ext4 boot fix** — busybox applet symlinks (incl. `/sbin/init`)
  installed at bake time to compensate for `apk add --no-scripts`
  skipping post-install hooks. Closes a kernel-panic-at-userspace
  failure mode caught on real hardware.
- **PXE tree perms fix** — output tree chmod'd to world-readable
  so dnsmasq-tftp + nginx (running as non-operator users) can
  serve `boot/initramfs-rpi`. Closes a 600-mode initramfs that
  caused TFTP "failed sending" + HTTP 403 + kernel panic.

## v0.3.1 — 2026-05-27 — Alpine ext4 sys-mode backend

- **`os_mode: ext4`** — Alpine on a real partitioned image
  (FAT `/boot` + ext4 `/`). Cross-arch bootstrap via
  apk-tools-static with `--no-scripts` + first-boot `apk fix`
  one-shot service. ONLY mode that supports `os_version: edge`.
  → [ROADMAP #19](ROADMAP.md#19-alpine-ext4-sys-mode-backend),
    [examples/pi-5-alpine-ext4.yaml](examples/pi-5-alpine-ext4.yaml).
- **Container-friendly imgxz** — euid-aware `_sudo` (no sudo
  package needed in containers SSH'd into as root), per-partition
  offset-based loop attach (sidesteps incus cgroup-BPF blocking
  major 259), stderr surfaced in errors, root-aware write helpers.

## v0.3.0 — 2026-05-26 — back-out

Dropped Alpine `edge` OS version + BE200 support. The
chroot+qemu+modloop infrastructure to bake-time-upgrade Alpine's
kernel was a dead-end (~370 LOC, fragile). Lightweight edge
selection re-introduced later (v0.3.1) under `os_mode: ext4`
where `apk upgrade linux-rpi` works normally.
→ [ROADMAP #2](ROADMAP.md#2-alpine-edge-os-version-dead-ended)
  has the post-mortem. Dead-end branch preserved at
  `kurt-cb/edge-mistake`.

---

Older releases (v0.2 and earlier) in [CHANGELOG.md](CHANGELOG.md).
