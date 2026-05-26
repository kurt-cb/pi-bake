"""Alpine RPi image baker — no-root, mtools-based.

Alpine RPi ships as a tarball you extract onto a FAT32 partition.
On boot, an apkovl tarball (per-host state overlay) is restored
into the live filesystem; that's where `/etc/hostname`,
`/etc/ssh/sshd_config`, etc. come from on subsequent boots.

To produce a single flashable `.img.gz`:
  1. Create an empty FAT32 image of fixed size with `mformat`.
  2. Extract the upstream Alpine RPi tarball.
  3. `mcopy` the extracted tree into the image.
  4. Generate a per-node apkovl.tar.gz (etc/, root/, runlevels).
  5. `mcopy` the apkovl in.
  6. gzip → final `.img.gz`.

No `losetup`, no root. Requires `mtools` + `dosfstools` on PATH.

Sizing: ~400 MB image is enough for the standard Alpine RPi
tarball (~150 MB extracted) + apkovl + future apk-cache headroom.
Operator-overridable via `image_size_mb`.
"""
from __future__ import annotations

import gzip
import io
import logging
import os
import shutil
import stat
import subprocess
import tarfile
import tempfile
from pathlib import Path

from pi_bake.config import NodeConfig
from pi_bake.download import fetch

LOG = logging.getLogger("pi_bake.alpine")

DEFAULT_IMAGE_SIZE_MB = 400

# Board-specific FAT device name. See LBU_MEDIA discussion in
# `_write_apkovl` near the etc/lbu/lbu.conf write. Pi Zero W is
# the only board that needs a non-default value; everything else
# falls through to "mmcblk0".
_LBU_MEDIA_BY_BOARD = {
    "pi-zero-w": "mmcblk0p1",
}


