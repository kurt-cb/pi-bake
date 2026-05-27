"""alpine_ext4 backend — unit tests that don't require sudo.

Bake-flow tests (losetup + mount + apk.static install) require
sudo + network. Those are gated behind PI_BAKE_INTEGRATION=1
following the test_apkfetch.py convention.
"""
from __future__ import annotations

import os

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
