"""Loading and validating the SSOT wire contracts in ``schemas/``."""

from __future__ import annotations

import json
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

PROBE_SCHEMA = "retrieval-request.schema.json"


class ContractViolationError(ValueError):
    """A payload does not satisfy its v0.1 wire contract."""


def default_schema_dir() -> Path:
    """Locate the SSOT schemas: bundled in the wheel, else the source checkout."""

    bundled = Path(str(resources.files("kasane"))) / "schemas"
    if (bundled / PROBE_SCHEMA).is_file():
        return bundled
    return Path(__file__).resolve().parents[2] / "schemas"


def load_schema(name: str, schema_dir: Path | None = None) -> dict[str, Any]:
    raw = ((schema_dir or default_schema_dir()) / name).read_text("utf-8")
    schema: dict[str, Any] = json.loads(raw)
    return schema


@cache
def _validator(name: str) -> Draft202012Validator:
    schema = load_schema(name)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_contract(name: str, value: Any) -> None:
    """Raise ``ContractViolationError`` naming the first violated location."""

    errors = sorted(_validator(name).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "<root>"
        raise ContractViolationError(f"{name}: {location}: {first.message}")
