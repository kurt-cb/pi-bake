# pi-bake recipe examples

Tested, minimal recipes for common shapes. Each one is a
self-contained YAML you can adapt with `cp` + 30 seconds of
editing.

| File | Shape |
|------|-------|
| [`pi-zero-2-w-wifi-station.yaml`](pi-zero-2-w-wifi-station.yaml) | Pi Zero 2 W joining an existing SSID via DHCP. The smallest useful recipe. |
| [`pi-5-wired-dhcp.yaml`](pi-5-wired-dhcp.yaml) | Pi 5 on wired ethernet via DHCP. Used as the §25 hardware lab's totaldns-server platform. |
| [`pi-5-be200-edge.yaml`](pi-5-be200-edge.yaml) | Pi 5 on Alpine `edge` to unlock the Intel BE200's `iwlwifi` driver (absent from stable 3.21). |
| [`pi-zero-w-armhf.yaml`](pi-zero-w-armhf.yaml) | Original Pi Zero W (32-bit ARMv6 — the only board still needing armhf Alpine). |

For every field in every form, see [`pi-bake.example.yaml`](../pi-bake.example.yaml)
at the repo root.

## Validating without baking

```
pi-bake build --config examples/pi-5-wired-dhcp.yaml --to-yaml /tmp/round-trip.yaml --no-bake
```

Reads the recipe through the strict validator, emits a clean
normalized YAML at `/tmp/round-trip.yaml`, and exits without
producing an `.img.gz`. Surfaces schema errors immediately —
useful when editing.

## Round-tripping CLI invocations to YAML

Operators who currently invoke `pi-bake build` with flags can
capture the same recipe as YAML:

```
pi-bake build \
  --board pi-zero-2-w --os alpine \
  --hostname pi-radio-1 --ssh-pubkey ~/.ssh/id_ed25519.pub \
  --wifi-ssid totaldns-lab --wifi-psk secret \
  --out ~/sdcards/pi-radio-1.img.gz \
  --to-yaml ~/recipes/pi-radio-1.yaml \
  --no-bake
```

Drop the `--no-bake` to also bake the image while saving the
recipe. Once you have the YAML, future bakes are just
`pi-bake build --config ~/recipes/pi-radio-1.yaml`.
