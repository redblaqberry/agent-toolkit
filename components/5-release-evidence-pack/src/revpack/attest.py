"""Ed25519 attestation, byte-compatible with DiscoverySpec's ``attest.v1``.

The scheme: Ed25519 over the ASCII bytes of the SHA-256 hex digest of the
canonical JSON encoding of the document with the attestation field removed.
Keys are PEM. The block carries the algorithm, a public-key fingerprint, the
digest, and the signature.

This is a deliberate reimplementation rather than a dependency on
``discoveryspec``, so the two repositories install independently. The cost of
that choice is drift, so ``tests/test_canonical_attest.py`` pins the canonical
form and the digest construction against fixed vectors. Two incompatible signature
formats across one toolchain would be a finding, not a feature.

Trust boundary, stated plainly: this proves that the holder of key K vouched
for this exact byte content. It does not prove who ran the producing tools, and
it is not a qualified electronic signature under eIDAS. Single authority, no
threshold signing, no registry, no trusted timestamp.
"""

from __future__ import annotations

import base64
import copy
import hashlib
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import digest
from .errors import AttestationError

ATTESTATION_VERSION = "attest.v1"
ATTESTATION_FIELD = "attestation"
MANIFEST_KIND = "release-manifest"

_BLOCK_KEYS = frozenset(
    {"version", "kind", "algorithm", "public_key_fingerprint", "digest_sha256", "signature"}
)


def generate_keypair() -> tuple[bytes, bytes]:
    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ),
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ),
    )


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError) as exc:
        raise AttestationError(f"cannot load signing key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise AttestationError(f"signing key {path} is not an Ed25519 private key")
    return key


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise AttestationError(f"cannot load verification key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AttestationError(f"verification key {path} is not an Ed25519 public key")
    return key


def public_key_fingerprint(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def body_digest(document: dict[str, Any]) -> str:
    """Digest of the document with its attestation field removed."""
    body = copy.deepcopy(document)
    body.pop(ATTESTATION_FIELD, None)
    return digest(body)


def sign(document: dict[str, Any], private: Ed25519PrivateKey, kind: str = MANIFEST_KIND) -> dict:
    """Return a copy of ``document`` carrying an attestation block."""
    computed = body_digest(document)
    signature = private.sign(computed.encode("ascii"))
    signed = copy.deepcopy(document)
    signed[ATTESTATION_FIELD] = {
        "version": ATTESTATION_VERSION,
        "kind": kind,
        "algorithm": "ed25519",
        "public_key_fingerprint": public_key_fingerprint(private.public_key()),
        "digest_sha256": computed,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return signed


def verify(
    document: dict[str, Any], public: Ed25519PublicKey, kind: str = MANIFEST_KIND
) -> None:
    """Raise ``AttestationError`` unless the document carries a valid signature.

    The attestation block sits outside its own digest, so nothing inside it is
    covered by the signature. Every field the verifier relies on is therefore
    checked explicitly here, and unknown fields are refused rather than carried
    as unauthenticated free space where an attacker could park data a careless
    reader might trust.
    """
    block = document.get(ATTESTATION_FIELD)
    if block is None:
        raise AttestationError("document carries no attestation to verify")
    if not isinstance(block, dict):
        raise AttestationError("attestation block is malformed")

    extra = set(block) - _BLOCK_KEYS
    missing = _BLOCK_KEYS - set(block)
    if extra or missing:
        raise AttestationError(
            "attestation block has an unexpected shape: unknown fields "
            f"{sorted(extra)}, missing fields {sorted(missing)}"
        )
    if block["algorithm"] != "ed25519" or block["version"] != ATTESTATION_VERSION:
        raise AttestationError(
            f"unsupported attestation: version {block['version']!r}, "
            f"algorithm {block['algorithm']!r}"
        )
    if block["kind"] != kind:
        raise AttestationError(
            f"attestation kind {block['kind']!r} does not match the expected kind {kind!r}"
        )

    fingerprint = public_key_fingerprint(public)
    if block["public_key_fingerprint"] != fingerprint:
        raise AttestationError(
            f"attestation was signed by a different key: block names "
            f"{block['public_key_fingerprint']!r}, verification key is {fingerprint!r}"
        )

    computed = body_digest(document)
    if block["digest_sha256"] != computed:
        raise AttestationError(
            "document digest does not match the attestation; the document was "
            "modified after signing"
        )
    if not isinstance(block["signature"], str):
        raise AttestationError(
            f"attestation signature must be a base64 string, got "
            f"{type(block['signature']).__name__}"
        )
    try:
        raw_signature = base64.b64decode(block["signature"], validate=True)
    except (ValueError, TypeError) as exc:
        raise AttestationError(f"attestation signature is not valid base64: {exc}") from exc
    try:
        public.verify(raw_signature, computed.encode("ascii"))
    except InvalidSignature as exc:
        raise AttestationError(
            "attestation signature does not verify against the supplied key"
        ) from exc
