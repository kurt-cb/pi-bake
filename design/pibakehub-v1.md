# pibakehub v1 — frozen design

## §0  Design status, numbering, and tracking

**This document is the FROZEN design for v1.** Once approved,
sections below are immutable. Changes do not happen here — they
happen by adding a new numbered section at the end, or by
superseding an existing one in a new design revision document
(`pibakehub-v2.md`, etc.).

**Numbering scheme** (stable forever — used as `§X.Y.Z` references
in code comments, commit messages, the features-todo doc, and the
features-complete doc):

- `§N` — top-level chapters (this section is §0).
- `§N.M` — subsystems within a chapter.
- `§N.M.K` — named features within a subsystem.
- `§N.M.K.J` — detail items, when needed to be individually
  traceable.

Every section heading in this document is prefixed with its
stable reference. **Never renumber.** If a section is dropped,
leave its reference reserved and write
"RESERVED — superseded by §X.Y" in its place.

**Status as of 2026-05-25:** DRAFT. Not yet approved. The pilot
under `pibakehub-pilot/` is a scratchpad informing the design,
not a shipped capability. The roadmap entry in `ROADMAP.md` is
marked 🚧 until §0 reaches APPROVED.

---

## §1  Project mission

### §1.1  What pibakehub IS

A **community-curated registry of pi-bake recipe fragments** —
one fragment per Raspberry Pi HAT, USB radio, M.2 card, display,
sensor, or other expansion. Each fragment encodes the
hardware-specific bits an operator would otherwise have to dig
out of a vendor wiki: `config.txt` overlays, kernel modules,
required firmware/driver packages, sysfs quirks.

Operators compose multiple fragments into one bake:

```
pi-bake build --config base.yaml \
              --pibakehub waveshare/poe-m2-hat-b \
              --pibakehub intel/be200
```

…and get a `.img.gz` that boots and works with that exact
hardware stack — no manual `dtoverlay=` hunting.

### §1.2  What pibakehub ISN'T

- **Not a HAL.** Fragments are STATIC configuration: bake-time
  edits to FAT files (`/uboot/usercfg.txt`), apkovl files
  (`/etc/apk/world`, `/etc/modules`). There is no runtime HAT
  detection, no probe, no "plug a HAT in and it autoconfigures."
  The operator declares what hardware they have; pibakehub
  encodes how to configure for it.
- **Not a package manager.** Fragments reference apk packages
  (Alpine) but pi-bake's existing apk-fetch (`§3.4` v0.2 feature)
  does the actual install. Fragments don't ship binaries.
- **Not a substitute for the manufacturer wiki.** Fragments link
  back to the source-of-truth manufacturer reference. When the
  hardware is new or obscure, that link IS the documentation;
  pibakehub just makes the config copy-pasta turnkey.
- **Not pi-bake itself.** pibakehub is a separate registry repo
  (planned: `github.com/pi-bake/recipes`). pi-bake clones /
  caches it locally and composes fragments at bake time. Pi-bake
  ships without any fragments bundled (the local cache starts
  empty until the operator runs `pi-bake pibakehub update`).

### §1.3  Design boundary

**A fragment is downstream-agnostic.** It describes hardware
config, not application config. A "waveshare/poe-m2-hat-b"
fragment turns on PCIe and 32-bit DMA. It does NOT install
totaldns. If a downstream project needs HAT + app combined,
they compose them: their own base recipe + the relevant
pibakehub fragments.

