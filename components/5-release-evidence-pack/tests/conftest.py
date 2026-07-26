from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest
import yaml

from revpack import attest
from revpack.pack import collect, decide, seal

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
CLEAN = FIXTURES / "clean"
DEFECT = FIXTURES / "defect"

# Genuine producer output, vendored so the conformance suite runs everywhere
# including CI. See fixtures/conformance/PROVENANCE.md.
VENDORED = FIXTURES / "conformance"

# Root of the sibling workspace holding the five producing repositories. Only
# the drift guard and the live-CLI tests need it; everything else runs from the
# vendored corpus and the authored fixtures.
FDE_ROOT = Path(os.environ.get("REVPACK_WORKSPACE", Path(__file__).resolve().parents[3]))

# Where each vendored file came from, so drift against upstream is detectable.
UPSTREAM_ORIGINS = {
    "discoveryspec-approved-contract.json":
        "01-discoveryspec/examples/invoice_automation/approved-contract.json",
    "canary-report.json": "04-mcp-contract-canary/report.json",
    "silobench-golden-hashes.json":
        "02-silobench/packages/scenario/fixtures/golden-hashes.json",
    "gate-run-clerk.json": "04-mcp-contract-canary/tests/data/golden-run-clerk.json",
    "gate-run-approver.json": "04-mcp-contract-canary/tests/data/golden-run-approver.json",
    "gate-run-v2-clerk.json": "04-mcp-contract-canary/tests/data/golden-run-v2-clerk.json",
    # statediff-verdict-SD-PAY-01.json has no committed origin: it is generated
    # by running the tool, and the live test regenerates and re-checks it.
}


def real_artifact(name: str) -> Path:
    """Locate a vendored producer artifact."""
    path = VENDORED / name
    assert path.is_file(), f"vendored conformance artifact missing: {path}"
    return path

# Pinned so a pack built twice from the same inputs is byte-identical, which is
# what lets the determinism test mean anything.
STAMP = "2026-07-18T20:00:00Z"

CLEAN_EVIDENCE = {
    "contract": CLEAN / "contract" / "approved-contract.json",
    "gate-run": CLEAN / "gate-run" / "run.json",
    "state-verdict": CLEAN / "state-verdict" / "SD-PAY-01.json",
    "blast-radius": CLEAN / "blast-radius" / "report.json",
    "environment-golden": CLEAN / "environment" / "golden-hashes.json",
    "silobench-verify": CLEAN / "silobench-verify" / "verify.json",
}

# The same release with real failures in three of its six artifacts. Defined
# once here because several suites need the pack that must be refused, and three
# copies of it would drift apart exactly when one of them stopped failing.
DEFECT_EVIDENCE = {
    **CLEAN_EVIDENCE,
    "gate-run": DEFECT / "gate-run" / "run.json",
    "blast-radius": DEFECT / "blast-radius" / "report.json",
    "silobench-verify": DEFECT / "silobench-verify" / "verify.json",
}


@pytest.fixture
def keys(tmp_path: Path) -> tuple[Path, Path]:
    private_pem, public_pem = attest.generate_keypair()
    private = tmp_path / "sign.pem"
    public = tmp_path / "verify.pem"
    private.write_bytes(private_pem)
    public.write_bytes(public_pem)
    return private, public


def write_yaml(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8", newline="\n")
    return path


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def build_pack(
    out: Path,
    evidence: dict[str, Path] | None = None,
    policy: Path | None = None,
    release: Path | None = None,
    traceability: Path | None = None,
    run_decide: bool = True,
):
    """Collect (and usually decide) a pack from fixture inputs."""
    sources = [(kind, str(path)) for kind, path in (evidence or CLEAN_EVIDENCE).items()]
    collect(
        sources,
        str(policy or CLEAN / "policy.yaml"),
        str(release or CLEAN / "release.yaml"),
        str(traceability or CLEAN / "traceability.yaml"),
        out,
        collected_at=STAMP,
        record_vcs=False,
    )
    return decide(out) if run_decide else None


def sealed_pack(out: Path, private_key: Path, **kwargs):
    build_pack(out, **kwargs)
    seal(out, str(private_key))
    return out


def assert_problem(exc_info, needle: str) -> None:
    """Assert a specific problem was reported.

    Errors carry a short message plus a structured `problems` list, so that a
    caller sees every fault at once instead of fixing them one per run. Tests
    therefore assert against the list, not the summary line.
    """
    problems = exc_info.value.problems
    assert any(needle in p for p in problems), f"{needle!r} not found in {problems}"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n"
    )


def copy_tree(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


def resign_manifest(pack: Path, manifest: dict, private_key: Path) -> None:
    """Write a doctored manifest back carrying a valid signature over it.

    Editing a sealed manifest in place and calling `verify` tests the signature
    and stops there. Re-signing is what reaches the checks that have to hold
    when the signer is the one lying: a signature says who wrote the numbers,
    never that the numbers are true.
    """
    manifest.pop("attestation", None)
    signed = attest.sign(manifest, attest.load_private_key(str(private_key)))
    write_json(pack / "manifest.json", signed)


# Machine-specific path shapes that must never reach a published fixture, and
# that carry no information the parsers use. Separators are normalised to "/"
# before matching so these patterns never have to express a backslash: writing
# one inside a character class is easy to get subtly wrong, and a pattern that
# silently matches nothing would leave the paths in place while every caller
# believed they had been redacted.
_WORKSPACE_PREFIX = re.compile(r"^.*/FDE/(?:jobprojects/)?", re.I)
_TEMP_PREFIX = re.compile(r"^.*/Temp/[^/]+/", re.I)


def redact_machine_paths(value):
    """Replace absolute invocation paths with stable placeholders.

    Producer artifacts record the argv they were invoked with, which embeds the
    author's drive layout, username, and temp directory. Vendoring those into a
    public repository publishes a machine map for no benefit: no parser and no
    rule in this project reads a command string. Every verdict, finding, count,
    and hash is left exactly as the producer emitted it.

    The drift guard redacts both sides before comparing, so this stays a
    redaction and never becomes a way to hide an upstream change.
    """
    if isinstance(value, str):
        if value.lower().endswith(("agent-eval-gate.exe", "agent-eval-gate")):
            return "agent-eval-gate"
        normalised = value.replace("\\", "/")
        if _TEMP_PREFIX.search(normalised):
            return "<tmp>/" + _TEMP_PREFIX.sub("", normalised)
        if _WORKSPACE_PREFIX.search(normalised):
            return "<workspace>/" + _WORKSPACE_PREFIX.sub("", normalised)
        return value
    if isinstance(value, list):
        return [redact_machine_paths(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_machine_paths(v) for k, v in value.items()}
    return value
