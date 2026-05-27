#!/usr/bin/env bash
# Drive a remote (privileged) bake host over SSH:
#   1. Build the local wheel (`make dist`).
#   2. Push it + install on the remote.
#   3. Copy the recipe YAML over, run `pi-bake build` there.
#   4. Pull the resulting .img.xz back into ./out/.
#
# Useful when your laptop doesn't have passwordless sudo / LXC /
# a kernel that supports the losetup operations the non-Alpine
# (and Alpine ext4) backends need.
#
# Remote host prerequisites:
#   util-linux (losetup, partprobe, lsblk, blkid, sfdisk)
#   dosfstools (mkfs.vfat) + e2fsprogs (mkfs.ext4)
#   xz, rsync, tar, cpio, openssl, ssh-keygen
#   python3 + pip3
# pi-bake skips `sudo` when invoked as root, so the remote host
# doesn't need the sudo package — but it must be SSH'd into as root
# (or otherwise privileged).
#
# Usage:
#   ./scripts/remote-bake.sh setup [REMOTE]
#   ./scripts/remote-bake.sh bake  <recipe.yaml> [REMOTE] [OUT_DIR]
#
#   REMOTE   defaults to root@10.31.237.169
#   OUT_DIR  defaults to ./out

set -euo pipefail

REMOTE_DEFAULT="root@10.31.237.169"
REMOTE_DIR="/root/pi-bake"
OUT_DIR_DEFAULT="./out"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30)

usage() {
    sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \?//'
    exit "${1:-1}"
}

require() {
    local missing=()
    for cmd in "$@"; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if (( ${#missing[@]} )); then
        echo "ERROR: missing local tool(s): ${missing[*]}" >&2
        exit 1
    fi
}

cmd_setup() {
    local remote="${1:-$REMOTE_DEFAULT}"
    require make ssh scp

    echo "==> Building wheel locally"
    make dist

    echo "==> Ensuring tools on $remote"
    ssh "${SSH_OPTS[@]}" "$remote" \
      "command -v sfdisk mkfs.vfat mkfs.ext4 losetup partprobe lsblk blkid rsync xz openssl ssh-keygen tar cpio python3 pip3 >/dev/null \
        || (apt-get update && apt-get install -y --no-install-recommends util-linux dosfstools e2fsprogs parted rsync xz-utils openssl openssh-client tar cpio python3-pip) \
        || (apk update && apk add util-linux util-linux-misc sfdisk dosfstools e2fsprogs parted rsync xz openssl openssh-keygen tar cpio python3 py3-pip)"

    echo "==> Pushing wheel to $remote:$REMOTE_DIR"
    ssh "${SSH_OPTS[@]}" "$remote" "mkdir -p $REMOTE_DIR/dist"
    scp "${SSH_OPTS[@]}" dist/py_pi_bake-*.whl "$remote:$REMOTE_DIR/dist/"

    echo "==> pip install"
    ssh "${SSH_OPTS[@]}" "$remote" \
      "pip3 install --break-system-packages --upgrade --force-reinstall $REMOTE_DIR/dist/py_pi_bake-*.whl"
    ssh "${SSH_OPTS[@]}" "$remote" "pi-bake --version"
}

cmd_bake() {
    local config="${1:-}"
    local remote="${2:-$REMOTE_DEFAULT}"
    local out_dir="${3:-$OUT_DIR_DEFAULT}"

    [[ -n "$config" ]] || { echo "ERROR: <recipe.yaml> required" >&2; usage 2; }
    [[ -f "$config" ]] || { echo "ERROR: $config not found" >&2; exit 1; }
    require ssh scp

    mkdir -p "$out_dir"

    echo "==> Copying $config → $remote:$REMOTE_DIR/recipe.yaml"
    scp "${SSH_OPTS[@]}" "$config" "$remote:$REMOTE_DIR/recipe.yaml"

    echo "==> Baking on $remote"
    ssh "${SSH_OPTS[@]}" "$remote" \
      "cd $REMOTE_DIR && rm -f output.img.* && pi-bake build --config recipe.yaml --out $REMOTE_DIR/output.img.xz"

    echo "==> Pulling result → $out_dir/"
    scp "${SSH_OPTS[@]}" "$remote:$REMOTE_DIR/output.img.*" "$out_dir/"
    ls -lh "$out_dir/"
}

cmd_shell() {
    local remote="${1:-$REMOTE_DEFAULT}"
    exec ssh "${SSH_OPTS[@]}" "$remote"
}

cmd_clean() {
    local remote="${1:-$REMOTE_DEFAULT}"
    ssh "${SSH_OPTS[@]}" "$remote" "rm -rf $REMOTE_DIR"
}

action="${1:-help}"; shift || true
case "$action" in
    setup) cmd_setup "$@" ;;
    bake)  cmd_bake "$@" ;;
    shell) cmd_shell "$@" ;;
    clean) cmd_clean "$@" ;;
    help|-h|--help) usage 0 ;;
    *) echo "unknown action: $action" >&2; usage 2 ;;
esac