def bake(
    *, url: str, node: NodeConfig, out_path: Path,
    image_size_mb: int = DEFAULT_IMAGE_SIZE_MB,
    alpine_branch: str = "v3.21",
    extra_packages: list[str] | None = None,
    arch: str = "aarch64",
) -> Path:
    """Build an Alpine RPi `.img.gz` for `node`. Returns out_path.

    Steps the operator might want to inspect: each one logs at INFO.

    `alpine_branch`: Alpine repo branch to write into
        `/etc/apk/repositories` — `v3.21` (default), `edge`, etc.
        Affects post-boot `apk` operations only; the bundled /apks
        cache still comes from the tarball.

    `extra_packages`: additional apk package names. When set,
        pi-bake fetches them + all recursive deps from upstream
        Alpine at bake time, drops the .apk files into
        `/apks/<arch>/` alongside the stock cache, regenerates and
        signs a fresh APKINDEX, and bakes the matching pubkey into
        the apkovl's `/etc/apk/keys/`. The packages land in
        `/etc/apk/world` so init's `apk add --no-network` installs
        them at INIT TIME (before sshd starts) — same code path as
        the baseline. No internet needed on the Pi. (See
        design/#3_study.md for the architecture.)

    `arch`: target arch for the Pi (matches `Board.arch` —
        `aarch64`, `armhf`). Drives cross-arch `apk fetch`.
    """
    _require_tools("mformat", "mcopy", "mmd")

    out_path = Path(out_path).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    tarball = fetch(url)

    with tempfile.TemporaryDirectory(prefix="pi-bake-alpine-") as td:
        td = Path(td)
        # 1. Empty FAT32 image.
        img = td / "image.img"
        LOG.info("creating %d MB FAT32 image at %s", image_size_mb, img)
        _create_fat32_image(img, image_size_mb)

        # 2. Extract the upstream tarball into a tree we can mcopy.
        extracted = td / "extracted"
        extracted.mkdir()
        LOG.info("extracting %s", tarball.name)
        with tarfile.open(tarball, "r:*") as tf:
            # `filter="data"` is Python 3.12+ and applies path-traversal
            # sanitization. Pre-3.12 lacks the kwarg; the official Alpine
            # tarball is trusted upstream content so the older permissive
            # behavior is fine. Try the safer call first, fall back.
            try:
                tf.extractall(extracted, filter="data")
            except TypeError:
                tf.extractall(extracted)

        # 2.5. Bake-time apk-fetch + signed-index regen, whenever
        # the operator declares extras. Drops fetched .apks into
        # /apks/<arch>/ alongside the stock cache (flat layout),
        # regenerates APKINDEX.tar.gz with a fresh RSA signing key,
        # bakes the matching pubkey into the apkovl. Init's
        # `apk add --root $sysroot --no-network` then installs
        # operator extras AT INIT TIME from the same code path as
        # the stock baseline — no late-boot local.d script needed.
        # See design/#3_study.md for the architecture; pieces live
        # in apkfetch.py.
        apk_signing_pubkey_bytes = b""
        apk_signing_pubkey_name = ""
        extras = list(extra_packages or [])
        if extras:
            from pi_bake import apkfetch
            LOG.info("bake-time apk-fetch: %d package(s)", len(extras))
            apk_static = apkfetch.ensure_apk_static()
            keys_dir = apkfetch.extract_initramfs_keys(
                extracted, td / "initramfs-keys",
            )
            apks_dir = extracted / "apks" / arch
            apkfetch.fetch_packages(
                apk_static=apk_static,
                target_arch=arch,
                alpine_branch=alpine_branch,
                packages=extras,
                out_dir=apks_dir,
                keys_dir=keys_dir,
            )
            privkey, pubkey, pubkey_name = apkfetch.make_signing_key(
                td / "signing",
            )
            apkfetch.regen_signed_index(
                apk_static=apk_static,
                apks_dir=apks_dir,
                privkey=privkey,
                pubkey_name=pubkey_name,
            )
            apk_signing_pubkey_bytes = pubkey.read_bytes()
            apk_signing_pubkey_name = pubkey_name

        # 2.6. Optional: /boot/usercfg.txt FAT edit (Recipe.config_txt).
        # The stock Alpine config.txt has `include usercfg.txt` already,
        # so the bootloader picks our additions up without touching the
        # shipped file. Used for HAT enablement (dtoverlay=, dtparam=).
        if node.config_txt:
            usercfg = extracted / "usercfg.txt"
            body = "# Written by pi-bake — operator-declared HAT/peripheral overlays.\n"
            body += "\n".join(node.config_txt) + "\n"
            usercfg.write_text(body)
            LOG.info("usercfg.txt: %d line(s) → FAT", len(node.config_txt))

        # 3. Pour the tree into the FAT32 image.
        LOG.info("mcopy: tarball → image")
        for child in sorted(extracted.iterdir()):
            _mcopy_into(img, child, "/")

        # 4. Per-node apkovl.tar.gz.
        apkovl_path = td / f"{node.hostname}.apkovl.tar.gz"
        LOG.info("generating apkovl: %s", apkovl_path.name)
        _write_apkovl(
            apkovl_path, node,
            alpine_branch=alpine_branch,
            extra_packages=extras,
            apk_signing_pubkey_bytes=apk_signing_pubkey_bytes,
            apk_signing_pubkey_name=apk_signing_pubkey_name,
        )
        _mcopy_into(img, apkovl_path, "/")

        # 5. gzip → out_path.
        LOG.info("compressing → %s", out_path)
        with open(img, "rb") as src, gzip.open(out_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, length=1 << 20)

    LOG.info("DONE: %s (%d MB)", out_path, out_path.stat().st_size >> 20)
    return out_path


# --------------------------------------------------------------------------- #
# FAT32 image helpers (mtools)                                                 #
# --------------------------------------------------------------------------- #

def _create_fat32_image(path: Path, size_mb: int) -> None:
    """Create an empty FAT32 image at `path` of exactly `size_mb`
    megabytes. Uses `truncate` + `mformat` (no root)."""
    size_bytes = size_mb * 1024 * 1024
    with open(path, "wb") as f:
        f.truncate(size_bytes)
    # -i for image file, -F for FAT32, -v for volume label.
    subprocess.run(
        ["mformat", "-i", str(path), "-F", "-v", "PI-BAKE", "::"],
        check=True, capture_output=True, text=True,
    )


