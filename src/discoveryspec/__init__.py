"""discoveryspec: compile discovery transcripts into executable deployment contracts."""

from .loader import ContractLoadError, load_contract, load_schema
from .models import DeploymentContract, OpenQuestion, Requirement
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
    "Transcript",
    "TranscriptError",
    "Turn",
    "parse_transcript",
    "ValidationReport",
    "validate_contract",
]
