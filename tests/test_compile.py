"""Compile-pipeline behavior: the extractor is untrusted, the pipeline owns
the trust envelope. A compiled contract is always an unsigned draft, pinned
to the parsed transcript, and fully validated before anything is written."""

import json
import shutil

import pytest
from typer.testing import CliRunner

from discoveryspec import ExtractionError, StubExtractor, compile_contract, parse_transcript
from discoveryspec.cli import app
from tests.conftest import APPROVED_PATH, DRAFT_PATH, TRANSCRIPT_PATH

runner = CliRunner()


def combined(result) -> str:
    return result.output + (result.stderr or "")


def write_fixture(tmp_path, contract_dict, name="extraction.json"):
    fixture = tmp_path / name
    fixture.write_text(
        json.dumps(contract_dict, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return fixture


def compile_cli(transcript_path, fixture_path, out, *extra):
    return runner.invoke(app, [
        "compile", "--transcript", str(transcript_path),
        "--fixture", str(fixture_path), "--out", str(out), *extra,
    ])


# --- the happy path -----------------------------------------------------------

def test_compile_reproduces_golden_draft(tmp_path):
    # replaying the recorded extraction of the bundled transcript must
    # reproduce the golden draft byte for byte
    out = tmp_path / "draft.json"
    result = compile_cli(TRANSCRIPT_PATH, DRAFT_PATH, out)
    assert result.exit_code == 0, combined(result)
    assert out.read_bytes() == DRAFT_PATH.read_bytes()
    assert "CONFLICTS (3)" in result.output
    assert "verdict: VALID" in result.output
    assert "next: resolve the conflicts" in result.output


FORGED_ATTESTATION = {
    "version": "attest.v1",
    "kind": "deployment-contract",
    "algorithm": "ed25519",
    "public_key_fingerprint": "deadbeefdeadbeef",
    "digest_sha256": "0" * 64,
    "signature": "Zm9yZ2Vk",
}


def test_compile_strips_an_attestation_the_extractor_invented(tmp_path):
    # only approve --signing-key signs. An extraction that hands back an
    # attestation would otherwise produce a draft that looks signed to anything
    # reading the field, so the envelope forces it back to None
    forged = json.loads(DRAFT_PATH.read_text(encoding="utf-8"))
    forged["metadata"]["approval_signature"] = dict(FORGED_ATTESTATION)
    out = tmp_path / "draft.json"
    result = compile_cli(TRANSCRIPT_PATH, write_fixture(tmp_path, forged), out)
    assert result.exit_code == 0, combined(result)
    meta = json.loads(out.read_text(encoding="utf-8"))["metadata"]
    assert meta.get("approval_signature") is None
    # and the result is byte-identical to a draft that never carried one
    assert out.read_bytes() == DRAFT_PATH.read_bytes()


def test_compile_forces_draft_envelope(tmp_path):
    # an extraction claiming to be approved is stripped back to an unsigned
    # draft: no extractor can mint an approval
    out = tmp_path / "draft.json"
    result = compile_cli(TRANSCRIPT_PATH, APPROVED_PATH, out)
    assert result.exit_code == 0, combined(result)
    meta = json.loads(out.read_text(encoding="utf-8"))["metadata"]
    assert meta["status"] == "draft"
    assert meta["approved_by"] is None
    assert meta["approved_at"] is None


def test_compile_pins_transcript_when_fixture_has_no_pin(tmp_path, draft_dict, transcript):
    draft_dict["metadata"]["transcript_sha256"] = None
    fixture = write_fixture(tmp_path, draft_dict)
    out = tmp_path / "draft.json"
    result = compile_cli(TRANSCRIPT_PATH, fixture, out)
    assert result.exit_code == 0, combined(result)
    compiled = json.loads(out.read_text(encoding="utf-8"))
    assert compiled["metadata"]["transcript_sha256"] == transcript.sha256


def test_full_pipeline_compile_approve_export(tmp_path):
    # compile a resolved extraction, sign it off, export the gate suite: the
    # approved artifact must equal the golden approved contract byte for byte
    import yaml

    draft = tmp_path / "resolved-draft.json"
    assert compile_cli(TRANSCRIPT_PATH, APPROVED_PATH, draft).exit_code == 0
    shutil.copyfile(TRANSCRIPT_PATH, tmp_path / "transcript.md")

    approved = tmp_path / "approved.json"
    result = runner.invoke(app, [
        "approve", "--contract", str(draft), "--out", str(approved),
        "--by", "Anna Lindqvist (Operations), Jonas Weber (Security), Priya Nair (Finance)",
        "--date", "2026-07-16",
    ])
    assert result.exit_code == 0, combined(result)
    assert approved.read_bytes() == APPROVED_PATH.read_bytes()

    export = runner.invoke(app, [
        "export-gate", "--contract", str(approved), "--out", str(tmp_path / "export"),
    ])
    assert export.exit_code == 0, combined(export)
    data = yaml.safe_load((tmp_path / "export" / "scenarios.yaml").read_text(encoding="utf-8"))
    assert len(data["scenarios"]) == 10


def test_compiled_draft_with_conflicts_is_not_approvable(tmp_path):
    draft = tmp_path / "draft.json"
    assert compile_cli(TRANSCRIPT_PATH, DRAFT_PATH, draft).exit_code == 0
    shutil.copyfile(TRANSCRIPT_PATH, tmp_path / "transcript.md")
    result = runner.invoke(app, [
        "approve", "--contract", str(draft), "--out", str(tmp_path / "approved.json"),
        "--by", "Anyone", "--date", "2026-07-16",
    ])
    assert result.exit_code == 1
    assert "open conflict" in combined(result)


def test_compile_hint_carries_transcript_when_not_adjacent(tmp_path):
    # the draft lands away from the transcript, so the advertised follow-up
    # command must include the explicit --transcript or it would exit 2
    out = tmp_path / "draft.json"
    result = compile_cli(TRANSCRIPT_PATH, DRAFT_PATH, out)
    assert result.exit_code == 0, combined(result)
    assert f"--transcript {TRANSCRIPT_PATH}" in result.output


def test_compile_hint_omits_transcript_when_adjacent(tmp_path):
    transcript_copy = tmp_path / "transcript.md"
    shutil.copyfile(TRANSCRIPT_PATH, transcript_copy)
    out = tmp_path / "draft.json"
    result = compile_cli(transcript_copy, DRAFT_PATH, out)
    assert result.exit_code == 0, combined(result)
    assert "--transcript" not in result.output


# --- refusals (exit 2) ----------------------------------------------------------

def test_compile_refuses_fixture_from_different_transcript(tmp_path, draft_dict):
    draft_dict["metadata"]["transcript_sha256"] = "ab" * 32
    fixture = write_fixture(tmp_path, draft_dict)
    out = tmp_path / "draft.json"
    result = compile_cli(TRANSCRIPT_PATH, fixture, out)
    assert result.exit_code == 2
    assert "different transcript" in combined(result)
    assert not out.exists()


def test_compile_refuses_dangling_turn(tmp_path, draft_dict):
    draft_dict["requirements"][0]["source_turns"] = [99]
    fixture = write_fixture(tmp_path, draft_dict)
    out = tmp_path / "draft.json"
    result = compile_cli(TRANSCRIPT_PATH, fixture, out)
    assert result.exit_code == 2
    assert "T99" in combined(result)
    assert not out.exists()


def test_compile_refuses_schema_violation(tmp_path, draft_dict):
    del draft_dict["slo"]
    fixture = write_fixture(tmp_path, draft_dict)
    out = tmp_path / "draft.json"
    result = compile_cli(TRANSCRIPT_PATH, fixture, out)
    assert result.exit_code == 2
    assert "schema:" in combined(result)
    assert not out.exists()


def test_compile_refuses_non_object_metadata(tmp_path, draft_dict):
    draft_dict["metadata"] = []
    fixture = write_fixture(tmp_path, draft_dict)
    result = compile_cli(TRANSCRIPT_PATH, fixture, tmp_path / "draft.json")
    assert result.exit_code == 2
    assert "metadata" in combined(result)


def test_compile_refuses_unreadable_fixture(tmp_path):
    fixture = tmp_path / "broken.json"
    fixture.write_text("{ not json", encoding="utf-8")
    result = compile_cli(TRANSCRIPT_PATH, fixture, tmp_path / "draft.json")
    assert result.exit_code == 2


def test_compile_refuses_missing_fixture(tmp_path):
    result = compile_cli(TRANSCRIPT_PATH, tmp_path / "nowhere.json", tmp_path / "draft.json")
    assert result.exit_code == 2


def test_compile_stub_requires_fixture(tmp_path):
    result = runner.invoke(app, [
        "compile", "--transcript", str(TRANSCRIPT_PATH),
        "--out", str(tmp_path / "draft.json"),
    ])
    assert result.exit_code == 2
    assert "--fixture" in combined(result)


def test_compile_unknown_extractor(tmp_path):
    result = runner.invoke(app, [
        "compile", "--transcript", str(TRANSCRIPT_PATH),
        "--fixture", str(DRAFT_PATH), "--out", str(tmp_path / "draft.json"),
        "--extractor", "gpt9",
    ])
    assert result.exit_code == 2
    assert "unknown extractor" in combined(result)


def test_compile_malformed_transcript(tmp_path):
    bad = tmp_path / "transcript.md"
    bad.write_text("[T1] wrong padding (Role): text\n", encoding="utf-8")
    result = compile_cli(bad, DRAFT_PATH, tmp_path / "draft.json")
    assert result.exit_code == 2


def test_compile_missing_transcript(tmp_path):
    result = compile_cli(tmp_path / "nowhere.md", DRAFT_PATH, tmp_path / "draft.json")
    assert result.exit_code == 2


# --- output-path guards ----------------------------------------------------------

def test_compile_never_overwrites_without_force(tmp_path):
    out = tmp_path / "draft.json"
    out.write_text("sentinel", encoding="utf-8")
    refused = compile_cli(TRANSCRIPT_PATH, DRAFT_PATH, out)
    assert refused.exit_code == 2
    assert out.read_text(encoding="utf-8") == "sentinel"
    forced = compile_cli(TRANSCRIPT_PATH, DRAFT_PATH, out, "--force")
    assert forced.exit_code == 0, combined(forced)
    assert out.read_bytes() == DRAFT_PATH.read_bytes()


def test_compile_never_overwrites_the_transcript(tmp_path):
    transcript_copy = tmp_path / "transcript.md"
    shutil.copyfile(TRANSCRIPT_PATH, transcript_copy)
    original = transcript_copy.read_bytes()
    result = compile_cli(transcript_copy, DRAFT_PATH, transcript_copy, "--force")
    assert result.exit_code == 2
    assert "provenance source" in combined(result)
    assert transcript_copy.read_bytes() == original


def test_compile_never_overwrites_the_fixture(tmp_path, draft_dict):
    fixture = write_fixture(tmp_path, draft_dict)
    original = fixture.read_bytes()
    result = compile_cli(TRANSCRIPT_PATH, fixture, fixture, "--force")
    assert result.exit_code == 2
    assert "extraction fixture" in combined(result)
    assert fixture.read_bytes() == original


# --- library-level behavior -------------------------------------------------------

def test_compile_contract_does_not_mutate_extractor_output(draft_dict, transcript):
    class CannedExtractor:
        name = "canned"

        def extract(self, _transcript):
            return draft_dict

    draft_dict["metadata"]["transcript_sha256"] = None
    document, compiled, report = compile_contract(
        CannedExtractor(), transcript, "transcript.md"
    )
    assert draft_dict["metadata"]["transcript_sha256"] is None  # input untouched
    assert document["metadata"]["transcript_sha256"] == transcript.sha256
    assert compiled.metadata.status == "draft"
    assert report.exit_code == 0


def test_compile_contract_rejects_non_dict_extraction(transcript):
    class BrokenExtractor:
        name = "broken"

        def extract(self, _transcript):
            return ["not", "a", "contract"]

    with pytest.raises(ExtractionError) as excinfo:
        compile_contract(BrokenExtractor(), transcript, "transcript.md")
    assert "not a contract document" in str(excinfo.value)
    assert excinfo.value.exit_code == 2


def test_stub_extractor_reads_the_golden_fixture(transcript):
    stub = StubExtractor(fixture=DRAFT_PATH)
    raw = stub.extract(transcript)
    assert raw["metadata"]["project"] == "nordlicht-invoice-automation"


# --- the claude extractor (fake client; the adapter is untrusted either way) ----

class FakeBlock:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class FakeResponse:
    def __init__(self, text, stop_reason="end_turn"):
        self.stop_reason = stop_reason
        self.content = [FakeBlock("thinking"), FakeBlock("text", text)]


class FakeStream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._response


class FakeMessages:
    def __init__(self, response, calls):
        self._response = response
        self._calls = calls

    def stream(self, **kwargs):
        self._calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return FakeStream(self._response)


class FakeClient:
    def __init__(self, response):
        self.calls = []
        self.messages = FakeMessages(response, self.calls)


def claude_extractor(response):
    from discoveryspec import AnthropicExtractor

    client = FakeClient(response)
    return AnthropicExtractor(client=client), client


def test_claude_extraction_end_to_end(draft_dict, transcript):
    extractor, client = claude_extractor(FakeResponse(json.dumps(draft_dict)))
    document, compiled, report = compile_contract(extractor, transcript, "transcript.md")
    assert compiled.metadata.status == "draft"
    assert document["metadata"]["transcript_sha256"] == transcript.sha256
    assert report.exit_code == 0
    assert len(report.conflict_groups) == 3  # the seeded conflicts survive extraction

    call = client.calls[0]
    assert call["model"] == "claude-opus-4-8"
    assert call["thinking"] == {"type": "adaptive"}
    assert "deployment-contract.v1" in call["system"]  # schema rides in the prompt
    assert "[T01]" in call["messages"][0]["content"]  # canonical numbered turns


def test_claude_extraction_tolerates_code_fence(draft_dict, transcript):
    fenced = "```json\n" + json.dumps(draft_dict) + "\n```"
    extractor, _ = claude_extractor(FakeResponse(fenced))
    document, _, _ = compile_contract(extractor, transcript, "transcript.md")
    assert document["metadata"]["project"] == "nordlicht-invoice-automation"


def test_claude_extraction_refusal_is_refused(transcript):
    from discoveryspec import ExtractionError

    extractor, _ = claude_extractor(FakeResponse("", stop_reason="refusal"))
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(transcript)
    assert "declined" in str(excinfo.value)
    assert excinfo.value.exit_code == 2


def test_claude_extraction_truncation_is_refused(transcript):
    from discoveryspec import ExtractionError

    extractor, _ = claude_extractor(FakeResponse("{", stop_reason="max_tokens"))
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(transcript)
    assert "truncated" in str(excinfo.value)


def test_claude_extraction_invalid_json_is_refused(transcript):
    from discoveryspec import ExtractionError

    extractor, _ = claude_extractor(FakeResponse("here is your contract: {oops"))
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(transcript)
    assert "not valid contract JSON" in str(excinfo.value)


def test_claude_extraction_non_object_is_refused(transcript):
    from discoveryspec import ExtractionError

    extractor, _ = claude_extractor(FakeResponse("[1, 2, 3]"))
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(transcript)
    assert "not a contract document" in str(excinfo.value)


def test_claude_extraction_api_failure_is_refused(transcript):
    from discoveryspec import ExtractionError

    extractor, _ = claude_extractor(RuntimeError("connection reset"))
    with pytest.raises(ExtractionError) as excinfo:
        extractor.extract(transcript)
    assert "model call failed" in str(excinfo.value)


def test_claude_extraction_hallucinated_turns_are_refused(draft_dict, transcript):
    from discoveryspec import ExtractionError

    draft_dict["requirements"][0]["source_turns"] = [99]
    extractor, _ = claude_extractor(FakeResponse(json.dumps(draft_dict)))
    with pytest.raises(ExtractionError) as excinfo:
        compile_contract(extractor, transcript, "transcript.md")
    assert any("T99" in problem for problem in excinfo.value.problems)


def test_claude_extraction_client_construction_failure_is_refused(transcript, monkeypatch):
    # SDK present but the client cannot be built (credential/config resolution):
    # an ExtractionError, never a raw traceback
    import sys
    import types

    from discoveryspec import AnthropicExtractor, ExtractionError

    fake_sdk = types.ModuleType("anthropic")

    class BrokenAnthropic:
        def __init__(self):
            raise RuntimeError("could not resolve authentication method")

    fake_sdk.Anthropic = BrokenAnthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake_sdk)
    with pytest.raises(ExtractionError) as excinfo:
        AnthropicExtractor().extract(transcript)
    assert "cannot construct the model client" in str(excinfo.value)
    assert excinfo.value.exit_code == 2


def test_cli_claude_extractor_rejects_fixture(tmp_path):
    result = runner.invoke(app, [
        "compile", "--transcript", str(TRANSCRIPT_PATH), "--extractor", "claude",
        "--fixture", str(DRAFT_PATH), "--out", str(tmp_path / "draft.json"),
    ])
    assert result.exit_code == 2
    assert "--fixture only applies to the stub extractor" in combined(result)


def test_cli_stub_extractor_rejects_model(tmp_path):
    result = runner.invoke(app, [
        "compile", "--transcript", str(TRANSCRIPT_PATH), "--fixture", str(DRAFT_PATH),
        "--model", "claude-opus-4-8", "--out", str(tmp_path / "draft.json"),
    ])
    assert result.exit_code == 2
    assert "--model only applies to the claude extractor" in combined(result)
