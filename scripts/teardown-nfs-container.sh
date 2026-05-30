#!/bin/bash
# Tear down the `nfs-pi-bake` incus container. Clean rollback of
# whatever scripts/setup-nfs-container.sh built.

set -euo pipefail

CONTAINER=nfs-pi-bake

if ! incus list "$CONTAINER" -f csv -c n 2>/dev/null | grep -qx "$CONTAINER"; then
  echo "container $CONTAINER not found — nothing to tear down"
  exit 0
fi

echo "stopping $CONTAINER..."
incus stop "$CONTAINER" --force 2>/dev/null || true

echo "deleting $CONTAINER..."
incus delete "$CONTAINER"

echo "done — host ports 8801 + 8802 should now be free"
