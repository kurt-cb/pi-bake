#!/bin/bash
# Stand up the `nfs-pi-bake` incus container — a userspace NFS-v3
# server appliance for Raspbian PXE bakes (ROADMAP #27).
#
# Reproduces the container infrastructure from scratch:
#   - unprivileged Alpine container (no CAP_SYS_ADMIN needed —
#     uses userspace unfs3 instead of kernel-NFS)
#   - unfs3 + rpcbind installed
#   - NFS-v3 export at /srv/nfs/pi-bake/
#   - unfsd ports FIXED: 2049 (nfsd) + 4858 (mountd) — no rpcbind
#     registration (-p flag), clients specify ports explicitly
#   - incus proxy: host:8801 -> container:2049 (nfsd)
#   - incus proxy: host:8803 -> container:4858 (mountd)
#   - /usr/local/bin/pi-bake-import-rootfs <hostname> <tarball>
#     unpacks a rootfs tarball into /srv/nfs/pi-bake/<hostname>/
#     with ownership preserved
#
# Why NFS-v3 + unfs3 instead of kernel NFS-v4:
#   Kernel-NFS in LXC requires a privileged container (CAP_SYS_ADMIN
#   to mount /proc/fs/nfsd). The userspace alternative `nfs-ganesha`
#   isn't packaged in Alpine. unfs3 is the only userspace NFS server
#   available — NFS-v3-only, no kernel module deps, runs unprivileged.
#   Trade-off: clients need mount option `vers=3,port=2049,mountport=4858`
#   (longer cmdline) — kernel-NFS-v4 would have been one port. For
#   a lab PXE flow that's fine.
#
# Push model: pi-bake on the local bake host pushes via incus's
# native IPC (no SSH needed):
#   incus file push /tmp/td-pi5.tar.gz nfs-pi-bake/tmp/td-pi5.tar.gz
#   incus exec nfs-pi-bake -- /usr/local/bin/pi-bake-import-rootfs \
#     td-pi5 /tmp/td-pi5.tar.gz
#
# For a remote bake host that isn't running incus, ssh into the
# container directly at its incusbr0 IP (no port forward needed
# from the same subnet; for cross-subnet, add an incus proxy device
# for port 22 manually).
#
# Idempotent: re-running with the container already up does
# nothing destructive. To start fresh: `./scripts/teardown-nfs-container.sh`.

set -euo pipefail

CONTAINER=nfs-pi-bake
ALPINE_IMAGE=images:alpine/3.21
NFS_PORT_HOST=${NFS_PORT_HOST:-8801}
MOUNTD_PORT_HOST=${MOUNTD_PORT_HOST:-8803}
NFS_PORT_CONTAINER=2049
MOUNTD_PORT_CONTAINER=4858
LAB_SUBNET=${LAB_SUBNET:-192.168.4.0/24}

log() { printf '\033[1;36m[setup-nfs]\033[0m %s\n' "$*" >&2; }

# ---- container lifecycle ----

if incus list "$CONTAINER" -f csv -c n 2>/dev/null | grep -qx "$CONTAINER"; then
  log "container $CONTAINER already exists — reusing"
  if [ "$(incus list "$CONTAINER" -f csv -c s)" != "RUNNING" ]; then
    log "starting $CONTAINER"
    incus start "$CONTAINER"
  fi
else
  log "launching $CONTAINER from $ALPINE_IMAGE (unprivileged)"
  incus launch "$ALPINE_IMAGE" "$CONTAINER"
fi

# Wait for IP — boot is fast but networking comes up async
for i in 1 2 3 4 5; do
  ip=$(incus list "$CONTAINER" -f csv -c 4)
  if [ -n "$ip" ]; then break; fi
  sleep 1
done
log "$CONTAINER at $ip"

# ---- packages ----

log "installing unfs3 + rpcbind + tar (idempotent)"
incus exec "$CONTAINER" -- apk add --no-cache \
  unfs3 unfs3-openrc rpcbind rpcbind-openrc tar >/dev/null

# ---- exports ----

log "configuring NFS-v3 export at /srv/nfs/pi-bake/"
incus exec "$CONTAINER" -- sh -c "
mkdir -p /srv/nfs/pi-bake
cat > /etc/exports <<EOF
/srv/nfs/pi-bake 127.0.0.1/32(rw,no_root_squash)
/srv/nfs/pi-bake $LAB_SUBNET(rw,no_root_squash)
/srv/nfs/pi-bake 10.31.0.0/16(rw,no_root_squash)
EOF
"

log "configuring unfsd: fixed ports, no rpcbind registration"
incus exec "$CONTAINER" -- sh -c "
cat > /etc/conf.d/unfs3 <<EOF
# Fixed ports so incus proxy forwards a known pair:
#   2049 = nfsd
#   4858 = mountd
# -p skips rpcbind registration — clients specify ports
# explicitly in mount options.
# -t = TCP only (cleaner for lab PXE).
UNFSD_OPTS=\"-n $NFS_PORT_CONTAINER -m $MOUNTD_PORT_CONTAINER -p -t\"
EOF
"

