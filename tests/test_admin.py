from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from kasane.admin import main as admin_main
from kasane.trust import AgentContext, TrustRejectedError, TrustService
from kasane.vault.errors import VaultError
from kasane.vault.models import EventType, KnowledgeStatus
from kasane.vault.repository import VaultRepository

REF = "vault://project/kasane/procedures/admin-target"


def agent(session: str, actor: str = "opencode") -> AgentContext:
    return AgentContext(actor=actor, session=session, model="m1")


def proposal(
    ref: str = REF, body: str = "Rebuild caches after deploys."
) -> dict[str, Any]:
    return {
        "schema_version": "0.1",
        "knowledge_ref": ref,
        "knowledge_type": "procedure",
        "title": ref.rsplit("/", 1)[-1].replace("-", " "),
        "body": body,
        "sources": [{"source_type": "session", "handle": "s"}],
    }


def distinct(index: int) -> str:
    """Body text that never trips the near-duplicate refusal."""

    import hashlib

    digest = hashlib.sha256(str(index).encode()).hexdigest()
    return " ".join(digest[i : i + 8] for i in range(0, 48, 8))


def signal(kind: str, ref: str = REF, **fields: Any) -> dict[str, Any]:
    return {"schema_version": "0.1", "knowledge_ref": ref, "kind": kind, **fields}


def to_t2(service: TrustService, ref: str = REF, author: str = "opencode") -> None:
    service.propose(proposal(ref), agent(f"author-{ref}", author))
    service.signal(
        signal("verification", ref, verification="passed", note="ok"),
        agent(f"v-{ref}", "reviewer"),
    )
    for session in ("u1", "u2"):
        service.signal(
            signal("outcome", ref, outcome="success", note="ok"),
            agent(f"{session}-{ref}", "user"),
        )


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return tmp_path / "vault.db"


@pytest.fixture
def service(vault: Path) -> TrustService:
    return TrustService(VaultRepository(vault))


def run(vault: Path, *args: str, capsys: pytest.CaptureFixture[str]) -> Any:
    code = admin_main(["--vault", str(vault), "--json", *args])
    assert code == 0
    return json.loads(capsys.readouterr().out)


# Write gates ---------------------------------------------------------------


def test_freeze_refuses_agent_writes_but_not_operator_actions(
    service: TrustService,
) -> None:
    to_t2(service)
    service.freeze(reason="incident")
    with pytest.raises(TrustRejectedError, match="frozen"):
        service.propose(proposal(REF + "-new"), agent("x"))
    with pytest.raises(TrustRejectedError, match="frozen"):
        service.signal(signal("corroboration"), agent("y"))
    assert service.approve(REF).record.status is KnowledgeStatus.CANONICAL
    service.unfreeze(reason="resolved")
    service.propose(proposal(REF + "-new", "Rotate credentials quarterly."), agent("x"))


def test_block_refuses_one_writer_only(service: TrustService) -> None:
    service.block("rogue", reason="spam")
    with pytest.raises(TrustRejectedError, match="blocked"):
        service.propose(proposal(), agent("s", "rogue"))
    service.propose(proposal(), agent("s", "opencode"))
    service.unblock("rogue", reason="cleared")
    service.signal(signal("corroboration"), agent("t", "rogue"))


def test_reserved_writers_cannot_be_blocked(service: TrustService) -> None:
    for name in ("operator", "policy", " "):
        with pytest.raises(TrustRejectedError):
            service.block(name, reason="no")


def test_operator_log_is_append_only(vault: Path, service: TrustService) -> None:
    service.freeze(reason="incident")
    connection = sqlite3.connect(vault)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM operator_actions")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE operator_actions SET reason = 'x'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO operator_actions(action, reason, details_json, actor,"
                " timestamp) VALUES ('FREEZE', '  ', '{}', 'operator', 't')"
            )
    finally:
        connection.close()


# Quarantine ----------------------------------------------------------------


def test_quarantine_is_a_dry_run_by_default(service: TrustService) -> None:
    service.propose(proposal(), agent("s", "rogue"))
    plan = service.quarantine(reason="bad batch", actor="rogue")
    assert (plan.targets, plan.applied, plan.archived) == ([REF], False, [])
    assert service.assess(REF).record.status is KnowledgeStatus.CANDIDATE
    assert service.repository.list_operator_actions() == []


