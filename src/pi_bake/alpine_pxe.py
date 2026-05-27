"""Alpine PXE-tree baker — output is a TFTP+HTTP boot tree, not a flashable image.

This is the v0.3.2 backend that closes the "lab recovery" use case
proven hands-on 2026-05-27 (see feature_request.md gotchas + the
working /var/lib/tftpboot/<mac>/ tree that booted the CM4 over PXE
with no SD card). Same Alpine recipe schema as the diskless and
ext4 modes; output shape is different — a per-host directory the
operator drops into their TFTP root (and serves over HTTP via the
companion nginx setup, see ngnix_setup.md).

What this produces
------------------
```
out_path/
├── bootcode.bin, start4.elf, fixup4.dat, ...   # Pi firmware (from tarball)
├── bcm*.dtb, overlays/                          # DTBs (from tarball)
├── boot/
│   ├── vmlinuz-rpi                              # kernel (from tarball)
│   ├── initramfs-rpi                            # network-aware init (from tarball)
│   └── modloop-rpi                              # squashfs (from tarball)
├── apks/<arch>/                                 # apk cache + signed APKINDEX
│   ├── APKINDEX.tar.gz   (re-signed when packages: is non-empty)
│   └── *.apk             (baseline + operator extras + deps)
├── <hostname>.apkovl.tar.gz                     # per-host config overlay
├── config.txt                                   # kernel=boot/vmlinuz-rpi etc.
├── cmdline.txt                                  # ip=dhcp + apkovl=URL + alpine_repo=URL
└── usercfg.txt                                  # operator HAT overlays
```

Operator deploys this whole subtree at
`/var/lib/tftpboot/<cm4-mac>/` (or wherever their Pi firmware looks
based on the device's EEPROM boot-order config). dnsmasq-tftp + nginx
both serve the same tree — TFTP for the firmware-fed boot files
(config.txt → kernel/initramfs/dtbs), HTTP for the URL fetches the
init script does after kernel hand-off (apkovl + apks).

Why pi-bake produces a per-host directory instead of an image:
the PXE protocol delivers files, not a disk image, so there's
nothing to dd. Operator copies the tree to the lab host's TFTP root.

Sudo requirement: NONE. PXE bake is no-root like the diskless bake.
"""
from __future__ import annotations

import logging
import shutil
import tarfile
from pathlib import Path

from pi_bake import alpine, apkfetch
from pi_bake.config import NodeConfig
from pi_bake.download import fetch

LOG = logging.getLogger("pi_bake.alpine_pxe")


