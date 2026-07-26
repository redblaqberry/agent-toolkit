"""discoveryspec: compile discovery transcripts into executable deployment contracts."""

from .approve import ApprovalError, approve_contract
from .attest import (
    AttestationError,
    generate_keypair,
    sign_contract,
    sign_run,
    verify_contract,
    verify_run,
)
from .brand import Brand, BrandError, DEFAULT_BRAND, load_brand
from .report import ReportError, render_report
from .extraction import (
    AnthropicExtractor,
    ExtractionError,
    Extractor,
    StubExtractor,
    compile_contract,
)
from .loader import (
    ContractLoadError,
    contract_from_raw,
    load_contract,
    load_contract_raw,
    load_schema,
    parse_contract_json,
)
from .runner import (
    RunError,
    evaluate_slos,
    execute_scenarios,
    percentile_nearest_rank,
    provenance_report,
    scenario_cost_eur,
)
from .models import DeploymentContract, OpenQuestion, Requirement
from .scenarios import CompileError, GateScenario, compile_scenarios, gate_export
from .transcript import Transcript, TranscriptError, Turn, parse_transcript
from .validate import ValidationReport, validate_contract

__version__ = "0.1.0"

__all__ = [
    "ApprovalError",
    "approve_contract",
    "AttestationError",
    "generate_keypair",
    "sign_contract",
    "sign_run",
    "verify_contract",
    "verify_run",
    "Brand",
    "BrandError",
    "DEFAULT_BRAND",
    "load_brand",
    "ReportError",
    "render_report",
    "AnthropicExtractor",
    "ExtractionError",
    "Extractor",
    "StubExtractor",
    "compile_contract",
    "ContractLoadError",
    "contract_from_raw",
    "load_contract",
    "load_contract_raw",
    "load_schema",
    "parse_contract_json",
    "RunError",
    "evaluate_slos",
    "execute_scenarios",
    "percentile_nearest_rank",
    "provenance_report",
    "scenario_cost_eur",
    "DeploymentContract",
    "OpenQuestion",
    "Requirement",
    "CompileError",
    "GateScenario",
    "compile_scenarios",
    "gate_export",
    "Transcript",
    "TranscriptError",
    "Turn",
    "parse_transcript",
    "ValidationReport",
    "validate_contract",
]
