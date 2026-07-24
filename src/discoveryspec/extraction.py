"""Compile a transcript into a draft contract through an extraction adapter.

The extractor is an untrusted component: whatever it emits is treated as a
claim, never as a fact. The pipeline around it owns the trust envelope and
enforces it regardless of what the adapter returns:

- a compiled contract is ALWAYS an unsigned draft; ``status``,
  ``approved_by``, ``approved_at``, and ``approval_signature`` are forced, so
  no extractor can mint an approved contract or hand back an attestation
- the transcript name and SHA-256 pin are stamped from the transcript that
  was actually parsed, not from anything the extractor says
- the assembled document must pass the full pipeline (JSON Schema, model,
  validator, turn provenance against the transcript) before it is returned;
  an extraction whose citations do not exist in the transcript is refused,
  not written

Adapters implement the ``Extractor`` protocol. ``StubExtractor`` is the
deterministic reference adapter: it replays a recorded extraction result
from a JSON file, which keeps the whole compile-approve-export pipeline
testable without any model access. A recorded extraction that pins a
transcript SHA-256 is refused when replayed against different bytes.
``AnthropicExtractor`` is the live adapter: one structured Claude call over
the parsed transcript. Its output receives no trust either; the same
envelope forcing and end-to-end validation apply, so a hallucinated turn
citation or an invented requirement is refused, not written.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .loader import ContractLoadError, contract_from_raw, load_contract_raw, load_schema, parse_contract_json
from .models import DeploymentContract
from .transcript import Transcript
from .validate import ValidationReport, validate_contract

CONTRACT_VERSION = "deployment-contract.v2"
DEFAULT_EXTRACTION_MODEL = "claude-opus-5"

EXTRACTION_RULES = """\
You are the extraction stage of DiscoverySpec. You turn a technical discovery
transcript into a DRAFT deployment-contract.v2 JSON document. You extract; you
never invent, resolve, or decide.

Rules:
- Output ONLY the JSON document. No prose, no markdown fences, no comments.
- Every requirement cites the exact transcript turn numbers it was extracted
  from in source_turns. Never cite a turn that does not support the statement.
- Requirement ids are REQ-001, REQ-002, ... in order of first appearance;
  open question ids are UNK-001, UNK-002, ...
- When two stakeholders contradict each other, create BOTH requirements with
  status "conflict", list each other in conflicts_with (symmetric), and leave
  resolution null. Conflicts are resolved by humans after extraction, never
  by you.
- When the call does not answer something the contract requires (an SLO, data
  governance, an owner), record an open question with status "open", the
  turns where the gap surfaced, a named owner, and blocking=true if the
  contract cannot ship without the answer; leave the contract field null.
- Executable sections (kpis, roles, allowed_actions, escalation_rules,
  security_constraints, slo, environment, data_governance) may only reference
  requirements with status "resolved".
- Leave acceptance_rules empty. Acceptance rules carry the inputs a test sends
  the agent and the outcome it must produce; they are authored during human
  review, once the conflicts are resolved and it is settled what the agent is
  actually promising. Inventing them here would mean inventing test data.
- metadata: fill project (kebab-case), system (a human-readable name for the
  system under test, e.g. "supplier-invoice agent"), and customer from the
  call; set status "draft", approved_by null, approved_at null,
  transcript_sha256 null, and transcript to the file name you are given. The
  pipeline re-stamps this envelope and validates every citation against the
  transcript; anything unsupported is rejected.

The document must validate against this JSON Schema:
"""


class ExtractionError(ValueError):
    """The transcript could not be compiled into a valid draft contract.

    ``exit_code`` mirrors the CLI convention: 2 for structural problems
    (unreadable fixture, schema or model violation, broken provenance).
    """

    def __init__(self, message: str, exit_code: int = 2, problems: list[str] | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.problems = problems or []


class Extractor(Protocol):
    """An extraction adapter: turns a parsed transcript into a contract-shaped
    document (a dict in ``deployment-contract.v2`` layout). The result is a
    proposal; the compile pipeline validates it and owns the metadata
    envelope."""

    name: str

    def extract(self, transcript: Transcript) -> dict: ...


@dataclass(frozen=True)
class StubExtractor:
    """Replays a recorded extraction result from a JSON file.

    The fixture is a contract-shaped document, typically captured from a
    correct extraction (the bundled ``draft-contract.json`` is exactly that).
    If the fixture pins a ``transcript_sha256``, replaying it against a
    transcript with different bytes is refused: a recording declares its
    source and must not be passed off as an extraction of something else.
    """

    fixture: Path
    name: str = "stub"

    def extract(self, transcript: Transcript) -> dict:
        try:
            raw = load_contract_raw(self.fixture)
        except ContractLoadError as exc:
            raise ExtractionError(
                f"stub extraction fixture {self.fixture} is unreadable",
                problems=exc.problems,
            ) from exc
        if not isinstance(raw, dict):
            raise ExtractionError(
                f"stub extraction fixture {self.fixture} is not a JSON object"
            )
        metadata = raw.get("metadata")
        pinned = metadata.get("transcript_sha256") if isinstance(metadata, dict) else None
        if pinned is not None and pinned != transcript.sha256:
            raise ExtractionError(
                f"stub extraction fixture {self.fixture} was recorded from a different "
                f"transcript (fixture pins {str(pinned)[:12]}..., supplied transcript "
                f"is {transcript.sha256[:12]}...); refusing to replay it"
            )
        return raw


def _render_transcript(transcript: Transcript, transcript_name: str) -> str:
    """Render the parsed turns back to canonical numbered form, so the model
    sees exactly the numbering the validator will check citations against."""
    lines = [f"Transcript file name: {transcript_name}", ""]
    for turn in transcript.turns:
        lines.append(f"[T{turn.number:02d}] {turn.speaker} ({turn.role}): {turn.text}")
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    """Tolerate a single markdown code fence around the JSON document."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].lstrip()
    return stripped


