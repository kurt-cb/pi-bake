"""alpine_ext4 backend — unit tests that don't require sudo.

Bake-flow tests (losetup + mount + apk.static install) require
sudo + network. Those are gated behind PI_BAKE_INTEGRATION=1
following the test_apkfetch.py convention.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from pi_bake import alpine_ext4


# --------------------------------------------------------------------------- #
# Package set                                                                  #
# --------------------------------------------------------------------------- #


def test_baseline_packages_include_kernel_and_bootloader():
    """The two essentials for a bootable Pi sys-mode image."""
    assert "linux-rpi" in alpine_ext4._BASELINE_PACKAGES
    assert "raspberrypi-bootloader" in alpine_ext4._BASELINE_PACKAGES


def test_baseline_packages_include_sshd_and_sftp():
    """CLAUDE.md lesson #5: openssh-server doesn't ship sftp-server;
    must add openssh-sftp-server explicitly or scp fails."""
    assert "openssh-server" in alpine_ext4._BASELINE_PACKAGES
    assert "openssh-sftp-server" in alpine_ext4._BASELINE_PACKAGES


def test_baseline_packages_use_dhcpcd_not_udhcpc():
    """CLAUDE.md lesson #7: dhcpcd over udhcpc — udhcpc 1.37 +
    Pi 5 macb driver is broken."""
    assert "dhcpcd" in alpine_ext4._BASELINE_PACKAGES
    assert "udhcpc" not in alpine_ext4._BASELINE_PACKAGES


def test_baseline_packages_include_openrc():
    """init system."""
    assert "openrc" in alpine_ext4._BASELINE_PACKAGES


def test_baseline_packages_include_chrony():
    """Time sync — Alpine baseline."""
    assert "chrony" in alpine_ext4._BASELINE_PACKAGES
    assert "chrony-openrc" in alpine_ext4._BASELINE_PACKAGES


def test_wifi_packages_include_brcm_firmware():
    """Pi's onboard radios are Broadcom — without firmware, no
    wifi on Pi 3/4/5/Zero W."""
    assert "linux-firmware-brcm" in alpine_ext4._WIFI_PACKAGES
    assert "wpa_supplicant" in alpine_ext4._WIFI_PACKAGES
    assert "wpa_supplicant-openrc" in alpine_ext4._WIFI_PACKAGES


# --------------------------------------------------------------------------- #
# Runlevel layout                                                              #
# --------------------------------------------------------------------------- #


def test_runlevels_include_sshd_in_default():
    """sshd must come up at boot — that's the whole point of
    pi-bake's first-boot UX."""
    assert "sshd" in alpine_ext4._RUNLEVELS["default"]


def test_runlevels_include_firstboot_fix_in_default():
    """The one-shot service that finalizes --no-scripts triggers."""
    assert "pi-bake-firstboot-fix" in alpine_ext4._RUNLEVELS["default"]


def test_runlevels_include_dhcpcd_in_default():
    assert "dhcpcd" in alpine_ext4._RUNLEVELS["default"]


def test_runlevels_include_modules_in_boot():
    """Kernel module force-load via /etc/modules — handled by the
    openrc `modules` service in the `boot` runlevel."""
    assert "modules" in alpine_ext4._RUNLEVELS["boot"]


def test_runlevels_include_hostname_in_boot():
    assert "hostname" in alpine_ext4._RUNLEVELS["boot"]


# --------------------------------------------------------------------------- #
# First-boot fix-up script                                                     #
# --------------------------------------------------------------------------- #


def test_firstboot_script_is_openrc_shebang():
    """The script lands in /etc/init.d/, which expects openrc-run."""
    assert alpine_ext4._FIRSTBOOT_FIX_SCRIPT.startswith("#!/sbin/openrc-run")


def test_firstboot_script_self_removes_on_success():
    """Idempotency: must rc-update del itself after running once,
    otherwise it'd re-run on every boot."""
    assert "rc-update del pi-bake-firstboot-fix" in (
        alpine_ext4._FIRSTBOOT_FIX_SCRIPT
    )


def test_firstboot_script_uses_no_network_apk_fix():
    """Bake-time fetched .apks are already on disk; first boot must
    not require network."""
    assert "apk fix --no-network" in alpine_ext4._FIRSTBOOT_FIX_SCRIPT


