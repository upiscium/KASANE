from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from kasane.mcp_server import (
    PROPOSE_TOOL,
    TOOL_NAME,
    WRITE_TOOLS,
    WriteAccess,
    build_server,
    write_tool_schemas,
)
from kasane.mcp_server.__main__ import main as cli_main
from kasane.retrieval.service import Level0RetrievalService
from kasane.trust import AgentContext, TrustService
from kasane.trust.similar import find_similar, tokens
from kasane.vault.models import KnowledgeStatus
from kasane.vault.repository import VaultRepository

ROOT = Path(__file__).resolve().parents[1]
REF = "vault://project/kasane/procedures/rebuild-index-after-schema-change"


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.repository = VaultRepository(tmp_path / "vault.db")
        self.retrieval = Level0RetrievalService(self.repository, tmp_path / "index.db")
        self.trust = TrustService(self.repository)

    def call(self, session: str, tool: str, args: Any, actor: str = "opencode") -> Any:
        agent = AgentContext(actor=actor, session=session, model="m1")
        server = build_server(self.retrieval, write=WriteAccess(self.trust, agent))

        async def run() -> Any:
            async with Client(server) as client:
                return await client.call_tool(tool, args)

        return anyio.run(run)


def proposal(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": "0.1",
        "knowledge_ref": REF,
        "knowledge_type": "procedure",
        "title": "Rebuild the derived index after a schema change",
        "body": "Drop and rebuild the lexical index; never migrate it in place.",
        "tags": ["index", "migration"],
        "sources": [{"source_type": "session", "handle": "session:author"}],
    }
    value.update(overrides)
    return value


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _list_tools(server: Any) -> Any:
    async def run() -> Any:
        async with Client(server) as client:
            return await client.list_tools()

    return anyio.run(run).tools


def test_write_tools_only_when_enabled(env: Env) -> None:
    read_only = _list_tools(build_server(env.retrieval))
    assert [tool.name for tool in read_only] == [TOOL_NAME]
    agent = AgentContext(actor="opencode", session="s", model="m1")
    tools = _list_tools(
        build_server(env.retrieval, write=WriteAccess(env.trust, agent))
    )
    assert [tool.name for tool in tools] == [TOOL_NAME, *WRITE_TOOLS]


def test_write_tool_schemas_derive_from_contracts() -> None:
    schemas = write_tool_schemas()
    proposal_ssot = json.loads(
        (ROOT / "schemas/knowledge-proposal.schema.json").read_text()
    )
    proposal_ssot.pop("$id")
    assert schemas[PROPOSE_TOOL] == proposal_ssot
    signal = json.loads((ROOT / "schemas/knowledge-signal.schema.json").read_text())
    for name, schema in schemas.items():
        if name == PROPOSE_TOOL:
            continue
        assert schema["additionalProperties"] is False
        for prop, definition in schema["properties"].items():
            assert definition == signal["properties"][prop]


def test_full_intake_loop_over_mcp(env: Env) -> None:
    created = env.call("author", PROPOSE_TOOL, proposal())
    assert created.is_error is False
    assert created.structured_content["tier"] == "T0"

    env.call("a", "kasane_corroborate", {"knowledge_ref": REF})
    verified = env.call(
        "b", "kasane_corroborate", {"knowledge_ref": REF, "note": "same"}
    )
    assert verified.structured_content["status"] == "verified"

    for session in ("u1", "u2"):
        result = env.call(
            session,
            "kasane_report_outcome",
            {"knowledge_ref": REF, "outcome": "success", "note": "rebuilt cleanly"},
        )
    assert result.structured_content["tier"] == "T2"
    assert result.structured_content["approval_eligible"] is True
    # Policy never reaches canonical; still not served by Level 0 (until #41).
    assert env.repository.get_knowledge(REF).status is KnowledgeStatus.VERIFIED


def test_propose_returns_similar_existing_knowledge(env: Env) -> None:
    env.call("author", PROPOSE_TOOL, proposal())
    near = env.call(
        "other",
        PROPOSE_TOOL,
        proposal(
            knowledge_ref=REF + "-again",
            title="Rebuild the lexical index after schema changes",
        ),
    )
    refs = [item["knowledge_ref"] for item in near.structured_content["similar"]]
    assert refs == [REF]
    unrelated = env.call(
        "other",
        PROPOSE_TOOL,
        proposal(
            knowledge_ref="vault://project/kasane/pitfalls/tz",
            knowledge_type="pitfall",
            title="Naive timestamps break freshness checks",
            body="Always store timezone-aware ISO 8601 values.",
            tags=["time"],
        ),
    )
    assert unrelated.structured_content["similar"] == []


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        (PROPOSE_TOOL, proposal(sources=[]), "proposal refused"),
        (
            "kasane_verify",
            {"knowledge_ref": REF, "verification": "passed", "note": "me"},
            "self-verification",
        ),
        (
            "kasane_report_outcome",
            {"knowledge_ref": REF, "outcome": "maybe", "note": "x"},
            "refused",
        ),
        ("kasane_corroborate", {"knowledge_ref": "vault://global/missing"}, "unknown"),
    ],
)
def test_refusals_are_tool_errors(env: Env, tool: str, args: Any, message: str) -> None:
    env.call("author", PROPOSE_TOOL, proposal())
    result = env.call("author", tool, args)
    assert result.is_error is True
    assert message in result.content[0].text


def test_duplicate_proposal_is_refused(env: Env) -> None:
    env.call("author", PROPOSE_TOOL, proposal())
    again = env.call("other", PROPOSE_TOOL, proposal())
    assert again.is_error is True
    assert "corroborate" in again.content[0].text


def test_japanese_text_is_comparable() -> None:
    assert {"索引", "再構"} <= tokens("索引を再構築する")
    assert tokens("Ｉｎｄｅｘ") == {"index"}  # NFKC folds full-width forms


def test_find_similar_ignores_retired_and_self(env: Env) -> None:
    env.call("author", PROPOSE_TOOL, proposal())
    records = env.repository.list_knowledge()
    assert (
        find_similar("Rebuild the derived index", [], "", records, exclude_ref=REF)
        == []
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--allow-write"],
        ["--allow-write", "--actor", "opencode"],
        ["--allow-write", "--actor", "operator", "--model", "m"],
    ],
)
def test_serve_write_requires_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    for name in ("KASANE_ACTOR", "KASANE_MODEL", "KASANE_SESSION"):
        monkeypatch.delenv(name, raising=False)
    vault = tmp_path / "vault.db"
    VaultRepository(vault)
    with pytest.raises(SystemExit) as raised:
        cli_main(["serve", "--vault", str(vault), *extra])
    assert raised.value.code == 2
