"""Pack lifecycle, release binding, decision precedence, and tamper boundaries.

The boundary that matters most is the last one: a pack whose decision.json was
forged before sealing is cryptographically perfect. Every hash matches and the
signature verifies, because nothing changed after signing. Only recomputing the
decision from the sealed evidence catches it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from conftest import (
    CLEAN,
    CLEAN_EVIDENCE,
    DEFECT,
    DEFECT_EVIDENCE,
    assert_problem,
    build_pack,
    load_yaml,
    read_json,
    resign_manifest,
    sealed_pack,
    write_json,
    write_yaml,
)
import revpack.pack as pack_module
from revpack.errors import PackError
from revpack.lattice import Status
from revpack.pack import compute_decision, seal, verify


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_clean_evidence_yields_go(tmp_path):
    decision = build_pack(tmp_path / "pack")
    assert decision.state == "GO"


def test_sealed_clean_pack_verifies(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    result = verify(pack, str(public))
    assert any("recomputed and matches" in n for n in result.notes)
    assert result.decision_rechecked


def test_manifest_indexes_every_file_except_itself(tmp_path, keys):
    private, _ = keys
    pack = sealed_pack(tmp_path / "pack", private)
    manifest = read_json(pack / "manifest.json")
    indexed = {e["path"] for e in manifest["entries"]}
    on_disk = {
        str(p.relative_to(pack)).replace("\\", "/") for p in pack.rglob("*") if p.is_file()
    }
    # The manifest cannot contain a hash of bytes that include that hash, so it
    # excludes itself by construction and is covered by its own attestation.
    assert on_disk - indexed == {"manifest.json"}
    assert indexed - on_disk == set()


def test_decision_is_deterministic(tmp_path):
    a = build_pack(tmp_path / "a")
    b = build_pack(tmp_path / "b")
    assert a.model_dump(mode="json") == b.model_dump(mode="json")
    assert (tmp_path / "a" / "decision.json").read_bytes() == (
        tmp_path / "b" / "decision.json"
    ).read_bytes()


def test_evidence_is_copied_byte_identically(tmp_path):
    build_pack(tmp_path / "pack")
    original = (CLEAN / "gate-run" / "run.json").read_bytes()
    copied = (tmp_path / "pack" / "evidence" / "gate-run" / "run.json").read_bytes()
    # Never rewritten: altering evidence would break any producer's own embedded
    # signature and make the pack's hashes describe something else.
    assert copied == original


def test_collect_refuses_a_non_empty_pack_directory(tmp_path):
    out = tmp_path / "pack"
    build_pack(out)
    with pytest.raises(PackError, match="already exists and is not empty"):
        build_pack(out)


# ---------------------------------------------------------------------------
# defects
# ---------------------------------------------------------------------------


def test_defect_evidence_yields_no_go(tmp_path):
    decision = build_pack(tmp_path / "pack", evidence=DEFECT_EVIDENCE)
    assert decision.state == "NO_GO"


def test_fixture_substitution_fails_binding(tmp_path):
    decision = build_pack(tmp_path / "pack", evidence=DEFECT_EVIDENCE)
    binding = next(b for b in decision.bindings if b.check == "scenario_id_matches_trajectory")
    assert binding.status is Status.FAIL
    assert "TASK-10" in binding.detail


def test_traceability_conflict_is_reported(tmp_path):
    # The real defect: the canary consumer map and statediff disagree about
    # which requirement TASK-10 exercises, and both ids exist in the contract,
    # so referential integrity catches nothing.
    decision = build_pack(tmp_path / "pack", evidence=DEFECT_EVIDENCE)
    rule = next(r for r in decision.rules if r.kind == "traceability_conflict")
    assert rule.status is Status.FAIL
    assert any("TASK-10" in f and "REQ-009" in f for f in rule.findings)


def test_golden_mismatch_drives_the_class_to_fail(tmp_path):
    decision = build_pack(tmp_path / "pack", evidence=DEFECT_EVIDENCE)
    outcome = next(c for c in decision.evidence_classes if c.kind == "silobench-verify")
    assert outcome.status is Status.FAIL


def test_scenario_without_assertions_is_inconclusive_not_failing(tmp_path):
    decision = build_pack(tmp_path / "pack", evidence=DEFECT_EVIDENCE)
    rule = next(r for r in decision.rules if r.kind == "assertion_density")
    assert rule.status is Status.INCONCLUSIVE
    assert any("TASK-12" in f for f in rule.findings)


def test_replay_cannot_evidence_a_latency_slo(tmp_path):
    decision = build_pack(tmp_path / "pack", evidence=DEFECT_EVIDENCE)
    rule = next(r for r in decision.rules if r.kind == "slo")
    assert rule.status is Status.INCONCLUSIVE
    assert any("replay" in f for f in rule.findings)


def test_declared_no_change_contradicted_by_the_report_fails_binding(tmp_path):
    decision = build_pack(tmp_path / "pack", evidence=DEFECT_EVIDENCE)
    binding = next(b for b in decision.bindings if b.check == "contract_fingerprints")
    assert binding.status is Status.FAIL


# ---------------------------------------------------------------------------
# release binding
# ---------------------------------------------------------------------------


def test_contract_from_another_release_fails_binding(tmp_path):
    release = load_yaml(CLEAN / "release.yaml")
    release["contract_sha256"] = "f" * 64
    path = write_yaml(tmp_path / "release.yaml", release)
    decision = build_pack(tmp_path / "pack", release=path)
    assert decision.state == "NO_GO"
    assert any(b.check == "contract_sha256" and b.status is Status.FAIL for b in decision.bindings)


def test_state_verdict_from_another_environment_fails_binding(tmp_path):
    release = load_yaml(CLEAN / "release.yaml")
    release["environment"]["initial_state_hash"] = "e" * 64
    path = write_yaml(tmp_path / "release.yaml", release)
    decision = build_pack(tmp_path / "pack", release=path)
    assert any(
        b.check == "before_state_hash" and b.status is Status.FAIL for b in decision.bindings
    )


def test_state_verdict_from_another_schema_fails_binding(tmp_path):
    # A matching before-state hash proves a common seed, not a common schema: six
    # of the twelve golden hashes are identical, so a schema-2 verdict can share a
    # schema-1 release's initial hash and bind on that alone. The schema the
    # verdict declares in artifacts.schema_version was never compared.
    verdict = read_json(CLEAN / "state-verdict" / "SD-PAY-01.json")
    verdict["artifacts"]["schema_version"] = 2
    path = tmp_path / "SD-PAY-01.json"
    write_json(path, verdict)
    evidence = dict(CLEAN_EVIDENCE)
    evidence["state-verdict"] = path
    decision = build_pack(tmp_path / "pack", evidence=evidence)
    assert any(
        b.check == "schema_version" and b.status is Status.FAIL for b in decision.bindings
    )
    assert decision.state == "NO_GO"


def test_stale_traceability_map_fails_binding(tmp_path):
    # A map authored against a different contract carries requirement ids whose
    # meaning may have shifted underneath it.
    trace = load_yaml(CLEAN / "traceability.yaml")
    trace["contract_sha256"] = "d" * 64
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)
    binding = next(b for b in decision.bindings if b.kind == "traceability-map")
    assert binding.status is Status.FAIL
    assert decision.state == "NO_GO"


def test_binding_reports_the_run_level_gap_honestly(tmp_path):
    decision = build_pack(tmp_path / "pack")
    binding = next(b for b in decision.bindings if b.check == "before_state_hash")
    assert binding.status is Status.PASS
    # Matching hashes prove a common seeded environment, not that this verdict
    # graded this gate run. Claiming more would be the overclaim the project
    # exists to catch.
    assert "not that this verdict graded this gate run" in binding.detail


def test_dropping_a_declared_silobench_task_loses_its_coverage(tmp_path):
    """The silobench-verify class must cover every task the map declares.

    Without a must_cover projection the class was satisfied by count alone, so a
    two-task verification output that dropped TASK-12 still cleared on TASK-10
    while that task's golden comparison silently disappeared and the release
    stayed GO.
    """
    verify = read_json(CLEAN / "silobench-verify" / "verify.json")
    trimmed = [entry for entry in verify if entry["task_id"] != "TASK-12"]
    assert len(trimmed) == 1, "the fixture shape moved"
    path = tmp_path / "verify.json"
    write_json(path, trimmed)
    evidence = dict(CLEAN_EVIDENCE)
    evidence["silobench-verify"] = path
    decision = build_pack(tmp_path / "pack", evidence=evidence)
    klass = next(c for c in decision.evidence_classes if c.kind == "silobench-verify")
    assert "TASK-12" in klass.uncovered
    assert klass.status is Status.INCONCLUSIVE
    assert decision.state != "GO"


def test_a_failed_git_status_is_recorded_as_unknown_not_clean(tmp_path, monkeypatch):
    """rev-parse can answer while status fails, and clean is not the safe default.

    `_vcs_for` ignored the return code of `git status --porcelain`, so a failed
    status left empty stdout and recorded `dirty: false`, a positive clean-tree
    claim the command never verified. A status this build could not run is
    unknown, not clean, so the whole observation is dropped.
    """
    import types

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd:
            return types.SimpleNamespace(returncode=0, stdout="abc123def456\n")
        return types.SimpleNamespace(returncode=128, stdout="")

    monkeypatch.setattr(pack_module.subprocess, "run", fake_run)
    assert pack_module._vcs_for(tmp_path / "evidence" / "file.json") is None


# ---------------------------------------------------------------------------
# decision precedence
# ---------------------------------------------------------------------------


def test_missing_required_evidence_cannot_yield_go(tmp_path):
    evidence = dict(CLEAN_EVIDENCE)
    del evidence["state-verdict"]
    decision = build_pack(tmp_path / "pack", evidence=evidence)
    assert decision.state != "GO"
    assert decision.state == "INCOMPLETE"


def test_no_go_dominates_incomplete(tmp_path):
    # One class fails while another is missing. A proven violation is a decision
    # that can be defended; reporting "cannot tell" over it would understate it.
    evidence = dict(DEFECT_EVIDENCE)
    del evidence["state-verdict"]
    decision = build_pack(tmp_path / "pack", evidence=evidence)
    assert decision.state == "NO_GO"
    assert decision.fail_count > 0
    assert decision.inconclusive_count > 0


def test_uncovered_scenarios_prevent_go_even_when_a_member_passes(tmp_path):
    # Coverage-by-count: one passing artifact of the right kind must not satisfy
    # a class while the scenarios the release depends on have no evidence.
    trace = load_yaml(CLEAN / "traceability.yaml")
    trace["entries"].append(
        {
            "requirement": "REQ-007",
            "silobench_tasks": ["TASK-99"],
            "statediff_scenarios": [],
            "justification": "a task with no evidence in this pack",
        }
    )
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)
    outcome = next(c for c in decision.evidence_classes if c.kind == "gate-run")
    assert "TASK-99" in outcome.uncovered
    assert decision.state != "GO"


def test_requirement_with_no_traceability_entry_fails_coverage(tmp_path):
    trace = load_yaml(CLEAN / "traceability.yaml")
    trace["entries"] = [e for e in trace["entries"] if e["requirement"] != "REQ-016"]
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)
    rule = next(r for r in decision.rules if r.kind == "requirement_coverage")
    assert rule.status is Status.FAIL
    assert "REQ-016" in decision.traceability.uncovered_requirements


def test_traceability_entry_for_an_unknown_requirement_fails(tmp_path):
    trace = load_yaml(CLEAN / "traceability.yaml")
    trace["entries"].append(
        {
            "requirement": "REQ-999",
            "silobench_tasks": [],
            "statediff_scenarios": [],
            "justification": "references a requirement the contract does not contain",
        }
    )
    path = write_yaml(tmp_path / "traceability.yaml", trace)
    decision = build_pack(tmp_path / "pack", traceability=path)
    assert "REQ-999" in decision.traceability.dangling


# ---------------------------------------------------------------------------
# stage chain
# ---------------------------------------------------------------------------


def test_seal_before_decide_is_refused(tmp_path, keys):
    private, _ = keys
    build_pack(tmp_path / "pack", run_decide=False)
    with pytest.raises(PackError, match="decide"):
        seal(tmp_path / "pack", str(private))


def test_seal_refuses_a_decision_from_different_inputs(tmp_path, keys):
    private, _ = keys
    pack = tmp_path / "pack"
    build_pack(pack)
    # Swap the evidence underneath a decision that was computed from the old
    # set. A stale derived artifact must not be signable.
    (pack / "evidence" / "gate-run" / "run.json").write_bytes(
        (DEFECT / "gate-run" / "run.json").read_bytes()
    )
    with pytest.raises(PackError, match="different inputs"):
        seal(pack, str(private))


def test_seal_refuses_a_decision_from_another_semantics_version(tmp_path, keys):
    """Signing a decision this build cannot vouch for produces a dead pack.

    `seal` stamps the manifest with the semantics version of the build that
    signs, and `verify` refuses any pack whose decision declares a different one.
    So decide, upgrade across a semantics change, then seal used to produce a
    correctly signed pack that verification rejects forever and reports as an
    edited decision: a true statement about the pack and a false accusation
    against the operator. The refusal costs one comparison here and is otherwise
    discovered when the dossier is reopened.
    """
    from revpack.models import SEMANTICS_VERSION

    private, _ = keys
    pack = tmp_path / "pack"
    build_pack(pack)
    payload = read_json(pack / "decision.json")
    payload["semantics_version"] = SEMANTICS_VERSION - 1
    write_json(pack / "decision.json", payload)

    with pytest.raises(PackError, match="semantics this build does not implement"):
        seal(pack, str(private))


def test_decide_refuses_evidence_that_drifted_from_the_index(tmp_path):
    pack = tmp_path / "pack"
    build_pack(pack)
    (pack / "evidence" / "gate-run" / "run.json").write_bytes(b"{}")
    with pytest.raises(PackError, match="does not match the envelope index"):
        compute_decision(pack)


# ---------------------------------------------------------------------------
# tamper boundaries
# ---------------------------------------------------------------------------


def test_b1_tampered_evidence_fails_verification(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    target = pack / "evidence" / "state-verdict" / "SD-PAY-01.json"
    payload = read_json(target)
    payload["status"] = "fail"
    write_json(target, payload)
    with pytest.raises(PackError, match="structural verification"):
        verify(pack, str(public))


def test_b1_truncated_evidence_fails_verification(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    (pack / "evidence" / "contract" / "approved-contract.json").write_bytes(b"")
    with pytest.raises(PackError, match="structural verification"):
        verify(pack, str(public))


def test_b2_tampered_policy_fails_verification(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    policy = load_yaml(pack / "policy.yaml")
    policy["rules"] = [r for r in policy["rules"] if r["id"] != "R-008"]
    (pack / "policy.yaml").write_text(
        yaml.safe_dump(policy, sort_keys=False), encoding="utf-8", newline="\n"
    )
    with pytest.raises(PackError, match="structural verification"):
        verify(pack, str(public))


def test_b3_tampered_decision_fails_verification(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    payload = read_json(pack / "decision.json")
    payload["state"] = "NO_GO"
    write_json(pack / "decision.json", payload)
    with pytest.raises(PackError, match="structural verification"):
        verify(pack, str(public))


def test_b5_unlisted_file_fails_closure(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    (pack / "evidence" / "extra.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PackError) as exc:
        verify(pack, str(public))
    assert_problem(exc, "absent from the manifest")


def test_b5_unlisted_nested_file_fails_closure(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    nested = pack / "evidence" / "sneaky" / "deep" / "x.json"
    nested.parent.mkdir(parents=True)
    nested.write_text("{}", encoding="utf-8")
    with pytest.raises(PackError) as exc:
        verify(pack, str(public))
    assert_problem(exc, "absent from the manifest")


def test_b5_deleted_file_fails_closure(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    (pack / "evidence" / "blast-radius" / "report.json").unlink()
    with pytest.raises(PackError) as exc:
        verify(pack, str(public))
    assert_problem(exc, "absent from the pack")


def test_b6_stripped_attestation_fails(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    manifest = read_json(pack / "manifest.json")
    del manifest["attestation"]
    write_json(pack / "manifest.json", manifest)
    with pytest.raises(Exception, match="no attestation"):
        verify(pack, str(public))


def test_b6_manifest_hash_edit_fails(tmp_path, keys):
    # The manifest is signed, so editing one of its hashes is a signature fault
    # and the attestation names it precisely. The content check underneath is
    # exercised by the re-signed case below, which is the only shape of this
    # attack that gets past the signature at all.
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    manifest = read_json(pack / "manifest.json")
    manifest["entries"][0]["sha256"] = "0" * 64
    write_json(pack / "manifest.json", manifest)
    with pytest.raises(Exception, match="modified after signing"):
        verify(pack, str(public))


def test_b6_a_signed_manifest_with_a_false_hash_still_fails(tmp_path, keys):
    """A valid signature over a false claim is still a false claim."""
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    manifest = read_json(pack / "manifest.json")
    manifest["entries"][0]["sha256"] = "0" * 64
    resign_manifest(pack, manifest, private)
    with pytest.raises(PackError) as exc:
        verify(pack, str(public))
    assert_problem(exc, "does not match the manifest entry")


def test_b6_wrong_key_fails(tmp_path, keys):
    from revpack import attest

    private, _ = keys
    pack = sealed_pack(tmp_path / "pack", private)
    _, other_public = attest.generate_keypair()
    other = tmp_path / "other.pem"
    other.write_bytes(other_public)
    with pytest.raises(Exception, match="different key"):
        verify(pack, str(other))


def test_b7_unknown_field_in_attestation_fails(tmp_path, keys):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    manifest = read_json(pack / "manifest.json")
    manifest["attestation"]["responsible_person"] = "not actually signed by anyone"
    write_json(pack / "manifest.json", manifest)
    with pytest.raises(Exception, match="unexpected shape"):
        verify(pack, str(public))


# ---------------------------------------------------------------------------
# directory closure against the filesystem
# ---------------------------------------------------------------------------


def _symlink_or_skip(link, target, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # Windows needs a privilege
        pytest.skip(f"this platform does not permit creating symlinks: {exc}")


def test_a_directory_symlink_hiding_content_is_refused(tmp_path, keys):
    """The hole rglob leaves open.

    rglob does not descend into a directory symlink, so an entire subtree could
    travel inside a pack that verified clean: every file rglob could see had a
    manifest entry, every entry had a file, and closure reported the pack
    complete while unmanifested content rode along inside it.
    """
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "unmanifested.json").write_text("{}", encoding="utf-8")
    _symlink_or_skip(pack / "evidence" / "extra", hidden, directory=True)

    with pytest.raises(PackError, match="symbolic link inside the pack"):
        verify(pack, str(public))


def test_sealing_a_pack_holding_a_file_symlink_is_refused(tmp_path, keys):
    """The hole rglob leaves open in the other direction.

    is_file() follows a file symlink, so seal would hash bytes living outside
    the pack and index them as though they were in it. The target can then be
    replaced without touching anything the manifest covers, and a pack copied
    anywhere else loses the content entirely.
    """
    private, _ = keys
    pack = tmp_path / "pack"
    build_pack(pack)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    _symlink_or_skip(pack / "evidence" / "linked.json", outside)

    with pytest.raises(PackError, match="symbolic link inside the pack"):
        seal(pack, str(private))


def test_an_envelope_reaching_outside_the_pack_through_a_link_is_refused(tmp_path):
    """Why the path check cannot stay lexical.

    `evidence/gate-run/run.json` is relative, has no `..`, and hashes to exactly
    what the envelope records. Every text-level check passes. Only comparing the
    resolved path against the pack root notices that the bytes are somewhere
    else on the host.
    """
    pack = tmp_path / "pack"
    build_pack(pack)
    target = pack / "evidence" / "gate-run" / "run.json"
    outside = tmp_path / "outside.json"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    _symlink_or_skip(target, outside)

    with pytest.raises(PackError, match="outside the pack"):
        compute_decision(pack)


def test_the_symlink_refusal_is_covered_where_links_cannot_be_created(tmp_path, keys, monkeypatch):
    """Keeps the refusal tested on hosts that will not create a link.

    The three tests above need a real symlink and skip where the platform
    forbids one, which on Windows is the default. This one fakes the single
    filesystem answer the walk depends on, so the refusal is never left with no
    coverage at all on the machine the author is sitting at.
    """
    private, _ = keys
    pack = tmp_path / "pack"
    build_pack(pack)
    victim = (pack / "evidence" / "gate-run" / "run.json").resolve()
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path, "is_symlink", lambda self: self.resolve() == victim or real_is_symlink(self)
    )
    with pytest.raises(PackError, match="symbolic link inside the pack"):
        seal(pack, str(private))


def test_the_containment_check_is_covered_where_links_cannot_be_created(tmp_path, monkeypatch):
    """The same, for the resolved-path guard behind the symlink refusal."""
    pack = tmp_path / "pack"
    build_pack(pack)
    victim = pack / "evidence" / "gate-run" / "run.json"
    outside = tmp_path / "outside.json"
    real_resolve = Path.resolve

    def resolve_elsewhere(self, *args, **kwargs):
        resolved = real_resolve(self, *args, **kwargs)
        return real_resolve(outside) if resolved == real_resolve(victim) else resolved

    monkeypatch.setattr(Path, "resolve", resolve_elsewhere)
    with pytest.raises(PackError, match="outside the pack"):
        compute_decision(pack)


def test_a_filename_containing_a_backslash_is_not_aliased(tmp_path):
    """A POSIX filename may contain a literal backslash, and it is not a separator.

    `str(relpath).replace("\\", "/")` rewrote such a name onto a different path:
    a file named `we\\ird.json` was indexed as `we/ird.json`, which resolves to
    another file or to nothing, so its content check was skipped at verify time
    while directory closure still balanced. `as_posix()` rewrites only the
    separator the running platform actually uses, so the backslash survives as
    part of the name. Windows treats backslash as a separator, so the alias
    cannot arise there and the case is POSIX-only.
    """
    if os.name == "nt":
        pytest.skip("Windows treats backslash as a path separator; the alias cannot arise")
    pack = tmp_path / "pack"
    build_pack(pack)
    (pack / "evidence" / "gate-run" / "we\\ird.json").write_text("{}", encoding="utf-8")
    listed = pack_module._pack_files(pack)
    assert "evidence/gate-run/we\\ird.json" in listed
    assert "evidence/gate-run/we/ird.json" not in listed


# ---------------------------------------------------------------------------
# bounded, ordered verification
# ---------------------------------------------------------------------------


def test_a_manifest_over_the_size_ceiling_is_refused(tmp_path, keys, monkeypatch):
    # The manifest is the one document parsed before its signature can be
    # checked, so its ingestion is bounded.
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    monkeypatch.setattr(pack_module, "MAX_MANIFEST_BYTES", 16)
    with pytest.raises(PackError, match="byte ceiling"):
        verify(pack, str(public))


def test_a_manifest_over_the_entry_ceiling_is_refused(tmp_path, keys, monkeypatch):
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    monkeypatch.setattr(pack_module, "MAX_MANIFEST_ENTRIES", 2)
    with pytest.raises(PackError, match="entry ceiling"):
        verify(pack, str(public))


def test_a_repeated_entry_costs_no_hashing_at_all(tmp_path, keys, monkeypatch):
    """Closure is settled before a single file is read.

    Duplicates were detected early and raised late, so a manifest naming one
    large file N times bought N full hashes from an unauthenticated document.
    Raising on closure first makes the work that follows exactly one pass over
    the pack's real bytes.
    """
    private, public = keys
    pack = sealed_pack(tmp_path / "pack", private)
    manifest = read_json(pack / "manifest.json")
    manifest["entries"].extend(dict(manifest["entries"][0]) for _ in range(50))
    write_json(pack / "manifest.json", manifest)

    hashed = []
    monkeypatch.setattr(pack_module, "file_sha256", lambda path: hashed.append(path) or "0" * 64)

    with pytest.raises(PackError) as exc:
        verify(pack, str(public))
    assert_problem(exc, "listed more than once")
    assert hashed == []


def test_an_unsigned_manifest_costs_no_hashing_either(tmp_path, keys, monkeypatch):
    """Content comparison runs after the attestation, not before it."""
    from revpack import attest

    private, _ = keys
    pack = sealed_pack(tmp_path / "pack", private)
    _, other_public = attest.generate_keypair()
    other = tmp_path / "other.pem"
    other.write_bytes(other_public)

    hashed = []
    monkeypatch.setattr(pack_module, "file_sha256", lambda path: hashed.append(path) or "0" * 64)

    with pytest.raises(Exception, match="different key"):
        verify(pack, str(other))
    assert hashed == []


# ---------------------------------------------------------------------------
# a blocking rule that declines to run is not a pass
# ---------------------------------------------------------------------------


def test_a_blocking_rule_that_does_not_apply_cannot_clear_a_release():
    # _decide_state read only FAIL and INCONCLUSIVE, so NOT_APPLICABLE on a
    # blocking rule was a silent pass. models.py makes `blocking` default to
    # true, so this reached any rule that returned "does not apply", by design
    # or by a gap in an implementation.
    from revpack.models import RuleOutcome
    from revpack.pack import _decide_state

    blocking = RuleOutcome(id="R-x", kind="slo", blocking=True, status=Status.NOT_APPLICABLE)
    assert _decide_state([], [blocking], []) == "INCOMPLETE"

    # Advisory rules are untouched: the policy said this one does not decide.
    advisory = RuleOutcome(id="R-y", kind="slo", blocking=False, status=Status.NOT_APPLICABLE)
    assert _decide_state([], [advisory], []) == "GO"


def test_b8_decision_forged_before_sealing_is_caught(tmp_path, keys):
    """The boundary hashes cannot defend.

    Everything here is cryptographically perfect: the decision was forged
    BEFORE sealing, so no byte changed afterwards, every hash matches, and the
    signature verifies. Only recomputing the decision from the sealed evidence
    exposes it.
    """
    private, public = keys
    pack = tmp_path / "pack"
    build_pack(pack, evidence=DEFECT_EVIDENCE)

    payload = read_json(pack / "decision.json")
    assert payload["state"] == "NO_GO"
    payload["state"] = "GO"
    payload["fail_count"] = 0
    payload["inconclusive_count"] = 0
    for rule in payload["rules"]:
        rule["status"] = "PASS"
        rule["findings"] = []
    for outcome in payload["evidence_classes"]:
        outcome["status"] = "PASS"
    for binding in payload["bindings"]:
        binding["status"] = "PASS"
    write_json(pack / "decision.json", payload)

    # Sealing succeeds: the inputs did not change, so the chain digest is intact.
    seal(pack, str(private))

    with pytest.raises(PackError, match="does not follow from the sealed evidence") as exc:
        verify(pack, str(public))
    assert any("sealed as GO" in p for p in exc.value.problems)


def test_b8_forged_pack_passes_every_hash_and_signature_check(tmp_path, keys):
    """Proves the previous test is not passing for an incidental reason."""
    from revpack import attest
    from revpack.canonical import file_sha256

    private, public = keys
    pack = tmp_path / "pack"
    build_pack(pack, evidence=DEFECT_EVIDENCE)
    payload = read_json(pack / "decision.json")
    payload["state"] = "GO"
    write_json(pack / "decision.json", payload)
    seal(pack, str(private))

    manifest = read_json(pack / "manifest.json")
    for entry in manifest["entries"]:
        assert file_sha256(pack / entry["path"]) == entry["sha256"]
    attest.verify(manifest, attest.load_public_key(str(public)))


def test_unsealed_pack_cannot_be_verified(tmp_path, keys):
    _, public = keys
    build_pack(tmp_path / "pack")
    with pytest.raises(PackError, match="not sealed"):
        verify(tmp_path / "pack", str(public))
