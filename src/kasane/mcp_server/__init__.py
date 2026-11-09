"""stdio MCP server for KASANE.

Always exposes ``kasane_retrieve``. With write access configured it also
exposes the agent intake tools of Issue #39: ``kasane_propose``,
``kasane_corroborate``, ``kasane_verify`` and ``kasane_report_outcome``.
Agent identity (actor, session, model) comes from server configuration.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import mcp_types as types
from mcp.server import Server, ServerRequestContext

from kasane.contracts import ContractViolationError, default_schema_dir, load_schema
from kasane.trust import AgentContext, TrustRejectedError, TrustService
from kasane.trust.similar import find_similar

TOOL_NAME = "kasane_retrieve"
PROPOSE_TOOL = "kasane_propose"
CORROBORATE_TOOL = "kasane_corroborate"
VERIFY_TOOL = "kasane_verify"
OUTCOME_TOOL = "kasane_report_outcome"
WRITE_TOOLS = (PROPOSE_TOOL, CORROBORATE_TOOL, VERIFY_TOOL, OUTCOME_TOOL)

REQUEST_SCHEMA = "retrieval-request.schema.json"
PROPOSAL_SCHEMA = "knowledge-proposal.schema.json"
SIGNAL_SCHEMA = "knowledge-signal.schema.json"

TOOL_DESCRIPTION = (
    "Retrieve the smallest sufficient task context from the KASANE knowledge "
    "vault as a Context Capsule v0.1 that fits the requested budget. Only "
    "canonical knowledge is returned. The result is the capsule JSON itself, "
    "or a budget_too_small_for_capsule protocol error."
)
WRITE_DESCRIPTIONS = {
    PROPOSE_TOOL: (
        "Record reusable procedural knowledge you established in this session "
        "(a procedure, design approach, situational guidance, pitfall, or "
        "constraint) as a new candidate. Cite at least one source. State the "
        "situation it applies to in the body. The result lists similar existing "
        "knowledge: if one of them already says the same thing, corroborate it "
        "next time instead of proposing a duplicate."
    ),
    CORROBORATE_TOOL: (
        "Independently confirm that existing knowledge matches what you found "
        "yourself. Do not corroborate knowledge you only read; derive it first."
    ),
    VERIFY_TOOL: (
        "Record an independent verification of a candidate: passed, failed, or "
        "rejected, with a note on how you checked. You cannot verify your own "
        "proposals. Only candidates take verifications."
    ),
    OUTCOME_TOOL: (
        "After applying knowledge from KASANE to a real task, report whether it "
        "worked (success) or not (failure), with a short note on the task. "
        "Report failures too: they protect other agents."
    ),
}
READ_INSTRUCTIONS = (
    "Call kasane_retrieve with a Retrieval Request v0.1 before searching for "
    "project knowledge yourself; trust the capsule's budget."
)
WRITE_INSTRUCTIONS = (
    " Report kasane_report_outcome for knowledge you applied. Propose knowledge "
    "only when it is reusable beyond this task; prefer corroborating existing "
    "knowledge over near-duplicates."
)


class RetrievalBackend(Protocol):
    def retrieve(self, request_value: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class WriteAccess:
    """Enables the intake tools for one configured agent identity."""

    service: TrustService
    agent: AgentContext


def load_request_schema(schema_dir: Path | None = None) -> dict[str, Any]:
    """Return the request schema (SSOT) as the tool input schema.

    ``$id`` is dropped because it identifies the wire contract document,
    not the MCP tool; every field definition is passed through unchanged.
    """

    return _tool_schema(REQUEST_SCHEMA, schema_dir)


def write_tool_schemas(schema_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Input schemas for the write tools, derived from the SSOT contracts.

    Signal tools take a subset of the signal contract's own property
    definitions; the server adds ``schema_version`` and ``kind`` and validates
    the assembled signal against the full contract.
    """

    signal = load_schema(SIGNAL_SCHEMA, schema_dir)["properties"]

    def subset(required: list[str], optional: list[str]) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": {name: signal[name] for name in [*required, *optional]},
        }

    return {
        PROPOSE_TOOL: _tool_schema(PROPOSAL_SCHEMA, schema_dir),
        CORROBORATE_TOOL: subset(["knowledge_ref"], ["note"]),
        VERIFY_TOOL: subset(["knowledge_ref", "verification", "note"], []),
        OUTCOME_TOOL: subset(["knowledge_ref", "outcome", "note"], []),
    }


