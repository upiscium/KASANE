"""Internal, disposable semantic retrieval state.

This is a derived-state primitive, not a retrieval policy API.  The canonical
Vault database is an explicit ownership boundary and is never opened or
mutated by this module.  Integrity and provider checks detect accidental
corruption or configuration mismatch; they are not authentication against a
hostile filesystem writer.
"""

from __future__ import annotations

import hashlib
import sqlite3
import struct
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from kasane.vault.json_codec import JsonValue, canonical_json
from kasane.vault.models import KnowledgeRecord

from .embeddings import (
    SEMANTIC_DOCUMENT_REPRESENTATION_VERSION,
    EmbeddingProvider,
    EmbeddingProviderIdentity,
    embed_documents,
    embed_query,
    semantic_document,
    validate_embedding_provider,
    validate_embedding_vector,
)

_SCHEMA_VERSION = "semantic-index-v1"


@dataclass(frozen=True)
class SemanticIndexSyncResult:
    rebuilt: bool
    watermark: str


@dataclass(frozen=True)
class SemanticSearchHit:
    knowledge_ref: str
    score: float


class DerivedSemanticIndex:
    """A derived vector index which has no dependency on Vault storage."""

    def __init__(
        self,
        database_path: str | Path,
        provider: EmbeddingProvider,
        *,
        canonical_database_path: str | Path,
    ) -> None:
        self.database_path = Path(database_path).expanduser()
        self.canonical_database_path = Path(canonical_database_path).expanduser()
        self._validate_ownership()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.provider = provider

    def synchronize(
        self, records: Iterable[KnowledgeRecord]
    ) -> SemanticIndexSyncResult:
        records = sorted(records, key=lambda record: record.knowledge_ref)
        identity = validate_embedding_provider(self.provider)
        watermark = self._watermark(records)
        documents = [semantic_document(record) for record in records]
        checksums = [self._checksum(document.encode("utf-8")) for document in documents]
        expected_metadata = self._metadata(
            identity, watermark, self._document_checksum(checksums)
        )

        try:
            with closing(self._connect()) as connection:
                initialized = self._initialize(connection)
                valid = not initialized and self._matches(
                    connection, records, checksums, expected_metadata
                )
                if valid:
                    return SemanticIndexSyncResult(False, watermark)
        except sqlite3.DatabaseError:
            self._replace_corrupt_file()

        # This is deliberately outside the transaction: a failing provider can
        # never remove or alter an already valid published index.
        vectors = embed_documents(self.provider, documents)
        self._publish(records, checksums, vectors, expected_metadata)
        return SemanticIndexSyncResult(True, watermark)

    def rebuild(self, records: Iterable[KnowledgeRecord]) -> SemanticIndexSyncResult:
        records = sorted(records, key=lambda record: record.knowledge_ref)
        identity = validate_embedding_provider(self.provider)
        watermark = self._watermark(records)
        documents = [semantic_document(record) for record in records]
        checksums = [self._checksum(document.encode("utf-8")) for document in documents]
        metadata = self._metadata(
            identity, watermark, self._document_checksum(checksums)
        )
        vectors = embed_documents(self.provider, documents)
        try:
            self._publish(records, checksums, vectors, metadata)
        except sqlite3.DatabaseError:
            # A caller can observe a whole-file replacement between synchronize
            # and search.  Remove only the owned derived file, then retry once.
            self._replace_corrupt_file()
            self._publish(records, checksums, vectors, metadata)
        return SemanticIndexSyncResult(True, watermark)

    def search(
        self, query: str, allowed_refs: Iterable[str]
    ) -> list[SemanticSearchHit]:
        refs = set(allowed_refs)
        if not refs:
            return []
        with closing(self._connect()) as connection:
            self._validate_ready(connection)
            rows = connection.execute(
                "SELECT knowledge_ref, vector, vector_checksum FROM semantic_vectors"
            ).fetchall()
        query_vector = embed_query(self.provider, query)
        hits: list[SemanticSearchHit] = []
        for ref, blob, checksum in rows:
            if ref not in refs:
                continue
            vector = self._decode_vector(blob, checksum)
            hits.append(
                SemanticSearchHit(ref, sum(a * b for a, b in zip(query_vector, vector)))
            )
        return sorted(hits, key=lambda hit: (-hit.score, hit.knowledge_ref))

    def _validate_ready(self, connection: sqlite3.Connection) -> None:
        """Reject an index from another vector space before querying it."""
        identity = validate_embedding_provider(self.provider)
        metadata = dict(connection.execute("SELECT key, value FROM semantic_metadata"))
        expected = self._metadata(
            identity,
            metadata.get("canonical_watermark", ""),
            metadata.get("document_checksum", ""),
        )
        for key in (
            "schema_version",
            "provider_id",
            "model_id",
            "embedding_dimension",
            "representation_version",
        ):
            if metadata.get(key) != expected[key]:
                raise sqlite3.DatabaseError(f"semantic index identity mismatch: {key}")

    def _publish(
        self,
        records: list[KnowledgeRecord],
        checksums: list[str],
        vectors: tuple[tuple[float, ...], ...],
        metadata: dict[str, str],
    ) -> None:
        metadata = dict(metadata)
        vector_checksums = [
            self._checksum(self._encode_vector(vector)) for vector in vectors
        ]
        metadata["vector_integrity_checksum"] = self._document_checksum(
            vector_checksums
        )
        with closing(self._connect()) as connection:
            self._initialize(connection)
            self._validate_ownership(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._validate_ownership(connection)
                connection.execute("DELETE FROM semantic_vectors")
                for record, document_checksum, vector in zip(
                    records, checksums, vectors
                ):
                    packed = self._encode_vector(vector)
                    self._validate_ownership(connection)
                    connection.execute(
                        "INSERT INTO semantic_vectors VALUES (?, ?, ?, ?, ?)",
                        (
                            record.knowledge_ref,
                            packed,
                            self._checksum(packed),
                            document_checksum,
                            record.revision,
                        ),
                    )
                self._validate_ownership(connection)
                connection.execute("DELETE FROM semantic_metadata")
                self._validate_ownership(connection)
                connection.executemany(
                    "INSERT INTO semantic_metadata VALUES (?, ?)", metadata.items()
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _initialize(self, connection: sqlite3.Connection) -> bool:
        self._validate_ownership(connection)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        initialized = not {"semantic_metadata", "semantic_vectors"} <= tables
        self._validate_ownership(connection)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS semantic_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._validate_ownership(connection)
        connection.execute("""CREATE TABLE IF NOT EXISTS semantic_vectors (
            knowledge_ref TEXT PRIMARY KEY, vector BLOB NOT NULL, vector_checksum TEXT NOT NULL,
            document_checksum TEXT NOT NULL, revision INTEGER NOT NULL)""")
        self._validate_schema(connection)
        connection.commit()
        return initialized

    @staticmethod
    def _validate_schema(connection: sqlite3.Connection) -> None:
        if [
            r[1] for r in connection.execute("PRAGMA table_info(semantic_metadata)")
        ] != ["key", "value"]:
            raise sqlite3.DatabaseError("invalid semantic metadata schema")
        if [
            r[1] for r in connection.execute("PRAGMA table_info(semantic_vectors)")
        ] != [
            "knowledge_ref",
            "vector",
            "vector_checksum",
            "document_checksum",
            "revision",
        ]:
            raise sqlite3.DatabaseError("invalid semantic vector schema")

    def _matches(
        self,
        connection: sqlite3.Connection,
        records: list[KnowledgeRecord],
        checksums: list[str],
        metadata: dict[str, str],
    ) -> bool:
        stored = dict(connection.execute("SELECT key, value FROM semantic_metadata"))
        if any(stored.get(key) != value for key, value in metadata.items()):
            return False
        rows = connection.execute(
            "SELECT knowledge_ref, vector, vector_checksum, document_checksum, revision FROM semantic_vectors ORDER BY knowledge_ref"
        ).fetchall()
        if len(rows) != len(records):
            return False
        for record, document_checksum, row in zip(records, checksums, rows):
            if (
                row[0] != record.knowledge_ref
                or row[3] != document_checksum
                or row[4] != record.revision
            ):
                return False
            try:
                self._decode_vector(row[1], row[2])
            except (TypeError, ValueError, struct.error):
                return False
        return stored.get("vector_integrity_checksum") == self._document_checksum(
            [row[2] for row in rows]
        )

    def _decode_vector(self, blob: bytes, checksum: str) -> tuple[float, ...]:
        if self._checksum(blob) != checksum:
            raise ValueError("semantic vector integrity check failed")
        identity = validate_embedding_provider(self.provider)
        if len(blob) != identity.embedding_dimension * 8:
            raise ValueError("semantic vector dimension mismatch")
        return validate_embedding_vector(
            struct.unpack(f"!{identity.embedding_dimension}d", blob),
            identity.embedding_dimension,
        )

    @staticmethod
    def _encode_vector(vector: tuple[float, ...]) -> bytes:
        return struct.pack(f"!{len(vector)}d", *vector)

    @staticmethod
    def _checksum(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _document_checksum(checksums: list[str]) -> str:
        return hashlib.sha256(canonical_json(cast_json(checksums)).encode()).hexdigest()

    @staticmethod
    def _watermark(records: list[KnowledgeRecord]) -> str:
        state: list[JsonValue] = [
            {
                "knowledge_ref": r.knowledge_ref,
                "revision": r.revision,
                "status": r.status.value,
            }
            for r in records
        ]
        return hashlib.sha256(canonical_json(state).encode()).hexdigest()

    @staticmethod
    def _metadata(
        identity: EmbeddingProviderIdentity, watermark: str, document_checksum: str
    ) -> dict[str, str]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_id": identity.provider_id,
            "model_id": identity.model_id,
            "embedding_dimension": str(identity.embedding_dimension),
            "representation_version": SEMANTIC_DOCUMENT_REPRESENTATION_VERSION,
            "canonical_watermark": watermark,
            "document_checksum": document_checksum,
        }

    def _connect(self) -> sqlite3.Connection:
        self._validate_ownership()
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        try:
            self._validate_ownership(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def _validate_ownership(self, connection: sqlite3.Connection | None = None) -> None:
        """Ensure the opened/target database can never be the canonical DB."""
        derived = self.database_path
        canonical = self.canonical_database_path
        if derived.resolve(strict=False) == canonical.resolve(strict=False):
            raise ValueError(
                "derived semantic database must not equal the canonical Vault database"
            )
        for path, label in ((derived, "derived"), (canonical, "canonical")):
            if path.is_symlink():
                raise ValueError(f"{label} database path must not be a symlink")
        try:
            if derived.exists() and canonical.exists():
                derived_stat = derived.stat()
                canonical_stat = canonical.stat()
                if (derived_stat.st_dev, derived_stat.st_ino) == (
                    canonical_stat.st_dev,
                    canonical_stat.st_ino,
                ):
                    raise ValueError(
                        "derived semantic database must not alias the canonical Vault database"
                    )
        except FileNotFoundError:
            pass
        if connection is not None:
            opened = next(
                (
                    Path(row[2])
                    for row in connection.execute("PRAGMA database_list")
                    if row[1] == "main"
                ),
                None,
            )
            if opened is None or not opened.exists():
                raise sqlite3.DatabaseError("semantic database target is unavailable")
            opened_stat = opened.stat()
            if canonical.exists() and (
                opened_stat.st_dev,
                opened_stat.st_ino,
            ) == (canonical.stat().st_dev, canonical.stat().st_ino):
                raise ValueError(
                    "opened semantic database aliases the canonical Vault database"
                )
            if opened.resolve(strict=False) == canonical.resolve(strict=False):
                raise ValueError(
                    "opened semantic database equals the canonical Vault database"
                )

    def _replace_corrupt_file(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            self._validate_ownership()
            Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)


def cast_json(values: list[str]) -> list[JsonValue]:
    return list(values)


SemanticIndex = DerivedSemanticIndex
