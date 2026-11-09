from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import jsonschema
import pytest
from mcp import Client

from kasane.evaluation.level0 import TEST_TOKENIZER_ID, seed_fixture_vault
from kasane.mcp_server import TOOL_NAME, build_server, load_request_schema
from kasane.mcp_server.__main__ import main as cli_main
from kasane.retrieval.service import Level0RetrievalService
from kasane.vault.repository import VaultRepository

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evaluation/level0/knowledge-fixture.json"
GOLDEN = json.loads((ROOT / "evaluation/level0/golden-queries.json").read_text())
BASELINE = json.loads((ROOT / "evaluation/level0/baseline.json").read_text())
PROFILES = json.loads((ROOT / "evaluation/level0/budget-profiles.json").read_text())
CAPSULE_SCHEMA = json.loads((ROOT / "schemas/context-capsule.schema.json").read_text())
ERROR_SCHEMA = json.loads((ROOT / "schemas/retrieval-error.schema.json").read_text())


@pytest.fixture
def service(tmp_path: Path) -> Level0RetrievalService:
    vault = tmp_path / "vault.db"
    seed_fixture_vault(FIXTURE, vault)
    return Level0RetrievalService(
        VaultRepository(vault),
        tmp_path / "index.db",
        token_counters={TEST_TOKENIZER_ID: len},
    )


def _call(service: Level0RetrievalService, name: str, arguments: Any) -> Any:
    async def run() -> Any:
        async with Client(build_server(service)) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(run)


def _request(query_id: str = "q01-exact-title") -> dict[str, Any]:
    query = next(q for q in GOLDEN["queries"] if q["query_id"] == query_id)
    request: dict[str, Any] = dict(query["request"])
    request["budget"] = dict(PROFILES["profiles"]["normal"])
    request["tokenizer"] = {"id": TEST_TOKENIZER_ID}
    request.setdefault("min_trust", "T3")  # compare with the canonical baseline
    return request


def test_lists_exactly_one_tool_with_ssot_input_schema(
    service: Level0RetrievalService,
) -> None:
    async def run() -> Any:
        async with Client(build_server(service)) as client:
            return await client.list_tools()

    tools = anyio.run(run).tools
    assert [tool.name for tool in tools] == [TOOL_NAME]
    expected = json.loads((ROOT / "schemas/retrieval-request.schema.json").read_text())
    expected.pop("$id")
    assert tools[0].input_schema == expected == load_request_schema()


def test_returns_schema_valid_capsule(service: Level0RetrievalService) -> None:
    result = _call(service, TOOL_NAME, _request())
    assert result.is_error is False
    capsule = result.structured_content
    jsonschema.validate(capsule, CAPSULE_SCHEMA)
    assert json.loads(result.content[0].text) == capsule
    refs = [item["uri"] for item in capsule["knowledge_refs"]]
    assert refs[0] == "vault://global/transactions/atomic-write"


def test_seeded_vault_matches_level0_baseline_selection(
    service: Level0RetrievalService,
) -> None:
    per_query = {
        item["query_id"]: item for item in BASELINE["profiles"]["normal"]["per_query"]
    }
    for query in GOLDEN["queries"]:
        result = _call(service, TOOL_NAME, _request(query["query_id"]))
        artifact = result.structured_content
        selected = [item["uri"] for item in artifact.get("knowledge_refs", [])]
        assert selected == per_query[query["query_id"]]["selected_refs"], query[
            "query_id"
        ]


def test_budget_too_small_returns_wire_error(service: Level0RetrievalService) -> None:
    request = _request()
    request["budget"] = {"max_tokens": 1, "max_bytes": 1, "max_evidence_items": 1}
    result = _call(service, TOOL_NAME, request)
    assert result.is_error is True
    jsonschema.validate(result.structured_content, ERROR_SCHEMA)


def test_invalid_request_is_tool_error_not_crash(
    service: Level0RetrievalService,
) -> None:
    result = _call(service, TOOL_NAME, {"schema_version": "0.1", "query": "x"})
    assert result.is_error is True
    assert result.content[0].text.startswith("invalid retrieval request:")


def test_unknown_tool_is_tool_error(service: Level0RetrievalService) -> None:
    result = _call(service, "other", {})
    assert result.is_error is True


def test_serve_fails_closed_without_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KASANE_VAULT_PATH", raising=False)
    missing = tmp_path / "missing.db"
    with pytest.raises(SystemExit) as raised:
        cli_main(["serve", "--vault", str(missing)])
    assert raised.value.code == 2
    assert not missing.exists()
    with pytest.raises(SystemExit):
        cli_main(["serve"])


def test_seed_refuses_existing_vault(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    assert cli_main(["seed-fixture", "--vault", str(vault)]) == 0
    with pytest.raises(SystemExit) as raised:
        cli_main(["seed-fixture", "--vault", str(vault)])
    assert raised.value.code == 2


def test_server_default_scope_applies_only_when_request_omits_scope(
    tmp_path: Path,
) -> None:
    from kasane.trust import AgentContext, TrustService

    repository = VaultRepository(tmp_path / "vault.db")
    service = Level0RetrievalService(
        repository, tmp_path / "index.db", token_counters={TEST_TOKENIZER_ID: len}
    )
    TrustService(repository).propose(
        {
            "schema_version": "0.1",
            "knowledge_ref": "vault://project/kasane/procedures/scoped",
            "knowledge_type": "procedure",
            "title": "Scoped project knowledge",
            "body": "project scoped marker",
            "sources": [{"source_type": "other", "handle": "t"}],
        },
        AgentContext("a", "s", "m"),
    )
    base = {
        "schema_version": "0.1",
        "query": "project scoped marker",
        "mode": "fast",
        "min_trust": "T0",
        "budget": {"max_tokens": 5000, "max_bytes": 20000, "max_evidence_items": 3},
    }

    def refs(server: Any, request: dict[str, Any]) -> list[str]:
        async def run() -> Any:
            async with Client(server) as client:
                return await client.call_tool(TOOL_NAME, request)

        capsule = anyio.run(run).structured_content
        return [item["uri"] for item in capsule.get("knowledge_refs", [])]

    plain = build_server(service)
    widened = build_server(service, default_scope=["global", "project/kasane"])
    assert refs(plain, base) == []  # contract default is ["global"]
    assert refs(widened, base) == ["vault://project/kasane/procedures/scoped"]
    assert refs(widened, {**base, "scope": ["global"]}) == []  # explicit wins


def test_invalid_default_scope_fails_at_startup(tmp_path: Path) -> None:
    vault = tmp_path / "vault.db"
    VaultRepository(vault)
    with pytest.raises(SystemExit) as raised:
        cli_main(["serve", "--vault", str(vault), "--default-scope", "bogus/../x"])
    assert raised.value.code == 2