# ---- import helper ----

log "installing /usr/local/bin/pi-bake-import-rootfs"
incus exec "$CONTAINER" -- sh -c '
cat > /usr/local/bin/pi-bake-import-rootfs <<EOF
#!/bin/sh
# pi-bake-import-rootfs <hostname> <tarball-path>
# Extracts a pi-bake rootfs tarball into /srv/nfs/pi-bake/<hostname>/
# with ownership preserved (running as root, default behavior).
# Idempotent: wipes prior contents and re-extracts.
set -eu
HOSTNAME="\${1:?usage: pi-bake-import-rootfs <hostname> <tarball-path>}"
TARBALL="\${2:?usage: pi-bake-import-rootfs <hostname> <tarball-path>}"
TARGET="/srv/nfs/pi-bake/\$HOSTNAME"

case "\$HOSTNAME" in
  *[!a-z0-9-]*|""|.*|*..*)
    echo "ERROR: refusing hostname \$HOSTNAME (must be DNS-label safe)" >&2
    exit 2
    ;;
esac

[ -f "\$TARBALL" ] || { echo "ERROR: tarball not found: \$TARBALL" >&2; exit 2; }

rm -rf "\$TARGET"
mkdir -p "\$TARGET"
tar -xpf "\$TARBALL" -C "\$TARGET"

echo "imported \$HOSTNAME from \$TARBALL -> \$TARGET"
echo "size: \$(du -sh "\$TARGET" | cut -f1)"
EOF
chmod +x /usr/local/bin/pi-bake-import-rootfs
'

# ---- service runlevels ----

log "enabling unfs3 + rpcbind at container boot"
incus exec "$CONTAINER" -- sh -c '
rc-update add rpcbind default 2>/dev/null || true
rc-update add unfs3 default 2>/dev/null || true
'

# ---- start / restart services ----

log "starting rpcbind + unfs3"
incus exec "$CONTAINER" -- sh -c '
rc-service rpcbind start >/dev/null 2>&1 || true
rc-service unfs3 restart >/dev/null 2>&1
'

# ---- incus proxy devices (host port forwards) ----

log "incus proxy: host:$NFS_PORT_HOST -> container:$NFS_PORT_CONTAINER (nfsd)"
if incus config device list "$CONTAINER" | grep -q "^nfs-proxy"; then
  log "  nfs-proxy already configured — reusing"
else
  incus config device add "$CONTAINER" nfs-proxy proxy \
    listen=tcp:0.0.0.0:"$NFS_PORT_HOST" \
    connect=tcp:127.0.0.1:"$NFS_PORT_CONTAINER" >/dev/null
fi

log "incus proxy: host:$MOUNTD_PORT_HOST -> container:$MOUNTD_PORT_CONTAINER (mountd)"
if incus config device list "$CONTAINER" | grep -q "^mountd-proxy"; then
  log "  mountd-proxy already configured — reusing"
else
  incus config device add "$CONTAINER" mountd-proxy proxy \
    listen=tcp:0.0.0.0:"$MOUNTD_PORT_HOST" \
    connect=tcp:127.0.0.1:"$MOUNTD_PORT_CONTAINER" >/dev/null
fi

# ---- summary ----

cat <<EOF

=== nfs-pi-bake container ready (unprivileged) ===

  container:    $CONTAINER (unprivileged, $(incus list "$CONTAINER" -f csv -c 4))
  NFS-v3:       host:$NFS_PORT_HOST    -> container:$NFS_PORT_CONTAINER (nfsd)
  mountd:       host:$MOUNTD_PORT_HOST -> container:$MOUNTD_PORT_CONTAINER (mountd)
  export:       /srv/nfs/pi-bake (rw,no_root_squash)
  allowed:      $LAB_SUBNET + 127.0.0.1/32 + 10.31.0.0/16 (incusbr0)

Push a rootfs from this host:
  tar -czf /tmp/td-pi5-1.tar.gz -C /path/to/rootfs .
  incus file push /tmp/td-pi5-1.tar.gz $CONTAINER/tmp/td-pi5-1.tar.gz
  incus exec $CONTAINER -- \\
    /usr/local/bin/pi-bake-import-rootfs td-pi5-1 /tmp/td-pi5-1.tar.gz

Pi cmdline.txt for NFS-root boot (lab subnet uses host forwards):
  root=/dev/nfs \\
    nfsroot=<bake-host>:/td-pi5-1,vers=3,proto=tcp,port=$NFS_PORT_HOST,mountport=$MOUNTD_PORT_HOST,nolock \\
    rw ip=dhcp rootwait

Teardown:
  ./scripts/teardown-nfs-container.sh
EOF
