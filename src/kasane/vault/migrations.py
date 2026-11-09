"""Deterministic SQLite schema migrations for Vault Core."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

CURRENT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


_STATUS_VALUES = "'candidate','verified','canonical','deprecated','archived'"
_EVENT_VALUES_V1 = (
    "'CREATED','UPDATED','VERIFIED','PROMOTED','SUPERSEDED','DEPRECATED',"
    "'ARCHIVED','REVALIDATION_REQUESTED'"
)
_EVENT_VALUES = (
    "'CREATED','UPDATED','VERIFICATION_RECORDED','VERIFIED','PROMOTED',"
    "'SUPERSEDED','DEPRECATED','ARCHIVED','REVALIDATION_REQUESTED'"
)


MIGRATIONS = (
    Migration(
        version=1,
        name="vault_core_v0_1",
        statements=(
            f"""
            CREATE TABLE knowledge_records (
                knowledge_ref TEXT PRIMARY KEY,
                namespace TEXT NOT NULL,
                knowledge_path TEXT NOT NULL,
                knowledge_type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ({_STATUS_VALUES})),
                stability TEXT,
                tags_json TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                applies_when_json TEXT NOT NULL,
                counterconditions_json TEXT NOT NULL,
                sources_json TEXT NOT NULL,
                provenance_json TEXT NOT NULL,
                generated_json TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                stale_after TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                UNIQUE(namespace, knowledge_path)
            )
            """,
            f"""
            CREATE TABLE knowledge_events (
                event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                knowledge_ref TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                actor TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ({_EVENT_VALUES_V1})),
                metadata_json TEXT NOT NULL,
                FOREIGN KEY (knowledge_ref) REFERENCES knowledge_records(knowledge_ref)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            "CREATE INDEX knowledge_records_namespace_idx ON knowledge_records(namespace, knowledge_ref)",
            "CREATE INDEX knowledge_events_history_idx ON knowledge_events(knowledge_ref, event_sequence)",
            """
            CREATE TRIGGER knowledge_events_no_update
            BEFORE UPDATE ON knowledge_events
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_events is append-only');
            END
            """,
            """
            CREATE TRIGGER knowledge_events_no_delete
            BEFORE DELETE ON knowledge_events
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_events is append-only');
            END
            """,
        ),
    ),
    Migration(
        version=2,
        name="append_only_and_verification_outcomes",
        statements=(
            """
            ALTER TABLE knowledge_records
            ADD COLUMN verification_outcome TEXT CHECK (
                verification_outcome IN ('passed','failed','rejected')
            )
            """,
            "DROP TRIGGER knowledge_events_no_update",
            "DROP TRIGGER knowledge_events_no_delete",
            "DROP INDEX knowledge_events_history_idx",
            "ALTER TABLE knowledge_events RENAME TO knowledge_events_v1",
            f"""
            CREATE TABLE knowledge_events (
                event_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                knowledge_ref TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK (revision >= 1),
                actor TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ({_EVENT_VALUES})),
                metadata_json TEXT NOT NULL,
                FOREIGN KEY (knowledge_ref) REFERENCES knowledge_records(knowledge_ref)
                    ON UPDATE RESTRICT ON DELETE RESTRICT
            )
            """,
            """
            INSERT INTO knowledge_events(
                event_sequence, event_id, knowledge_ref, revision, actor,
                timestamp, event_type, metadata_json
            )
            SELECT event_sequence, event_id, knowledge_ref, revision, actor,
                   timestamp, event_type, metadata_json
            FROM knowledge_events_v1
            ORDER BY event_sequence
            """,
            "DROP TABLE knowledge_events_v1",
            "CREATE INDEX knowledge_events_history_idx ON knowledge_events(knowledge_ref, event_sequence)",
            """
            CREATE TRIGGER knowledge_events_no_replace
            BEFORE INSERT ON knowledge_events
            WHEN EXISTS (
                SELECT 1 FROM knowledge_events
                WHERE event_id = NEW.event_id
                   OR event_sequence = NEW.event_sequence
            )
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_events is append-only');
            END
            """,
            """
            CREATE TRIGGER knowledge_events_no_update
            BEFORE UPDATE ON knowledge_events
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_events is append-only');
            END
            """,
            """
            CREATE TRIGGER knowledge_events_no_delete
            BEFORE DELETE ON knowledge_events
            BEGIN
                SELECT RAISE(ABORT, 'knowledge_events is append-only');
            END
            """,
        ),
    ),
)


_CREATE_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
)
"""


def initialize_schema(connection: sqlite3.Connection, applied_at: str) -> None:
    """Apply every pending migration atomically in ascending order."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(_CREATE_MIGRATION_TABLE)
        applied = {
            row[0]
            for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        }
        unknown = applied - {migration.version for migration in MIGRATIONS}
        if unknown:
            raise RuntimeError(
                f"database contains unknown schema versions: {sorted(unknown)}"
            )
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, applied_at),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def schema_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version").fetchone()[0])
