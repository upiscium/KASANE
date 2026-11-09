"""Disposable SQLite FTS5 index for deterministic Level 0 retrieval."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from kasane.vault.json_codec import JsonValue, canonical_json
from kasane.vault.models import KnowledgeRecord

_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True)
class IndexSyncResult:
    rebuilt: bool
    watermark: str


@dataclass(frozen=True)
class SearchHit:
    knowledge_ref: str
    score: float


def lexical_tokens(value: str) -> tuple[str, ...]:
    """Return safe terms; caller text is never used as FTS query syntax."""

    return tuple(_TOKEN_PATTERN.findall(value.casefold()))


def normalized_terms(value: str) -> str:
    return " ".join(lexical_tokens(value))


class DerivedLexicalIndex:
    """Independent derived state rebuilt solely from canonical Vault records."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)

    def synchronize(self, records: list[KnowledgeRecord]) -> IndexSyncResult:
        watermark = self._watermark(records)
        expected_checksum = self._document_checksum(self._document_rows(records))
        try:
            with closing(self._connect()) as connection:
                initialized = self._initialize(connection)
                stored = connection.execute(
                    "SELECT value FROM retrieval_metadata WHERE key = 'watermark'"
                ).fetchone()
                stored_checksum = connection.execute(
                    "SELECT value FROM retrieval_metadata WHERE key = 'document_checksum'"
                ).fetchone()
                actual_checksum = self._document_checksum(
                    connection.execute(
                        """
                        SELECT knowledge_ref, title, tags, topics, body
                        FROM retrieval_fts
                        ORDER BY knowledge_ref
                        """
                    ).fetchall()
                )
                if (
                    initialized
                    or stored is None
                    or stored[0] != watermark
                    or stored_checksum is None
                    or stored_checksum[0] != expected_checksum
                    or actual_checksum != expected_checksum
                ):
                    self._rebuild_connection(connection, records, watermark)
                    return IndexSyncResult(True, watermark)
                return IndexSyncResult(False, watermark)
        except sqlite3.DatabaseError:
            self._replace_corrupt_file()
            with closing(self._connect()) as connection:
                self._initialize(connection)
                self._rebuild_connection(connection, records, watermark)
            return IndexSyncResult(True, watermark)

    def rebuild(self, records: list[KnowledgeRecord]) -> IndexSyncResult:
        watermark = self._watermark(records)
        try:
            with closing(self._connect()) as connection:
                self._initialize(connection)
                self._rebuild_connection(connection, records, watermark)
        except sqlite3.DatabaseError:
            self._replace_corrupt_file()
            with closing(self._connect()) as connection:
                self._initialize(connection)
                self._rebuild_connection(connection, records, watermark)
        return IndexSyncResult(True, watermark)

    def search(self, query: str) -> list[SearchHit]:
        tokens = lexical_tokens(query)
        if not tokens:
            return []
        expression = " OR ".join(f'"{token}"' for token in tokens)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT knowledge_ref,
                       bm25(retrieval_fts, 0.0, 8.0, 6.0, 4.0, 1.0) AS score
                FROM retrieval_fts
                WHERE retrieval_fts MATCH ?
                ORDER BY score ASC, knowledge_ref ASC
                """,
                (expression,),
            ).fetchall()
        return [SearchHit(row[0], float(row[1])) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path, timeout=30.0)

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> bool:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }
        initialized = not {"retrieval_metadata", "retrieval_fts"} <= tables
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS retrieval_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(
                knowledge_ref UNINDEXED,
                title,
                tags,
                topics,
                body,
                tokenize = 'unicode61 remove_diacritics 2'
            )
            """
        )
        DerivedLexicalIndex._validate_schema(connection)
        connection.commit()
        return initialized

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        metadata_columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(retrieval_metadata)")
        ]
        fts_columns = [
            row[1] for row in connection.execute("PRAGMA table_info(retrieval_fts)")
        ]
        fts_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'retrieval_fts'"
        ).fetchone()
        fts_sql = str(fts_sql_row[0]).casefold() if fts_sql_row else ""
        if metadata_columns != ["key", "value"]:
            raise sqlite3.DatabaseError("derived retrieval metadata schema is invalid")
        if fts_columns != ["knowledge_ref", "title", "tags", "topics", "body"]:
            raise sqlite3.DatabaseError("derived retrieval FTS schema is invalid")
        if "virtual table" not in fts_sql or "using fts5" not in fts_sql:
            raise sqlite3.DatabaseError("derived retrieval index is not FTS5")

    def _rebuild_connection(
        self,
        connection: sqlite3.Connection,
        records: list[KnowledgeRecord],
        watermark: str,
    ) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM retrieval_fts")
            document_rows = self._document_rows(records)
            for row in document_rows:
                connection.execute(
                    """
                    INSERT INTO retrieval_fts(knowledge_ref, title, tags, topics, body)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    row,
                )
            connection.execute(
                """
                INSERT INTO retrieval_metadata(key, value) VALUES ('watermark', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (watermark,),
            )
            connection.execute(
                """
                INSERT INTO retrieval_metadata(key, value)
                VALUES ('document_checksum', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (self._document_checksum(document_rows),),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _replace_corrupt_file(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)

    @staticmethod
    def _watermark(records: list[KnowledgeRecord]) -> str:
        state: list[JsonValue] = [
            {
                "knowledge_ref": record.knowledge_ref,
                "revision": record.revision,
                "status": record.status.value,
            }
            for record in sorted(records, key=lambda item: item.knowledge_ref)
        ]
        return hashlib.sha256(canonical_json(state).encode("utf-8")).hexdigest()

    @staticmethod
    def _document_rows(
        records: list[KnowledgeRecord],
    ) -> list[tuple[str, str, str, str, str]]:
        return [
            (
                record.knowledge_ref,
                record.title,
                " ".join(record.tags),
                " ".join([record.namespace, record.knowledge_path.replace("/", " ")]),
                record.body,
            )
            for record in sorted(records, key=lambda item: item.knowledge_ref)
        ]

    @staticmethod
    def _document_checksum(rows: list[tuple[str, str, str, str, str]]) -> str:
        payload: list[JsonValue] = [
            [knowledge_ref, title, tags, topics, body]
            for knowledge_ref, title, tags, topics, body in rows
        ]
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
