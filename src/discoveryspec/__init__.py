"""discoveryspec: compile discovery transcripts into executable deployment contracts."""

from .loader import ContractLoadError, load_contract, load_schema
from .models import DeploymentContract, OpenQuestion, Requirement
from .scenarios import CompileError, GateScenario, compile_scenarios, gate_export
from .transcript import Transcript, TranscriptError, Turn, parse_transcript
from .validate import ValidationReport, validate_contract

__version__ = "0.1.0"

__all__ = [
    "ContractLoadError",
    "load_contract",
    "load_schema",
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
