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

    Strategy: rely on Alpine RPi's diskless init, which on first boot
    runs `apk add --root $sysroot --no-network` reading packages from
    the overlay's `/etc/apk/world` and pulling apks from the local
    `/media/mmcblk0/apks/` cache that ships in the tarball. So as long
    as everything we want is in the stock cache, no network is needed
    to come up with sshd + DHCP + NTP wired up. No first-boot script,
    no over-the-network apk fetch dance — the package set just IS at
    the end of the first boot.

    Stock Alpine RPi tarball (verified) ships: openssh-server,
    openssh-server-common-openrc, dhcpcd, dhcpcd-openrc, chrony,
    chrony-openrc, wpa_supplicant, wpa_supplicant-openrc, iw,
    ifupdown-ng-wifi, ca-certificates-bundle. NOT shipped: avahi,
    dbus, linux-firmware-brcm, linux-firmware-intel — those need
    a bake-time fetch (future v0.2 work, see ROADMAP.md).

    DHCP choice: dhcpcd, not busybox udhcpc. udhcpc 1.37 + Alpine 3.21
    + Pi 5's macb driver hangs with "address family not supported".
    dhcpcd is more reliable across kernels and is what setup-alpine
    selects by default in recent releases.

    Files we lay down (paths relative to /):
      etc/hostname                    — node.hostname
      etc/hosts                       — localhost + hostname entry
      etc/timezone                    — node.timezone
      etc/ssh/sshd_config             — root-by-key only, no passwords
      root/.ssh/authorized_keys       — node.all_pubkeys
      etc/network/interfaces          — lo only (+ eth0 static path)
      etc/apk/world                   — packages init will install
      etc/apk/repositories            — local cache + upstream main/community
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
    # usage. mmcblk0 is the SD card's FAT partition, mounted at
    # /media/mmcblk0 by Alpine RPi init.
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
            b'LBU_MEDIA="mmcblk0"\n'
            b'BACKUP_LIMIT=3\n'
        ),
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
    # works for anything not in the cache. Track the matching version
    # to avoid mixed-release ABI surprises.
    members.append((
        "etc/apk/repositories",
        (
            "/media/mmcblk0/apks\n"
            "http://dl-cdn.alpinelinux.org/alpine/v3.21/main\n"
            "http://dl-cdn.alpinelinux.org/alpine/v3.21/community\n"
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
    members.append((
        "etc/apk/world",
        ("\n".join(world_pkgs) + "\n").encode(),
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

    # /etc/runlevels/default/* — symlinks into /etc/init.d/ enable
    # services at boot. The target init scripts don't exist yet when
    # the apkovl is extracted; they appear when init's `apk add`
    # installs the corresponding `*-openrc` packages from world (a few
    # lines below). By the time the `default` runlevel actually starts,
    # both symlink and target exist.
    runlevel_svcs = ["networking", "sshd", "chronyd"]
    if not node.has_static_ip:
        runlevel_svcs.append("dhcpcd")
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