def test_quarantine_archives_authored_knowledge_and_reports_vouched(
    service: TrustService,
) -> None:
    rogue_ref = "vault://project/kasane/procedures/rogue-canonical"
    to_t2(service, rogue_ref, author="rogue")
    service.approve(rogue_ref)
    other = "vault://project/kasane/procedures/other-author"
    service.propose(
        proposal(other, "Pin dependency versions in lockfiles."), agent("o", "opencode")
    )
    service.signal(signal("corroboration", other), agent("r", "rogue"))

    plan = service.quarantine(reason="compromised", actor="rogue", apply=True)

    assert plan.archived == [rogue_ref]
    assert plan.vouched == [other]
    events = service.repository.list_events(rogue_ref)
    assert [e.event_type for e in events[-2:]] == [
        EventType.DEPRECATED,
        EventType.ARCHIVED,
    ]
    assert {e.actor for e in events[-2:]} == {"operator"}
    assert service.assess(other).record.status is KnowledgeStatus.CANDIDATE
    logged = service.repository.list_operator_actions()
    assert [a.action for a in logged] == ["QUARANTINE"]
    assert logged[0].target == "rogue"


def test_quarantine_since_and_refs(service: TrustService) -> None:
    now = {"value": "2026-01-01T00:00:00Z"}
    repository = VaultRepository(
        service.repository.database_path, clock=lambda: now["value"]
    )
    timed = TrustService(repository)
    old = "vault://project/kasane/procedures/old"
    timed.propose(proposal(old, "Use WAL mode for SQLite."), agent("a", "rogue"))
    now["value"] = "2026-06-01T00:00:00Z"
    new = "vault://project/kasane/procedures/new"
    timed.propose(proposal(new, "Never log secrets."), agent("b", "rogue"))
    plan = timed.quarantine(
        reason="x", actor="rogue", since="2026-03-01T00:00:00+00:00"
    )
    assert plan.targets == [new]
    by_ref = service.quarantine(reason="x", refs=[old])
    assert by_ref.targets == [old]
    with pytest.raises(TrustRejectedError):
        service.quarantine(reason="x", refs=["vault://project/kasane/missing/ref"])
    with pytest.raises(TrustRejectedError, match="actor or"):
        service.quarantine(reason="x")
    with pytest.raises(TrustRejectedError, match="timezone"):
        service.quarantine(reason="x", actor="rogue", since="2026-03-01T00:00:00")


# Review carry-overs from PR #44 ----------------------------------------------


