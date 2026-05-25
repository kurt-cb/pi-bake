# pibakehub-pilot

**Scratchpad** for the pibakehub v1 design exercise. Not a
shipped capability — see [`design/pibakehub-v1.md`](../design/pibakehub-v1.md)
for the full design and [`ROADMAP.md`](../ROADMAP.md) (🚧 markers)
for status.

## What's in here

```
pibakehub-pilot/
├── README.md                          # this file
├── waveshare/
│   ├── poe-hat-f/fragment.yaml        # SCRAPED (untested)
│   ├── m2-hat-plus/fragment.yaml      # SCRAPED (untested)
│   ├── wm8960-audio-hat/fragment.yaml # SCRAPED (untested)
│   ├── rs485-can-hat/fragment.yaml    # SCRAPED (untested)
│   ├── fan-hat/fragment.yaml          # SCRAPED (untested)
│   ├── power-management-hat/fragment.yaml  # SCRAPED (untested)
│   └── ai-hat-plus/fragment.yaml      # SCRAPED (untested)
└── intel/
    └── be200/fragment.yaml            # VERIFIED on pi-5 + alpine edge
```

## Why these 7 + 1

Seven Waveshare HATs were scraped from
[waveshare.com/wiki](https://www.waveshare.com/wiki/Main_Page) on
2026-05-25 to stress-test the design schema. Variety covers:

- **No config required**: `poe-hat-f` (pure hardware HAT).
- **PCIe enablement**: `m2-hat-plus`, `ai-hat-plus`.
- **I2C-driven peripherals**: `fan-hat`, `power-management-hat`.
- **SPI + UART overlays**: `rs485-can-hat`.
- **I2S codec + DKMS driver**: `wm8960-audio-hat`.

Plus one **non-Waveshare, operator-verified** fragment:

- `intel/be200` — Wi-Fi 7 M.2 module, verified on the same Pi 5 +
  Alpine edge bake that motivated the v0.2 air-gap apk-fetch work.

The 7 Waveshare fragments carry `provenance:` blocks (scraped
from manufacturer wiki, untested) and NO `verified_on:` entries.
When pi-bake's CLI lands `--pibakehub`, bakes using them will
emit the §6.2 warning. `intel/be200` is the example of what
"verified" looks like.

## How to use the prototype

Doesn't compose into a real bake yet. Drives the prototype
script:

```sh
python3 tools/pibakehub_compose.py \
    --base examples/pi-5-wired-dhcp.yaml \
    --pibakehub waveshare/m2-hat-plus \
    --pibakehub intel/be200 \
    --root pibakehub-pilot/
```

Prints the merged recipe + any warnings/errors. See
[`tools/pibakehub_compose.py`](../tools/pibakehub_compose.py).

## Scraping reproducibility

The HTML pages used as input live in `/tmp/waveshare-scrape/`
during the scrape session; they're NOT checked in (transient).
Each fragment's `provenance.scraped_from` URL is the source of
truth.

To re-scrape:

```sh
# Browser User-Agent required (Waveshare 403's bare curl + WebFetch).
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 \
    (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
curl -A "$UA" https://www.waveshare.com/wiki/<page> -o <slug>.html
```
