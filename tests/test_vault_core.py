from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from kasane import (
    EventType,
    InvalidKnowledgeIdentityError,
    InvalidLifecycleTransitionError,
    InvalidMetadataError,
    KnowledgeDraft,
    KnowledgeStatus,
    StaleRevisionError,
    VaultRepository,
    VaultService,
    VerificationOutcome,
)
from kasane.vault.config import default_database_path
from kasane.vault.migrations import CURRENT_SCHEMA_VERSION


def sequence(prefix: str) -> Callable[[], str]:
    counter = 0

    def next_value() -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}-{counter:04d}"

    return next_value


@pytest.fixture
def repository(tmp_path: Path) -> VaultRepository:
    return VaultRepository(
        tmp_path / "vault.db",
        clock=sequence("2026-08-22T00:00:00Z"),
        event_id_factory=sequence("event"),
    )


@pytest.fixture
def draft() -> KnowledgeDraft:
    return KnowledgeDraft(
        knowledge_ref="vault://project/terreate/buffer/write-contract",
        knowledge_type="contract",
        title="Buffer write contract",
        body="Writes are committed atomically.",
        stability="evolving",
        tags=["buffer", "transaction"],
        scope={"project": "terreate", "paths": ["src/**"]},
        applies_when={"operation": "write"},
        counterconditions=[{"when": "read-only"}],
        sources=[{"kind": "repository", "uri": "repo://terreate/docs/write.md"}],
        provenance={"authors": ["agent-a"], "chain": {"parent": "source-1"}},
        generated={"generator": "fixture", "parameters": {"temperature": 0}},
        verification={"state": "unverified"},
        stale_after="2027-01-01T00:00:00Z",
    )


