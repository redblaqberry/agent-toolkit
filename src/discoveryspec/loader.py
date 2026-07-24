"""Load and structurally check contract JSON files.

Two layers, both fail closed: the canonical JSON Schema (the public,
language-neutral definition shipped inside the package) and the Pydantic
model (the runtime representation). A contract must pass both.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from .models import DeploymentContract

SCHEMA_RESOURCE = "deployment-contract.v1.schema.json"


class ContractLoadError(ValueError):
    """The file is not a structurally valid deployment contract."""

    def __init__(self, path: str, problems: list[str]):
        self.path = path
        self.problems = problems
        super().__init__(f"{path}: {len(problems)} structural problem(s)")


def load_schema() -> dict:
    text = (
        resources.files("discoveryspec")
        .joinpath("schemas", SCHEMA_RESOURCE)
        .read_text(encoding="utf-8")
    )
    return json.loads(text)


def _reject_constant(constant: str) -> None:
    raise ValueError(f"non-standard JSON constant {constant!r} is not allowed")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}; ambiguous contracts are rejected")
        obj[key] = value
    return obj


def parse_contract_json(text: str) -> object:
    """Parse contract JSON text with the fail-closed guards: NaN/Infinity
    constants and duplicate keys are rejected, never silently accepted.
    Raises ValueError on any violation."""
    return json.loads(
        text,
        parse_constant=_reject_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )


def load_contract_raw(path: str | Path) -> dict:
    """Parse a contract file into a raw dict with the fail-closed JSON guards
    (no NaN/Infinity constants, no duplicate keys); the schema and model
    checks happen in ``contract_from_raw``."""
    path = Path(path)
    try:
        return parse_contract_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError covers JSON and UTF-8 decoding
        raise ContractLoadError(str(path), [f"cannot read contract JSON: {exc}"]) from exc


def contract_from_raw(raw: object, source: str) -> DeploymentContract:
    """Schema-check and model-check an already-parsed contract document.

    ``source`` labels the document in error messages (a file path, or a
    marker like ``<stamped contract>`` for in-memory documents).
    """
    validator = Draft202012Validator(load_schema())
    problems = [
        f"schema: {'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in sorted(validator.iter_errors(raw), key=lambda e: str(e.absolute_path))
    ]
    if problems:
        raise ContractLoadError(source, problems)

    try:
        return DeploymentContract.model_validate(raw)
    except ValidationError as exc:
        problems = [
            f"model: {'.'.join(str(loc) for loc in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ContractLoadError(source, problems) from exc


def load_contract(path: str | Path) -> DeploymentContract:
    """Parse, schema-check, and model-check a contract file.

    Raises ContractLoadError listing every problem found; never returns a
    partially valid contract. NaN/Infinity literals, invalid UTF-8, and
    unreadable files all fail here.
    """
    path = Path(path)
    return contract_from_raw(load_contract_raw(path), str(path))
