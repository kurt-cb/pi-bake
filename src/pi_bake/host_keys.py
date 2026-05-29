"""Derive deterministic ed25519 SSH host keys from a seed string.

Same seed -> identical (priv, pub) bytes, byte-for-byte, across
bake hosts. Lets the operator say `ssh_host_key: usehost` and
get a stable per-hostname SSH identity without managing a
separate key file.

***SECURITY WARNING — TESTING / LABS ONLY***

The `usehost` and `seed:<string>` sentinel forms produce host
keys that are PREDICTABLE from public information (the hostname,
or whatever seed string the operator put in version-controlled
YAML). Anyone who can guess the seed can compute the same
private key offline and use it to impersonate the device or
silently MITM SSH sessions. There is no cryptographic secrecy
in a key derived from a label.

These forms exist to make lab + CI setups convenient — they
defeat the `known_hosts` "REMOTE HOST IDENTIFICATION HAS
CHANGED" warning across reflashes without an operator-managed
keystore. **Do not use them for production / WAN-exposed
devices.** For real deployments, use the file-path form with
a per-host ed25519 keypair generated from `/dev/urandom`.

Implementation: hash the seed with SHA-256 + a domain-separation
salt -> 32 bytes -> wrap as a PKCS#8-encoded ed25519 OneAsymmetricKey
(RFC 8410 sec 7). Shell out to ssh-keygen -y to derive the
public key. OpenSSH's sshd reads PKCS#8 PEM host keys natively;
no format conversion needed.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path

LOG = logging.getLogger("pi_bake.host_keys")

_INSECURE_WARNING = (
    "ssh_host_key=%s derives a PREDICTABLE host key from %s — "
    "NOT a SECURE option, use for testing and labs only. "
    "Anyone who can guess the seed can compute this private key "
    "offline and impersonate / MITM the device. For production, "
    "use ssh_host_key: <path-to-real-keypair>."
)

# RFC 8410 sec 7 — PKCS#8 PrivateKeyInfo for an ed25519 key with
# a 32-byte raw seed. Constant ASN.1 header; only the seed varies.
#   SEQUENCE (46 bytes)
#     INTEGER 0                           (version)
#     SEQUENCE (5 bytes)                  (AlgorithmIdentifier)
#       OID 1.3.101.112                   (id-Ed25519)
#     OCTET STRING (34 bytes)             (privateKey)
#       OCTET STRING (32 bytes)             (CurvePrivateKey)
#         <seed>
_PKCS8_ED25519_HEADER = bytes.fromhex("302e020100300506032b657004220420")

# Domain-separation salt — bumping the version invalidates all
# previously derived keys. Bake-tested keys depend on this string,
# so do NOT change it without a major-version bump.
_KDF_SALT = b"pi-bake-host-key-v1\x00"


def derive_seed(seed_input: str) -> bytes:
    """Return the 32-byte ed25519 seed for `seed_input` (deterministic)."""
    return hashlib.sha256(_KDF_SALT + seed_input.encode("utf-8")).digest()


def derive_ed25519_pem(seed_input: str) -> bytes:
    """Return PKCS#8 PEM-encoded ed25519 private key for `seed_input`."""
    seed = derive_seed(seed_input)
    der = _PKCS8_ED25519_HEADER + seed
    b64 = base64.encodebytes(der).decode("ascii").strip()
    return (
        b"-----BEGIN PRIVATE KEY-----\n"
        + b64.encode("ascii") + b"\n"
        + b"-----END PRIVATE KEY-----\n"
    )


def derive_ed25519_keypair(
    seed_input: str, comment: str = "",
) -> tuple[bytes, bytes]:
    """Return (priv_pem_bytes, pub_openssh_bytes).

    priv_pem_bytes: PKCS#8 PEM. sshd reads this natively.
    pub_openssh_bytes: `ssh-ed25519 AAAA... <comment>\\n`.
    """
    priv = derive_ed25519_pem(seed_input)
    with tempfile.TemporaryDirectory(prefix="pibake-hostkey-") as td:
        keyfile = Path(td) / "k"
        keyfile.write_bytes(priv)
        os.chmod(keyfile, 0o600)
        proc = subprocess.run(
            ["ssh-keygen", "-y", "-f", str(keyfile)],
            check=True, capture_output=True,
        )
    pub = proc.stdout.strip()
    if comment:
        pub = pub + b" " + comment.encode("utf-8")
    return priv, pub + b"\n"


def resolve_host_key_spec(
    spec: str, hostname: str,
) -> tuple[bytes, bytes] | None:
    """Resolve a `ssh_host_key:` recipe value to (priv, pub) or None.

    Accepted forms:
      ""             -> None (caller auto-generates a random pair)
      "usehost"      -> derive ed25519 from `hostname`
      "seed:<str>"   -> derive ed25519 from `<str>` literal
      <path>         -> read priv from path, pub from `<path>.pub`

    Path form preserves the v0.2 behavior. The new sentinel forms
    are deterministic — same seed input -> same key, across bake
    hosts, with no cache file to keep.
    """
    if not spec:
        return None
    if spec == "usehost":
        LOG.warning(_INSECURE_WARNING, "usehost", f"hostname {hostname!r}")
        return derive_ed25519_keypair(hostname, comment=f"root@{hostname}")
    if spec.startswith("seed:"):
        seed_input = spec[len("seed:"):]
        if not seed_input:
            raise ValueError(
                "ssh_host_key: 'seed:' prefix requires a non-empty seed"
            )
        LOG.warning(_INSECURE_WARNING, f"seed:{seed_input!r}", "literal seed")
        return derive_ed25519_keypair(seed_input, comment=f"root@{hostname}")
    priv_path = Path(spec).expanduser()
    if not priv_path.is_file():
        raise ValueError(
            f"ssh_host_key path not found: {priv_path} "
            f"(value must be a file path, 'usehost', or 'seed:<string>')"
        )
    pub_path = Path(str(priv_path) + ".pub")
    if not pub_path.is_file():
        raise ValueError(
            f"ssh_host_key public key not found: {pub_path} "
            f"(expected at <ssh_host_key>.pub)"
        )
    return priv_path.read_bytes(), pub_path.read_bytes()
