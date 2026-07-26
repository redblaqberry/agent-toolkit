"""The keygen command: the signed workflow has to be reachable from the CLI.

Everything else in the attestation story is only usable if an operator can
produce a key pair without writing Python, so these tests check the round trip
(keygen -> approve --signing-key -> validate --verify-key) and the refusals
that protect a key already in use.
"""

import json
import os
import shutil
import sys

import pytest
from typer.testing import CliRunner

from discoveryspec.attest import load_private_key, load_public_key, public_key_fingerprint
from discoveryspec.cli import app
from tests.conftest import APPROVED_PATH, TRANSCRIPT_PATH

runner = CliRunner()


def combined(result) -> str:
    return result.output + (result.stderr or "")


@pytest.fixture()
def workspace(tmp_path):
    """A fully resolved draft next to its transcript, ready to be approved."""
    shutil.copyfile(TRANSCRIPT_PATH, tmp_path / "transcript.md")
    approved = json.loads(APPROVED_PATH.read_text(encoding="utf-8"))
    approved["metadata"].update(
        status="draft", approved_by=None, approved_at=None, transcript_sha256=None
    )
    (tmp_path / "resolved.json").write_text(
        json.dumps(approved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return tmp_path


def test_keygen_writes_a_usable_pair(tmp_path):
    result = runner.invoke(app, ["keygen", "--out", str(tmp_path / "approval-key")])
    assert result.exit_code == 0, combined(result)

    private_path = tmp_path / "approval-key.pem"
    public_path = tmp_path / "approval-key.pub"
    assert private_path.exists() and public_path.exists()

    private = load_private_key(private_path)
    public = load_public_key(public_path)
    assert public_key_fingerprint(private.public_key()) == public_key_fingerprint(public)
    assert public_key_fingerprint(public) in result.output


def test_keygen_pair_signs_and_verifies_end_to_end(workspace):
    ws = workspace
    assert runner.invoke(app, ["keygen", "--out", str(ws / "key")]).exit_code == 0

    approved = runner.invoke(app, [
        "approve", "--contract", str(ws / "resolved.json"),
        "--by", "Anna Lindqvist (Operations)", "--date", "2026-07-16",
        "--out", str(ws / "approved.json"), "--signing-key", str(ws / "key.pem"),
    ])
    assert approved.exit_code == 0, combined(approved)

    verified = runner.invoke(app, [
        "validate", "--contract", str(ws / "approved.json"),
        "--require-approved", "--verify-key", str(ws / "key.pub"),
    ])
    assert verified.exit_code == 0, combined(verified)
    assert "signature: VERIFIED" in verified.output


def test_keygen_refuses_to_replace_an_existing_key(tmp_path):
    first = runner.invoke(app, ["keygen", "--out", str(tmp_path / "key")])
    assert first.exit_code == 0, combined(first)
    original = (tmp_path / "key.pem").read_bytes()

    second = runner.invoke(app, ["keygen", "--out", str(tmp_path / "key")])
    assert second.exit_code == 2
    assert "stop verifying" in combined(second)
    assert (tmp_path / "key.pem").read_bytes() == original


def test_keygen_force_replaces_the_pair(tmp_path):
    assert runner.invoke(app, ["keygen", "--out", str(tmp_path / "key")]).exit_code == 0
    original = (tmp_path / "key.pem").read_bytes()

    forced = runner.invoke(app, ["keygen", "--out", str(tmp_path / "key"), "--force"])
    assert forced.exit_code == 0, combined(forced)
    assert (tmp_path / "key.pem").read_bytes() != original
    private = load_private_key(tmp_path / "key.pem")
    public = load_public_key(tmp_path / "key.pub")
    assert public_key_fingerprint(private.public_key()) == public_key_fingerprint(public)


def test_keygen_creates_the_private_key_owner_only(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX file modes are not enforced on Windows")
    assert runner.invoke(app, ["keygen", "--out", str(tmp_path / "key")]).exit_code == 0
    mode = os.stat(tmp_path / "key.pem").st_mode & 0o777
    assert mode == 0o600, oct(mode)


def test_keygen_leaves_no_private_key_when_the_public_half_fails(tmp_path, monkeypatch):
    """A private key whose public half is missing cannot be verified against,
    so a half-written pair must not survive."""
    from pathlib import Path

    real_write_bytes = Path.write_bytes

    def fail_on_public(self, data):
        if self.suffix == ".pub":
            raise OSError("disk full")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail_on_public)
    result = runner.invoke(app, ["keygen", "--out", str(tmp_path / "key")])

    assert result.exit_code == 2
    assert not (tmp_path / "key.pem").exists()
    assert not (tmp_path / "key.pub").exists()
