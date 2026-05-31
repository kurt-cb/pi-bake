"""Raspberry Pi OS Lite PXE (NFS-root) baker — `os_mode: pxe`.

Output is NOT a flashable `.img.xz`. Instead pi-bake emits two
ready-to-deploy tarballs under `output.path/`:

  <hostname>-tftp.tar.gz    — bootloader, kernel, initramfs,
                              DTBs, cmdline.txt with nfsroot=
  <hostname>-rootfs.tar.gz  — full Pi OS rootfs with all
                              operator customization baked in
                              directly (no firstrun.sh, no SD-card
                              expectations)

Plus a `DEPLOY.md` operator hint inside `output.path/` with the
exact untar + push commands for the lab's NFS server.

## Why the two-tarball shape

SD-card bakes produce one .img.xz the operator dd's. PXE bakes
have two roles: kernel/initramfs/DTBs go on TFTP, rootfs goes
on the NFS server — different lab-side destinations. Splitting
at bake time avoids the operator disassembling an .img.xz.

## Why customization is baked direct (no firstrun.sh)

The v0.4 firstrun.sh approach edits its OWN `/boot/firmware/
cmdline.txt` to remove the `systemd.run=` directive after first
success. That works on SD because cmdline.txt is on the same FAT
partition both bake-time and at-boot. Over NFS-root, the cmdline
the bootloader reads is on the TFTP server (separate file from
the rootfs), and firstrun.sh can't reach it — so every
subsequent boot retries firstrun.sh which has self-deleted from
the rootfs, systemd's run-unit creation fails,
`systemd.run_failure_action=poweroff` fires, and the CM4 powers
off in a loop. Discovered 2026-05-30 the hard way.

For NFS-root we own the rootfs at bake time. Just edit
`/etc/passwd` / `/home/<user>/.ssh/authorized_keys` /
`/etc/hostname` / `/etc/ssh/ssh_host_*` directly. No first-boot
script needed.

## Service-mask list (Pi-OS-over-NFS-root incompatibilities)

Every one of these breaks NFS-root boot in some way; all empirically
hit during the 2026-05-30 CM4 validation:

  - regenerate_ssh_host_keys.service — would wipe pre-baked keys
  - init_resize2fs_once.service — tries to resize a nonexistent SD
  - NetworkManager.service + NetworkManager-wait-online.service
    — would fight kernel's `ip=dhcp` over eth0 and break NFS
  - dhcpcd.service — same conflict
  - dphys-swapfile.service — creates swap file on slow NFS
  - rpi-eeprom-update.service — firmware-mailbox probes that fail
  - userconfig.service + userconf-pi.service — interactive
    first-boot wizard prompts on tty1 and stalls boot
  - sshswitch.service — Pi-OS wrapper; redundant when ssh.service
    + ssh.socket are enabled directly
  - udisks2.service — graphical-target leftover

Also disabled / overridden:

  - default.target → multi-user.target (Pi OS Lite Bookworm
    defaults to graphical.target which pulls in udisks2)
  - getty@tty1.service ENABLED (Pi OS Lite has it disabled —
    userconfig autologin was supposed to take its slot)
  - ssh.socket ENABLED (Bookworm switched to socket-activated
    sshd; ssh.service alone won't start)
  - /etc/ssh/sshd_config.d/rename_user.conf REMOVED (Banner
    references userconf-pi's banner file which may not be
    reliable after userconf-pi is masked)
  - /etc/systemd/journald.conf.d/persistent.conf CREATED with
    `Storage=persistent` so future boot failures are diagnosable
    from the NFS server side (`/var/log/journal/` is the path)
  - /etc/fstab PARTUUID lines commented (no SD to mount)

## config.txt strip

Stock Pi OS config.txt enables GPU / camera / display
auto-detect which triggers Pi-firmware-mailbox property calls
that fail on a netbooting CM4 (`rpi_firmware_property_list`
errors in dmesg). Replaced with bare minimum:

  arm_64bit=1
  auto_initramfs=0
  enable_uart=1

## cmdline.txt

Stripped of `init=`, `root=PARTUUID=`, `rootfstype=ext4`,
`quiet`. Has `ip=dhcp root=/dev/nfs nfsroot=<recipe-nfs_server>`
with operator-provided mount options.

## A/B-NFS lab-side caveat

Modifying `/srv/nfs/pi-bake/<host>/` while the Pi is actively
mounting it causes stale-handle errors that can panic the
kernel. The DEPLOY.md output suggests an A/B pattern:

  /srv/nfs/pi-bake/<host>-a/   ← currently mounted
  /srv/nfs/pi-bake/<host>-b/   ← prepare new bake here
  /srv/nfs/pi-bake/<host>/     ← symlink, swapped at deploy

Pi-bake itself doesn't manage the symlink — that's lab-side
operator tooling. We just hint at it in the deploy instructions.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from pi_bake import imgxz
from pi_bake.config import NodeConfig
from pi_bake.download import fetch
from pi_bake.raspbian import (
    _BAKERS,
    _DEFAULT_BAKER_CODENAME,
    _RaspbianBakerBase,
    _detect_codename,
    _random_locked_password_hash,
)

LOG = logging.getLogger("pi_bake.raspbian_pxe")

_BOOT_PART = 1
_ROOT_PART = 2

# Service unit names to mask in the NFS-rootfs (empty unit files
# in /etc/systemd/system/<name>.service). See module docstring
# for why each one breaks NFS-root boot.
_SERVICES_TO_MASK: tuple[str, ...] = (
    "regenerate_ssh_host_keys",
    "init_resize2fs_once",
    "NetworkManager",
    "NetworkManager-wait-online",
    "dhcpcd",
    "dphys-swapfile",
    "rpi-eeprom-update",
    "userconfig",
    "userconf-pi",
    "sshswitch",
    "udisks2",
)

# Files to exclude from the rootfs tarball — character device
# nodes that fail mknod under an unprivileged NFS server (the
# typical operator-local setup). Modern Pi OS uses devtmpfs
# which auto-populates `/dev` at boot, so absence of static
# nodes is harmless.
_ROOTFS_TAR_EXCLUDES: tuple[str, ...] = (
    "./dev/null", "./dev/zero", "./dev/console", "./dev/tty",
    "./dev/random", "./dev/urandom", "./dev/ptmx", "./dev/full",
)

_MINIMAL_CONFIG_TXT = (
    "# pi-bake NFS-root - minimal config.txt\n"
    "# Stripped of GPU/camera/display/audio auto-detect which\n"
    "# trigger Pi-firmware-mailbox property calls that fail on\n"
    "# a netbooting CM4 (rpi_firmware_property_list errors).\n"
    "arm_64bit=1\n"
    "auto_initramfs=0\n"
    "enable_uart=1\n"
)


def bake(
    *,
    url: str,
    node: NodeConfig,
    out_path: Path,
    pxe_nfs_server: str,
    pxe_nfs_mount_options: str = "",
    pxe_nfs_push: str = "",
    image_size_mb: int = 0,
) -> Path:
    """Bake Raspbian PXE/NFS-root: TFTP tarball + rootfs tarball.

    Returns the output directory containing the two tarballs +
    a DEPLOY.md hint file.

    Sudo is required for losetup + mount (same as the SD baker).
    """
    del image_size_mb  # PXE bakes have no fixed size; rootfs is whatever Pi OS img is

    out_dir = Path(out_path).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    hostname = node.hostname
    tftp_tarball = out_dir / f"{hostname}-tftp.tar.gz"
    rootfs_tarball = out_dir / f"{hostname}-rootfs.tar.gz"

    # Resolve the codename so we can reuse the matching baker's
    # subclass-specific behavior if any future divergence appears.
    codename = _detect_codename(url)
    baker = _BAKERS.get(codename, _BAKERS[_DEFAULT_BAKER_CODENAME])
    LOG.info("raspbian-pxe: codename=%s baker=%s",
             codename, type(baker).__name__)

    xz_path = fetch(url)
    with tempfile.TemporaryDirectory(prefix="pi-bake-rpxe-") as td:
        td_path = Path(td)
        raw = imgxz.decompress_xz(xz_path, td_path / "raw")
        LOG.info("raw image: %s (%d MB)", raw.name, raw.stat().st_size >> 20)

        mi = imgxz.mount_image(raw, td_path / "mounts")
        try:
            boot = mi.mounts[_BOOT_PART]
            root = mi.mounts[_ROOT_PART]
            _apply_pxe_transforms(
                boot=boot, root=root, node=node, baker=baker,
                pxe_nfs_server=pxe_nfs_server,
                pxe_nfs_mount_options=pxe_nfs_mount_options,
            )
            _emit_tarballs(
                boot=boot, root=root,
                tftp_out=tftp_tarball,
                rootfs_out=rootfs_tarball,
            )
        finally:
            imgxz.unmount_image(mi)

    _emit_deploy_md(
        out_dir=out_dir, hostname=hostname,
        tftp_tarball=tftp_tarball, rootfs_tarball=rootfs_tarball,
        pxe_nfs_server=pxe_nfs_server,
        pxe_nfs_mount_options=pxe_nfs_mount_options,
        pxe_nfs_push=pxe_nfs_push,
    )

    LOG.info(
        "DONE: %s (tftp=%dMB, rootfs=%dMB)",
        out_dir,
        tftp_tarball.stat().st_size >> 20,
        rootfs_tarball.stat().st_size >> 20,
    )
    return out_dir


def _apply_pxe_transforms(
    *,
    boot: Path,
    root: Path,
    node: NodeConfig,
    baker: _RaspbianBakerBase,
    pxe_nfs_server: str,
    pxe_nfs_mount_options: str,
) -> None:
    """Mutate the mounted boot + rootfs in place to produce a
    pure-NFS-root Pi OS that boots without firstrun.sh."""
    # ---- /boot/firmware (FAT) — config.txt + cmdline.txt ----
    imgxz.write_file(boot, "config.txt",
                     _MINIMAL_CONFIG_TXT.encode(), mode=0o644)
    imgxz.write_file(
        boot, "cmdline.txt",
        _cmdline(pxe_nfs_server, pxe_nfs_mount_options).encode(),
        mode=0o644,
    )

    # ---- copy /boot/firmware contents into rootfs so running OS sees them ----
    subprocess.run(
        ["cp", "-a", str(boot) + "/.", str(root / "boot/firmware/")],
        check=True,
    )

    # ---- /etc/fstab — comment out PARTUUID= lines (no SD to mount) ----
    fstab_path = root / "etc/fstab"
    original = fstab_path.read_text()
    new = "\n".join(
        ("#" + line if line.startswith("PARTUUID=") else line)
        for line in original.splitlines()
    ) + "\n"
    imgxz.write_file(root, "etc/fstab", new, mode=0o644)

    # ---- default.target -> multi-user.target ----
    default_tgt = root / "etc/systemd/system/default.target"
    if default_tgt.exists() or default_tgt.is_symlink():
        default_tgt.unlink()
    os.symlink("/lib/systemd/system/multi-user.target", str(default_tgt))

    # ---- mask NFS-hostile services (empty unit file = masked) ----
    for svc in _SERVICES_TO_MASK:
        for tgt in ("multi-user.target.wants", "graphical.target.wants"):
            link = root / f"etc/systemd/system/{tgt}/{svc}.service"
            if link.is_symlink() or link.exists():
                link.unlink()
        (root / f"etc/systemd/system/{svc}.service").write_bytes(b"")

    # ---- enable ssh.service + ssh.socket explicitly ----
    mu_wants = root / "etc/systemd/system/multi-user.target.wants"
    mu_wants.mkdir(parents=True, exist_ok=True)
    _ensure_symlink(
        mu_wants / "ssh.service",
        "/lib/systemd/system/ssh.service",
    )
    sock_wants = root / "etc/systemd/system/sockets.target.wants"
    sock_wants.mkdir(parents=True, exist_ok=True)
    _ensure_symlink(
        sock_wants / "ssh.socket",
        "/lib/systemd/system/ssh.socket",
    )

    # ---- enable getty@tty1 (Pi OS Lite has it disabled by default) ----
    getty_wants = root / "etc/systemd/system/getty.target.wants"
    getty_wants.mkdir(parents=True, exist_ok=True)
    _ensure_symlink(
        getty_wants / "getty@tty1.service",
        "/lib/systemd/system/getty@.service",
    )

    # ---- remove /etc/ssh/sshd_config.d/rename_user.conf ----
    # references /usr/share/userconf-pi/sshd_banner which may not
    # be reliable once userconf-pi is masked; sshd refuses to start
    # if Banner points at a missing file.
    rename_conf = root / "etc/ssh/sshd_config.d/rename_user.conf"
    if rename_conf.exists():
        rename_conf.unlink()

    # ---- pre-generate SSH host keys (regen service is masked) ----
    for kt in ("rsa", "ecdsa", "ed25519"):
        keyfile = root / f"etc/ssh/ssh_host_{kt}_key"
        if not keyfile.exists():
            subprocess.run(
                ["ssh-keygen", "-q", "-N", "", "-t", kt,
                 "-f", str(keyfile)],
                check=True,
            )

    # ---- persistent journal (dir + config) ----
    (root / "var/log/journal").mkdir(parents=True, exist_ok=True)
    journald_dropin = root / "etc/systemd/journald.conf.d"
    journald_dropin.mkdir(parents=True, exist_ok=True)
    imgxz.write_file(
        root, "etc/systemd/journald.conf.d/persistent.conf",
        b"[Journal]\nStorage=persistent\n", mode=0o644,
    )

    # ---- delete firstrun.sh — customization is baked DIRECT below ----
    firstrun = root / "boot/firmware/firstrun.sh"
    if firstrun.exists():
        firstrun.unlink()

    # ---- bake operator customization DIRECT into rootfs ----
    # hostname
    imgxz.write_file(root, "etc/hostname",
                     f"{node.hostname}\n".encode(), mode=0o644)
    # /etc/hosts: rewrite the 127.0.1.1 line
    hosts_path = root / "etc/hosts"
    hosts_content = hosts_path.read_text() if hosts_path.exists() else ""
    new_hosts = "\n".join(
        line for line in hosts_content.splitlines()
        if not line.strip().startswith("127.0.1.1")
    ) + f"\n127.0.1.1\t{node.hostname}\n"
    imgxz.write_file(root, "etc/hosts", new_hosts, mode=0o644)

    # users: bake operator-named users (or default pi user if no
    # `user:` block) directly into /etc/passwd + /etc/shadow +
    # /home/<name>/.ssh/authorized_keys. The standard SD-bake
    # firstrun.sh approach doesn't work over NFS — see module
    # docstring.
    _bake_users_into_rootfs(root, node)

    # also drop operator key into /root/.ssh/ as recovery hatch
    pi_keys = node.authorized_keys_text()
    imgxz.write_file(root, "root/.ssh/authorized_keys",
                     pi_keys.encode(), mode=0o600)
    imgxz.chown(root, "root/.ssh", uid=0, gid=0)
    imgxz.chown(root, "root/.ssh/authorized_keys", uid=0, gid=0)


def _bake_users_into_rootfs(root: Path, node: NodeConfig) -> None:
    """Create operator users directly in /etc/passwd + /etc/shadow.

    For PXE-root we can't use firstrun.sh (see module docstring).
    Pi OS images ship with no 'pi' user (userconf-pi creates it
    on SD boot). We do the equivalent at bake time:
      - useradd-style entries in /etc/passwd
      - locked random password hash in /etc/shadow
      - home dir with .ssh/authorized_keys

    Backwards-compat: if `node.users` is empty, create `pi` user
    with the same group set the legacy raspbian.py firstrun.sh
    uses, matching SD-bake behavior.
    """
    from pi_bake.config import UserConfig

    if node.users:
        users = list(node.users)
    else:
        users = [UserConfig(
            name="pi",
            groups=["sudo", "video", "audio", "plugdev", "users",
                    "games", "input", "netdev", "gpio", "i2c", "spi"],
            shell="/bin/bash",
            authorized_keys=node.authorized_keys_text(),
        )]

    # One locked-random hash for all users (key auth only; the
    # password is never actually used).
    pwd_hash = _random_locked_password_hash()

    # Read existing /etc/passwd + /etc/shadow + /etc/group
    passwd_path = root / "etc/passwd"
    shadow_path = root / "etc/shadow"
    group_path = root / "etc/group"
    passwd = passwd_path.read_text() if passwd_path.exists() else ""
    shadow = shadow_path.read_text() if shadow_path.exists() else ""
    group = group_path.read_text() if group_path.exists() else ""

    # Allocate uids starting at 1000 (skip existing entries)
    used_uids = set()
    for line in passwd.splitlines():
        parts = line.split(":")
        if len(parts) >= 3 and parts[2].isdigit():
            used_uids.add(int(parts[2]))
    next_uid = max([uid for uid in used_uids if uid >= 1000] or [999]) + 1

    new_passwd_lines = passwd.splitlines()
    new_shadow_lines = shadow.splitlines()
    new_group_lines = group.splitlines()

    for u in users:
        # Skip if user already exists (shouldn't happen with stock
        # Pi OS image which has no 'pi' user, but defensive).
        if any(line.startswith(f"{u.name}:") for line in new_passwd_lines):
            continue
        uid = next_uid
        next_uid += 1
        home = f"/home/{u.name}"
        # /etc/passwd: name:x:uid:gid:gecos:home:shell
        new_passwd_lines.append(
            f"{u.name}:x:{uid}:{uid}:,,,:{home}:{u.shell}"
        )
        # /etc/shadow: name:hash:lastchange:min:max:warn:inactive:expire:flag
        # lastchange = 19873 (days since epoch ~= 2024-05) is fine; not
        # meaningful for key auth.
        new_shadow_lines.append(
            f"{u.name}:{pwd_hash}:19873:0:99999:7:::"
        )
        # Primary group
        new_group_lines.append(f"{u.name}:x:{uid}:")
        # Supplementary groups — add user to each existing group line
        wanted = set(u.groups)
        for i, gline in enumerate(new_group_lines):
            parts = gline.split(":")
            if len(parts) < 4 or parts[0] not in wanted:
                continue
            members = parts[3].split(",") if parts[3] else []
            if u.name not in members:
                members.append(u.name)
                parts[3] = ",".join(m for m in members if m)
                new_group_lines[i] = ":".join(parts)

        # Home dir + .ssh + authorized_keys
        (root / home.lstrip("/")).mkdir(parents=True, exist_ok=True)
        (root / home.lstrip("/") / ".ssh").mkdir(parents=True, exist_ok=True)
        imgxz.write_file(
            root, f"{home.lstrip('/')}/.ssh/authorized_keys",
            u.authorized_keys.encode(), mode=0o600,
        )
        imgxz.chown(root, home.lstrip("/"), uid=uid, gid=uid)
        imgxz.chown(root, f"{home.lstrip('/')}/.ssh", uid=uid, gid=uid)
        imgxz.chown(
            root, f"{home.lstrip('/')}/.ssh/authorized_keys",
            uid=uid, gid=uid,
        )

    imgxz.write_file(root, "etc/passwd",
                     ("\n".join(new_passwd_lines) + "\n").encode(),
                     mode=0o644)
    imgxz.write_file(root, "etc/shadow",
                     ("\n".join(new_shadow_lines) + "\n").encode(),
                     mode=0o640)
    imgxz.chown(root, "etc/shadow", uid=0, gid=42)  # shadow group on Debian
    imgxz.write_file(root, "etc/group",
                     ("\n".join(new_group_lines) + "\n").encode(),
                     mode=0o644)


def _ensure_symlink(link_path: Path, target: str) -> None:
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    os.symlink(target, str(link_path))


def _cmdline(nfs_server: str, mount_options: str) -> str:
    """Build cmdline.txt for NFS-root boot.

    `nfs_server` is the operator's `pxe.nfs_server` field —
    format `host[:port]:path`. `mount_options` is the operator's
    `pxe.nfs_mount_options` (optional, e.g. `vers=3,proto=tcp,
    mountport=8803,nolock`).
    """
    nfsroot = nfs_server
    if mount_options:
        nfsroot = f"{nfs_server},{mount_options}"
    return (
        "console=serial0,115200 console=tty1 ip=dhcp "
        f"root=/dev/nfs nfsroot={nfsroot} "
        "rw rootwait fsck.repair=yes\n"
    )


def _emit_tarballs(
    *, boot: Path, root: Path, tftp_out: Path, rootfs_out: Path,
) -> None:
    """Bundle /boot/firmware and / into the two output tarballs."""
    LOG.info("building %s", tftp_out.name)
    subprocess.run(
        ["tar", "-C", str(boot), "-czf", str(tftp_out), "."],
        check=True,
    )
    LOG.info("building %s (~1.5GB)", rootfs_out.name)
    cmd = [
        "tar", "-C", str(root), "-czf", str(rootfs_out),
        *[f"--exclude={ex}" for ex in _ROOTFS_TAR_EXCLUDES],
        ".",
    ]
    subprocess.run(cmd, check=True)


def _emit_deploy_md(
    *,
    out_dir: Path,
    hostname: str,
    tftp_tarball: Path,
    rootfs_tarball: Path,
    pxe_nfs_server: str,
    pxe_nfs_mount_options: str,
    pxe_nfs_push: str,
) -> None:
    """Write a DEPLOY.md alongside the tarballs with the exact
    operator commands to deploy to the lab TFTP + NFS server."""
    # Parse nfs_server: host:port:path
    parts = pxe_nfs_server.split(":")
    if len(parts) == 3:
        host, port, nfs_path = parts
    elif len(parts) == 2:
        host, nfs_path = parts
        port = "2049"
    else:
        host = port = nfs_path = "?"

    if pxe_nfs_push.startswith("incus:"):
        container = pxe_nfs_push.split(":", 1)[1]
        push_cmds = (
            f"incus file push {rootfs_tarball.name} {container}/tmp/\n"
            f"incus exec {container} -- \\\n"
            f"  /usr/local/bin/pi-bake-import-rootfs "
            f"{hostname} /tmp/{rootfs_tarball.name}"
        )
    elif pxe_nfs_push.startswith("ssh://"):
        # ssh://user@host:port — strip the scheme + parse
        target = pxe_nfs_push[len("ssh://"):]
        if "/" in target:
            target = target.split("/")[0]
        push_cmds = (
            f"scp {rootfs_tarball.name} {target}:/tmp/\n"
            f"ssh {target} \\\n"
            f"  pi-bake-import-rootfs {hostname} /tmp/{rootfs_tarball.name}"
        )
    else:
        push_cmds = (
            f"# operator-specific: push {rootfs_tarball.name} to the NFS\n"
            f"# server, then extract into {nfs_path} as root.\n"
            f"# Example using a local NFS server:\n"
            f"#   sudo tar -xpf {rootfs_tarball.name} -C {nfs_path}"
        )

    deploy = f"""# Deploy: {hostname}

