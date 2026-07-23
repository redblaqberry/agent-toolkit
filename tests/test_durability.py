"""Archival durability: can a pack sealed today be verified by a later build?

A release dossier exists to be reopened years later. That makes tool evolution a
correctness concern, not a packaging detail. Two failure modes matter and they
must be answered differently:

- The wording of an explanation changes. The verdict is untouched, so this must
  verify cleanly. Reporting it as tampering would be a false accusation against
  an honest pack, and a tool that cries wolf on its own cosmetic edits will not
  be trusted when it reports a real forgery.
- The MEANING of a rule changes. The verdict can no longer be re-derived, so the
  honest answer is to confirm integrity and state plainly that the decision was
  not rechecked, rather than to silently re-judge an old pack under new rules.
"""

from __future__ import annotations

import pytest

import revpack.binding as binding_module
import revpack.pack as pack_module
from conftest import (
    DEFECT_EVIDENCE,
    build_pack,
    read_json,
    resign_manifest,
    sealed_pack,
    write_json,
)
from revpack.canonical import file_sha256
from revpack.errors import PackError
from revpack.models import SEMANTICS_VERSION
from revpack.pack import verify


def forge_a_sealed_decision(pack, private, mutate):
    """Seal a pack, then rewrite decision.json and re-sign the manifest over it.

    The forger holds a signing key, which is the case verification has to survive
    on its own. It is also the only way left to build this pack: `seal` refuses
    to sign a decision whose semantics the signing build does not implement, so
    the mismatch can no longer arrive through an honest command and has to be
    assembled afterwards, manifest entry included.
    """
    payload = read_json(pack / "decision.json")
    mutate(payload)
    write_json(pack / "decision.json", payload)

    manifest = read_json(pack / "manifest.json")
    entry = next(e for e in manifest["entries"] if e["path"] == "decision.json")
    entry["sha256"] = file_sha256(pack / "decision.json")
    entry["bytes"] = (pack / "decision.json").stat().st_size
    resign_manifest(pack, manifest, private)


def test_cosmetic_wording_change_does_not_invalidate_a_sealed_pack(tmp_path, keys, monkeypatch):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    verify(pack, str(public))

    # A later release improves one human-readable message.
    monkeypatch.setattr(
        binding_module,
        "RUN_LEVEL_GAP",
        binding_module.RUN_LEVEL_GAP + " (see the limitations section)",
    )

    result = verify(pack, str(public))
    assert result.decision_rechecked
    assert any("recomputed and matches" in n for n in result.notes)
    assert any("explanatory text differs" in n for n in result.notes)


def test_the_verdict_itself_is_still_enforced_after_a_wording_change(tmp_path, keys, monkeypatch):
    """Guards against fixing the false positive by weakening the real check."""
    private, public = keys
    pack = tmp_path / "pack"
    build_pack(pack, evidence=DEFECT_EVIDENCE)
    payload = read_json(pack / "decision.json")
    assert payload["state"] == "NO_GO"
    payload["state"] = "GO"
    write_json(pack / "decision.json", payload)
    pack_module.seal(pack, str(private))

    monkeypatch.setattr(binding_module, "RUN_LEVEL_GAP", "totally different wording")

    with pytest.raises(PackError, match="does not follow from the sealed evidence"):
        verify(pack, str(public))


def test_a_status_change_is_still_caught_even_though_prose_is_ignored(tmp_path, keys):
    private, public = keys
    pack = tmp_path / "pack"
    build_pack(pack)
    payload = read_json(pack / "decision.json")
    # Flip one rule status and nothing else. Prose is excluded from comparison,
    # so this is the check that proves statuses are not.
    payload["rules"][0]["status"] = "FAIL"
    write_json(pack / "decision.json", payload)
    pack_module.seal(pack, str(private))
    with pytest.raises(PackError, match="does not follow"):
        verify(pack, str(public))


def test_semantics_change_verifies_integrity_but_refuses_to_reconfirm_the_verdict(
    tmp_path, keys, monkeypatch
):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)

    # A later release changes what a rule means.
    monkeypatch.setattr(pack_module, "SEMANTICS_VERSION", SEMANTICS_VERSION + 1)

    result = verify(pack, str(public))
    assert not result.decision_rechecked
    assert "integrity verified" in result.unrecheckable
    assert "NOT rechecked" in result.unrecheckable
    assert not any("recomputed and matches" in n for n in result.notes)


def test_a_forged_semantics_version_cannot_switch_recomputation_off(tmp_path, keys):
    """The durability skip must not be reachable from the forged document itself.

    Forging `state: GO` alone is caught by recomputation. Forging it together
    with a semantics_version this build does not implement used to walk straight
    past that check and verify clean, because the version that decided whether
    to recompute was read from decision.json, which is the file being forged.
    The manifest carries the version stamped by the build that actually sealed
    the pack, and the signature covers it, so the two must agree.
    """
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private, evidence=DEFECT_EVIDENCE)
    assert read_json(pack / "decision.json")["state"] == "NO_GO"

    def forge(payload):
        payload["state"] = "GO"
        payload["fail_count"] = 0
        payload["inconclusive_count"] = 0
        payload["semantics_version"] = SEMANTICS_VERSION + 1

    forge_a_sealed_decision(pack, private, forge)

    with pytest.raises(PackError, match="semantics the sealing build did not implement"):
        verify(pack, str(public))