def _mcopy_into(img: Path, src: Path, dest: str = "/") -> None:
    """`mcopy -s -i <img> <src> ::<dest>`.

    `dest` is the path inside the FAT image — normalized to start with
    `/`, then prefixed with `::` to form mtools' "image-root-relative"
    syntax. `::/` puts files at the FAT root; `::/apkovl/` inside an
    apkovl subdir, etc. Recursive (`-s`) handles directories.
    """
    if not dest.startswith("/"):
        dest = "/" + dest
    target = f"::{dest}"
    cmd = ["mcopy", "-Q", "-s", "-i", str(img), str(src), target]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"mcopy failed: {' '.join(cmd)}\n--- stderr ---\n{r.stderr}"
        )


# --------------------------------------------------------------------------- #
# apkovl generation                                                            #
# --------------------------------------------------------------------------- #

def _write_apkovl(
    out: Path,
    node: NodeConfig,
    *,
    alpine_branch: str = "v3.21",
    extra_packages: list[str] | None = None,
    apk_signing_pubkey_bytes: bytes = b"",
    apk_signing_pubkey_name: str = "",
) -> None:
    """Build an Alpine apkovl.tar.gz for `node`.

    Strategy: rely on Alpine RPi's diskless init, which on first
    boot runs `apk add --root $sysroot --no-network` reading
    packages from the overlay's `/etc/apk/world` and pulling
    .apks from `/media/mmcblk0/apks/<arch>/` (the FAT-resident
    cache). Everything ships from one transaction at init time —
    no late-boot scripts, no network on first boot.

    Stock Alpine RPi tarball ships ~100 baseline .apks in
    /apks/<arch>/ (sshd, dhcpcd, chrony, wpa_supplicant, etc.).
    Operator-declared `packages:` extras get fetched + dropped
    into the SAME directory at bake time, and the APKINDEX gets
    regenerated + signed with a fresh per-bake RSA key. The
    matching pubkey is baked into /etc/apk/keys/ so init's apk
    add trusts the new index. See apkfetch.py +
    design/#3_study.md for the architecture.

    DHCP choice: dhcpcd, not busybox udhcpc. udhcpc 1.37 + Alpine
    3.21 + Pi 5's macb driver hangs with "address family not
    supported". dhcpcd is more reliable across kernels and is
    what setup-alpine selects by default in recent releases.

    Files we lay down (paths relative to /):
      etc/hostname                    — node.hostname
      etc/hosts                       — localhost + hostname entry
      etc/timezone                    — node.timezone
      etc/ssh/sshd_config             — root-by-key only, no passwords
      etc/ssh/ssh_host_<type>_key{,.pub} — stable host identity
      root/.ssh/authorized_keys       — node.all_pubkeys
      etc/network/interfaces          — lo only (+ eth0 static path)
      etc/apk/world                   — baseline + operator extras (init installs)
      etc/apk/repositories            — local cache + upstream main/community
      etc/apk/keys/<pi-bake-…>.rsa.pub — when extras were fetched + signed
      etc/runlevels/default/sshd      — symlink, started at boot
      etc/runlevels/default/dhcpcd    — symlink (skipped on static-IP)
      etc/runlevels/default/chronyd   — symlink
      etc/runlevels/default/networking — symlink (brings up lo + static eth0)
      etc/wpa_supplicant/wpa_supplicant.conf + runlevel — only when wifi
    """
    members: list[tuple[str, bytes, int, bool]] = []
    # (path, content, mode, is_symlink)

    # The whole reason the rest of this works. Alpine RPi's /init,
    # when it finds an apkovl, SKIPS the "add default boot services"
    # block (rc_add modloop sysinit, rc_add modules boot, …) UNLESS
    # this marker file is present. Without modloop in sysinit, the
    # squashfs of kernel modules never mounts on /lib/modules, so
    # af_packet, ipv6, almost every needed network driver is absent
    # — and every DHCP client fails with "Address family not
    # supported by protocol" (seen on Pi 5 with both busybox udhcpc
    # and dhcpcd 10.x). The init script deletes this marker after
    # consuming it, so it's truly one-shot.
    members.append(("etc/.default_boot_services", b"", 0o644, False))

    # lbu (Alpine "local backup") writes a fresh apkovl onto the FAT
    # so the operator's post-boot changes survive reboot. Without
    # LBU_MEDIA set, `lbu commit` and even `lbu status` just print
    # usage.
    #
    # Board-specific FAT device name (the totaldns operator hit this
    # 2026-05-26 — without it, lbu commit on Pi Zero W silently writes
    # to nowhere, breaking apkovl persistence):
    #   - Pi 5 / Pi 4 / Pi 3 / Pi Zero 2 W: Alpine RPi init mounts
    #     the FAT at /media/mmcblk0 (whole-device mount, no partition
    #     table on the FAT). LBU_MEDIA="mmcblk0" is right.
    #   - Pi Zero W (original, armhf): Alpine RPi init mounts at
    #     /media/mmcblk0p1 (partition 1 of a partitioned image).
    #     LBU_MEDIA="mmcblk0p1" is right.
    # If node.board is unset (operator constructed NodeConfig
    # directly without going through Recipe), fall back to mmcblk0
    # — works for the common case.
    lbu_media = _LBU_MEDIA_BY_BOARD.get(node.board, "mmcblk0")
    #
    # BACKUP_LIMIT=3 turns on lbu's built-in apkovl rotation: each
    # commit shifts the previous apkovl to `<host>.apkovl.tar.gz.0`,
    # then `.1`, then `.2`, dropping the oldest beyond 3. The `.N`
    # suffix doesn't match the bootloader's `*.apkovl.tar.gz` glob,
    # so old backups sit on FAT without being mis-loaded — unlike a
    # manual `cp foo.apkovl.tar.gz foo.bak` which DID confuse the
    # loader on at least one occasion (operator had to power-cycle
    # + rename to recover).
    members.append((
        "etc/lbu/lbu.conf",
        (
            f'LBU_MEDIA="{lbu_media}"\n'
            f'BACKUP_LIMIT=3\n'
        ).encode(),
        0o644, False,
    ))

    members.append(("etc/hostname", f"{node.hostname}\n".encode(), 0o644, False))
    members.append((
        "etc/hosts",
        (
            "127.0.0.1 localhost\n"
            f"127.0.1.1 {node.hostname}\n"
            "::1 localhost ip6-localhost ip6-loopback\n"
            "ff02::1 ip6-allnodes\nff02::2 ip6-allrouters\n"
        ).encode(),
        0o644, False,
    ))
    members.append(("etc/timezone", f"{node.timezone}\n".encode(), 0o644, False))

    # /etc/modules — kernel modules forced loaded at boot. Most
    # modules autoload via udev or kernel builtins; this file is
    # the override for hardware that needs an explicit modprobe
    # before the runlevel using it comes up (e.g. mcp251x for the
    # MCP2515 SPI CAN controller — autoload doesn't fire until
    # the spi-bcm2835 master is up, which the dtoverlay enables,
    # but explicitly listing it removes a race on slower boards).
    if node.modules:
        modules_body = (
            "# Written by pi-bake — operator-declared kernel modules.\n"
            + "\n".join(node.modules) + "\n"
        )
        members.append(("etc/modules", modules_body.encode(), 0o644, False))

    # sshd_config: enable, no passwords, root login by key.
    # Alpine's openssh is built WITHOUT PAM — `UsePAM yes` makes sshd
    # refuse to start ("Bad configuration option: UsePAM"). Don't add
    # it back unless you also switch to a PAM-enabled openssh build.
    # `ChallengeResponseAuthentication` was renamed to
    # `KbdInteractiveAuthentication` in openssh 8.7; 9.9 still parses
    # it but it's redundant when PasswordAuthentication=no.
    sshd_cfg = (
        "PermitRootLogin prohibit-password\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        "PrintMotd no\n"
        "AcceptEnv LANG LC_*\n"
        "Subsystem sftp /usr/lib/ssh/sftp-server\n"
    )
    members.append(("etc/ssh/sshd_config", sshd_cfg.encode(), 0o600, False))

    # /etc/ssh/ssh_host_<type>_key{,.pub} — bake a stable SSH host
    # identity instead of letting sshd's first-boot init regenerate
    # one. Without this, each reflash makes the operator's
    # known_hosts flag the rebuilt Pi as "REMOTE HOST IDENTIFICATION
    # HAS CHANGED" + breaks pyinfra runs that don't set
    # StrictHostKeyChecking=no. Pair is either operator-provided
    # (NodeConfig.ssh_host_key_{priv,pub}) — stable across builds —
    # or auto-generated here as a fresh ed25519 pair, which is at
    # least stable across reflashes of the same .img.gz.
    host_priv = node.ssh_host_key_priv
    host_pub = node.ssh_host_key_pub
    if not host_priv:
        host_priv, host_pub = _generate_ed25519_host_key(node.hostname)
    key_type = _ssh_host_key_type(host_pub)
    members.append((
        f"etc/ssh/ssh_host_{key_type}_key",
        host_priv,
        0o600, False,
    ))
    members.append((
        f"etc/ssh/ssh_host_{key_type}_key.pub",
        host_pub,
        0o644, False,
    ))

    members.append(("root/.ssh/authorized_keys",
                    node.authorized_keys_text().encode(), 0o600, False))

    # /etc/network/interfaces — lo is always managed by `networking`.
    # For DHCP nodes, eth0 + wlan0 are NOT listed here: dhcpcd runs as
    # a daemon and watches all interfaces, so listing them under
    # `networking` would race with dhcpcd. For static-IP nodes, eth0
    # IS listed (and dhcpcd is dropped from the runlevel entirely —
    # see further down).
    interfaces = "auto lo\niface lo inet loopback\n"
    if node.has_static_ip:
        interfaces += (
            f"\nauto eth0\n"
            f"iface eth0 inet static\n"
            f"    address {node.static_address_only}\n"
            f"    netmask {node.static_netmask}\n"
            f"    gateway {node.gateway_ipv4}\n"
        )
    members.append(("etc/network/interfaces", interfaces.encode(), 0o644, False))

    # /etc/resolv.conf for static-IP nodes (DHCP fills this via dhcpcd
    # but static doesn't). Default to Cloudflare + Google — operator
    # can replace post-boot if they want their own resolver.
    if node.has_static_ip:
        members.append((
            "etc/resolv.conf",
            b"nameserver 1.1.1.1\nnameserver 8.8.8.8\n",
            0o644, False,
        ))

    # /etc/apk/repositories — local FAT cache first (init's `--no-network`
    # path resolves from here), then upstream so post-boot `apk add`
    # works for anything not in the cache. `alpine_branch` controls
    # the upstream branch: `v3.21` (stable) or `edge` (rolling — used
    # when the operator needs drivers/firmware only present on edge,
    # e.g. Intel BE200 iwlwifi). Edge is rolling so reproducibility
    # is weaker; the trade is unavoidable for edge-only hardware.
    members.append((
        "etc/apk/repositories",
        (
            f"/media/mmcblk0/apks\n"
            f"http://dl-cdn.alpinelinux.org/alpine/{alpine_branch}/main\n"
            f"http://dl-cdn.alpinelinux.org/alpine/{alpine_branch}/community\n"
        ).encode(),
        0o644, False,
    ))

    # /etc/apk/world — the package set Alpine init installs on first
    # boot from the local /media/mmcblk0/apks cache. All listed
    # packages MUST exist in the stock RPi tarball; anything else
    # would need bake-time fetch (deferred — see module docstring).
    world_pkgs = [
        "alpine-base",
        "openssh-server",
        "openssh-server-common-openrc",
        # openssh-server alone has no sftp-server binary, so modern
        # scp (openssh 9.0+ defaults to SFTP protocol) fails with
        # "subsystem request failed". pyinfra also leans on SFTP for
        # file pushes. Ships in stock RPi tarball — free to include.
        "openssh-sftp-server",
        # openssh CLIENT (scp + ssh binaries on the Pi itself). Lets
        # operators push files INTO the Pi with `scp` from another
        # host (modern scp 9.0+ uses SFTP, but the legacy `scp -O`
        # path needs scp binary on the remote — and operators
        # reaching for plain `scp file pi:/...` expect it to work).
        # Also lets the Pi originate ssh/scp outbound (e.g. pulling
        # configs from a peer). Ships in stock RPi tarball.
        "openssh-client-default",
        "chrony",
        "chrony-openrc",
    ]
    if not node.has_static_ip:
        world_pkgs += ["dhcpcd", "dhcpcd-openrc"]
    if node.has_wifi:
        world_pkgs += [
            "wpa_supplicant",
            "wpa_supplicant-openrc",
            "iw",
            "ifupdown-ng-wifi",
        ]
    # /etc/apk/world — the complete package set Alpine init
    # installs from the local FAT cache at first boot:
    #
    #   apk add --root $sysroot --no-network $(cat etc/apk/world)
    #
    # Baseline (sshd/dhcpcd/chrony/etc.) ships in the stock RPi
    # tarball's /apks/<arch>/ cache. Operator-declared extras
    # (`packages:` in YAML — avahi, dbus, linux-firmware-*, etc.)
    # were fetched into the same /apks/<arch>/ at bake time and
    # the APKINDEX was regenerated + signed with a fresh
    # pi-bake key (see apkfetch.py + design/#3_study.md).
    # Trusting that key happens via the apk_signing_pubkey block
    # below.
    #
    # ALL packages go in /etc/apk/world. There's no longer a
    # "baseline vs extras" split — init's wholesale-or-nothing
    # apk add handles everything in one transaction.
    extras = sorted(set(extra_packages or []))
    world_pkgs.extend(extras)
    members.append((
        "etc/apk/world",
        ("\n".join(world_pkgs) + "\n").encode(),
        0o644, False,
    ))

    # /etc/apk/keys/<pi-bake-XXXX.rsa.pub> — trust our bake-time
    # APKINDEX signature. Alpine RPi init's `cp -a /etc/apk/keys
    # $sysroot/etc/apk` (line ~936 of boot/initramfs-rpi/init)
    # merges initramfs's alpine-devel keys WITH whatever the
    # apkovl provided here, so our key coexists with Alpine's
    # official ones. apk-tools' verifier accepts a signature if
    # ANY trusted key matches.
    if apk_signing_pubkey_bytes:
        members.append((
            f"etc/apk/keys/{apk_signing_pubkey_name}",
            apk_signing_pubkey_bytes,
            0o644, False,
        ))

    # /etc/dhcpcd.conf — dhcpcd default config does NOT send DHCP
    # option 12 (hostname); the `hostname` directive on its own
    # line tells dhcpcd to populate option 12 from /etc/hostname.
    # When `node.dhcp_send_hostname=False`, we bake the line as a
    # comment instead — the device acts as an intentional test
    # fixture for DHCP servers that need to recover the hostname
    # via mDNS or accept a synthesized placeholder.
    hostname_line = (
        "hostname\n" if node.dhcp_send_hostname
        else "# hostname    # disabled by pi-bake (--no-dhcp-hostname)\n"
    )
    members.append((
        "etc/dhcpcd.conf",
        (
            "# /etc/dhcpcd.conf — written by pi-bake.\n"
            "# Read /etc/hostname + send it as DHCP option 12.\n"
            + hostname_line +
            "# Standard set most installs want.\n"
            "duid\n"
            "persistent\n"
            "option rapid_commit\n"
            "option domain_name_servers, domain_name, domain_search\n"
            "option classless_static_routes\n"
            "option interface_mtu\n"
            "require dhcp_server_identifier\n"
            "slaac private\n"
        ).encode(),
        0o644, False,
    ))

    if node.has_wifi:
        members.append((
            "etc/wpa_supplicant/wpa_supplicant.conf",
            node.wpa_supplicant_conf().encode(),
            0o600, False,
        ))
        # Tell wpa_supplicant-openrc which interface to drive. Without
        # this it boots in no-interface mode and never associates.
        members.append((
            "etc/conf.d/wpa_supplicant",
            b'wpa_supplicant_args="-iwlan0"\n',
            0o644, False,
        ))
        # Power-save off on wlan0. The Pi Zero W's BCM43438 chipset
        # aggressively power-saves by default: L2 stays associated but
        # ARP/L3 traffic gets dropped, so the device looks "up" while
        # being unreachable from peers. Fix is `iw dev wlan0 set
        # power_save off`. Harmless no-op on cards that handle PS
        # properly, so we apply unconditionally when wifi is on.
        # Runs at boot via OpenRC `local`; rc_add picked up via the
        # runlevel default symlink (already added below for `local`
        # service when other /etc/local.d/ scripts are present).
        members.append((
            "etc/local.d/wlan-power-save-off.start",
            (
                "#!/bin/sh\n"
                "# Written by pi-bake — disable WiFi power save (BCM43438 fix).\n"
                "# Harmless on cards that handle PS properly.\n"
                "# Wait briefly for wpa_supplicant to bring the iface up,\n"
                "# then turn PS off.\n"
                "for i in 1 2 3 4 5; do\n"
                "    if ip link show wlan0 >/dev/null 2>&1; then\n"
                "        iw dev wlan0 set power_save off 2>/dev/null && break\n"
                "    fi\n"
                "    sleep 1\n"
                "done\n"
                "exit 0\n"
            ).encode(),
            0o755, False,
        ))

    # /etc/runlevels/default/* — symlinks into /etc/init.d/ enable
    # services at boot. The target init scripts don't exist yet when
    # the apkovl is extracted; they appear when init's `apk add`
    # installs the corresponding `*-openrc` packages from world (a few
    # lines below). By the time the `default` runlevel actually starts,
    # both symlink and target exist.
    runlevel_svcs = ["networking", "sshd", "chronyd"]
    if not node.has_static_ip:
        runlevel_svcs.append("dhcpcd")
    # `local` runs /etc/local.d/*.start at boot. Today the only
    # consumer is the wlan power-save fix (BCM43438), so we only
    # add it to the default runlevel when wifi is on. Operator-
    # declared extras used to require `local` (for the v0.2-era
    # install-extras.start script), but #3 moved that install
    # path into init time via /etc/apk/world, so `local` is no
    # longer needed for the extras workflow.
    if node.has_wifi:
        runlevel_svcs.append("local")
    if node.has_wifi:
        runlevel_svcs.append("wpa_supplicant")
    for svc in runlevel_svcs:
        members.append((
            f"etc/runlevels/default/{svc}",
            f"/etc/init.d/{svc}".encode(),
            0o777, True,
        ))

    # Pack as tar.gz.
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, content, mode, is_symlink in members:
            ti = tarfile.TarInfo(name=path)
            if is_symlink:
                ti.type = tarfile.SYMTYPE
                ti.linkname = content.decode()
                ti.size = 0
            else:
                ti.size = len(content)
            ti.mode = mode
            ti.uid = 0
            ti.gid = 0
            ti.mtime = 0
            if is_symlink:
                tf.addfile(ti)
            else:
                tf.addfile(ti, io.BytesIO(content))

    out.write_bytes(buf.getvalue())


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _require_tools(*names: str) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise RuntimeError(
            f"missing required tool(s) on PATH: {missing}. "
            f"On Fedora: sudo dnf install mtools dosfstools. "
            f"On Debian/Ubuntu: sudo apt install mtools dosfstools."
        )


