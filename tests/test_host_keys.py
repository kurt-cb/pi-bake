"""Deterministic ed25519 host-key derivation (ssh_host_key: usehost / seed:...)."""

from __future__ import annotations

import base64
import shutil
import subprocess

import pytest

from pi_bake.host_keys import (
    derive_ed25519_keypair,
    derive_ed25519_pem,
    derive_seed,
    resolve_host_key_spec,
)


def _has_ssh_keygen() -> bool:
    return shutil.which("ssh-keygen") is not None


# ---------- seed derivation: pure-stdlib, no system deps ----------


def test_derive_seed_is_32_bytes():
    assert len(derive_seed("foo")) == 32


def test_derive_seed_is_deterministic():
    assert derive_seed("td-pi5-1") == derive_seed("td-pi5-1")


def test_derive_seed_differs_per_input():
    assert derive_seed("td-pi5-1") != derive_seed("td-pi5-2")


def test_derive_seed_is_locked_for_v1():
    """KDF output is part of the on-disk contract — changing the
    salt or hash rotates every previously baked deterministic
    host key. Lock a known vector so accidental tweaks blow up
    loudly. Bumping requires a version-suffix change to the salt
    ("v1" -> "v2") and a release note."""
    assert derive_seed("td-pi5-1").hex() == (
        "6063def27f64e8b625d08050f13cceb749d180e86886182fe0399ab69c70f321"
    )


def test_derive_pem_starts_with_pkcs8_header():
    pem = derive_ed25519_pem("foo")
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----\n")
    assert pem.rstrip().endswith(b"-----END PRIVATE KEY-----")


def test_derive_pem_decodes_to_48_bytes_with_ed25519_oid():
    """PKCS#8 ed25519 with raw seed = 16-byte header + 32-byte seed = 48."""
    pem = derive_ed25519_pem("foo")
    b64_body = b"".join(
        line for line in pem.splitlines()
        if line and not line.startswith(b"-----")
    )
    der = base64.b64decode(b64_body)
    assert len(der) == 48
    # OID 1.3.101.112 (id-Ed25519): tag 06, len 03, value 2b 65 70
    assert der[7:12] == bytes.fromhex("06032b6570")


def test_derive_pem_is_deterministic():
    assert derive_ed25519_pem("td-pi5-1") == derive_ed25519_pem("td-pi5-1")


# ---------- keypair derivation: needs ssh-keygen on PATH ----------


@pytest.mark.skipif(not _has_ssh_keygen(), reason="ssh-keygen not installed")
def test_derive_keypair_is_deterministic():
    p1, k1 = derive_ed25519_keypair("td-pi5-1")
    p2, k2 = derive_ed25519_keypair("td-pi5-1")
    assert p1 == p2
    assert k1 == k2


@pytest.mark.skipif(not _has_ssh_keygen(), reason="ssh-keygen not installed")
def test_derive_keypair_pub_is_ssh_ed25519_format():
    _, pub = derive_ed25519_keypair("td-pi5-1")
    fields = pub.split()
    assert fields[0] == b"ssh-ed25519"
    # base64-decoded blob has 0x00 0x00 0x00 0x0b "ssh-ed25519" prefix
    blob = base64.b64decode(fields[1])
    assert blob[:4] == b"\x00\x00\x00\x0b"
    assert blob[4:15] == b"ssh-ed25519"


@pytest.mark.skipif(not _has_ssh_keygen(), reason="ssh-keygen not installed")
def test_derive_keypair_different_seeds_different_pubs():
    _, pub1 = derive_ed25519_keypair("td-pi5-1")
    _, pub2 = derive_ed25519_keypair("td-pi5-2")
    # Compare just the base64 blob, ignore comment differences
    assert pub1.split()[1] != pub2.split()[1]


@pytest.mark.skipif(not _has_ssh_keygen(), reason="ssh-keygen not installed")
def test_derive_keypair_priv_is_valid_openssh_input():
    """sshd reads PKCS#8 PEM ed25519 host keys natively. Round-trip
    through ssh-keygen -y to confirm our PEM is well-formed."""
    priv, expected_pub = derive_ed25519_keypair("td-pi5-1")
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "k")
        with open(f, "wb") as fh:
            fh.write(priv)
        os.chmod(f, 0o600)
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", f],
            check=True, capture_output=True,
        )
    # Our derive_ed25519_keypair adds an explicit comment; bare
    # ssh-keygen -y output has no comment. Compare the key blob.
    assert result.stdout.split()[1] == expected_pub.split()[1]


# ---------- resolve_host_key_spec: dispatching the three forms ----------


def test_resolve_empty_returns_none():
    assert resolve_host_key_spec("", "td-pi5-1") is None


@pytest.mark.skipif(not _has_ssh_keygen(), reason="ssh-keygen not installed")
def test_resolve_usehost_uses_hostname_as_seed():
    a = resolve_host_key_spec("usehost", "td-pi5-1")
    b = derive_ed25519_keypair("td-pi5-1", comment="root@td-pi5-1")
    assert a == b


@pytest.mark.skipif(not _has_ssh_keygen(), reason="ssh-keygen not installed")
def test_resolve_usehost_changes_with_hostname():
    a = resolve_host_key_spec("usehost", "td-pi5-1")
    b = resolve_host_key_spec("usehost", "td-pi5-2")
    assert a[1].split()[1] != b[1].split()[1]


@pytest.mark.skipif(not _has_ssh_keygen(), reason="ssh-keygen not installed")
def test_resolve_seed_prefix_uses_literal_seed():
    a = resolve_host_key_spec("seed:fleet-A", "td-pi5-1")
    b = resolve_host_key_spec("seed:fleet-A", "td-pi5-2")
    # Same seed string -> same pubkey blob, even across hostnames.
    assert a[1].split()[1] == b[1].split()[1]


def test_resolve_seed_prefix_empty_raises():
    with pytest.raises(ValueError, match="non-empty seed"):
        resolve_host_key_spec("seed:", "td-pi5-1")


def test_resolve_missing_file_path_raises(tmp_path):
    with pytest.raises(ValueError, match="path not found"):
        resolve_host_key_spec(str(tmp_path / "nope"), "td-pi5-1")


def test_resolve_path_without_pub_raises(tmp_path):
    priv = tmp_path / "k"
    priv.write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n")
    with pytest.raises(ValueError, match="public key not found"):
        resolve_host_key_spec(str(priv), "td-pi5-1")


def test_resolve_path_form_reads_both_files(tmp_path):
    priv = tmp_path / "k"
    priv.write_bytes(b"PRIV-PLACEHOLDER")
    (tmp_path / "k.pub").write_bytes(b"ssh-ed25519 AAA test\n")
    result = resolve_host_key_spec(str(priv), "td-pi5-1")
    assert result == (b"PRIV-PLACEHOLDER", b"ssh-ed25519 AAA test\n")
