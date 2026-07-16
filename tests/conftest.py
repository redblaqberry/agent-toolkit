import json
from pathlib import Path

import pytest

from discoveryspec import parse_transcript

REPO = Path(__file__).parent.parent
EXAMPLE = REPO / "examples" / "invoice_automation"
FIXTURES = Path(__file__).parent / "fixtures"

TRANSCRIPT_PATH = EXAMPLE / "transcript.md"
DRAFT_PATH = EXAMPLE / "draft-contract.json"
APPROVED_PATH = EXAMPLE / "approved-contract.json"


@pytest.fixture(scope="session")
def transcript():
    return parse_transcript(TRANSCRIPT_PATH)


@pytest.fixture()
def draft_dict() -> dict:
    return json.loads(DRAFT_PATH.read_text(encoding="utf-8"))


@pytest.fixture()
def approved_dict() -> dict:
    return json.loads(APPROVED_PATH.read_text(encoding="utf-8"))