def test_firstboot_script_creates_done_marker():
    """If the script ever re-runs (e.g. rc-update del failed),
    the done-marker check short-circuits cleanly."""
    assert "/var/lib/pi-bake/firstboot.done" in (
        alpine_ext4._FIRSTBOOT_FIX_SCRIPT
    )


# --------------------------------------------------------------------------- #
# bake() argument validation (no sudo, no network)                             #
# --------------------------------------------------------------------------- #


def test_bake_rejects_image_size_too_small(tmp_path):
    """Image must fit FAT /boot + at least a minimal ext4 root."""
    from pi_bake.config import NodeConfig
    node = NodeConfig(
        hostname="t",
        ssh_pubkey="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItesting x",
    )
    with pytest.raises(ValueError, match="too small"):
        alpine_ext4.bake(
            node=node, out_path=tmp_path / "out.img.xz",
            image_size_mb=100,
        )


# --------------------------------------------------------------------------- #
# busybox symlink replication (regression — 2026-05-27 boot failure)            #
#                                                                              #
# Bug: `apk add --no-scripts` skipped busybox's post-install hook              #
# (`busybox --install -s`), so /sbin/init etc. weren't created.                #
# Kernel boots, mounts root, then panics "Cannot find init".                   #
# --------------------------------------------------------------------------- #


def test_install_busybox_symlinks_creates_sbin_init(tmp_path):
    """The critical regression: /sbin/init must be a symlink to
    /bin/busybox after _install_busybox_symlinks runs."""
    # Build a fake root that looks like what apk add --no-scripts
    # produces: /bin/busybox exists + /etc/busybox-paths.d/busybox
    # manifest lists the symlink paths.
    root = tmp_path / "rootfs"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"fake-busybox-binary")
    (root / "etc" / "busybox-paths.d").mkdir(parents=True)
    manifest = root / "etc" / "busybox-paths.d" / "busybox"
    # Minimal but realistic — these are the most critical applets.
    manifest.write_text(
        "busybox\n"           # already exists, should skip
        "sbin/init\n"         # THE one — without this kernel panics
        "bin/sh\n"
        "sbin/reboot\n"
        "sbin/poweroff\n"
        "\n"                   # blank line, should skip
        "# comment\n"          # comment, should skip
        "usr/bin/awk\n"
    )

    alpine_ext4._install_busybox_symlinks(root)

    # The critical one: /sbin/init exists + points at /bin/busybox.
    sbin_init = root / "sbin" / "init"
    assert sbin_init.is_symlink(), \
        "/sbin/init must be a symlink (regression — 2026-05-27 boot failure)"
    assert sbin_init.readlink() == Path("/bin/busybox")
    # Other applets follow the same pattern.
    assert (root / "bin" / "sh").is_symlink()
    assert (root / "sbin" / "reboot").is_symlink()
    assert (root / "sbin" / "poweroff").is_symlink()
    assert (root / "usr" / "bin" / "awk").is_symlink()
    # The existing /bin/busybox is NOT replaced.
    assert not (root / "bin" / "busybox").is_symlink()
    assert (root / "bin" / "busybox").read_bytes() == b"fake-busybox-binary"


def test_install_busybox_symlinks_creates_parent_dirs(tmp_path):
    """Applet paths like usr/sbin/X land in dirs the bootstrap may
    not have created — _install_busybox_symlinks mkdirs as needed."""
    root = tmp_path / "rootfs"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"x")
    (root / "etc" / "busybox-paths.d").mkdir(parents=True)
    # sbin/init is required by the function's final sanity check;
    # the test's main interest is the unusual nested path.
    (root / "etc" / "busybox-paths.d" / "busybox").write_text(
        "sbin/init\n"
        "usr/sbin/totally-new-dir/applet\n"
    )

    alpine_ext4._install_busybox_symlinks(root)

    target = root / "usr" / "sbin" / "totally-new-dir" / "applet"
    assert target.is_symlink()
    # Parent dir got mkdir -p'd as needed.
    assert (root / "usr" / "sbin" / "totally-new-dir").is_dir()


