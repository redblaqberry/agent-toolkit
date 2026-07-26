"""Cryptographic attestation for approved contracts and run reports.

Structural validation proves a document is well formed; it cannot prove that
a human with authority approved it, because anyone who can edit the JSON can
set ``status: approved``. Attestation closes that gap: ``approve`` signs a
canonical digest of the whole contract with an Ed25519 private key held by
the approval authority, and ``validate`` / ``export-gate`` / ``run`` /
``report`` verify that signature against the authority's public key. Editing
any byte of the contract, or the approver names, or the transcript pin,
invalidates the signature; forging an approval requires the private key.

Trust boundary, stated plainly: this protects against anyone who does NOT
hold the signing key. It attests "the holder of key K vouches that these
named people approved this exact contract". It is a single-authority model;
per-approver identities and threshold multi-signature are future work, as is
an append-only attestation registry. The signature travels inside the
document (``metadata.approval_signature`` / the run report's ``attestation``
field) and is excluded from its own digest.

The signature is Ed25519 over the SHA-256 of a canonical JSON encoding
(sorted keys, no insignificant whitespace, UTF-8) of the document with the
signature field removed. Keys are stored as PEM.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ATTESTATION_VERSION = "attest.v1"
CONTRACT_SIGNATURE_FIELD = "approval_signature"
RUN_ATTESTATION_FIELD = "attestation"


class AttestationError(ValueError):
    """A signing or verification operation could not be completed; exit 2."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.exit_code = 2
        self.problems = problems or []


# ---------------------------------------------------------------------------
# canonical digest
# ---------------------------------------------------------------------------

def canonical_bytes(payload: dict) -> bytes:
    """Deterministic JSON encoding: sorted keys, compact separators, UTF-8.

    Two documents with the same content but different key order or whitespace
    canonicalize identically, so the signature is over meaning, not layout."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(payload: dict) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------

def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_pem, public_pem) for a fresh Ed25519 key."""
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def load_private_key(path: str | Path) -> Ed25519PrivateKey:
    try:
        data = Path(path).read_bytes()
        key = serialization.load_pem_private_key(data, password=None)
    except (OSError, ValueError) as exc:
        raise AttestationError(f"cannot load signing key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise AttestationError(f"signing key {path} is not an Ed25519 private key")
    return key


def load_public_key(path: str | Path) -> Ed25519PublicKey:
    try:
        data = Path(path).read_bytes()
        key = serialization.load_pem_public_key(data)
    except (OSError, ValueError) as exc:
        raise AttestationError(f"cannot load verification key {path}: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise AttestationError(f"verification key {path} is not an Ed25519 public key")
    return key


def public_key_fingerprint(public: Ed25519PublicKey) -> str:
    raw = public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# sign / verify a document with an in-band signature field
# ---------------------------------------------------------------------------

def _split(document: dict, field_path: list[str]) -> tuple[dict, dict | None]:
    """Return (document-without-signature, signature-or-None). ``field_path``
    is the nested key holding the signature (e.g. ["metadata", "..."])."""
    import copy

    stripped = copy.deepcopy(document)
    node = stripped
    for key in field_path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return stripped, None
    signature = node.pop(field_path[-1], None) if isinstance(node, dict) else None
    return stripped, signature


def sign_document(
    document: dict, field_path: list[str], private: Ed25519PrivateKey, kind: str
) -> dict:
    """Return a copy of ``document`` with an attestation block written at
    ``field_path``. The digest covers the document with that block removed."""
    body, _ = _split(document, field_path)
    body_digest = digest(body)
    signature = private.sign(body_digest.encode("ascii"))
    block = {
        "version": ATTESTATION_VERSION,
        "kind": kind,
        "algorithm": "ed25519",
        "public_key_fingerprint": public_key_fingerprint(private.public_key()),
        "digest_sha256": body_digest,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    import copy

    signed = copy.deepcopy(document)
    node = signed
    for key in field_path[:-1]:
        node = node.setdefault(key, {})
    node[field_path[-1]] = block
    return signed


_BLOCK_KEYS = frozenset(
    {"version", "kind", "algorithm", "public_key_fingerprint", "digest_sha256", "signature"}
)


def verify_document(
    document: dict, field_path: list[str], public: Ed25519PublicKey, kind: str
) -> None:
    """Raise AttestationError unless ``document`` carries a valid signature at
    ``field_path`` for the given public key AND the expected ``kind``.

    The attestation block sits outside its own digest, so nothing inside it is
    covered by the signature; every field the verifier relies on must therefore
    be checked here explicitly, and unknown fields are refused rather than
    carried as unauthenticated free space."""
    body, block = _split(document, field_path)
    if block is None:
        raise AttestationError("document carries no attestation to verify")
    if not isinstance(block, dict):
        raise AttestationError("attestation block is malformed")
    extra = set(block) - _BLOCK_KEYS
    missing = _BLOCK_KEYS - set(block)
    if extra or missing:
        raise AttestationError(
            "attestation block has an unexpected shape: "
            f"unknown fields {sorted(extra)}, missing fields {sorted(missing)}"
        )
    if block.get("algorithm") != "ed25519" or block.get("version") != ATTESTATION_VERSION:
        raise AttestationError(
            f"unsupported attestation: version {block.get('version')!r}, "
            f"algorithm {block.get('algorithm')!r}"
        )
    if block.get("kind") != kind:
        raise AttestationError(
            f"attestation kind {block.get('kind')!r} does not match the "
            f"expected kind {kind!r}"
        )
    fingerprint = public_key_fingerprint(public)
    if block.get("public_key_fingerprint") != fingerprint:
        raise AttestationError(
            "attestation was signed by a different key: block names "
            f"{block.get('public_key_fingerprint')!r}, verification key is "
            f"{fingerprint!r}"
        )
    body_digest = digest(body)
    if block.get("digest_sha256") != body_digest:
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
        signature = base64.b64decode(block["signature"], validate=True)
    except (ValueError, TypeError) as exc:
        raise AttestationError(f"attestation signature is not valid base64: {exc}") from exc
    try:
        public.verify(signature, body_digest.encode("ascii"))
    except InvalidSignature as exc:
        raise AttestationError(
            "attestation signature does not verify against the supplied key"
        ) from exc


# ---------------------------------------------------------------------------
# convenience wrappers for the two document kinds
# ---------------------------------------------------------------------------

CONTRACT_PATH = ["metadata", CONTRACT_SIGNATURE_FIELD]
RUN_PATH = [RUN_ATTESTATION_FIELD]


def sign_contract(raw: dict, private: Ed25519PrivateKey) -> dict:
    return sign_document(raw, CONTRACT_PATH, private, kind="deployment-contract")


def verify_contract(raw: dict, public: Ed25519PublicKey) -> None:
    verify_document(raw, CONTRACT_PATH, public, kind="deployment-contract")


def contract_has_signature(raw: dict) -> bool:
    metadata = raw.get("metadata")
    return isinstance(metadata, dict) and metadata.get(CONTRACT_SIGNATURE_FIELD) is not None


def sign_run(report: dict, private: Ed25519PrivateKey) -> dict:
    return sign_document(report, RUN_PATH, private, kind="run-report")


def verify_run(report: dict, public: Ed25519PublicKey) -> None:
    verify_document(report, RUN_PATH, public, kind="run-report")


def run_has_signature(report: dict) -> bool:
    return report.get(RUN_ATTESTATION_FIELD) is not None
