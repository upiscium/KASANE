"""Minimal stdio MCP server exposing Level 0 retrieval as ``kasane_retrieve``."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

import mcp_types as types
from mcp.server import Server, ServerRequestContext

TOOL_NAME = "kasane_retrieve"
TOOL_DESCRIPTION = (
    "Retrieve the smallest sufficient task context from the KASANE knowledge "
    "vault as a Context Capsule v0.1 that fits the requested budget. Only "
    "canonical knowledge is returned. The result is the capsule JSON itself, "
    "or a budget_too_small_for_capsule protocol error."
)
REQUEST_SCHEMA = "retrieval-request.schema.json"


class RetrievalBackend(Protocol):
    def retrieve(self, request_value: Mapping[str, Any]) -> Any: ...


def default_schema_dir() -> Path:
    """Locate the SSOT schemas: bundled in the wheel, else the source checkout."""

    bundled = Path(str(resources.files("kasane"))) / "schemas"
    if (bundled / REQUEST_SCHEMA).is_file():
        return bundled
    return Path(__file__).resolve().parents[3] / "schemas"


def load_request_schema(schema_dir: Path | None = None) -> dict[str, Any]:
    """Return the request schema (SSOT) as the tool input schema.

    ``$id`` is dropped because it identifies the wire contract document,
    not the MCP tool; every field definition is passed through unchanged.
    """

    raw = ((schema_dir or default_schema_dir()) / REQUEST_SCHEMA).read_text("utf-8")
    schema: dict[str, Any] = json.loads(raw)
    schema.pop("$id", None)
    return schema


def build_server(
    backend: RetrievalBackend, *, schema_dir: Path | None = None
) -> Server[Any]:
    input_schema = load_request_schema(schema_dir)
    tool = types.Tool(
        name=TOOL_NAME,
        description=TOOL_DESCRIPTION,
        input_schema=input_schema,
    )

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[tool])

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name != TOOL_NAME:
            return _tool_error(f"unknown tool: {params.name}")
        try:
            result = backend.retrieve(dict(params.arguments or {}))
        except ValueError as exc:
            # Malformed requests have no v0.1 wire error; report at tool level.
            return _tool_error(f"invalid retrieval request: {exc}")
        artifact: dict[str, Any] = result.artifact
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=_dumps(artifact))],
            structured_content=artifact,
            is_error=result.error is not None,
        )

    return Server(
        "kasane",
        version=_version(),
        instructions=(
            "Call kasane_retrieve with a Retrieval Request v0.1 before searching "
            "for project knowledge yourself; trust the capsule's budget."
        ),
        get_tool_input_schema=lambda name: input_schema if name == TOOL_NAME else None,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _tool_error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("kasane-knowledge")
    except Exception:  # noqa: BLE001 - metadata absent in source checkouts
        return "0.0.0"


__all__ = ["TOOL_NAME", "build_server", "default_schema_dir", "load_request_schema"]