This is the same boundary pi-bake itself follows (see
`CLAUDE.md` "Project boundary — DO NOT conflate with downstream
projects").

---

## §2  Registry repository shape

### §2.1  Layout

Eventually: a separate git repo at `github.com/pi-bake/recipes`
with the following tree:

```
recipes/
├── index.json              # top-level catalog (auto-generated)
├── schema/
│   └── fragment.v1.json    # JSON Schema for fragment YAML
├── waveshare/
│   ├── poe-m2-hat-b/
│   │   ├── fragment.yaml
│   │   └── README.md
│   ├── poe-hat-f/
│   │   ├── fragment.yaml
│   │   └── README.md
│   └── …
├── intel/
│   └── be200/
│       ├── fragment.yaml
│       └── README.md
├── adafruit/
│   └── …
└── pibake/                 # pibakehub-internal fragments (e.g.
    └── …                   # "alpine-edge-iwlwifi-loader" pseudo-HATs)
```

### §2.2  Why git

- Diff-able review of new contributions.
- Built-in versioning + history (operators can pin a fragment
  to a commit SHA for reproducibility).
- Trivial mirror / clone for air-gap operators (see §8).
- Standard PR-based contribution flow (see §9).

### §2.3  index.json

Auto-generated from the fragment tree. Shape:

```json
{
  "generated": "2026-05-25T12:00:00Z",
  "schema_version": 1,
  "fragments": [
    {
      "slug": "waveshare/poe-m2-hat-b",
      "display_name": "Waveshare PoE M.2 HAT (B-Key)",
      "manufacturer_url": "https://www.waveshare.com/wiki/PoE_M.2_HAT_(B)",
      "verified_count": 1,
      "tags": ["poe", "pcie", "m2", "pi-5"]
    },
    …
  ]
}
```

`pi-bake pibakehub list` reads this for fast search without
needing to walk every fragment file.

### §2.4  Pilot living elsewhere (transitional)

The v0.2 pilot lives at `pi-bake/pibakehub-pilot/` inside the
pi-bake repo itself — same tree shape as §2.1 minus
`index.json` (the pilot is too small to need an index). When
the design freezes and the real registry repo lands, the pilot
gets either migrated or archived.

---

## §3  Fragment YAML schema

### §3.1  Required fields

```yaml
schema_version: 1
vendor: waveshare                       # kebab-case manufacturer slug
slug: poe-m2-hat-b                      # kebab-case product slug
display_name: "Waveshare PoE M.2 HAT (B-Key)"
manufacturer_url: https://www.waveshare.com/wiki/PoE_M.2_HAT_(B)
description: |
  Short markdown blurb of what this hardware does.
```

`vendor` + `slug` together form the fragment's globally-unique
reference: `waveshare/poe-m2-hat-b`. That's what operators type
on the CLI (`--pibakehub waveshare/poe-m2-hat-b`).

### §3.2  Composition fields (the "what to bake" payload)

All optional; a fragment that only adds a link to the
manufacturer wiki (no config) is valid.

```yaml
# Hardware compatibility constraints.
requires:
  oneof_boards: [pi-4, pi-5]            # error if base.board not in list
  os: alpine                            # error if base.os doesn't match
  os_version_min: edge                  # error if base.os_version < this
                                        # (see §3.2.1 for ordering)
  conflicts_with:                       # other --pibakehub slugs that
    - waveshare/poe-hat-f               # can't coexist with this one

# /uboot/usercfg.txt edits — appended (deduplicated by line).
config_txt:
  - dtparam=pciex1
  - dtoverlay=pcie-32bit-dma

# Kernel modules to ensure loaded at boot — appended to /etc/modules
# in the apkovl, deduplicated.
modules:
  - iwlwifi

# apk packages — appended to recipe.packages, deduplicated.
# Composed with the v0.2 apk_fetch path (§3.4 of pi-bake design):
# if base recipe has `apk_fetch: true`, these get bake-time
# fetched + offline-installed.
packages:
  - linux-firmware-intel

# Drop-in /etc/local.d/<name>.start scripts. Keyed by basename
# so multiple fragments adding the same name conflict (and
# pi-bake errors with a clear message).
local_d_scripts:
  - name: enable-pcie-monitor.start
    mode: 0o755
    content: |
      #!/bin/sh
      echo 1 > /sys/module/pcie/parameters/aspm
```

#### §3.2.1  os_version ordering

Used by `requires.os_version_min`. Defined for Alpine as:

```
3.19 < 3.20 < 3.21 < edge
```

Point releases compare numerically. `edge` is the upper bound
(rolling). A fragment can require `os_version_min: edge` if it
needs an edge-only kernel feature; bakes with a stable
`os_version` fail-fast with a clear error.

### §3.3  Provenance (REQUIRED for scraped-only fragments)

When a fragment is auto-generated from a manufacturer wiki and
nobody has manually verified it on hardware:

```yaml
provenance:
  scraped_from: https://www.waveshare.com/wiki/PoE_M.2_HAT_(B)
  scraped_date: 2026-05-25
  scraper_version: pibakehub-pilot-0.1
  notes: |
    Extracted from the "Working with Raspberry Pi" section of the
    manufacturer wiki. NOT verified on physical hardware.
```

The `provenance` block makes scraped-only origin auditable. When
the first operator verifies the fragment on real hardware (§5),
a `verified_on:` entry is added; `provenance` stays as
attribution. A fragment can have both: scraped origin AND
operator-verified records.

### §3.4  Optional documentation

```yaml
caveats: |
  Multi-line markdown. Things the operator needs to know:
  - Cooling: PCIe usage warms the Pi 5 SoC; passive heatsink
    insufficient under load.
  - Power: PoE+ (802.3at) required for PCIe at full speed.
sees_also:
  - https://blog.example.org/be200-on-pi-5
  - waveshare/poe-hat-f                 # related fragment
tags:                                   # searchable in `pi-bake pibakehub list --tag`
  - poe
  - pcie
  - m2
  - pi-5
```

---

## §4  Composition semantics

### §4.1  Multi-fragment composition

```
pi-bake build --config base.yaml \
              --pibakehub waveshare/poe-m2-hat-b \
              --pibakehub intel/be200
```

Composition order is deterministic: **CLI argument order**.
Fragments compose left-to-right; the operator's base recipe is
the leftmost (lowest-priority) input.

### §4.2  Merge rules (per fragment field)

| Field             | Rule                                                                          |
|-------------------|-------------------------------------------------------------------------------|
| `packages`        | Append to base; deduplicated.                                                 |
| `modules`         | Append to base; deduplicated.                                                 |
| `config_txt`      | Append to base; deduplicated by EXACT line match.                             |
| `local_d_scripts` | Append; conflict on duplicate `name:` is a HARD ERROR.                        |
| `requires.*`      | Each check fires against the base+composed state; first failure aborts bake.  |
| Everything else   | Fragment-local metadata; not merged into anything.                            |

### §4.3  Read-only operator fields

Fragments **never** touch:

- `hostname`
- `ssh_pubkey` / `extra_pubkeys` / `ssh_host_key`
- `network.*`
- `wifi.*`
- `output.*`
- `apk_fetch`
- `os` / `os_version` / `board` / `timezone`

These are operator concerns. A fragment that wants to require a
specific `os` or `board` does it through `requires.*`, which is
a check, not a mutation.

### §4.4  Conflict detection

Two fragments composing the same bake can clash:

- Both declare a `local_d_scripts` entry with the same `name:`.
  → HARD ERROR at compose time.
- One declares `requires.conflicts_with: [other/fragment]` AND
  the other fragment is on the CLI.
  → HARD ERROR.
- Both append the SAME `config_txt` line.
  → SILENT DEDUP (the same line means the same thing).
- One sets a kernel boot parameter; another sets a contradictory
  one (e.g. `dtoverlay=disable-bt` + `dtoverlay=miniuart-bt`).
  → NOT detected automatically. v1 trusts the operator + fragment
  authors. v2 may add a `config_txt_conflicts` registry.

### §4.5  Error reporting

When composition fails, the error message lists:

1. Which fragment introduced the failing constraint.
2. What the base recipe currently provides.
3. What the fragment expects.

Example:

```
pi-bake build failed: composition error.

Fragment 'intel/be200' (loaded position 2/2) requires:
    requires.os_version_min: edge

Base recipe provides:
    os: alpine
    os_version: 3.21.4

intel/be200's iwlwifi support needs Alpine edge's linux-rpi
(6.12.85+); stable 3.21's linux-rpi-6.12.13 strips wireless/intel/
from the modloop. Either:
  - Change base recipe: os_version: edge
  - Remove --pibakehub intel/be200
```

---

## §5  Verified-on records

### §5.1  Schema

```yaml
verified_on:
  - board: pi-5
    os: alpine
    os_version: edge                    # exact match preferred
    components_present:                 # other --pibakehub slugs in the bake
      - intel/be200
    components_absent: []               # explicit "this wasn't there"
    verified_by: kurt-cb                # GitHub-ish handle
    verified_date: 2026-05-25
    image_sha256: 1a2b3c4d…             # OPTIONAL: sha256 of the .img.gz
                                        # that was tested
    notes: |
      iwlwifi attaches successfully, BE200 enumerates on PCIe.
      Modloop from linux-rpi-6.12.85. mDNS resolution works.
```

Multiple records allowed — one per verified hardware/OS combo.

### §5.2  Matching rules

When an operator runs:
```
pi-bake build --config base.yaml --pibakehub waveshare/poe-m2-hat-b
```

…pi-bake checks the fragment's `verified_on` list for an entry
where:

1. `board` == base.board
2. `os` == base.os
3. `os_version` == base.os_version
4. `components_present` ⊆ {other --pibakehub slugs the operator
   passed}
5. `components_absent` ∩ {other --pibakehub slugs} == ∅

If a match exists → operator gets a green "✓ verified" message
at bake time. If no match → yellow "⚠ untested for this exact
config" warning (see §6).

### §5.3  Provenance of "verified_by"

Honor system in v1. The verified_by handle is informational;
pibakehub doesn't try to authenticate it. CI on the registry repo
can run a hardware-in-the-loop smoke test in a future version (a
Pi rack hosted by the project), but for v1 the source-of-truth is
the human who landed the PR.

### §5.4  Demoting verified records

If a kernel/firmware update breaks a previously-verified config,
the existing `verified_on` entry is **never edited or deleted**.
Instead, a new entry is added:

```yaml
verified_on:
  - board: pi-5
    os: alpine
    os_version: edge
    verified_by: kurt-cb
    verified_date: 2026-05-25
    notes: working with linux-rpi-6.12.85
  - board: pi-5
    os: alpine
    os_version: edge
    verified_date: 2026-08-12
    verified_by: kurt-cb
    status: regressed                   # NEW field; default "ok"
    notes: |
      linux-rpi-6.12.110 breaks iwlwifi attachment.
      Pin os_version: 3.21.6 + manual upgrade-hold for now.
```

This preserves history. `pi-bake pibakehub info <slug>` shows
both, in date order, so operators see the regression timeline.

---

## §6  Scraped-untested provenance + warnings

### §6.1  Scraper output

The pilot scraper (and any future registry-side scraper) emits
fragments with:

1. A `provenance:` block (§3.3) recording WHERE the data came
   from and WHEN.
2. NO `verified_on:` entry. The fragment is "scraped, not
   verified."
3. An auto-generated `caveats:` line: "Auto-generated from
   <URL> on <date>; not verified on hardware. Please verify and
   contribute back."

### §6.2  pi-bake warning behavior

When the operator bakes with a fragment that has NO matching
`verified_on` entry (per §5.2):

```
⚠ pibakehub warning:
  waveshare/poe-m2-hat-b has no verified record for:
    board=pi-5  os=alpine  os_version=edge
    components_present=[]
  This fragment was auto-scraped from:
    https://www.waveshare.com/wiki/PoE_M.2_HAT_(B)  (2026-05-25)
  Verify the bake works, then send a PR:
    https://github.com/pi-bake/recipes/issues/new?template=verified.yaml
```

The warning does NOT stop the bake. It just gives the operator
the context they need to decide "should I trust this?"

### §6.3  Strict mode (opt-in)

```
pi-bake build … --pibakehub-strict
```

Promotes the warning to a hard error. Useful for CI / appliance
builds where shipping with an unverified fragment is unacceptable.

---

## §7  CLI integration

### §7.1  Build-time

```
pi-bake build --config base.yaml \
              --pibakehub <vendor>/<slug>     # repeatable
              [--pibakehub-strict]            # see §6.3
              [--pibakehub-ref <git-ref>]     # pin registry to a SHA/tag
```

`--pibakehub-ref` defaults to whatever the local cache has (see
§8). Operators reproducing an old bake set it explicitly.

### §7.2  Registry inspection subcommands

```
pi-bake pibakehub list                    # list all fragments
pi-bake pibakehub list --tag poe          # filter by tag
pi-bake pibakehub list --board pi-5       # filter by required board
pi-bake pibakehub list --verified         # only fragments with at
                                          # least one verified_on record
pi-bake pibakehub info <vendor>/<slug>    # detailed view of one fragment,
                                          # including all verified_on
                                          # records + provenance + caveats
pi-bake pibakehub update                  # git pull / refresh local
                                          # cache from registry remote
pi-bake pibakehub diff <vendor>/<slug>    # show what this fragment would
                                          # add to a hypothetical bake
                                          # (config.txt lines, packages, etc.)
```

### §7.3  YAML recipe integration

A base recipe can also declare fragments inline (so the bake is
fully reproducible from one YAML file):

```yaml
# my-pi.yaml
hostname: my-pi
board: pi-5
os: alpine
os_version: edge
ssh_pubkey: ~/.ssh/id_ed25519.pub
network:
  mode: dhcp

pibakehub:                              # NEW top-level key
  ref: "v2026.05.25"                    # OPTIONAL: pin registry ref
  fragments:
    - waveshare/poe-m2-hat-b
    - intel/be200

output:
  path: ~/sdcards/my-pi.img.gz
```

CLI flags merge with YAML — `--pibakehub` adds to whatever the
YAML lists.

---

## §8  Local cache + air-gap

### §8.1  Cache location

```
~/.cache/pi-bake/pibakehub/
├── recipes/                  # git clone of the registry repo
└── HEAD.ref                  # the git ref currently checked out
```

### §8.2  Refresh

```
pi-bake pibakehub update [--ref <git-ref>]
```

Does `git clone` on first use, `git fetch + checkout` thereafter.
Without `--ref`, defaults to `origin/main`. With `--ref`,
detaches HEAD on the requested commit/tag.

### §8.3  Air-gap workflow

On an internet-connected machine:

```
pi-bake pibakehub update              # populates ~/.cache/pi-bake/pibakehub/
rsync -a ~/.cache/pi-bake/pibakehub/ <air-gap-machine>:~/.cache/pi-bake/pibakehub/
```

On the air-gapped machine, bakes work without further network
access. Compose this with `apk_fetch: true` (`§3.4` of pi-bake
design, v0.2) and the entire bake-to-flash flow is reproducible
offline.

### §8.4  Pinning for reproducibility

```yaml
pibakehub:
  ref: "v2026.05.25"                  # tag in the registry repo
  fragments:
    - waveshare/poe-m2-hat-b
```

Combined with `apk_fetch: true`, two bakes from the same YAML +
same source archive produce byte-identical images (modulo SSH
host key auto-gen — set `ssh_host_key:` to remove that variable).

---

## §9  Submission flow

### §9.1  Adding a new fragment

Operator gets a HAT working on their Pi:

1. Write `recipes/<vendor>/<slug>/fragment.yaml` (using §3 schema).
2. Add a `verified_on:` entry for the exact bake they tested.
3. PR against `github.com/pi-bake/recipes`.

CI on the PR runs:

- JSON Schema validation against `schema/fragment.v1.json`.
- Lint (vendor slug exists, manufacturer_url reachable, etc.).
- Future: qemu-system-aarch64 boot smoke (parses config.txt,
  doesn't kernel-panic on init).

### §9.2  Adding a `verified_on` to an existing fragment

Operator successfully bakes someone else's fragment on their own
hardware combo:

1. Edit `recipes/<vendor>/<slug>/fragment.yaml`.
2. Append a `verified_on:` entry.
3. PR.

CI runs the same checks plus a sanity check that the new
verified_on entry is well-formed.

### §9.3  Reporting regressions

When a previously-verified config breaks (kernel update, firmware
shift), operator follows §5.4: append a new `verified_on` entry
with `status: regressed`. Maintainers may tag a registry release
that excludes the regressed combos from `pi-bake pibakehub list
--verified` output.

### §9.4  Vendor-author fragments

Hardware vendors are welcome to author + maintain their own
fragments. The submission flow is identical (PR), but vendor
submissions can be tagged with a `vendor_authored: true` flag in
the fragment YAML so pi-bake can surface that ("✓ vendor-
authored fragment") in `pibakehub info`.

---

## §10  Pilot status

### §10.1  v0.2 pilot scope

`pibakehub-pilot/` in the pi-bake repo, populated by scraping
5–7 Waveshare HAT wiki pages and one verified fragment for the
BE200 + Pi 5 + Alpine edge combo (the bake setup that motivated
this whole exercise).

The pilot uses §3's schema but lives in the pi-bake repo (not
the planned separate registry repo). When v1 ships, the pilot
either migrates upstream or gets archived.

### §10.2  Pilot prototype script

`tools/pibakehub_compose.py` — small Python tool that:

1. Loads a base YAML recipe.
2. Loads N fragment YAMLs by slug.
3. Validates schema (§3) on each.
4. Composes per §4 rules.
5. Prints the merged recipe.

Not wired into `pi-bake build` yet — that's v0.3+. The script
proves the design works on real scraped data.

### §10.3  Lessons feed back

Anything the pilot surfaces (schema gaps, awkward composition
cases, ambiguous scraped content) goes into §11+ as new sections.
The §0–§9 contract above does NOT change in response to pilot
findings — those become v2 if they reshape the schema.

---

## §11  RESERVED (future appendix slot)

Use for design clarifications, alternate-considered approaches,
or addenda that fall out of pilot work without invalidating §0–§9.
