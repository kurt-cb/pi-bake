"""raspbian_pxe.py — backend that produces two tarballs + DEPLOY.md
for NFS-root PXE deploys of Pi OS Lite (ROADMAP #27).

These tests cover the bake-side logic that doesn't need losetup:
service-mask list completeness, cmdline.txt content, config.txt
shape, user-bake-direct logic, schema validation. The full
end-to-end bake (mount + tar + emit) needs sudo + an actual
Raspbian .img.xz and is exercised on real hardware (validated
2026-05-30 on a CM4 — see ROADMAP #27 hardware-validation note).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pi_bake.config import NodeConfig, UserConfig
from pi_bake.raspbian_pxe import (
    _MINIMAL_CONFIG_TXT,
    _ROOTFS_TAR_EXCLUDES,
    _SERVICES_TO_MASK,
    _bake_users_into_rootfs,
    _cmdline,
    _emit_deploy_md,
)


@pytest.fixture
def no_sudo_imgxz():
    """Stub out imgxz.write_file/chown for unit tests so they
    don't invoke sudo. Production behavior is exercised on real
    hardware (see ROADMAP #27 validation entry)."""
    def _write(mount_root, rel_path, content, *, mode=0o644):
        body = content.encode() if isinstance(content, str) else content
        target = mount_root / rel_path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        target.chmod(mode)
    def _chown(*a, **k):
        pass  # ownership can't be set as non-root; tests skip
    with patch("pi_bake.raspbian_pxe.imgxz.write_file", side_effect=_write), \
         patch("pi_bake.raspbian_pxe.imgxz.chown", side_effect=_chown):
        yield


# ---- service-mask list ----


def test_service_mask_list_includes_known_offenders():
    """Each of these breaks NFS-root boot in some specific way.
    The list is empirically derived from the 2026-05-30 CM4 boot
    debug session — every entry has a reason in raspbian_pxe.py's
    module docstring."""
    required = {
        "regenerate_ssh_host_keys",   # would wipe pre-baked keys
        "init_resize2fs_once",        # tries to resize nonexistent SD
        "NetworkManager",             # fights kernel ip=dhcp
        "NetworkManager-wait-online", # ditto
        "dhcpcd",                     # ditto
        "dphys-swapfile",             # swap file on slow NFS
        "rpi-eeprom-update",          # firmware-mailbox probe fails
        "userconfig",                 # interactive first-boot wizard
        "userconf-pi",                # silent variant of same
        "sshswitch",                  # redundant when ssh.service enabled
        "udisks2",                    # graphical-target leftover
    }
    actual = set(_SERVICES_TO_MASK)
    missing = required - actual
    assert not missing, f"missing service masks: {missing}"


def test_rootfs_tar_excludes_problematic_dev_nodes():
    """Unprivileged NFS server can't `tar -xpf` character device
    nodes (CAP_MKNOD denied). Modern Pi OS uses devtmpfs to
    auto-populate /dev — so excluding the static nodes at bake
    time avoids the extract errors without losing functionality."""
    expected = {
        "./dev/null", "./dev/zero", "./dev/console", "./dev/tty",
        "./dev/random", "./dev/urandom", "./dev/ptmx", "./dev/full",
    }
    assert set(_ROOTFS_TAR_EXCLUDES) == expected


# ---- cmdline.txt content ----


def test_cmdline_includes_nfs_server():
    out = _cmdline("192.168.4.2:8801:/srv/nfs/pi-bake/cm4-pxe", "")
    assert "nfsroot=192.168.4.2:8801:/srv/nfs/pi-bake/cm4-pxe" in out


def test_cmdline_includes_mount_options_when_provided():
    out = _cmdline(
        "192.168.4.2:8801:/srv/nfs/pi-bake/cm4",
        "vers=3,proto=tcp,mountport=8803,nolock",
    )
    assert "nfsroot=192.168.4.2:8801:/srv/nfs/pi-bake/cm4,vers=3,proto=tcp,mountport=8803,nolock" in out


def test_cmdline_has_required_directives():
    """Kernel must do its own DHCP (ip=dhcp) and mount NFS as
    root (root=/dev/nfs). Without these, the kernel never brings
    up the network or the rootfs."""
    out = _cmdline("server:/path", "")
    assert "ip=dhcp" in out
    assert "root=/dev/nfs" in out
    assert "rootwait" in out


def test_cmdline_lacks_sd_specific_directives():
    """No `init=/usr/lib/raspberrypi-sys-mods/firstboot` (the
    SD-resize init that would fail with no partition to resize),
    no `root=PARTUUID=...`, no `rootfstype=ext4`."""
    out = _cmdline("server:/path", "")
    assert "init=" not in out
    assert "PARTUUID" not in out
    assert "rootfstype" not in out


# ---- config.txt content ----


def test_minimal_config_txt_omits_firmware_probes():
    """GPU / camera / display / audio auto-detect lines all
    trigger Pi-firmware-mailbox property calls that fail or hang
    on a netbooting CM4. The minimal config strips them all."""
    forbidden = [
        "dtoverlay=vc4-kms-v3d", "camera_auto_detect",
        "display_auto_detect", "dtparam=audio",
    ]
    for f in forbidden:
        assert f not in _MINIMAL_CONFIG_TXT, f"unexpected: {f}"


def test_minimal_config_txt_has_required_pxe_settings():
    assert "auto_initramfs=0" in _MINIMAL_CONFIG_TXT
    assert "arm_64bit=1" in _MINIMAL_CONFIG_TXT
    assert "enable_uart=1" in _MINIMAL_CONFIG_TXT  # serial console for debug


# ---- _bake_users_into_rootfs logic ----


def _seed_minimal_rootfs(tmp_path: Path) -> Path:
    """Create a sham rootfs with the file shapes the bake-direct
    code path mutates. Skips files it doesn't need."""
    root = tmp_path / "rootfs"
    (root / "etc").mkdir(parents=True)
    (root / "etc/passwd").write_text(
        "root:x:0:0:root:/root:/bin/bash\n"
        "daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
    )
    (root / "etc/shadow").write_text(
        "root:*:19873:0:99999:7:::\n"
        "daemon:*:19873:0:99999:7:::\n"
    )
    (root / "etc/group").write_text(
        "root:x:0:\n"
        "daemon:x:1:\n"
        "sudo:x:27:\n"
        "video:x:44:\n"
        "gpio:x:997:\n"
    )
    return root


def test_bake_users_default_creates_pi_user(tmp_path, no_sudo_imgxz):
    """No `user:` in recipe → pi user gets baked with the
    standard Pi OS group set."""
    root = _seed_minimal_rootfs(tmp_path)
    node = NodeConfig(
        hostname="cm4-pxe", ssh_pubkey="ssh-ed25519 AAAAfake op@bake",
    )
    _bake_users_into_rootfs(root, node)
    passwd = (root / "etc/passwd").read_text()
    assert "pi:x:1000:1000" in passwd
    assert "/home/pi:/bin/bash" in passwd
    # supplementary groups (subset — group lines have to exist)
    group = (root / "etc/group").read_text()
    assert "sudo:x:27:pi" in group
    assert "video:x:44:pi" in group


def test_bake_users_named_user(tmp_path, no_sudo_imgxz):
    """`user:` block → that user gets created instead of pi."""
    root = _seed_minimal_rootfs(tmp_path)
    node = NodeConfig(
        hostname="cm4-pxe",
        ssh_pubkey="ssh-ed25519 AAAAtop top@host",
        users=[UserConfig(
            name="kurt", groups=["sudo"],
            shell="/bin/bash",
            authorized_keys="ssh-ed25519 AAAAkurt kurt@laptop",
        )],
    )
    _bake_users_into_rootfs(root, node)
    passwd = (root / "etc/passwd").read_text()
    assert "kurt:x:1000:1000" in passwd
    assert "/home/kurt:/bin/bash" in passwd
    # pi user NOT created
    assert "pi:x:" not in passwd


def test_bake_users_writes_authorized_keys(tmp_path, no_sudo_imgxz):
    """authorized_keys lands in /home/<name>/.ssh/ with mode 600."""
    root = _seed_minimal_rootfs(tmp_path)
    node = NodeConfig(
        hostname="cm4-pxe",
        ssh_pubkey="ssh-ed25519 AAAAtop top@host",
        users=[UserConfig(
            name="alice", groups=["sudo"],
            shell="/bin/bash",
            authorized_keys="ssh-ed25519 AAAAalice alice@x",
        )],
    )
    _bake_users_into_rootfs(root, node)
    authkeys = (root / "home/alice/.ssh/authorized_keys").read_text()
    assert "AAAAalice" in authkeys


def test_bake_users_password_hash_is_set(tmp_path, no_sudo_imgxz):
    """/etc/shadow gets a sha-512 crypted hash for each user
    (key auth means it's never used, but Pi OS sshd refuses
    accounts with truly-empty password fields)."""
    root = _seed_minimal_rootfs(tmp_path)
    node = NodeConfig(
        hostname="cm4-pxe", ssh_pubkey="ssh-ed25519 AAAA op@x",
    )
    _bake_users_into_rootfs(root, node)
    shadow = (root / "etc/shadow").read_text()
    pi_line = next(l for l in shadow.splitlines() if l.startswith("pi:"))
    parts = pi_line.split(":")
    assert parts[1].startswith("$6$"), "expected sha-512 crypt"


# ---- DEPLOY.md emission ----


def test_deploy_md_uses_incus_push_when_recipe_specifies(tmp_path):
    (tmp_path / "cm4-pxe-tftp.tar.gz").write_bytes(b"a")
    (tmp_path / "cm4-pxe-rootfs.tar.gz").write_bytes(b"a")
    _emit_deploy_md(
        out_dir=tmp_path, hostname="cm4-pxe",
        tftp_tarball=tmp_path / "cm4-pxe-tftp.tar.gz",
        rootfs_tarball=tmp_path / "cm4-pxe-rootfs.tar.gz",
        pxe_nfs_server="192.168.4.2:8801:/srv/nfs/pi-bake/cm4-pxe",
        pxe_nfs_mount_options="vers=3,nolock",
        pxe_nfs_push="incus:nfs-pi-bake",
    )
    deploy = (tmp_path / "DEPLOY.md").read_text()
    assert "incus file push" in deploy
    assert "nfs-pi-bake" in deploy
    assert "pi-bake-import-rootfs cm4-pxe" in deploy


def test_deploy_md_uses_scp_when_recipe_specifies_ssh(tmp_path):
    (tmp_path / "t.tar.gz").write_bytes(b"a")
    (tmp_path / "r.tar.gz").write_bytes(b"a")
    _emit_deploy_md(
        out_dir=tmp_path, hostname="cm4-pxe",
        tftp_tarball=tmp_path / "t.tar.gz",
        rootfs_tarball=tmp_path / "r.tar.gz",
        pxe_nfs_server="192.168.4.2:2049:/srv/nfs/cm4",
        pxe_nfs_mount_options="",
        pxe_nfs_push="ssh://root@192.168.4.2:22",
    )
    deploy = (tmp_path / "DEPLOY.md").read_text()
    assert "scp" in deploy
    assert "ssh root@192.168.4.2:22" in deploy


def test_deploy_md_falls_back_to_manual_when_no_push_specified(tmp_path):
    (tmp_path / "t.tar.gz").write_bytes(b"a")
    (tmp_path / "r.tar.gz").write_bytes(b"a")
    _emit_deploy_md(
        out_dir=tmp_path, hostname="cm4-pxe",
        tftp_tarball=tmp_path / "t.tar.gz",
        rootfs_tarball=tmp_path / "r.tar.gz",
        pxe_nfs_server="192.168.4.2:2049:/srv/nfs/cm4",
        pxe_nfs_mount_options="",
        pxe_nfs_push="",  # operator hasn't specified
    )
    deploy = (tmp_path / "DEPLOY.md").read_text()
    assert "operator-specific" in deploy
    assert "/srv/nfs/cm4" in deploy


def test_deploy_md_documents_ab_nfs_pattern(tmp_path):
    """DEPLOY.md must include the A/B-NFS recommendation —
    modifying the active slot while the Pi has it mounted
    causes stale handle errors."""
    (tmp_path / "t.tar.gz").write_bytes(b"a")
    (tmp_path / "r.tar.gz").write_bytes(b"a")
    _emit_deploy_md(
        out_dir=tmp_path, hostname="cm4",
        tftp_tarball=tmp_path / "t.tar.gz",
        rootfs_tarball=tmp_path / "r.tar.gz",
        pxe_nfs_server="x:2049:/srv/nfs/cm4",
        pxe_nfs_mount_options="",
        pxe_nfs_push="",
    )
    deploy = (tmp_path / "DEPLOY.md").read_text()
    assert "A/B" in deploy or "stale" in deploy
