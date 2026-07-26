"""Canonical encoding and attestation.

The canonical form is pinned against fixed vectors because it is the
compatibility contract with DiscoverySpec's `attest.v1`. This project
reimplements that scheme rather than depending on it, so drift here would
silently produce two incompatible signature formats in one toolchain.
"""

from __future__ import annotations

import base64

import pytest

from revpack import attest
from revpack.canonical import canonical_bytes, digest, inputs_digest, write_text_atomic
from revpack.errors import AttestationError


def test_canonical_form_sorts_keys_and_strips_whitespace():
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_canonical_form_is_insensitive_to_input_key_order():
    assert digest({"a": 1, "b": {"d": 4, "c": 3}}) == digest({"b": {"c": 3, "d": 4}, "a": 1})


def test_canonical_form_keeps_non_ascii_unescaped():
    # ensure_ascii=False, matching DiscoverySpec. Escaping would change the
    # bytes and therefore every digest taken over a document with an umlaut.
    assert canonical_bytes({"k": "Sliwinski"}) == b'{"k":"Sliwinski"}'
    assert canonical_bytes({"k": "ä"}) == '{"k":"ä"}'.encode("utf-8")


def test_digest_is_sha256_of_the_canonical_bytes():
    import hashlib

    payload = {"z": [1, 2], "a": "x"}
    assert digest(payload) == hashlib.sha256(canonical_bytes(payload)).hexdigest()


def test_inputs_digest_is_order_independent():
    a = [("b.json", "b" * 64), ("a.json", "a" * 64)]
    assert inputs_digest(a) == inputs_digest(list(reversed(a)))


def test_inputs_digest_changes_when_any_input_changes():
    base = [("a.json", "a" * 64)]
    assert inputs_digest(base) != inputs_digest([("a.json", "b" * 64)])
    assert inputs_digest(base) != inputs_digest(base + [("b.json", "b" * 64)])


def test_write_text_atomic_uses_lf_on_every_platform(tmp_path):
    # Manifest entries hash raw bytes, so a CRLF write on Windows would make a
    # pack fail verification everywhere else.
    target = tmp_path / "x.txt"
    write_text_atomic(target, "a\nb\n")
    assert target.read_bytes() == b"a\nb\n"


def test_sign_then_verify_round_trips(tmp_path):
    private_pem, public_pem = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    (tmp_path / "p.pem").write_bytes(public_pem)
    private = attest.load_private_key(tmp_path / "k.pem")
    public = attest.load_public_key(tmp_path / "p.pem")

    signed = attest.sign({"hello": "world"}, private)
    attest.verify(signed, public)


def test_signature_covers_the_document_but_not_the_block_itself(tmp_path):
    private_pem, _ = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    private = attest.load_private_key(tmp_path / "k.pem")

    signed = attest.sign({"a": 1}, private)
    # The digest is over the body with the attestation removed, so it must not
    # depend on the block's own contents.
    assert signed["attestation"]["digest_sha256"] == digest({"a": 1})


def test_modified_document_fails_verification(tmp_path):
    private_pem, public_pem = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    (tmp_path / "p.pem").write_bytes(public_pem)
    private = attest.load_private_key(tmp_path / "k.pem")
    public = attest.load_public_key(tmp_path / "p.pem")

    signed = attest.sign({"state": "NO_GO"}, private)
    signed["state"] = "GO"
    with pytest.raises(AttestationError, match="modified after signing"):
        attest.verify(signed, public)


def test_wrong_key_fails_verification(tmp_path):
    private_pem, _ = attest.generate_keypair()
    _, other_public_pem = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    (tmp_path / "other.pem").write_bytes(other_public_pem)
    private = attest.load_private_key(tmp_path / "k.pem")
    other = attest.load_public_key(tmp_path / "other.pem")

    signed = attest.sign({"a": 1}, private)
    with pytest.raises(AttestationError, match="signed by a different key"):
        attest.verify(signed, other)


def test_unknown_field_inside_the_block_is_refused(tmp_path):
    # The block sits outside its own digest, so anything inside it is
    # unauthenticated. Unknown fields are refused rather than carried as free
    # space a careless reader might trust.
    private_pem, public_pem = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    (tmp_path / "p.pem").write_bytes(public_pem)
    private = attest.load_private_key(tmp_path / "k.pem")
    public = attest.load_public_key(tmp_path / "p.pem")

    signed = attest.sign({"a": 1}, private)
    signed["attestation"]["approved_by"] = "someone who did not approve this"
    with pytest.raises(AttestationError, match="unexpected shape"):
        attest.verify(signed, public)


def test_missing_field_in_the_block_is_refused(tmp_path):
    private_pem, public_pem = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    (tmp_path / "p.pem").write_bytes(public_pem)
    private = attest.load_private_key(tmp_path / "k.pem")
    public = attest.load_public_key(tmp_path / "p.pem")

    signed = attest.sign({"a": 1}, private)
    del signed["attestation"]["kind"]
    with pytest.raises(AttestationError, match="unexpected shape"):
        attest.verify(signed, public)


def test_wrong_kind_is_refused(tmp_path):
    private_pem, public_pem = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    (tmp_path / "p.pem").write_bytes(public_pem)
    private = attest.load_private_key(tmp_path / "k.pem")
    public = attest.load_public_key(tmp_path / "p.pem")

    signed = attest.sign({"a": 1}, private, kind="traceability-map")
    with pytest.raises(AttestationError, match="does not match the expected kind"):
        attest.verify(signed, public, kind="release-manifest")


def test_missing_attestation_is_refused(tmp_path):
    _, public_pem = attest.generate_keypair()
    (tmp_path / "p.pem").write_bytes(public_pem)
    public = attest.load_public_key(tmp_path / "p.pem")
    with pytest.raises(AttestationError, match="no attestation"):
        attest.verify({"a": 1}, public)


def test_malformed_base64_signature_is_refused(tmp_path):
    private_pem, public_pem = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    (tmp_path / "p.pem").write_bytes(public_pem)
    private = attest.load_private_key(tmp_path / "k.pem")
    public = attest.load_public_key(tmp_path / "p.pem")

    signed = attest.sign({"a": 1}, private)
    signed["attestation"]["signature"] = "not!base64!"
    with pytest.raises(AttestationError, match="not valid base64"):
        attest.verify(signed, public)


def test_valid_base64_wrong_signature_is_refused(tmp_path):
    private_pem, public_pem = attest.generate_keypair()
    (tmp_path / "k.pem").write_bytes(private_pem)
    (tmp_path / "p.pem").write_bytes(public_pem)
    private = attest.load_private_key(tmp_path / "k.pem")
    public = attest.load_public_key(tmp_path / "p.pem")

    signed = attest.sign({"a": 1}, private)
    signed["attestation"]["signature"] = base64.b64encode(b"\x00" * 64).decode("ascii")
    with pytest.raises(AttestationError, match="does not verify"):
        attest.verify(signed, public)


def test_non_ed25519_key_is_rejected(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "rsa.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    with pytest.raises(AttestationError, match="not an Ed25519 private key"):
        attest.load_private_key(path)