def _generate_ed25519_host_key(hostname: str) -> tuple[bytes, bytes]:
    """Generate a fresh ed25519 SSH host keypair via ssh-keygen.

    Returns (priv_bytes, pub_bytes) — exactly what
    `/etc/ssh/ssh_host_ed25519_key{,.pub}` should contain. Comment
    encodes the bake host + target hostname so it's identifiable
    in a known_hosts entry.
    """
    if shutil.which("ssh-keygen") is None:
        raise RuntimeError(
            "ssh-keygen not on PATH; required for SSH host key auto-gen. "
            "Install openssh-client (Debian/Ubuntu) or openssh "
            "(Fedora/Alpine), or pass NodeConfig.ssh_host_key_priv/pub "
            "to skip auto-generation."
        )
    with tempfile.TemporaryDirectory(prefix="pi-bake-hostkey-") as td:
        priv = Path(td) / "ssh_host_ed25519_key"
        pub = Path(td) / "ssh_host_ed25519_key.pub"
        subprocess.run(
            [
                "ssh-keygen", "-q", "-t", "ed25519",
                "-N", "",
                "-f", str(priv),
                "-C", f"pi-bake@{hostname}",
            ],
            check=True, capture_output=True, text=True,
        )
        return priv.read_bytes(), pub.read_bytes()


def _ssh_host_key_type(pub: bytes) -> str:
    """`ed25519` | `rsa` | `ecdsa` — derived from the pubkey's
    first whitespace-delimited field. Raises on unknown types."""
    first = pub.split(None, 1)[0].decode(errors="replace")
    if first == "ssh-ed25519":
        return "ed25519"
    if first == "ssh-rsa":
        return "rsa"
    if first.startswith("ecdsa-sha2-"):
        return "ecdsa"
    raise ValueError(
        f"SSH host pubkey starts with {first!r}; expected "
        f"ssh-ed25519, ssh-rsa, or ecdsa-sha2-*"
    )