def build_server(
    backend: RetrievalBackend,
    *,
    write: WriteAccess | None = None,
    schema_dir: Path | None = None,
) -> Server[Any]:
    schemas: dict[str, dict[str, Any]] = {TOOL_NAME: load_request_schema(schema_dir)}
    descriptions = {TOOL_NAME: TOOL_DESCRIPTION}
    if write is not None:
        schemas.update(write_tool_schemas(schema_dir))
        descriptions.update(WRITE_DESCRIPTIONS)
    tools = [
        types.Tool(name=name, description=descriptions[name], input_schema=schema)
        for name, schema in schemas.items()
    ]
    handlers: dict[str, Callable[[dict[str, Any]], types.CallToolResult]] = {
        TOOL_NAME: lambda args: _retrieve(backend, args)
    }
    if write is not None:
        handlers[PROPOSE_TOOL] = lambda args: _propose(write, args)
        for name, kind in (
            (CORROBORATE_TOOL, "corroboration"),
            (VERIFY_TOOL, "verification"),
            (OUTCOME_TOOL, "outcome"),
        ):
            handlers[name] = _signal_handler(write, kind)

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        handler = handlers.get(params.name)
        if handler is None:
            return _tool_error(f"unknown tool: {params.name}")
        return handler(dict(params.arguments or {}))

    return Server(
        "kasane",
        version=_version(),
        instructions=READ_INSTRUCTIONS + (WRITE_INSTRUCTIONS if write else ""),
        get_tool_input_schema=schemas.get,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def _retrieve(backend: RetrievalBackend, args: dict[str, Any]) -> types.CallToolResult:
    try:
        result = backend.retrieve(args)
    except ValueError as exc:
        # Malformed requests have no v0.1 wire error; report at tool level.
        return _tool_error(f"invalid retrieval request: {exc}")
    artifact: dict[str, Any] = result.artifact
    return _structured(artifact, is_error=result.error is not None)


def _propose(write: WriteAccess, args: dict[str, Any]) -> types.CallToolResult:
    try:
        result = write.service.propose(args, write.agent)
    except (ContractViolationError, TrustRejectedError) as exc:
        return _tool_error(f"proposal refused: {exc}")
    similar = find_similar(
        result.record.title,
        result.record.tags,
        result.record.body,
        write.service.repository.list_knowledge(),
        exclude_ref=result.record.knowledge_ref,
    )
    payload = {
        **result.summary(),
        "similar": [item.summary() for item in similar],
    }
    return _structured(payload)


def _signal_handler(
    write: WriteAccess, kind: str
) -> Callable[[dict[str, Any]], types.CallToolResult]:
    def handle(args: dict[str, Any]) -> types.CallToolResult:
        signal = {"schema_version": "0.1", "kind": kind, **args}
        try:
            result = write.service.signal(signal, write.agent)
        except (ContractViolationError, TrustRejectedError) as exc:
            return _tool_error(f"{kind} refused: {exc}")
        return _structured(result.summary())

    return handle


def _tool_schema(name: str, schema_dir: Path | None) -> dict[str, Any]:
    schema = load_schema(name, schema_dir)
    schema.pop("$id", None)
    return schema


def _structured(
    value: dict[str, Any], *, is_error: bool = False
) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=_dumps(value))],
        structured_content=value,
        is_error=is_error,
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


__all__ = [
    "PROPOSE_TOOL",
    "TOOL_NAME",
    "WRITE_TOOLS",
    "WriteAccess",
    "build_server",
    "default_schema_dir",
    "load_request_schema",
    "write_tool_schemas",
]
