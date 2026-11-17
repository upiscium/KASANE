"""``kasane-admin move``: relocate knowledge with its history (Issue #49)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kasane.admin import main as admin_main
from kasane.retrieval.service import Level0RetrievalService
from kasane.trust import AgentContext, TrustRejectedError, TrustService
from kasane.vault.errors import StaleRevisionError
from kasane.vault.models import EventType, KnowledgeStatus
from kasane.vault.repository import VaultRepository

OLD = "vault://project/suzka/pitfall/sqlite-rebuild"
NEW = "vault://global/pitfall/sqlite-rebuild"


def agent(session: str, actor: str = "claude-code") -> AgentContext:
    return AgentContext(actor=actor, session=session, model="m1")


def proposal(ref: str = OLD) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "knowledge_ref": ref,
        "knowledge_type": "pitfall",
        "title": "SQLite table rebuild quotes the renamed table",
        "body": "Situation: rebuilding a SQLite table. RENAME stores a quoted name.",
        "tags": ["sqlite"],
        "sources": [{"source_type": "session", "handle": "s"}],
    }


def signal(kind: str, ref: str = OLD, **fields: Any) -> dict[str, Any]:
    return {"schema_version": "0.1", "knowledge_ref": ref, "kind": kind, **fields}


def to_t2(service: TrustService, ref: str = OLD) -> None:
    service.propose(proposal(ref), agent("author"))
    service.signal(
        signal("verification", ref, verification="passed", note="ok"),
        agent("v", "reviewer"),
    )
    for session in ("u1", "u2"):
        service.signal(
            signal("outcome", ref, outcome="success", note="ok"),
            agent(session, "user"),
        )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / "vault.db"


@pytest.fixture
def service(vault: Path) -> TrustService:
    return TrustService(VaultRepository(vault))


def snapshot(vault: Path) -> tuple[Any, ...]:
    connection = sqlite3.connect(vault)
    try:
        return tuple(
            connection.execute(query).fetchall()
            for query in (
                (
                    "SELECT knowledge_ref, status, revision FROM knowledge_records "
                    "ORDER BY knowledge_ref"
                ),
                (
                    "SELECT event_id, knowledge_ref FROM knowledge_events "
                    "ORDER BY event_sequence"
                ),
                "SELECT sequence, action FROM operator_actions ORDER BY sequence",
            )
        )
    finally:
        connection.close()


def retrieved(vault: Path, scope: list[str]) -> list[str]:
    retrieval = Level0RetrievalService(
        VaultRepository(vault), vault.with_name("index.db")
    )
    capsule = retrieval.retrieve(
        {
            "schema_version": "0.1",
            "mode": "fast",
            "query": "SQLite table rebuild",
            "min_trust": "T0",
            "scope": scope,
            "budget": {"max_tokens": 2000, "max_bytes": 8000, "max_evidence_items": 5},
        }
    ).artifact
    return [item["uri"] for item in capsule.get("knowledge_refs", [])]


def test_dry_run_reports_the_plan_and_changes_nothing(
    vault: Path, service: TrustService
) -> None:
    service.propose(proposal(), agent("author"))
    before = snapshot(vault)
    plan = service.move(OLD, NEW, reason="general SQLite knowledge")
    assert plan.summary() == {
        "applied": False,
        "from": OLD,
        "to": NEW,
        "status": "candidate",
        "tier": "T0",
        "events": 1,
    }
    assert snapshot(vault) == before


def test_move_copies_record_and_history_and_retires_the_source(
    vault: Path, service: TrustService
) -> None:
    service.propose(proposal(), agent("author"))
    source = service.repository.get_knowledge(OLD)
    original = service.repository.list_events(OLD)
    plan = service.move(OLD, NEW, reason="general SQLite knowledge", apply=True)
    assert (plan.applied, plan.tier, plan.events) == (True, "T0", 1)

    moved = service.repository.get_knowledge(NEW)
    assert (moved.namespace, moved.knowledge_path) == (
        "global",
        "pitfall/sqlite-rebuild",
    )
    for field in (
        "title",
        "body",
        "tags",
        "sources",
        "status",
        "revision",
        "created_at",
    ):
        assert getattr(moved, field) == getattr(source, field)

    copies = service.repository.list_events(NEW)
    assert [(e.event_type, e.actor, e.timestamp, e.revision) for e in copies] == [
        (e.event_type, e.actor, e.timestamp, e.revision) for e in original
    ]
    assert copies[0].event_id != original[0].event_id
    assert isinstance(copies[0].metadata, dict) and isinstance(
        original[0].metadata, dict
    )
    assert copies[0].metadata["agent"] == original[0].metadata["agent"]
    assert copies[0].metadata["moved_from"] == [
        {"knowledge_ref": OLD, "event_id": original[0].event_id}
    ]

    retired = service.repository.get_knowledge(OLD)
    assert retired.status is KnowledgeStatus.ARCHIVED
    last = service.repository.list_events(OLD)[-1]
    assert last.event_type is EventType.ARCHIVED
    assert last.metadata == {
        "decision": "move",
        "moved_to": NEW,
        "reason": "general SQLite knowledge",
    }

    assert retrieved(vault, ["global", "project/kumiki"]) == [NEW]
    assert retrieved(vault, ["project/suzka"]) == []


def test_trust_and_authorship_survive_the_move(service: TrustService) -> None:
    to_t2(service)
    before = service.assess(OLD).assessment
    service.move(OLD, NEW, reason="general", apply=True)
    after = service.assess(NEW).assessment
    assert after.summary() == before.summary()
    assert after.tier == before.tier and after.approval_eligible
    assert after.author == before.author
    assert [r.record.knowledge_ref for r in service.approval_queue()] == [NEW]

    # The original author is still the author: no self-verification after a move.
    other = "vault://project/suzka/pitfall/second"
    service.propose(
        {**proposal(other), "body": "An unrelated second note."}, agent("a2")
    )
    service.move(other, "vault://global/pitfall/second", reason="general", apply=True)
    with pytest.raises(TrustRejectedError, match="self-verification"):
        service.signal(
            signal(
                "verification",
                "vault://global/pitfall/second",
                verification="passed",
                note="mine",
            ),
            agent("a2"),
        )


def test_repeated_moves_keep_the_link_to_the_first_event(
    service: TrustService,
) -> None:
    service.propose(proposal(), agent("author"))
    first = service.repository.list_events(OLD)[0].event_id
    service.move(OLD, NEW, reason="general", apply=True)
    hop = service.repository.list_events(NEW)[0].event_id
    final = "vault://global/sqlite/table-rebuild"
    plan = service.move(NEW, final, reason="regroup", apply=True)
    assert plan.events == 1
    copy = service.repository.list_events(final)[0]
    assert isinstance(copy.metadata, dict)
    assert copy.metadata["moved_from"] == [
        {"knowledge_ref": OLD, "event_id": first},
        {"knowledge_ref": NEW, "event_id": hop},
    ]
    assert service.assess(final).assessment.author == (
        service.assess(OLD).assessment.author
    )


def test_quarantine_by_author_still_finds_moved_knowledge(
    service: TrustService,
) -> None:
    service.propose(proposal(), agent("author"))
    service.move(OLD, NEW, reason="general", apply=True)
    plan = service.quarantine(reason="check", actor="claude-code")
    assert plan.targets == [NEW]


def test_canonical_source_retires_through_deprecated(service: TrustService) -> None:
    to_t2(service)
    service.approve(OLD)
    service.move(OLD, NEW, reason="general", apply=True)
    assert service.repository.get_knowledge(NEW).status is KnowledgeStatus.CANONICAL
    assert service.assess(NEW).assessment.summary()["tier"] == "T3"
    tail = [e.event_type for e in service.repository.list_events(OLD)][-2:]
    assert tail == [EventType.DEPRECATED, EventType.ARCHIVED]
    assert service.repository.get_knowledge(OLD).status is KnowledgeStatus.ARCHIVED


def test_one_move_action_is_logged(service: TrustService) -> None:
    service.propose(proposal(), agent("author"))
    service.move(OLD, NEW, reason="general", apply=True)
    actions = service.repository.list_operator_actions()
    assert [(a.action, a.target, a.reason) for a in actions] == [
        ("MOVE", OLD, "general")
    ]
    assert actions[0].details == {"from": OLD, "to": NEW, "events": 1}
    assert actions[0].actor == "operator"


@pytest.mark.parametrize(
    ("source", "target", "reason", "message"),
    [
        ("vault://project/suzka/pitfall/missing", NEW, "r", "unknown knowledge"),
        (OLD, "vault://bogus", "r", "invalid v0.1 knowledge identity"),
        (OLD, OLD, "r", "same knowledge_ref"),
        (OLD, "vault://global/pitfall/taken", "r", "already exists"),
        (OLD, NEW, "  ", "requires a reason"),
    ],
)
def test_refusals_leave_the_vault_unchanged(
    vault: Path,
    service: TrustService,
    source: str,
    target: str,
    reason: str,
    message: str,
) -> None:
    service.propose(proposal(), agent("author"))
    service.propose(
        {**proposal("vault://global/pitfall/taken"), "body": "Something else."},
        agent("other"),
    )
    before = snapshot(vault)
    for apply in (False, True):
        with pytest.raises(TrustRejectedError, match=message):
            service.move(source, target, reason=reason, apply=apply)
    assert snapshot(vault) == before


def test_archived_source_is_refused(vault: Path, service: TrustService) -> None:
    service.propose(proposal(), agent("author"))
    service.move(OLD, NEW, reason="general", apply=True)
    before = snapshot(vault)
    with pytest.raises(TrustRejectedError, match="only live knowledge"):
        service.move(OLD, "vault://global/pitfall/again", reason="r", apply=True)
    assert snapshot(vault) == before


def test_stale_view_is_refused_inside_the_transaction(
    vault: Path, service: TrustService
) -> None:
    service.propose(proposal(), agent("author"))
    record = service.repository.get_knowledge(OLD)
    first = service.repository.list_events(OLD)[0].event_id
    service.signal(signal("corroboration"), agent("late", "reviewer"))
    before = snapshot(vault)
    with pytest.raises(StaleRevisionError):
        service.repository.move_knowledge(
            OLD,
            NEW,
            expected_revision=record.revision,
            expected_last_event_id=first,
            actor="operator",
            reason="r",
        )
    assert snapshot(vault) == before


def test_failure_part_way_rolls_back(
    vault: Path, service: TrustService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.propose(proposal(), agent("author"))
    before = snapshot(vault)

    def explode(*_args: Any, **_kwargs: Any) -> int:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(
        VaultRepository, "_insert_operator_action", staticmethod(explode)
    )
    with pytest.raises(sqlite3.OperationalError):
        service.move(OLD, NEW, reason="r", apply=True)
    assert snapshot(vault) == before


def test_v4_vault_migrates_to_v5_keeping_operator_actions(tmp_path: Path) -> None:
    import kasane.vault.repository as repository_module
    from kasane.vault import migrations

    path = tmp_path / "v4.db"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:4])
        patch.setattr(repository_module, "CURRENT_SCHEMA_VERSION", 4)
        legacy = VaultRepository(path)
        legacy.record_operator_action("FREEZE", reason="incident", actor="operator")
        legacy.record_operator_action("UNFREEZE", reason="done", actor="operator")
        with pytest.raises(sqlite3.IntegrityError):
            legacy.record_operator_action("MOVE", reason="r", actor="operator")
        kept = legacy.list_operator_actions()
    upgraded = VaultRepository(path)
    assert upgraded.current_schema_version() == 5
    assert upgraded.list_operator_actions() == kept
    moved = upgraded.record_operator_action("MOVE", reason="r", actor="operator")
    assert moved.sequence == kept[-1].sequence + 1
    connection = sqlite3.connect(path)
    try:
        for statement in (
            "DELETE FROM operator_actions",
            "UPDATE operator_actions SET reason = 'x'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(statement)
    finally:
        connection.close()


def run(vault: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> Any:
    code = admin_main(["--vault", str(vault), "--json", *args])
    assert code == 0
    return json.loads(capsys.readouterr().out)


def test_cli_move_dry_run_then_apply(
    vault: Path, service: TrustService, capsys: pytest.CaptureFixture[str]
) -> None:
    service.propose(proposal(), agent("author"))
    dry = run(vault, "move", OLD, NEW, "--reason", "general", capsys=capsys)
    assert (dry["applied"], dry["from"], dry["to"]) == (False, OLD, NEW)
    assert service.repository.get_knowledge(OLD).status is KnowledgeStatus.CANDIDATE
    done = run(vault, "move", OLD, NEW, "--reason", "general", "--yes", capsys=capsys)
    assert (done["applied"], done["tier"]) == (True, "T0")
    assert service.repository.get_knowledge(OLD).status is KnowledgeStatus.ARCHIVED


def test_cli_move_refusal_is_a_message(
    vault: Path, service: TrustService, capsys: pytest.CaptureFixture[str]
) -> None:
    service.propose(proposal(), agent("author"))
    with pytest.raises(SystemExit) as raised:
        admin_main(["--vault", str(vault), "move", OLD, OLD, "--reason", "r", "--yes"])
    assert raised.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("kasane-admin: ") and "Traceback" not in err