def test_create_get_and_list_preserve_public_identity(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    created = repository.create_candidate(draft, actor="agent-a")

    assert created.knowledge_ref == draft.knowledge_ref
    assert created.namespace == "project/terreate"
    assert created.knowledge_path == "buffer/write-contract"
    assert created.status is KnowledgeStatus.CANDIDATE
    assert created.revision == 1
    assert repository.get_knowledge(draft.knowledge_ref) == created
    assert repository.list_knowledge(namespace="project/terreate") == [created]
    assert repository.list_knowledge(status=KnowledgeStatus.CANDIDATE) == [created]
    assert (
        repository.list_events(draft.knowledge_ref)[0].event_type is EventType.CREATED
    )


def test_service_exposes_required_repository_operations(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    service = VaultService(repository)
    created = service.create_candidate(draft, actor="agent-a")
    updated = service.update_candidate(
        replace(draft, title="Updated title"),
        expected_revision=created.revision,
        actor="agent-b",
    )
    assert service.get_knowledge(draft.knowledge_ref) == updated
    assert service.list_knowledge() == [updated]
    assert [event.event_type for event in service.list_events(draft.knowledge_ref)] == [
        EventType.CREATED,
        EventType.UPDATED,
    ]


def test_update_candidate_increments_revision_and_appends_event(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    created = repository.create_candidate(draft, actor="agent-a")
    updated = repository.update_candidate(
        replace(draft, body="New body", tags=["transaction"]),
        expected_revision=created.revision,
        actor="agent-b",
        event_metadata={"reason": "clarified"},
    )

    assert updated.body == "New body"
    assert updated.revision == 2
    events = repository.list_events(draft.knowledge_ref)
    assert [event.event_type for event in events] == [
        EventType.CREATED,
        EventType.UPDATED,
    ]
    assert events[-1].revision == 2
    assert events[-1].metadata == {"reason": "clarified"}


def test_stale_revision_changes_neither_state_nor_events(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    created = repository.create_candidate(draft, actor="agent-a")
    repository.update_candidate(
        replace(draft, title="Current"),
        expected_revision=created.revision,
        actor="agent-b",
    )
    before = repository.get_knowledge(draft.knowledge_ref)
    before_events = repository.list_events(draft.knowledge_ref)

    with pytest.raises(StaleRevisionError):
        repository.update_candidate(
            replace(draft, title="Stale"),
            expected_revision=created.revision,
            actor="agent-c",
        )

    assert repository.get_knowledge(draft.knowledge_ref) == before
    assert repository.list_events(draft.knowledge_ref) == before_events


def test_direct_candidate_to_canonical_is_rejected(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    created = repository.create_candidate(draft, actor="agent-a")
    with pytest.raises(InvalidLifecycleTransitionError):
        repository.transition_lifecycle(
            draft.knowledge_ref,
            KnowledgeStatus.CANONICAL,
            expected_revision=created.revision,
            actor="agent-a",
        )
    assert (
        repository.get_knowledge(draft.knowledge_ref).status
        is KnowledgeStatus.CANDIDATE
    )
    assert len(repository.list_events(draft.knowledge_ref)) == 1


def test_verification_and_forward_lifecycle(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    record = repository.create_candidate(draft, actor="writer")
    record = repository.record_verification(
        draft.knowledge_ref,
        expected_revision=record.revision,
        actor="reviewer",
        outcome=VerificationOutcome.PASSED,
        verification={"method": "review", "result": "pass", "checks": ["source"]},
    )
    assert record.status is KnowledgeStatus.CANDIDATE
    assert record.verification_outcome is VerificationOutcome.PASSED
    assert record.verification == {
        "checks": ["source"],
        "method": "review",
        "result": "pass",
    }

    record = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.VERIFIED,
        expected_revision=record.revision,
        actor="reviewer",
    )
    record = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.CANONICAL,
        expected_revision=record.revision,
        actor="promoter",
    )
    record = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.DEPRECATED,
        expected_revision=record.revision,
        actor="maintainer",
    )
    record = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.ARCHIVED,
        expected_revision=record.revision,
        actor="maintainer",
    )

    assert record.status is KnowledgeStatus.ARCHIVED
    assert record.revision == 6
    assert [
        event.event_type for event in repository.list_events(draft.knowledge_ref)
    ] == [
        EventType.CREATED,
        EventType.VERIFICATION_RECORDED,
        EventType.VERIFIED,
        EventType.PROMOTED,
        EventType.DEPRECATED,
        EventType.ARCHIVED,
    ]


@pytest.mark.parametrize(
    "outcome", [VerificationOutcome.FAILED, VerificationOutcome.REJECTED]
)
def test_failed_or_rejected_verification_never_grants_verified_status(
    repository: VaultRepository,
    draft: KnowledgeDraft,
    outcome: VerificationOutcome,
) -> None:
    created = repository.create_candidate(draft, actor="writer")
    recorded = repository.record_verification(
        draft.knowledge_ref,
        expected_revision=created.revision,
        actor="reviewer",
        outcome=outcome,
        verification={"reason": "checks did not pass"},
    )

    assert recorded.status is KnowledgeStatus.CANDIDATE
    assert recorded.revision == created.revision + 1
    assert recorded.verification_outcome is outcome
    assert [
        event.event_type for event in repository.list_events(draft.knowledge_ref)
    ] == [
        EventType.CREATED,
        EventType.VERIFICATION_RECORDED,
    ]
    before_events = repository.list_events(draft.knowledge_ref)

    with pytest.raises(InvalidLifecycleTransitionError, match="passed verification"):
        repository.transition_lifecycle(
            draft.knowledge_ref,
            KnowledgeStatus.VERIFIED,
            expected_revision=recorded.revision,
            actor="reviewer",
        )

    assert repository.get_knowledge(draft.knowledge_ref) == recorded
    assert repository.list_events(draft.knowledge_ref) == before_events


def test_failed_verification_event_failure_rolls_back_state_and_revision(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    created = repository.create_candidate(draft, actor="writer")
    connection = sqlite3.connect(repository.database_path)
    try:
        connection.execute(
            """
            CREATE TRIGGER test_reject_verification_event
            BEFORE INSERT ON knowledge_events
            WHEN NEW.event_type = 'VERIFICATION_RECORDED'
            BEGIN
                SELECT RAISE(ABORT, 'injected verification event failure');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(sqlite3.IntegrityError, match="verification event failure"):
        repository.record_verification(
            draft.knowledge_ref,
            expected_revision=created.revision,
            actor="reviewer",
            outcome=VerificationOutcome.FAILED,
            verification={"reason": "failed"},
        )

    assert repository.get_knowledge(draft.knowledge_ref) == created
    assert [
        event.event_type for event in repository.list_events(draft.knowledge_ref)
    ] == [EventType.CREATED]


def test_candidate_update_clears_a_previous_passed_verification(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    created = repository.create_candidate(draft, actor="writer")
    recorded = repository.record_verification(
        draft.knowledge_ref,
        expected_revision=created.revision,
        actor="reviewer",
        outcome=VerificationOutcome.PASSED,
        verification={"checks": ["source"]},
    )
    updated = repository.update_candidate(
        replace(draft, body="Changed after verification"),
        expected_revision=recorded.revision,
        actor="writer",
    )

    assert updated.verification_outcome is None
    with pytest.raises(InvalidLifecycleTransitionError, match="passed verification"):
        repository.transition_lifecycle(
            draft.knowledge_ref,
            KnowledgeStatus.VERIFIED,
            expected_revision=updated.revision,
            actor="reviewer",
        )


@pytest.mark.parametrize(
    "source", [KnowledgeStatus.CANDIDATE, KnowledgeStatus.VERIFIED]
)
def test_explicit_early_discard_policy(
    repository: VaultRepository, draft: KnowledgeDraft, source: KnowledgeStatus
) -> None:
    record = repository.create_candidate(draft, actor="writer")
    if source is KnowledgeStatus.VERIFIED:
        record = repository.record_verification(
            draft.knowledge_ref,
            expected_revision=record.revision,
            actor="reviewer",
            outcome=VerificationOutcome.PASSED,
            verification={"result": "pass"},
        )
        record = repository.transition_lifecycle(
            draft.knowledge_ref,
            KnowledgeStatus.VERIFIED,
            expected_revision=record.revision,
            actor="reviewer",
        )
    archived = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.ARCHIVED,
        expected_revision=record.revision,
        actor="maintainer",
        event_metadata={"policy": "early-discard"},
    )
    assert archived.status is KnowledgeStatus.ARCHIVED
    assert (
        repository.list_events(draft.knowledge_ref)[-1].event_type is EventType.ARCHIVED
    )


def test_update_after_verification_is_rejected(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    record = repository.create_candidate(draft, actor="writer")
    record = repository.record_verification(
        draft.knowledge_ref,
        expected_revision=record.revision,
        actor="reviewer",
        outcome=VerificationOutcome.PASSED,
        verification={"result": "pass"},
    )
    record = repository.transition_lifecycle(
        draft.knowledge_ref,
        KnowledgeStatus.VERIFIED,
        expected_revision=record.revision,
        actor="reviewer",
    )
    with pytest.raises(InvalidLifecycleTransitionError):
        repository.update_candidate(
            draft,
            expected_revision=record.revision,
            actor="writer",
        )


def test_signal_events_do_not_change_lifecycle_or_revision(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    record = repository.create_candidate(draft, actor="writer")
    repository.record_signal_event(
        draft.knowledge_ref,
        EventType.REVALIDATION_REQUESTED,
        expected_revision=record.revision,
        actor="reviewer",
        metadata={"reason": "freshness"},
    )
    repository.record_signal_event(
        draft.knowledge_ref,
        EventType.SUPERSEDED,
        expected_revision=record.revision,
        actor="reviewer",
        metadata={"replacement": "vault://global/new/path"},
    )

    current = repository.get_knowledge(draft.knowledge_ref)
    assert current.status is KnowledgeStatus.CANDIDATE
    assert current.revision == record.revision
    assert [
        event.event_type for event in repository.list_events(draft.knowledge_ref)
    ] == [
        EventType.CREATED,
        EventType.REVALIDATION_REQUESTED,
        EventType.SUPERSEDED,
    ]


def test_event_log_is_append_only_at_database_level(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    repository.create_candidate(draft, actor="writer")
    connection = sqlite3.connect(repository.database_path)
    try:
        connection.execute("PRAGMA recursive_triggers = OFF")
        original = connection.execute("SELECT * FROM knowledge_events").fetchone()
        assert original is not None
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE knowledge_events SET actor = 'tampered'")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM knowledge_events")
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                INSERT OR REPLACE INTO knowledge_events(
                    event_id, knowledge_ref, revision, actor, timestamp,
                    event_type, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    original[1],
                    original[2],
                    original[3],
                    "tampered",
                    original[5],
                    original[6],
                    original[7],
                ),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                """
                INSERT OR REPLACE INTO knowledge_events(
                    event_sequence, event_id, knowledge_ref, revision, actor,
                    timestamp, event_type, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    original[0],
                    "replacement-event",
                    original[2],
                    original[3],
                    "tampered",
                    original[5],
                    original[6],
                    original[7],
                ),
            )
        connection.rollback()
        assert (
            connection.execute("SELECT * FROM knowledge_events").fetchone() == original
        )
    finally:
        connection.close()
    repository.record_signal_event(
        draft.knowledge_ref,
        EventType.REVALIDATION_REQUESTED,
        expected_revision=1,
        actor="reviewer",
    )
    assert len(repository.list_events(draft.knowledge_ref)) == 2


def test_event_failure_rolls_back_state_update(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    created = repository.create_candidate(draft, actor="writer")
    connection = sqlite3.connect(repository.database_path)
    try:
        connection.execute(
            """
            CREATE TRIGGER test_reject_updated_event
            BEFORE INSERT ON knowledge_events
            WHEN NEW.event_type = 'UPDATED'
            BEGIN
                SELECT RAISE(ABORT, 'injected event failure');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
        repository.update_candidate(
            replace(draft, title="must roll back"),
            expected_revision=created.revision,
            actor="writer",
        )

    current = repository.get_knowledge(draft.knowledge_ref)
    assert current.title == draft.title
    assert current.revision == created.revision
    assert [
        event.event_type for event in repository.list_events(draft.knowledge_ref)
    ] == [EventType.CREATED]


def test_invalid_actor_rolls_back_candidate_and_event(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    with pytest.raises(ValueError, match="actor"):
        repository.create_candidate(draft, actor=" ")
    assert repository.list_knowledge() == []


def test_non_json_metadata_is_rejected_before_persistence(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    with pytest.raises(InvalidMetadataError):
        repository.create_candidate(
            replace(draft, scope={"invalid": float("nan")}), actor="writer"
        )
    assert repository.list_knowledge() == []


def test_wal_and_foreign_keys_are_enabled(repository: VaultRepository) -> None:
    assert repository.sqlite_settings() == {"journal_mode": "wal", "foreign_keys": 1}


def test_reopen_preserves_state_and_history(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    repository.create_candidate(draft, actor="writer")
    reopened = VaultRepository(repository.database_path)
    assert reopened.get_knowledge(draft.knowledge_ref).body == draft.body
    assert reopened.list_events(draft.knowledge_ref)[0].event_type is EventType.CREATED


def test_schema_version_and_initialization_are_deterministic(
    repository: VaultRepository,
) -> None:
    assert repository.current_schema_version() == CURRENT_SCHEMA_VERSION == 4
    VaultRepository(repository.database_path)
    connection = sqlite3.connect(repository.database_path)
    try:
        rows = connection.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        connection.close()
    assert rows == [
        (1, "vault_core_v0_1"),
        (2, "append_only_and_verification_outcomes"),
        (3, "trust_signal_events"),
        (4, "operator_actions"),
    ]


def test_v2_vault_migrates_to_v3_preserving_events(tmp_path: Path) -> None:
    import kasane.vault.repository as repository_module
    from kasane.vault import migrations

    path = tmp_path / "v2.db"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:2])
        patch.setattr(repository_module, "CURRENT_SCHEMA_VERSION", 2)
        legacy = VaultRepository(path)
        legacy.create_candidate(
            KnowledgeDraft(
                knowledge_ref="vault://global/migration/keep",
                knowledge_type="guidance",
                title="Keep",
                body="Events survive the v3 rebuild.",
            ),
            actor="agent:a",
        )
    upgraded = VaultRepository(path)
    assert upgraded.current_schema_version() == 4
    ref = "vault://global/migration/keep"
    assert [event.event_type for event in upgraded.list_events(ref)] == [
        EventType.CREATED
    ]
    upgraded.record_signal_event(
        ref, EventType.CORROBORATED, expected_revision=1, actor="agent:b"
    )
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM knowledge_events")
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("knowledge_ref", "namespace", "knowledge_path"),
    [
        (
            "vault://global/agent-development/verify-before-pass",
            "global",
            "agent-development/verify-before-pass",
        ),
        (
            "vault://project/terreate/buffer-write-contract",
            "project/terreate",
            "buffer-write-contract",
        ),
        (
            "vault://research/execution-integrity/admission-gap",
            "research/execution-integrity",
            "admission-gap",
        ),
        ("vault://private/team-a/review/notes", "private/team-a", "review/notes"),
    ],
)
def test_nested_vault_identity_round_trip(
    repository: VaultRepository,
    draft: KnowledgeDraft,
    knowledge_ref: str,
    namespace: str,
    knowledge_path: str,
) -> None:
    record = repository.create_candidate(
        replace(draft, knowledge_ref=knowledge_ref, title=knowledge_ref), actor="writer"
    )
    assert record.namespace == namespace
    assert record.knowledge_path == knowledge_path


def test_invalid_physical_identity_is_rejected(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    with pytest.raises(InvalidKnowledgeIdentityError):
        repository.create_candidate(
            replace(draft, knowledge_ref="sqlite://knowledge/42"), actor="writer"
        )


def test_json_metadata_round_trips_and_is_stored_canonically(
    repository: VaultRepository, draft: KnowledgeDraft
) -> None:
    created = repository.create_candidate(draft, actor="writer")
    assert created.scope == draft.scope
    assert created.applies_when == draft.applies_when
    assert created.counterconditions == draft.counterconditions
    assert created.sources == draft.sources
    assert created.provenance == draft.provenance
    assert created.generated == draft.generated

    connection = sqlite3.connect(repository.database_path)
    try:
        scope_json = connection.execute(
            "SELECT scope_json FROM knowledge_records WHERE knowledge_ref = ?",
            (draft.knowledge_ref,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert scope_json == '{"paths":["src/**"],"project":"terreate"}'


def test_default_runtime_path_uses_configured_or_xdg_state() -> None:
    assert default_database_path({"KASANE_DATA_ROOT": "/srv/kasane"}) == Path(
        "/srv/kasane/vault.db"
    )
    assert default_database_path({"XDG_STATE_HOME": "/state"}) == Path(
        "/state/KASANE/vault.db"
    )


def test_default_runtime_path_falls_back_to_home_state_kasane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_database_path({}) == tmp_path / ".local/state/KASANE/vault.db"