Pi-bake produced two tarballs:

  - `{tftp_tarball.name}` ({tftp_tarball.stat().st_size >> 20} MB)
    TFTP tree — kernel + initramfs + DTBs + cmdline.txt (NFS-root)
  - `{rootfs_tarball.name}` ({rootfs_tarball.stat().st_size >> 20} MB)
    Root filesystem — Pi OS Bookworm with all customization baked in

## 1. Deploy the TFTP tree

On the lab TFTP server, into the Pi's MAC-named subdirectory:

```bash
TFTP=/var/lib/tftpboot/<pi-mac>      # e.g. /var/lib/tftpboot/88-a2-9e-44-31-f3
find $TFTP -mindepth 1 -delete       # clean (parent dir owned by you)
tar -xzf {tftp_tarball.name} -C $TFTP/
```

## 2. Push the rootfs to the NFS server

```bash
{push_cmds}
```

## 3. Power-cycle the Pi

The Pi will:
  - PXE TFTP (kernel + DTB + cmdline)
  - Kernel ip=dhcp brings up eth0
  - Mounts NFS root at `{nfs_path}`
  - systemd starts → multi-user.target → ssh.service + getty@tty1
  - SSH reachable; HDMI console login prompt up

## NFS endpoint baked into cmdline

```
nfsroot={pxe_nfs_server}{',' + pxe_nfs_mount_options if pxe_nfs_mount_options else ''}
```

## A/B-NFS pattern (recommended)

Modifying `{nfs_path}` while the Pi has it mounted causes stale
file handle errors that can panic the kernel. Use distinct slot
directories + atomic redeploy:

  `{nfs_path}-a/`   ← currently mounted (active)
  `{nfs_path}-b/`   ← prepare new bake here
  `{nfs_path}/`     ← symlink, swap at deploy

Re-bake to the inactive slot, swap the symlink, power-cycle.
Old slot stays as rollback target.
"""
    (out_dir / "DEPLOY.md").write_text(deploy)
    LOG.info("wrote %s", out_dir / "DEPLOY.md")
