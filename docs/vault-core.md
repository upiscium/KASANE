# Vault Core v0.1

Vault Core is the canonical, transactional knowledge state boundary. Retrieval indexes and Context Capsules are derived and rebuildable; neither is a source of truth.

## Storage architecture

v0.1 uses Python's SQLite driver with WAL mode and foreign-key enforcement on every connection. The repository accepts an explicit database path. Without one it resolves, in order:

1. `KASANE_DATA_ROOT/vault.db`
2. `$XDG_STATE_HOME/KASANE/vault.db`
3. `~/.local/state/KASANE/vault.db`

Runtime databases therefore do not belong in the Git worktree. Tests use temporary database files.

Public identity follows the contract from Issue #4 and is separate from revision and physical storage:

```text
vault://global/<knowledge-path>
vault://project/<project>/<knowledge-path>
vault://research/<topic>/<knowledge-path>
vault://private/<scope>/<knowledge-path>
```

SQLite's internal event sequence is used only for deterministic ordering and is never exposed as knowledge identity.

## Schema and migrations

All DDL is centralized in `vault/migrations.py`. `schema_migrations` records each ordered migration and `PRAGMA user_version` records the current version. Initialization applies missing migrations inside one `BEGIN IMMEDIATE` transaction and rejects unknown applied versions. This is the v0.1 to v0.2 migration boundary; startup logic does not scatter table creation across repository methods.

## Transaction and concurrency contract

Every mutation uses an explicit `BEGIN IMMEDIATE` transaction. Callers provide `expected_revision`; a mismatch raises `StaleRevisionError` before state or event writes. Successful state mutations increment revision exactly once and append their event in the same transaction. Any event insertion failure rolls the state change back.

Signal events `SUPERSEDED` and `REVALIDATION_REQUESTED` refer to the current revision without changing lifecycle state or incrementing state revision. They still require an expected-revision check.

## Lifecycle policy

The normal path is:

```text
create_candidate -> candidate
record_verification(passed|failed|rejected) -> candidate
candidate + recorded passed verification -> verified
verified -> canonical -> deprecated -> archived
```

Recording verification evidence and granting verified trust are separate mutations. `record_verification` requires a typed `VerificationOutcome`, stores the evidence and outcome, increments revision, and emits `VERIFICATION_RECORDED` without changing candidate status. Only a recorded `passed` outcome permits the explicit candidate-to-verified transition, which emits `VERIFIED`. Failed or rejected outcomes remain candidates and cannot enter verified state; they may be revised, reverified, or explicitly archived. Updating candidate content clears any previously recorded outcome so stale verification cannot authorize promotion.

Direct candidate-to-canonical promotion is invalid. `create_candidate` has no status argument. Explicit early discard from `candidate` or `verified` to `archived` is allowed and emits `ARCHIVED`; this policy is tested. `SUPERSEDED` and `REVALIDATION_REQUESTED` are events, not statuses.

## Append-only event history

The event table stores `event_id`, logical `knowledge_ref`, state `revision`, actor, UTC timestamp, event type, and canonical JSON metadata. Service/repository APIs expose append and list operations only. SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers abort attempts to mutate existing events. A `BEFORE INSERT` collision trigger also rejects an existing `event_id` or `event_sequence`, so `INSERT OR REPLACE` cannot bypass append-only history even when a direct SQLite connection does not enable recursive triggers. Fresh, non-colliding events remain appendable.

## OKF preservation boundary

Knowledge state preserves type, title, body, stability, tags, scope, applicability, counterconditions, sources, provenance, generated metadata, verification metadata, freshness, timestamps, status, and revision. Structured values cross a strict JSON boundary and are stored with sorted keys, compact separators, UTF-8 text, and non-finite numbers rejected. Python repr and pickle are not used. Full OKF import/export remains outside v0.1.
