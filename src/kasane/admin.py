"""``kasane-admin``: the operator's tools (approval queue, audit, emergency stop).

Humans act here only at the top tier (approve/reject T2 items) and in
emergencies (freeze, block, quarantine). See docs/trust.md and Issue #40.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kasane.trust import (
    OPERATOR_ACTOR,
    TrustPolicyConfig,
    TrustRejectedError,
    TrustService,
)
from kasane.trust.service import parse_since
from kasane.vault.errors import VaultError
from kasane.vault.models import KnowledgeStatus
from kasane.vault.repository import VaultRepository

VAULT_ENV = "KASANE_VAULT_PATH"
EXCERPT_CHARS = 160


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            payload = _init(parser, args)
        else:
            service = TrustService(
                VaultRepository(_require_vault(parser, args.vault)), TrustPolicyConfig()
            )
            payload = _dispatch(service, args)
    except (TrustRejectedError, VaultError, ValueError, sqlite3.Error, OSError) as exc:
        # Operator input errors and refusals: a message, never a traceback.
        parser.exit(1, f"kasane-admin: {exc}\n")
    _emit(payload, as_json=args.json)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kasane-admin", description=__doc__)
    parser.add_argument("--vault", default=os.environ.get(VAULT_ENV))
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "init",
        help="create an empty Vault (default: $KASANE_DATA_ROOT or "
        "~/.local/state/kasane/vault.db); safe to re-run",
    )
    sub.add_parser("status", help="write gates, counts by status, queue size")
    sub.add_parser("queue", help="T2 items awaiting human approval")

    approve = sub.add_parser("approve", help="promote a T2 item to canonical")
    approve.add_argument("ref")
    approve.add_argument("--note")

    reject = sub.add_parser("reject", help="decline a T2 item (archives it)")
    reject.add_argument("ref")
    reject.add_argument("--reason", required=True)

    events = sub.add_parser("events", help="audit the event log")
    events.add_argument("--actor")
    events.add_argument("--ref")
    events.add_argument("--since", help="ISO 8601 with timezone")
    events.add_argument("--limit", type=int, default=200)

    sub.add_parser("reconcile", help="re-apply the trust policy to every record")

    for name, text in (
        ("freeze", "refuse all agent writes"),
        ("unfreeze", "allow writes"),
    ):
        gate = sub.add_parser(name, help=text)
        gate.add_argument("--reason", required=True)
    for name, text in (
        ("block", "refuse writes from one writer"),
        ("unblock", "lift a block"),
    ):
        gate = sub.add_parser(name, help=text)
        gate.add_argument("writer")
        gate.add_argument("--reason", required=True)

    quarantine = sub.add_parser(
        "quarantine", help="archive what a writer authored (dry run unless --yes)"
    )
    quarantine.add_argument("--actor")
    quarantine.add_argument("--since", help="ISO 8601 with timezone")
    quarantine.add_argument("--ref", action="append", default=[], dest="refs")
    quarantine.add_argument("--reason", required=True)
    quarantine.add_argument("--yes", action="store_true", help="apply the plan")
    return parser


def _dispatch(service: TrustService, args: argparse.Namespace) -> Any:
    command = args.command
    if command == "status":
        return _status(service)
    if command == "queue":
        return [_queue_item(service, result) for result in service.approval_queue()]
    if command == "approve":
        return service.approve(args.ref, note=args.note).summary()
    if command == "reject":
        return service.reject(args.ref, reason=args.reason).summary()
    if command == "events":
        return _events(service, args)
    if command == "reconcile":
        result = service.reconcile()
        return {"changed": result.changed, "skipped": result.skipped}
    if command == "freeze":
        service.freeze(reason=args.reason)
        return _status(service)
    if command == "unfreeze":
        service.unfreeze(reason=args.reason)
        return _status(service)
    if command == "block":
        service.block(args.writer, reason=args.reason)
        return _status(service)
    if command == "unblock":
        service.unblock(args.writer, reason=args.reason)
        return _status(service)
    if command == "quarantine":
        plan = service.quarantine(
            reason=args.reason,
            actor=args.actor,
            since=args.since,
            refs=args.refs,
            apply=args.yes,
        )
        return {
            "applied": plan.applied,
            "targets": plan.targets,
            "archived": plan.archived,
            "failed": plan.failed,
            "vouched_for_review": plan.vouched,
        }
    raise AssertionError(command)  # pragma: no cover - argparse restricts choices


def _init(parser: argparse.ArgumentParser, args: argparse.Namespace) -> dict[str, Any]:
    path = _vault_path(args.vault)
    if path.exists() and not path.is_file():
        parser.exit(1, f"kasane-admin: not a file: {path}\n")
    before = _schema_version_of(path) if path.is_file() else None
    repository = VaultRepository(path)  # creates, or migrates an existing Vault
    after = repository.current_schema_version()
    payload: dict[str, Any] = {
        "vault": str(repository.database_path),
        "created": before is None,
        "schema_version": after,
        "knowledge": len(repository.list_knowledge()),
    }
    if before is not None and before != after:
        payload["migrated_from"] = before
    return payload


def _schema_version_of(path: Path) -> int:
    """Read the schema version without migrating (0 for an unversioned file)."""

    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _vault_path(value: str | None) -> Path:
    from kasane.vault.config import default_database_path

    return Path(value).expanduser() if value else default_database_path()


def _status(service: TrustService) -> dict[str, Any]:
    controls = service.repository.write_controls()
    counts = {status.value: 0 for status in KnowledgeStatus}
    for record in service.repository.list_knowledge():
        counts[record.status.value] += 1
    return {
        "frozen": controls.frozen,
        "blocked_writers": sorted(controls.blocked_actors),
        "knowledge": counts,
        "approval_queue": len(service.approval_queue()),
    }


def _queue_item(service: TrustService, result: Any) -> dict[str, Any]:
    record = result.record
    events = service.repository.list_events(record.knowledge_ref)
    contributors = sorted(
        {event.actor for event in events if event.actor != OPERATOR_ACTOR}
    )
    return {
        **result.summary(),
        "title": record.title,
        "knowledge_type": record.knowledge_type,
        "excerpt": _excerpt(record.body),
        "actors": contributors,
    }


def _events(service: TrustService, args: argparse.Namespace) -> list[dict[str, Any]]:
    cutoff = None if args.since is None else parse_since(args.since)
    events = service.repository.search_events(
        actor=args.actor, knowledge_ref=args.ref, since=cutoff, limit=args.limit
    )
    return [
        {
            "timestamp": event.timestamp,
            "actor": event.actor,
            "event": event.event_type.value,
            "knowledge_ref": event.knowledge_ref,
            "revision": event.revision,
        }
        for event in events
    ]


def _excerpt(body: str) -> str:
    flat = " ".join(body.split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[: EXCERPT_CHARS - 1] + "…"


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if isinstance(payload, list):
        if not payload:
            print("(none)")
        for item in payload:
            print(_line(item))
        return
    for key, value in payload.items():
        print(f"{key}: {json.dumps(value, ensure_ascii=False)}")


def _line(item: dict[str, Any]) -> str:
    if "event" in item:
        return (
            f"{item['timestamp']}  {item['actor']:<12} {item['event']:<22} "
            f"{item['knowledge_ref']}@{item['revision']}"
        )
    return (
        f"{item['tier']}  {item['knowledge_ref']}  [{item['knowledge_type']}] "
        f"{item['title']}\n      successes={item['successes']} "
        f"corroborations={item['corroborations']} "
        f"verifications={item['independent_verifications']} "
        f"actors={','.join(item['actors'])}\n      {item['excerpt']}"
    )


def _require_vault(parser: argparse.ArgumentParser, value: str | None) -> Path:
    # Same default as `init`, so `init` then `status` works with no settings.
    path = _vault_path(value)
    if not path.is_file():
        parser.exit(
            2,
            f"kasane-admin: Vault DB not found: {path} "
            f"(run `kasane-admin init`, or set --vault / {VAULT_ENV})\n",
        )
    return path


if __name__ == "__main__":
    sys.exit(main())
