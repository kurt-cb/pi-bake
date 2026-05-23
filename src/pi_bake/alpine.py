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


def bake(
    *, url: str, node: NodeConfig, out_path: Path,
    image_size_mb: int = DEFAULT_IMAGE_SIZE_MB,
) -> Path:
    """Build an Alpine RPi `.img.gz` for `node`. Returns out_path.

    Steps the operator might want to inspect: each one logs at INFO.
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

        # 3. Pour the tree into the FAT32 image.
        LOG.info("mcopy: tarball → image")
        for child in sorted(extracted.iterdir()):
            _mcopy_into(img, child, "/")

        # 4. Per-node apkovl.tar.gz.
        apkovl_path = td / f"{node.hostname}.apkovl.tar.gz"
        LOG.info("generating apkovl: %s", apkovl_path.name)
        _write_apkovl(apkovl_path, node)
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

def _write_apkovl(out: Path, node: NodeConfig) -> None:
    """Build an Alpine apkovl.tar.gz for `node`.

    Files we lay down (paths relative to /):
      etc/hostname                    — node.hostname
      etc/hosts                       — minimal localhost + hostname entry
      etc/timezone                    — node.timezone
      etc/ssh/sshd_config             — PasswordAuthentication no
      root/.ssh/authorized_keys       — node.all_pubkeys
      root/.ssh                       — mode 0700
      etc/wpa_supplicant/wpa_supplicant.conf — only when wifi configured
      etc/network/interfaces          — eth0 = dhcp; wlan0 = dhcp if wifi
      etc/runlevels/default/sshd      — symlink so sshd starts at boot
      etc/runlevels/default/wpa_supplicant — same if wifi
      etc/local.d/pi-bake-firstboot.start — runs once: apk update + add openssh
      etc/runlevels/default/local     — symlink so local.d runs at boot

    NOT a complete apkovl — we lean on Alpine's first-boot
    behaviour to wire the rest. Anything role-specific is pyinfra's
    job after the box is up.
    """
    members: list[tuple[str, bytes, int, bool]] = []
    # (path, content, mode, is_symlink)

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

    # sshd_config: enable, no passwords, root login by key.
    sshd_cfg = (
        "PermitRootLogin prohibit-password\n"
        "PasswordAuthentication no\n"
        "ChallengeResponseAuthentication no\n"
        "UsePAM yes\n"
        "PrintMotd no\n"
        "AcceptEnv LANG LC_*\n"
        "Subsystem sftp /usr/lib/ssh/sftp-server\n"
    )
    members.append(("etc/ssh/sshd_config", sshd_cfg.encode(), 0o600, False))

    members.append(("root/.ssh/authorized_keys",
                    node.authorized_keys_text().encode(), 0o600, False))

    # /etc/network/interfaces — eth0 = static OR dhcp; wlan0 = dhcp if wifi.
    # `hostname` option tells udhcpc to send DHCP option 12 in its
    # requests; routers + totaldns name-locking can then identify
    # the device by its baked hostname instead of just MAC.
    dhcp_hostname = f"    hostname {node.hostname}\n"
    interfaces = "auto lo\niface lo inet loopback\n\nauto eth0\n"
    if node.has_static_ip:
        # Static for eth0. Avoids the udhcpc rabbit hole on quirky
        # kernels (busybox 1.37 + Alpine RPi 3.21 + Pi 5 macb driver
        # has seen "address family not supported" failures).
        interfaces += (
            f"iface eth0 inet static\n"
            f"    address {node.static_address_only}\n"
            f"    netmask {node.static_netmask}\n"
            f"    gateway {node.gateway_ipv4}\n"
        )
    else:
        interfaces += f"iface eth0 inet dhcp\n{dhcp_hostname}"
    if node.has_wifi:
        interfaces += (
            "\nauto wlan0\niface wlan0 inet dhcp\n" + dhcp_hostname
        )
        members.append((
            "etc/wpa_supplicant/wpa_supplicant.conf",
            node.wpa_supplicant_conf().encode(),
            0o600, False,
        ))
    members.append(("etc/network/interfaces", interfaces.encode(), 0o644, False))

    # /etc/resolv.conf for static-IP nodes (DHCP fills this on its own
    # but static doesn't). Default to Cloudflare + Google — operator
    # can replace post-boot if they want their own resolver.
    if node.has_static_ip:
        members.append((
            "etc/resolv.conf",
            b"nameserver 1.1.1.1\nnameserver 8.8.8.8\n",
            0o644, False,
        ))

    # First-boot script: install openssh + avahi (for .local mDNS
    # discovery — headless Pis are nearly unfindable without it) +
    # (if wifi) wpa_supplicant, then self-disable.
    firstboot = (
        "#!/bin/sh\n"
        "# pi-bake first-boot setup. Self-disables after one successful run.\n"
        "set -e\n"
        # Best-effort time sync BEFORE apk update — Pi has no RTC, so
        # the clock is whatever the OS booted with (often 1970). apk's
        # TLS verify (alpine repos are HTTPS) refuses to validate certs
        # dated wildly off, AND ssh-keygen complains about future-dated
        # keys. Two-step: pull a Date from a known HTTP server first,
        # then once chrony is in, real NTP takes over.\n"
        "date -u -s \"$(wget -q -S -O /dev/null http://dl-cdn.alpinelinux.org/ 2>&1 "
        "| awk '/^  Date:/ {sub(/^  Date: /,\"\"); print; exit}')\" 2>/dev/null || true\n"
        "apk update\n"
        # chrony for ongoing time sync; wifi-firmware metapackage pulls
        # in brcmfmac + iwlwifi blobs so both built-in Pi 5 WiFi (BCM43455)
        # and Intel BE200 PCIe cards have their firmware available;
        # avahi for headless mDNS discovery.
        "apk add openssh-server iproute2 ca-certificates avahi avahi-tools "
        "dbus chrony wifi-firmware\n"
    )
    if node.has_wifi:
        firstboot += "apk add wpa_supplicant wireless-regdb\n"
    firstboot += (
        "rc-update add sshd default\n"
        "rc-service sshd start\n"
        # dbus + avahi-daemon so `<hostname>.local` resolves on the LAN.
        "rc-update add dbus default\n"
        "rc-service dbus start\n"
        "rc-update add avahi-daemon default\n"
        "rc-service avahi-daemon start\n"
        # chrony for ongoing NTP sync (Pi has no RTC).
        "rc-update add chronyd default\n"
        "rc-service chronyd start\n"
    )
    if node.has_wifi:
        firstboot += (
            "rc-update add wpa_supplicant default\n"
            "rc-service wpa_supplicant start\n"
        )
    firstboot += (
        "# Self-disable: mv this script out of local.d so it won't run again.\n"
        "mv /etc/local.d/pi-bake-firstboot.start /var/log/pi-bake-firstboot.done\n"
    )
    members.append((
        "etc/local.d/pi-bake-firstboot.start",
        firstboot.encode(), 0o755, False,
    ))

    # /etc/runlevels/default/* — symlinks into /etc/init.d/ to enable services.
    for svc in ("local", "networking", "sshd"):
        members.append((
            f"etc/runlevels/default/{svc}",
            f"/etc/init.d/{svc}".encode(),
            0o777, True,
        ))
    if node.has_wifi:
        members.append((
            "etc/runlevels/default/wpa_supplicant",
            b"/etc/init.d/wpa_supplicant",
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