@dataclass
class AnthropicExtractor:
    """Extracts the draft contract with one structured Claude call.

    The transcript is rendered from the parsed turns (never raw file bytes),
    the deployment-contract JSON Schema rides in the system prompt, and the
    response must be a single JSON document. The result is a claim like any
    other extraction: the compile pipeline stamps the envelope and validates
    every citation against the transcript before anything is written.

    ``client`` is injectable for tests; when None, ``anthropic.Anthropic()``
    is constructed lazily inside ``extract`` (resolving credentials from the
    environment), so importing this module needs neither the SDK nor a key.
    """

    model: str = DEFAULT_EXTRACTION_MODEL
    max_tokens: int = 64000
    client: Any = None
    name: str = "claude"

    def extract(self, transcript: Transcript) -> dict:
        client = self.client
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ExtractionError(
                    "the claude extractor needs the SDK dependency: "
                    "pip install 'discoveryspec[llm]'"
                ) from exc
            try:
                client = anthropic.Anthropic()
            except Exception as exc:  # credential/config resolution can fail here
                raise ExtractionError(
                    f"cannot construct the model client: {type(exc).__name__}: {exc}"
                ) from exc

        transcript_name = Path(transcript.path).name
        system = EXTRACTION_RULES + json.dumps(load_schema(), indent=2)
        try:
            with client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                thinking={"type": "adaptive"},
                system=system,
                messages=[
                    {
                        "role": "user",
                        "content": _render_transcript(transcript, transcript_name),
                    }
                ],
            ) as stream:
                response = stream.get_final_message()
        except ExtractionError:
            raise
        except Exception as exc:  # any API failure is an infrastructure failure
            raise ExtractionError(
                f"extraction model call failed: {type(exc).__name__}: {exc}"
            ) from exc

        if response.stop_reason == "refusal":
            raise ExtractionError(
                "the extraction model declined the request (stop_reason refusal); "
                "no contract was produced"
            )
        if response.stop_reason == "max_tokens":
            raise ExtractionError(
                f"extraction output was truncated at max_tokens={self.max_tokens}; "
                f"a truncated contract is never used"
            )

        text = "".join(
            block.text for block in response.content if block.type == "text"
        )
        try:
            raw = parse_contract_json(_strip_code_fence(text))
        except ValueError as exc:
            raise ExtractionError(
                f"extraction response is not valid contract JSON: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ExtractionError(
                f"extraction response is {type(raw).__name__}, not a contract document"
            )
        return raw


def compile_contract(
    extractor: Extractor,
    transcript: Transcript,
    transcript_name: str,
) -> tuple[dict, DeploymentContract, ValidationReport]:
    """Run ``extractor`` and assemble a validated draft contract document.

    Returns the raw document (ready to serialize), the validated model, and
    the validation report (its findings, conflicts and open questions, are
    the draft's expected content). Raises ExtractionError when the extraction
    cannot become a structurally valid draft; nothing partially valid is ever
    returned.

    ``transcript_name`` is the file name recorded in ``metadata.transcript``
    (the caller knows the on-disk name; the pipeline pins the bytes).
    """
    extracted = extractor.extract(transcript)
    if not isinstance(extracted, dict):
        raise ExtractionError(
            f"extractor {extractor.name!r} returned {type(extracted).__name__}, "
            f"not a contract document"
        )

    raw = copy.deepcopy(extracted)
    raw["contract_version"] = CONTRACT_VERSION
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        raise ExtractionError(
            f"extractor {extractor.name!r} produced no metadata object; "
            f"metadata.project and metadata.customer must come from the extraction"
        )
    # the trust envelope is the pipeline's, not the extractor's: a compiled
    # contract is always an unsigned draft pinned to the verified bytes
    metadata["transcript"] = transcript_name
    metadata["transcript_sha256"] = transcript.sha256
    metadata["status"] = "draft"
    metadata["approved_by"] = None
    metadata["approved_at"] = None
    # no adapter may hand back an attestation: only approve --signing-key
    # produces one, and a draft carrying a signature would be a document this
    # pipeline never signed but that looks signed to anything reading the field.
    # Dropped rather than nulled, so a clean extraction is byte-identical to a
    # draft that never mentioned the field.
    metadata.pop("approval_signature", None)

    source = f"<{extractor.name} extraction>"
    try:
        contract = contract_from_raw(raw, source=source)
    except ContractLoadError as exc:
        raise ExtractionError(
            f"extraction is not a valid {CONTRACT_VERSION} document",
            problems=exc.problems,
        ) from exc

    report = validate_contract(contract, transcript)
    if report.errors:
        raise ExtractionError(
            "extraction failed validation against the transcript",
            problems=report.errors,
        )
    return raw, contract, report