def test_a_backdated_semantics_version_is_caught_too(tmp_path, keys):
    """The mismatch is refused in both directions, not just upward.

    Claiming an older semantics version is the same manoeuvre: it also makes the
    stored version differ from the running build, which is all the skip used to
    require.
    """
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private, evidence=DEFECT_EVIDENCE)

    def forge(payload):
        payload["state"] = "GO"
        payload["semantics_version"] = SEMANTICS_VERSION - 1

    forge_a_sealed_decision(pack, private, forge)

    with pytest.raises(PackError, match="semantics the sealing build did not implement"):
        verify(pack, str(public))


def test_verify_exits_2_when_it_could_not_recheck_the_decision(tmp_path, keys, monkeypatch):
    """Integrity confirmed is not the same result as verdict confirmed.

    Exit 0 for a pack whose verdict could not be re-derived would hand the same
    answer to `revpack verify && ship` as a full pass. Under this project's
    convention 2 is the code for "the tool could not decide", which is exactly
    what happened.
    """
    from typer.testing import CliRunner

    from revpack.cli import app

    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)

    runner = CliRunner()
    assert runner.invoke(app, ["verify", "--pack", str(pack), "--key", str(public)]).exit_code == 0

    monkeypatch.setattr(pack_module, "SEMANTICS_VERSION", SEMANTICS_VERSION + 1)
    result = runner.invoke(app, ["verify", "--pack", str(pack), "--key", str(public)])
    assert result.exit_code == 2


def test_semantics_change_still_catches_tampering(tmp_path, keys, monkeypatch):
    """Integrity checks must keep working when recomputation is skipped."""
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    target = pack / "evidence" / "state-verdict" / "SD-PAY-01.json"
    payload = read_json(target)
    payload["status"] = "fail"
    write_json(target, payload)

    monkeypatch.setattr(pack_module, "SEMANTICS_VERSION", SEMANTICS_VERSION + 1)

    with pytest.raises(PackError, match="structural verification"):
        verify(pack, str(public))


def test_pack_records_the_tool_version_that_produced_it(tmp_path, keys):
    from revpack import __version__

    private, _ = keys
    pack = sealed_pack(tmp_path / "pack", private)
    decision = read_json(pack / "decision.json")
    manifest = read_json(pack / "manifest.json")
    # The omission this project criticises in every upstream producer.
    assert decision["revpack_version"] == __version__
    assert manifest["revpack_version"] == __version__
    assert decision["semantics_version"] == SEMANTICS_VERSION


def test_tool_version_is_not_part_of_the_verdict(tmp_path):
    """A version bump alone must not change what the evidence says."""
    a = build_pack(tmp_path / "a")
    b = build_pack(tmp_path / "b")
    assert a.semantic_core() == b.semantic_core()
    # And the core excludes the fields that describe the producer rather than
    # the release.
    assert "revpack_version" not in a.semantic_core()
    assert "semantics_version" not in a.semantic_core()


def _forge_field(payload, dotted, value):
    """Set a field inside a decision.json dict by a slash path into lists/dicts."""
    node = payload
    parts = dotted.split("/")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    node[parts[-1]] = value


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(
            lambda p: _forge_field(p, "evidence_classes/0/reduce", "authoritative"),
            id="the reducer a class was judged under",
        ),
        pytest.param(
            lambda p: _forge_field(p, "evidence_classes/0/min_count", 0),
            id="the count a class had to meet",
        ),
        pytest.param(
            lambda p: _forge_field(p, "evidence_classes/0/members", ["EV-FORGED"]),
            id="which artifact was the deciding one",
        ),
        pytest.param(
            lambda p: _forge_field(
                p, "bindings/0/kind", p["bindings"][0]["kind"] + "-forged"
            ),
            id="which kind of artifact a binding tied to the release",
        ),
        pytest.param(
            lambda p: _forge_field(
                p, "traceability/declared_entries", p["traceability"]["declared_entries"] + 1
            ),
            id="how many entries the map declared",
        ),
    ],
)
def test_the_semantic_core_covers_everything_that_determines_the_verdict(tmp_path, keys, forge):
    """A pre-seal forgery of a decision-bearing field must not verify.

    Verification compares the semantic core, so any field left out of it is one a
    forger may change inside a signed pack without contradiction. None of these
    edits moves the top-level state, so before the core carried them a forged
    `reduce`, `min_count`, `members`, binding `kind`, or `declared_entries`
    recomputed equal and verified clean with the confirmed verdict's exit code.
    Each names something a reader would act on: which artifact decided, under
    which reducer, over how many entries.
    """
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    # State is untouched, so the only thing that can catch the edit is the core
    # carrying the forged field.
    before = read_json(pack / "decision.json")["state"]
    forge_a_sealed_decision(pack, private, forge)
    assert read_json(pack / "decision.json")["state"] == before

    with pytest.raises(PackError, match="does not follow from the sealed evidence"):
        verify(pack, str(public))


def test_the_semantic_core_still_ignores_pure_prose(tmp_path, keys):
    """Guards the other direction: the added fields must not re-admit wording.

    Widening the core to cover the reducer and the members must not accidentally
    fold `detail` or `findings` back in, or a cosmetic message change would once
    again read as tampering.
    """
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)

    def rewrite_prose(payload):
        payload["evidence_classes"][0]["detail"] = "a completely different explanation"
        if payload["rules"]:
            payload["rules"][0]["detail"] = "reworded"
            payload["rules"][0]["findings"] = ["a note nobody wrote before"]

    forge_a_sealed_decision(pack, private, rewrite_prose)
    result = verify(pack, str(public))
    assert result.decision_rechecked
    assert any("explanatory text differs" in n for n in result.notes)
