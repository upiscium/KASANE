"""Canonical JSON boundary for lossless portable metadata."""

from __future__ import annotations

import json
from typing import Any

from .errors import InvalidMetadataError

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


def canonical_json(value: JsonValue) -> str:
    """Serialize JSON data deterministically and reject non-JSON values."""

    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InvalidMetadataError("metadata must be finite canonical JSON") from error
    if decoded != value:
        raise InvalidMetadataError(
            "metadata does not round-trip through JSON losslessly"
        )
    return encoded


def parse_json(encoded: str) -> JsonValue:
    """Decode trusted canonical JSON stored by Vault Core."""

    return json.loads(encoded)


def require_json(value: Any) -> JsonValue:
    """Validate an API value and return a detached JSON-only copy."""

    return parse_json(canonical_json(value))