def test_committed_signal_is_not_reported_as_refused_when_policy_fails(
    service: TrustService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.propose(proposal(), agent("author"))

    def broken(ref: str) -> bool:
        raise VaultError("policy step failed")

    monkeypatch.setattr(service, "_apply_policy", broken)
    result = service.signal(signal("corroboration"), agent("a"))
    assert result.record.knowledge_ref == REF
    assert service.repository.list_events(REF)[-1].event_type is EventType.CORROBORATED


def test_reconcile_reports_skipped_records(
    service: TrustService, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.propose(proposal(), agent("author"))
    original = service._apply_policy

    def flaky(ref: str) -> bool:
        if ref == REF:
            raise VaultError("bad record")
        return original(ref)

    monkeypatch.setattr(service, "_apply_policy", flaky)
    result = service.reconcile()
    assert (result.changed, result.skipped) == ([], [REF])


# CLI -----------------------------------------------------------------------


def test_cli_queue_approve_status_events(
    vault: Path, service: TrustService, capsys: pytest.CaptureFixture[str]
) -> None:
    to_t2(service)
    queue = run(vault, "queue", capsys=capsys)
    assert [item["knowledge_ref"] for item in queue] == [REF]
    assert queue[0]["tier"] == "T2"
    assert {"opencode", "reviewer", "user"} <= set(queue[0]["actors"])

    approved = run(vault, "approve", REF, "--note", "fine", capsys=capsys)
    assert approved["status"] == "canonical"

    status = run(vault, "status", capsys=capsys)
    assert status["knowledge"]["canonical"] == 1
    assert (status["frozen"], status["approval_queue"]) == (False, 0)

    events = run(vault, "events", "--actor", "operator", capsys=capsys)
    assert [e["event"] for e in events] == ["PROMOTED"]
    later = run(vault, "events", "--since", "2999-01-01T00:00:00+00:00", capsys=capsys)
    assert later == []


def test_cli_freeze_block_quarantine(
    vault: Path, service: TrustService, capsys: pytest.CaptureFixture[str]
) -> None:
    service.propose(proposal(), agent("s", "rogue"))
    assert run(vault, "freeze", "--reason", "incident", capsys=capsys)["frozen"] is True
    blocked = run(vault, "block", "rogue", "--reason", "x", capsys=capsys)
    assert blocked["blocked_writers"] == ["rogue"]
    dry = run(vault, "quarantine", "--actor", "rogue", "--reason", "x", capsys=capsys)
    assert (dry["applied"], dry["targets"], dry["archived"]) == (False, [REF], [])
    applied = run(
        vault, "quarantine", "--actor", "rogue", "--reason", "x", "--yes", capsys=capsys
    )
    assert applied["archived"] == [REF]
    assert run(vault, "unfreeze", "--reason", "done", capsys=capsys)["frozen"] is False


def test_cli_errors_exit_nonzero(
    vault: Path, service: TrustService, capsys: pytest.CaptureFixture[str]
) -> None:
    service.propose(proposal(), agent("s"))
    with pytest.raises(SystemExit) as refused:
        admin_main(["--vault", str(vault), "reject", REF, "--reason", "no"])
    assert refused.value.code == 1
    assert "approval queue" in capsys.readouterr().err
    with pytest.raises(SystemExit) as missing:
        admin_main(["--vault", str(vault.with_name("missing.db")), "status"])
    assert missing.value.code == 2
    assert not vault.with_name("missing.db").exists()


def test_cli_human_output_is_readable(
    vault: Path, service: TrustService, capsys: pytest.CaptureFixture[str]
) -> None:
    to_t2(service)
    assert admin_main(["--vault", str(vault), "queue"]) == 0
    out = capsys.readouterr().out
    assert "T2" in out and REF in out and "successes=2" in out


# Regression tests for independent review findings on PR #46 -----------------


def test_events_show_the_newest_and_filter_since_as_instants(
    vault: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    now = {"value": "2026-01-01T00:00:00Z"}
    repository = VaultRepository(vault, clock=lambda: now["value"])
    service = TrustService(repository)
    for index in range(30):
        now["value"] = f"2026-01-01T00:00:{index:02d}Z"
        service.propose(
            proposal(
                f"vault://project/kasane/bulk/item-{index:02d}",
                distinct(index),
            ),
            agent(f"s{index}"),
        )
    newest = run(vault, "events", "--limit", "3", capsys=capsys)
    assert [e["knowledge_ref"].rsplit("-", 1)[-1] for e in newest] == ["27", "28", "29"]
    # Fraction-free cutoff must still include events at exactly that second.
    since = run(vault, "events", "--since", "2026-01-01T00:00:28+00:00", capsys=capsys)
    assert [e["knowledge_ref"].rsplit("-", 1)[-1] for e in since] == ["28", "29"]


def test_naive_stored_timestamps_do_not_crash_since_filters(vault: Path) -> None:
    repository = VaultRepository(vault, clock=lambda: "2026-05-01T00:00:00")
    service = TrustService(repository)
    service.propose(proposal(), agent("s", "rogue"))
    cutoff = "2026-04-01T00:00:00+00:00"
    assert service.quarantine(reason="x", actor="rogue", since=cutoff).targets == [REF]
    from kasane.trust.service import parse_since

    events = repository.search_events(since=parse_since(cutoff))
    assert [e.knowledge_ref for e in events] == [REF]


@pytest.mark.parametrize(
    "argv",
    [
        ["events", "--since", "garbage"],
        ["events", "--since", "2026-01-01T00:00:00"],
        ["events", "--ref", "vault://project/onlyowner"],
        ["quarantine", "--ref", "vault://project/onlyowner", "--reason", "x"],
        ["approve", "vault://global/a b"],
    ],
)
def test_bad_operator_input_is_a_message_not_a_traceback(
    vault: Path,
    service: TrustService,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        admin_main(["--vault", str(vault), *argv])
    assert raised.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("kasane-admin: ")
    assert "Traceback" not in err


def test_quarantine_logs_even_when_the_sweep_aborts(
    service: TrustService, monkeypatch: pytest.MonkeyPatch
) -> None:
    refs = [f"vault://project/kasane/rogue/item-{i}" for i in range(3)]
    for i, ref in enumerate(refs):
        service.propose(
            proposal(ref, distinct(100 + i)),
            agent(f"r{i}", "rogue"),
        )
    original = service._archive_now
    calls = {"n": 0}

    def locked(ref: str, reason: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("database is locked")
        original(ref, reason)

    monkeypatch.setattr(service, "_archive_now", locked)
    with pytest.raises(sqlite3.OperationalError):  # the CLI reports it, exit 1
        service.quarantine(reason="incident", actor="rogue", apply=True)
    logged = service.repository.list_operator_actions()
    assert [a.action for a in logged] == ["QUARANTINE"]
    details = logged[0].details
    assert isinstance(details, dict)
    assert details["archived"] == [refs[0]]
    assert details["failed"] == refs[1:]


def test_block_target_with_whitespace_is_refused(service: TrustService) -> None:
    for name in (" opencode", "opencode ", ""):
        with pytest.raises(TrustRejectedError):
            service.block(name, reason="x")


def test_swallowed_policy_failure_is_logged(
    service: TrustService,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service.propose(proposal(), agent("author"))

    def broken(ref: str) -> bool:
        raise VaultError("policy step failed")

    monkeypatch.setattr(service, "_apply_policy", broken)
    with caplog.at_level("WARNING", logger="kasane.trust.service"):
        service.signal(signal("corroboration"), agent("a"))
    assert "reconcile will retry" in caplog.text