def bake(
    *,
    url: str,
    node: NodeConfig,
    out_path: Path,
    pxe_server_url: str,
    alpine_branch: str = "v3.21",
    extra_packages: list[str] | None = None,
    arch: str = "aarch64",
) -> Path:
    """Build a PXE-ready boot tree for `node` at `out_path`.

    `url` — Alpine RPi tarball URL (same as the diskless backend uses).
    `pxe_server_url` — base HTTP URL the lab host serves the same tree
        at (used for `apkovl=` and `alpine_repo=` in cmdline.txt).
    `alpine_branch` — repo branch for the post-boot apk install
        (`v3.21` etc.).
    `extra_packages` — operator's `packages:`, fetched + signed into
        the staged apks tree (same machinery as the diskless backend's
        #3 init-time install path — see apkfetch.py).
    """
    out_path = Path(out_path).expanduser().resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    server = pxe_server_url.rstrip("/")
    LOG.info(
        "alpine pxe bake: hostname=%s arch=%s branch=%s out=%s server=%s",
        node.hostname, arch, alpine_branch, out_path, server,
    )

    # 1. Fetch + extract the Alpine RPi tarball into out_path. This
    # is the entire boot stack: firmware blobs, DTBs, boot/, apks/,
    # and a stock config.txt + cmdline.txt we'll overwrite at the
    # end. Same content as the diskless backend pours into a FAT
    # image; here we leave it as a normal directory tree.
    tarball = fetch(url)
    LOG.info("extracting %s into %s", tarball.name, out_path)
    with tarfile.open(tarball, "r:*") as tf:
        try:
            tf.extractall(out_path, filter="data")
        except TypeError:
            tf.extractall(out_path)

    # Python's `data` extraction filter preserves recorded modes
    # which the Alpine RPi tarball happens to ship at 0o600 for
    # some files (notably boot/initramfs-rpi). PXE clients pull
    # via dnsmasq-tftp + HTTP via nginx — BOTH run as non-kurt-cb
    # users on the lab host and can't read 600-mode files owned by
    # kurt-cb. Result: TFTP "failed sending" + nginx 403 + kernel
    # panic at userspace (no initramfs).
    #
    # Fix: chmod the entire output tree to a+rX (world-readable
    # files, world-executable dirs). Hands-on-validated 2026-05-27.
    LOG.info("normalizing tree perms to world-readable")
    for item in out_path.rglob("*"):
        if item.is_symlink():
            continue
        try:
            cur = item.stat().st_mode
            if item.is_dir():
                item.chmod(cur | 0o555)   # a+rx for dirs
            else:
                item.chmod(cur | 0o444)   # a+r for files
        except OSError as e:
            LOG.warning("chmod failed on %s: %s (continuing)", item, e)

    # 2. Bake-time apk-fetch for operator extras (if any). This
    # mirrors alpine.py's #3 init-time-install pattern: drop the
    # operator's packages + recursive deps into the apks/ tree,
    # regenerate + sign the APKINDEX, then bake the matching pubkey
    # into the apkovl so init's apk add trusts our signature.
    apk_signing_pubkey_bytes = b""
    apk_signing_pubkey_name = ""
    extras = list(extra_packages or [])
    if extras:
        LOG.info("bake-time apk-fetch: %d package(s)", len(extras))
        apk_static = apkfetch.ensure_apk_static()
        # Initramfs-extracted keys live in a sibling dir, not in the
        # PXE output tree (operator doesn't ship our scratch keys).
        keys_dir = apkfetch.extract_initramfs_keys(
            out_path, out_path.parent / f".pi-bake-{node.hostname}-keys",
        )
        apks_dir = out_path / "apks" / arch
        apkfetch.fetch_packages(
            apk_static=apk_static,
            target_arch=arch,
            alpine_branch=alpine_branch,
            packages=extras,
            out_dir=apks_dir,
            keys_dir=keys_dir,
        )
        sign_dir = out_path.parent / f".pi-bake-{node.hostname}-sign"
        privkey, pubkey, pubkey_name = apkfetch.make_signing_key(sign_dir)
        apkfetch.regen_signed_index(
            apk_static=apk_static,
            apks_dir=apks_dir,
            privkey=privkey,
            pubkey_name=pubkey_name,
        )
        apk_signing_pubkey_bytes = pubkey.read_bytes()
        apk_signing_pubkey_name = pubkey_name

    # 3. Generate the per-host apkovl. `explicit_eth0_dhcp=True` is
    # essential: in pure-PXE mode, dhcpcd-as-daemon doesn't reliably
    # bring up eth0 in time, so we want the `networking` service to
    # do it via an `auto eth0 inet dhcp` block. See feature_request.md
    # gotcha #2 from the 2026-05-27 hands-on PXE bring-up.
    apkovl_path = out_path / f"{node.hostname}.apkovl.tar.gz"
    LOG.info("generating apkovl: %s", apkovl_path.name)
    alpine._write_apkovl(
        apkovl_path, node,
        alpine_branch=alpine_branch,
        extra_packages=extras,
        apk_signing_pubkey_bytes=apk_signing_pubkey_bytes,
        apk_signing_pubkey_name=apk_signing_pubkey_name,
        explicit_eth0_dhcp=True,
    )

    # 4. Replace the tarball's shipped config.txt with our PXE-correct
    # one — kernel + initramfs paths under boot/ (the diskless tarball
    # already has them at the same paths), arm_64bit, UART on for
    # operator triage, and `include usercfg.txt` for HAT overlays.
    config_txt = (
        "# pi-bake — Alpine PXE recovery boot tree\n"
        "# Auto-generated. Operator additions go in usercfg.txt.\n"
        "\n"
        "arm_64bit=1\n"
        "enable_uart=1\n"
        "kernel=boot/vmlinuz-rpi\n"
        "initramfs boot/initramfs-rpi\n"
        "\n"
        "include usercfg.txt\n"
    )
    (out_path / "config.txt").write_text(config_txt)
    LOG.info("config.txt written")

    # 5. Write cmdline.txt — the magic line that turns PXE into pure
    # network boot.
    #   - ip=dhcp           → initramfs brings up eth0 (CONFIG_BCMGENET=y
    #                         in linux-rpi, so no module needed)
    #   - apkovl=URL        → init fetches per-host config via wget
    #   - alpine_repo=URL   → apk add (in init) fetches base system
    #
    # CRITICAL (gotcha #1, feature_request.md): alpine_repo URL must
    # NOT include the arch suffix. apk appends `/<arch>/APKINDEX.tar.gz`
    # itself. We pass `<server>/apks` — apk constructs
    # `<server>/apks/aarch64/APKINDEX.tar.gz`. Passing
    # `<server>/apks/aarch64` produces a 404 on
    # `<server>/apks/aarch64/aarch64/APKINDEX.tar.gz`.
    cmdline = (
        f"ip=dhcp "
        f"apkovl={server}/{node.hostname}.apkovl.tar.gz "
        f"alpine_repo={server}/apks "
        f"modules=loop,squashfs,sd-mod,usb-storage "
        f"console=tty1 console=serial0,115200\n"
    )
    (out_path / "cmdline.txt").write_text(cmdline)
    LOG.info("cmdline.txt: apkovl + alpine_repo at %s", server)

    # 6. usercfg.txt — operator HAT overlays (Recipe.config_txt).
    # Always present (config.txt's `include` would warn if missing).
    if node.config_txt:
        body = (
            "# pi-bake — operator-declared HAT/peripheral overlays\n"
            + "\n".join(node.config_txt) + "\n"
        )
        LOG.info("usercfg.txt: %d operator line(s)", len(node.config_txt))
    else:
        body = "# usercfg.txt — operator overrides go here\n"
    (out_path / "usercfg.txt").write_text(body)

    # 7. Clean up the scratch dirs from step 2.
    for scratch in (
        out_path.parent / f".pi-bake-{node.hostname}-keys",
        out_path.parent / f".pi-bake-{node.hostname}-sign",
    ):
        if scratch.is_dir():
            shutil.rmtree(scratch, ignore_errors=True)

    LOG.info("DONE: %s — deploy to /var/lib/tftpboot/<mac>/", out_path)
    return out_path
