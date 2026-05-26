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

