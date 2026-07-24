"""Cryptographic attestation: signing closes the forge-by-editing-JSON holes.

Structural validation cannot prove authentic approval; an Ed25519 signature
over the canonical contract (and run-report) digest can. These tests cover the
core sign/verify behavior and the two adversarial forgeries from the audit:
a hand-edited approved contract, and a failing run flipped to PASS."""

import copy
import hashlib
import json

import pytest

from discoveryspec import (
    AttestationError,
    generate_keypair,
    sign_contract,
    sign_run,
    verify_contract,
    verify_run,
)
from discoveryspec.attest import (
    contract_has_signature,
    digest,
    load_private_key,
    load_public_key,
    run_has_signature,
)


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("keys")
    private_pem, public_pem = generate_keypair()
    (d / "key.pem").write_bytes(private_pem)
    (d / "pub.pem").write_bytes(public_pem)
    return d


@pytest.fixture(scope="module")
def priv(keys):
    return load_private_key(keys / "key.pem")


@pytest.fixture(scope="module")
def pub(keys):
    return load_public_key(keys / "pub.pem")


CONTRACT = {
    "contract_version": "deployment-contract.v2",
    "metadata": {"project": "p", "customer": "c", "status": "approved",
                 "approved_by": "Anna, Jonas, Priya", "approved_at": "2026-07-16",
                 "transcript_sha256": "ab" * 32},
    "requirements": [{"id": "REQ-001", "title": "t"}],
}


def test_sign_verify_round_trip(priv, pub):
    signed = sign_contract(CONTRACT, priv)
    assert contract_has_signature(signed)
    verify_contract(signed, pub)  # does not raise


def test_digest_is_key_order_independent():
    a = {"x": 1, "y": {"a": 1, "b": 2}}
    b = {"y": {"b": 2, "a": 1}, "x": 1}
    assert digest(a) == digest(b)


def test_editing_the_approver_breaks_the_signature(priv, pub):
    signed = sign_contract(CONTRACT, priv)
    signed["metadata"]["approved_by"] = "Mallory"
    with pytest.raises(AttestationError) as excinfo:
        verify_contract(signed, pub)
    assert "modified after signing" in str(excinfo.value)


def test_editing_a_requirement_breaks_the_signature(priv, pub):
    signed = sign_contract(CONTRACT, priv)
    signed["requirements"][0]["title"] = "tampered"
    with pytest.raises(AttestationError):
        verify_contract(signed, pub)


def test_a_different_key_is_rejected(priv):
    other_private_pem, other_public_pem = generate_keypair()
    from cryptography.hazmat.primitives import serialization

    other_public = serialization.load_pem_public_key(other_public_pem)
    signed = sign_contract(CONTRACT, priv)
    with pytest.raises(AttestationError) as excinfo:
        verify_contract(signed, other_public)
    assert "different key" in str(excinfo.value)


def test_unsigned_contract_has_nothing_to_verify(pub):
    with pytest.raises(AttestationError) as excinfo:
        verify_contract(CONTRACT, pub)
    assert "no attestation" in str(excinfo.value)


def test_run_report_sign_verify_and_forgery(priv, pub):
    report = {"verdict": "FAIL", "passed": 9, "total": 10, "scenarios": []}
    signed = sign_run(report, priv)
    assert run_has_signature(signed)
    verify_run(signed, pub)
    forged = copy.deepcopy(signed)
    forged["verdict"] = "PASS"
    forged["passed"] = 10
    with pytest.raises(AttestationError):
        verify_run(forged, pub)


def test_signature_excludes_itself_from_the_digest(priv, pub):
    # re-signing an already-signed document must still verify (the old
    # signature field is stripped before the new digest is computed)
    signed_once = sign_contract(CONTRACT, priv)
    signed_twice = sign_contract(signed_once, priv)
    verify_contract(signed_twice, pub)


def test_flipped_kind_is_rejected(priv, pub):
    # kind lives outside the digest, so it must be validated explicitly: a
    # contract attestation relabeled as a run-report attestation (or vice
    # versa) is refused instead of silently accepted
    signed = sign_contract(CONTRACT, priv)
    signed["metadata"]["approval_signature"]["kind"] = "run-report"
    with pytest.raises(AttestationError) as excinfo:
        verify_contract(signed, pub)
    assert "kind" in str(excinfo.value)


def test_extra_attestation_fields_are_rejected(priv, pub):
    # the block is excluded from its own digest; any extra field inside it
    # would be unauthenticated free space, so unknown fields are refused
    signed = sign_contract(CONTRACT, priv)
    signed["metadata"]["approval_signature"]["note"] = "looks legit"
    with pytest.raises(AttestationError) as excinfo:
        verify_contract(signed, pub)
    assert "unexpected shape" in str(excinfo.value)


def test_contract_attestation_cannot_verify_as_run_report(priv, pub):
    # cross-kind replay: pasting a signed contract's block onto a run report
    # fails on the digest, and the kind check catches the relabeling path
    report = {"verdict": "PASS", "scenarios": []}
    signed_report = sign_run(report, priv)
    signed_report["attestation"]["kind"] = "deployment-contract"
    with pytest.raises(AttestationError):
        verify_run(signed_report, pub)


def test_non_string_signature_value_is_a_refusal_not_a_crash(priv, pub):
    # a JSON signature value of [] or 7 must be a controlled AttestationError
    # (exit 2 at the CLI), never an uncaught TypeError
    for bad in ([], 7, {}, None):
        signed = sign_run({"verdict": "PASS", "scenarios": []}, priv)
        signed["attestation"]["signature"] = bad
        with pytest.raises(AttestationError):
            verify_run(signed, pub)
