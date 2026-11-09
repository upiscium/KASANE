"""SQLite repository implementing Vault Core consistency guarantees."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from .config import default_database_path
from .errors import (
    InvalidLifecycleTransitionError,
    KnowledgeNotFoundError,
    StaleRevisionError,
)
from .identity import parse_knowledge_ref
from .json_codec import JsonValue, canonical_json, parse_json, require_json
from .migrations import CURRENT_SCHEMA_VERSION, initialize_schema, schema_version
from .models import (
    EventType,
    KnowledgeDraft,
    KnowledgeEvent,
    KnowledgeRecord,
    KnowledgeStatus,
    VerificationOutcome,
)

Clock = Callable[[], str]
EventIdFactory = Callable[[], str]


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def new_event_id() -> str:
    return str(uuid4())


_RECORD_COLUMNS = """
knowledge_ref, namespace, knowledge_path, knowledge_type, title, body, status,
stability, tags_json, scope_json, applies_when_json, counterconditions_json,
sources_json, provenance_json, generated_json, verification_json,
verification_outcome, stale_after, created_at, updated_at, revision
"""


class VaultRepository:
    """Repository boundary; public identities never expose SQLite keys."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        clock: Clock = utc_now,
        event_id_factory: EventIdFactory = new_event_id,
    ) -> None:
        self.database_path = Path(database_path or default_database_path()).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._event_id_factory = event_id_factory
        with self._connection() as connection:
            initialize_schema(connection, self._clock())
            if schema_version(connection) != CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    "Vault schema did not initialize to the supported version"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, isolation_level=None, timeout=30.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            connection.close()
            raise RuntimeError(f"SQLite WAL mode is required, got {mode!r}")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def current_schema_version(self) -> int:
        with self._connection() as connection:
            return schema_version(connection)

    def sqlite_settings(self) -> dict[str, int | str]:
        with self._connection() as connection:
            return {
                "journal_mode": str(
                    connection.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower(),
                "foreign_keys": int(
                    connection.execute("PRAGMA foreign_keys").fetchone()[0]
                ),
            }

    def create_candidate(
        self,
        draft: KnowledgeDraft,
        *,
        actor: str,
        event_metadata: JsonValue = None,
    ) -> KnowledgeRecord:
        identity = parse_knowledge_ref(draft.knowledge_ref)
        values = self._draft_values(draft)
        timestamp = self._clock()
        with self._transaction() as connection:
            connection.execute(
                f"""
                INSERT INTO knowledge_records({_RECORD_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.knowledge_ref,
                    identity.namespace,
                    identity.knowledge_path,
                    *values[:3],
                    KnowledgeStatus.CANDIDATE.value,
                    *values[3:-1],
                    None,
                    values[-1],
                    timestamp,
                    timestamp,
                    1,
                ),
            )
            self._append_event(
                connection,
                knowledge_ref=draft.knowledge_ref,
                revision=1,
                actor=actor,
                event_type=EventType.CREATED,
                metadata=event_metadata,
                timestamp=timestamp,
            )
        return self.get_knowledge(draft.knowledge_ref)

    def get_knowledge(self, knowledge_ref: str) -> KnowledgeRecord:
        parse_knowledge_ref(knowledge_ref)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT {_RECORD_COLUMNS} FROM knowledge_records WHERE knowledge_ref = ?",
                (knowledge_ref,),
            ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError(knowledge_ref)
        return self._record_from_row(row)

    def list_knowledge(
        self,
        *,
        namespace: str | None = None,
        status: KnowledgeStatus | None = None,
    ) -> list[KnowledgeRecord]:
        clauses: list[str] = []
        parameters: list[str] = []
        if namespace is not None:
            clauses.append("namespace = ?")
            parameters.append(namespace)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status.value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {_RECORD_COLUMNS} FROM knowledge_records{where} ORDER BY knowledge_ref",
                parameters,
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def update_candidate(
        self,
        draft: KnowledgeDraft,
        *,
        expected_revision: int,
        actor: str,
        event_metadata: JsonValue = None,
    ) -> KnowledgeRecord:
        parse_knowledge_ref(draft.knowledge_ref)
        values = self._draft_values(draft)
        timestamp = self._clock()
        with self._transaction() as connection:
            current = self._require_current(
                connection, draft.knowledge_ref, expected_revision
            )
            if current["status"] != KnowledgeStatus.CANDIDATE.value:
                raise InvalidLifecycleTransitionError(
                    "only candidate knowledge may be updated"
                )
            new_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE knowledge_records
                SET knowledge_type = ?, title = ?, body = ?, stability = ?, tags_json = ?,
                    scope_json = ?, applies_when_json = ?, counterconditions_json = ?,
                    sources_json = ?, provenance_json = ?, generated_json = ?,
                    verification_json = ?, verification_outcome = NULL,
                    stale_after = ?, updated_at = ?, revision = ?
                WHERE knowledge_ref = ? AND revision = ? AND status = 'candidate'
                """,
                (
                    *values,
                    timestamp,
                    new_revision,
                    draft.knowledge_ref,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRevisionError(draft.knowledge_ref)
            self._append_event(
                connection,
                knowledge_ref=draft.knowledge_ref,
                revision=new_revision,
                actor=actor,
                event_type=EventType.UPDATED,
                metadata=event_metadata,
                timestamp=timestamp,
            )
        return self.get_knowledge(draft.knowledge_ref)

    def record_verification(
        self,
        knowledge_ref: str,
        *,
        expected_revision: int,
        actor: str,
        outcome: VerificationOutcome,
        verification: JsonValue,
        event_metadata: JsonValue = None,
    ) -> KnowledgeRecord:
        parse_knowledge_ref(knowledge_ref)
        verification_value = require_json(verification)
        timestamp = self._clock()
        with self._transaction() as connection:
            current = self._require_current(
                connection, knowledge_ref, expected_revision
            )
            if current["status"] != KnowledgeStatus.CANDIDATE.value:
                raise InvalidLifecycleTransitionError(
                    "verification requires candidate knowledge"
                )
            new_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE knowledge_records
                SET verification_json = ?, verification_outcome = ?,
                    updated_at = ?, revision = ?
                WHERE knowledge_ref = ? AND revision = ? AND status = 'candidate'
                """,
                (
                    canonical_json(verification_value),
                    outcome.value,
                    timestamp,
                    new_revision,
                    knowledge_ref,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRevisionError(knowledge_ref)
            metadata: dict[str, JsonValue] = {
                "outcome": outcome.value,
                "verification": verification_value,
            }
            if event_metadata is not None:
                metadata["metadata"] = require_json(event_metadata)
            self._append_event(
                connection,
                knowledge_ref=knowledge_ref,
                revision=new_revision,
                actor=actor,
                event_type=EventType.VERIFICATION_RECORDED,
                metadata=metadata,
                timestamp=timestamp,
            )
        return self.get_knowledge(knowledge_ref)

    def transition_lifecycle(
        self,
        knowledge_ref: str,
        target: KnowledgeStatus,
        *,
        expected_revision: int,
        actor: str,
        event_metadata: JsonValue = None,
    ) -> KnowledgeRecord:
        parse_knowledge_ref(knowledge_ref)
        timestamp = self._clock()
        with self._transaction() as connection:
            current = self._require_current(
                connection, knowledge_ref, expected_revision
            )
            source = KnowledgeStatus(current["status"])
            if (
                source is KnowledgeStatus.CANDIDATE
                and target is KnowledgeStatus.VERIFIED
                and current["verification_outcome"] != VerificationOutcome.PASSED.value
            ):
                raise InvalidLifecycleTransitionError(
                    "verified status requires a recorded passed verification"
                )
            event_type = self._transition_event(source, target)
            new_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE knowledge_records
                SET status = ?, updated_at = ?, revision = ?
                WHERE knowledge_ref = ? AND revision = ?
                """,
                (
                    target.value,
                    timestamp,
                    new_revision,
                    knowledge_ref,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleRevisionError(knowledge_ref)
            self._append_event(
                connection,
                knowledge_ref=knowledge_ref,
                revision=new_revision,
                actor=actor,
                event_type=event_type,
                metadata=event_metadata,
                timestamp=timestamp,
            )
        return self.get_knowledge(knowledge_ref)

    def record_signal_event(
        self,
        knowledge_ref: str,
        event_type: EventType,
        *,
        expected_revision: int,
        actor: str,
        metadata: JsonValue = None,
    ) -> KnowledgeEvent:
        if event_type not in {EventType.SUPERSEDED, EventType.REVALIDATION_REQUESTED}:
            raise ValueError("only non-lifecycle signal events are accepted")
        parse_knowledge_ref(knowledge_ref)
        timestamp = self._clock()
        event_id = self._event_id_factory()
        with self._transaction() as connection:
            self._require_current(connection, knowledge_ref, expected_revision)
            self._append_event(
                connection,
                knowledge_ref=knowledge_ref,
                revision=expected_revision,
                actor=actor,
                event_type=event_type,
                metadata=metadata,
                timestamp=timestamp,
                event_id=event_id,
            )
        return next(
            event
            for event in self.list_events(knowledge_ref)
            if event.event_id == event_id
        )

    def list_events(self, knowledge_ref: str) -> list[KnowledgeEvent]:
        parse_knowledge_ref(knowledge_ref)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, knowledge_ref, revision, actor, timestamp, event_type,
                       metadata_json
                FROM knowledge_events
                WHERE knowledge_ref = ?
                ORDER BY event_sequence
                """,
                (knowledge_ref,),
            ).fetchall()
        return [
            KnowledgeEvent(
                event_id=row["event_id"],
                knowledge_ref=row["knowledge_ref"],
                revision=row["revision"],
                actor=row["actor"],
                timestamp=row["timestamp"],
                event_type=EventType(row["event_type"]),
                metadata=parse_json(row["metadata_json"]),
            )
            for row in rows
        ]

    def _require_current(
        self, connection: sqlite3.Connection, knowledge_ref: str, expected_revision: int
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT status, revision, verification_outcome
            FROM knowledge_records
            WHERE knowledge_ref = ?
            """,
            (knowledge_ref,),
        ).fetchone()
        if row is None:
            raise KnowledgeNotFoundError(knowledge_ref)
        if row["revision"] != expected_revision:
            raise StaleRevisionError(
                f"{knowledge_ref}: expected revision {expected_revision}, current {row['revision']}"
            )
        return row

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        knowledge_ref: str,
        revision: int,
        actor: str,
        event_type: EventType,
        metadata: JsonValue,
        timestamp: str,
        event_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO knowledge_events(
                event_id, knowledge_ref, revision, actor, timestamp, event_type, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id or self._event_id_factory(),
                knowledge_ref,
                revision,
                self._require_actor(actor),
                timestamp,
                event_type.value,
                canonical_json(require_json(metadata)),
            ),
        )

    def _draft_values(self, draft: KnowledgeDraft) -> tuple[Any, ...]:
        if not draft.knowledge_type.strip() or not draft.title.strip():
            raise ValueError("knowledge_type and title must be non-empty")
        if any(not isinstance(tag, str) or not tag for tag in draft.tags):
            raise ValueError("tags must contain non-empty strings")
        if len(set(draft.tags)) != len(draft.tags):
            raise ValueError("tags must be unique")
        return (
            draft.knowledge_type,
            draft.title,
            draft.body,
            draft.stability,
            canonical_json(require_json(draft.tags)),
            canonical_json(require_json(draft.scope)),
            canonical_json(require_json(draft.applies_when)),
            canonical_json(require_json(draft.counterconditions)),
            canonical_json(require_json(draft.sources)),
            canonical_json(require_json(draft.provenance)),
            canonical_json(require_json(draft.generated)),
            canonical_json(require_json(draft.verification)),
            draft.stale_after,
        )

    @staticmethod
    def _require_actor(actor: str) -> str:
        if not actor.strip():
            raise ValueError("actor must be non-empty")
        return actor

    @staticmethod
    def _transition_event(
        source: KnowledgeStatus, target: KnowledgeStatus
    ) -> EventType:
        policy = {
            (KnowledgeStatus.CANDIDATE, KnowledgeStatus.ARCHIVED): EventType.ARCHIVED,
            (KnowledgeStatus.CANDIDATE, KnowledgeStatus.VERIFIED): EventType.VERIFIED,
            (KnowledgeStatus.VERIFIED, KnowledgeStatus.CANONICAL): EventType.PROMOTED,
            (KnowledgeStatus.VERIFIED, KnowledgeStatus.ARCHIVED): EventType.ARCHIVED,
            (
                KnowledgeStatus.CANONICAL,
                KnowledgeStatus.DEPRECATED,
            ): EventType.DEPRECATED,
            (KnowledgeStatus.DEPRECATED, KnowledgeStatus.ARCHIVED): EventType.ARCHIVED,
        }
        try:
            return policy[(source, target)]
        except KeyError as error:
            raise InvalidLifecycleTransitionError(
                f"invalid lifecycle transition: {source.value} -> {target.value}"
            ) from error

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> KnowledgeRecord:
        tags_value = parse_json(row["tags_json"])
        if not isinstance(tags_value, list) or not all(
            isinstance(tag, str) for tag in tags_value
        ):
            raise RuntimeError("stored tags JSON violates the Vault schema")
        return KnowledgeRecord(
            knowledge_ref=row["knowledge_ref"],
            namespace=row["namespace"],
            knowledge_path=row["knowledge_path"],
            knowledge_type=row["knowledge_type"],
            title=row["title"],
            body=row["body"],
            status=KnowledgeStatus(row["status"]),
            stability=row["stability"],
            tags=cast(list[str], tags_value),
            scope=parse_json(row["scope_json"]),
            applies_when=parse_json(row["applies_when_json"]),
            counterconditions=parse_json(row["counterconditions_json"]),
            sources=parse_json(row["sources_json"]),
            provenance=parse_json(row["provenance_json"]),
            generated=parse_json(row["generated_json"]),
            verification=parse_json(row["verification_json"]),
            verification_outcome=(
                VerificationOutcome(row["verification_outcome"])
                if row["verification_outcome"] is not None
                else None
            ),
            stale_after=row["stale_after"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            revision=row["revision"],
        )