def test_install_busybox_symlinks_idempotent(tmp_path):
    """Re-running shouldn't barf if the symlinks already exist
    (e.g. partial bake retry)."""
    import os
    root = tmp_path / "rootfs"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"x")
    (root / "etc" / "busybox-paths.d").mkdir(parents=True)
    (root / "etc" / "busybox-paths.d" / "busybox").write_text("sbin/init\n")
    # Pre-create the symlink so the second run sees it exists.
    (root / "sbin").mkdir()
    os.symlink("/bin/busybox", root / "sbin" / "init")

    # Should not raise.
    alpine_ext4._install_busybox_symlinks(root)

    # Symlink still points where we expect.
    assert (root / "sbin" / "init").readlink() == Path("/bin/busybox")


def test_install_busybox_symlinks_fails_without_manifest(tmp_path):
    """Missing manifest = something went wrong with the apk bootstrap;
    bail loudly rather than producing a broken image."""
    root = tmp_path / "rootfs"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"x")
    # No /etc/busybox-paths.d/busybox.
    with pytest.raises(RuntimeError, match="busybox manifest missing"):
        alpine_ext4._install_busybox_symlinks(root)


def test_install_busybox_symlinks_fails_if_sbin_init_not_in_manifest(tmp_path):
    """If the manifest somehow ships without /sbin/init listed, the
    boot would still panic. Sanity-check at the end of the function
    catches that before we produce a doomed image."""
    root = tmp_path / "rootfs"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"x")
    (root / "etc" / "busybox-paths.d").mkdir(parents=True)
    # No /sbin/init in the manifest.
    (root / "etc" / "busybox-paths.d" / "busybox").write_text("bin/sh\n")
    with pytest.raises(RuntimeError, match="/sbin/init still missing"):
        alpine_ext4._install_busybox_symlinks(root)


def test_install_busybox_symlinks_routes_through_sudo_when_mount_unwritable(
    tmp_path, monkeypatch,
):
    """Regression for the 2026-05-31 bug: when alpine_ext4 ran as a
    non-root operator with sudo, native pathlib `symlink_to` /
    `mkdir` calls hit PermissionError on the root-owned mount because
    `_install_busybox_symlinks` didn't route through `imgxz._sudo`
    like the rest of the module. Now it must detect non-writable
    mounts and batch every write through one `sudo sh -c` shell-out
    (one call, not one-per-symlink — ~300 applets).

    We fake the non-root case by pinning os.access to return False
    for the mount root, then assert imgxz._sudo got the consolidated
    sh -c command containing every expected `ln -s` and a
    `mkdir -p` per unique parent.
    """
    root = tmp_path / "rootfs"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "busybox").write_bytes(b"x")
    (root / "etc" / "busybox-paths.d").mkdir(parents=True)
    (root / "etc" / "busybox-paths.d" / "busybox").write_text(
        "sbin/init\n"
        "bin/sh\n"
        "usr/sbin/halt\n"
    )
    # Pre-create sbin/init under the mount so the final sanity check
    # passes (we're faking "the sudo'd ln succeeded" without actually
    # running sudo). bin/sh + usr/sbin/halt land in the batched script
    # that the recorder captures.
    (root / "sbin").mkdir()
    (root / "sbin" / "init").symlink_to("/bin/busybox")

    # Force the non-root code path: pretend the mount isn't writable.
    real_access = os.access
    def fake_access(path, mode):
        if Path(path) == root and mode == os.W_OK:
            return False
        return real_access(path, mode)
    monkeypatch.setattr(alpine_ext4.os, "access", fake_access)

    captured: list[tuple] = []
    def fake_sudo(*args, **kwargs):
        captured.append((args, kwargs))
        return None
    monkeypatch.setattr(alpine_ext4.imgxz, "_sudo", fake_sudo)

    alpine_ext4._install_busybox_symlinks(root)

    # Exactly one sudo invocation — batched, not one-per-symlink.
    assert len(captured) == 1, \
        f"expected 1 batched sudo call, got {len(captured)}"
    args, kwargs = captured[0]
    assert args[0] == "sh", "must run via sh"
    assert args[1] == "-c"
    script = args[2]
    # /sbin/init was pre-created (skipped), so it shouldn't reappear.
    assert "sbin/init" not in script
    # The two paths that needed writing both show up.
    assert "ln -s /bin/busybox" in script
    assert f"{root}/bin/sh" in script
    assert f"{root}/usr/sbin/halt" in script
    # mkdir -p must be present for unique parents not yet existing.
    assert "mkdir -p" in script
    # set -e — fail fast inside the batched script.
    assert script.startswith("set -e")
