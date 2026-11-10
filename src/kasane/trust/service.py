"""Agent intake, trust signals, and human approval over Vault Core (Issue #39)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kasane.contracts import validate_contract
from kasane.vault.errors import (
    KnowledgeNotFoundError,
    StaleRevisionError,
    VaultError,
)
from kasane.vault.identity import parse_knowledge_ref
from kasane.vault.json_codec import JsonValue
from kasane.vault.models import (
    EventType,
    KnowledgeDraft,
    KnowledgeRecord,
    KnowledgeStatus,
    VerificationOutcome,
)
from kasane.vault.repository import VaultRepository

from .policy import (
    DEFAULT_POLICY,
    OPERATOR_ACTOR,
    POLICY_ACTOR,
    RESERVED_ACTORS,
    PolicyAction,
    TrustAssessment,
    TrustPolicyConfig,
    evaluate,
)
from .similar import SimilarKnowledge, find_similar

PROPOSAL_SCHEMA = "knowledge-proposal.schema.json"
SIGNAL_SCHEMA = "knowledge-signal.schema.json"
DEFAULT_MAX_BODY_BYTES = 12_000
_MAX_POLICY_STEPS = 4
# Measured on paraphrases: reworded duplicates scored 0.58 (ja) / 0.78 (en);
# same-topic different advice 0.27. Tentative until real data exists.
DEFAULT_DUPLICATE_THRESHOLD = 0.5
DEFAULT_HINT_THRESHOLD = 0.25


class TrustRejectedError(ValueError):
    """A write was refused; the Vault is unchanged."""


class DuplicateKnowledgeError(TrustRejectedError):
    """A proposal nearly duplicates live knowledge; corroborate that instead."""

    def __init__(self, matches: list[SimilarKnowledge]) -> None:
        self.matches = matches
        refs = ", ".join(match.knowledge_ref for match in matches)
        super().__init__(
            f"near-duplicate of existing knowledge ({refs}); corroborate it "
            "instead, or make the proposal distinct"
        )


@dataclass(frozen=True)
class AgentContext:
    """Who is writing. Comes from server configuration, never from payloads."""

    actor: str
    session: str
    model: str

    def __post_init__(self) -> None:
        for name in ("actor", "session", "model"):
            value = getattr(self, name)
            if not value.strip() or len(value) > 256:
                raise ValueError(f"agent {name} must be 1-256 non-blank characters")
        if self.actor in RESERVED_ACTORS:
            raise ValueError(f"actor {self.actor!r} is reserved")

    def metadata(self, **extra: JsonValue) -> dict[str, JsonValue]:
        return {"agent": {"session": self.session, "model": self.model}, **extra}


_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileResult:
    changed: list[str]
    skipped: list[str]


@dataclass(frozen=True)
class QuarantinePlan:
    targets: list[str]  # will be (or were) archived
    vouched: list[str]  # live knowledge the actor vouched for: review these
    archived: list[str]
    failed: list[str]
    applied: bool


@dataclass(frozen=True)
class TrustResult:
    record: KnowledgeRecord
    assessment: TrustAssessment
    last_event_id: str | None = None

    def summary(self) -> dict[str, JsonValue]:
        return {
            "knowledge_ref": self.record.knowledge_ref,
            "revision": self.record.revision,
            **self.assessment.summary(),
        }


class TrustService:
    def __init__(
        self,
        repository: VaultRepository,
        config: TrustPolicyConfig | None = None,
        *,
        write_scopes: Sequence[str] | None = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        duplicate_threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
    ) -> None:
        if not 0 < duplicate_threshold <= 1:
            raise ValueError("duplicate_threshold must be in (0, 1]")
        self.repository = repository
        self.config = config or DEFAULT_POLICY
        # An empty list means "nowhere", not "everywhere".
        self.write_scopes = None if write_scopes is None else tuple(write_scopes)
        self.max_body_bytes = max_body_bytes
        self.duplicate_threshold = duplicate_threshold

    # Agent operations -------------------------------------------------

    def propose(self, proposal: Mapping[str, Any], agent: AgentContext) -> TrustResult:
        with _refusing():
            return self._propose_unchecked(proposal, agent)

    def _propose_unchecked(
        self, proposal: Mapping[str, Any], agent: AgentContext
    ) -> TrustResult:
        self._require_open(agent)
        validate_contract(PROPOSAL_SCHEMA, proposal)
        ref = str(proposal["knowledge_ref"])
        self._require_writable(ref)
        body = str(proposal["body"])
        if len(body.encode("utf-8")) > self.max_body_bytes:
            raise TrustRejectedError(f"body exceeds {self.max_body_bytes} UTF-8 bytes")
        stale_after = proposal.get("stale_after")
        if stale_after is not None:
            _require_aware_timestamp(str(stale_after))
        if self._exists(ref):
            raise TrustRejectedError(
                f"{ref} already exists; corroborate it instead of re-proposing"
            )
        duplicates = self.similar(
            str(proposal["title"]),
            [str(tag) for tag in proposal.get("tags", [])],
            body,
            threshold=self.duplicate_threshold,
        )
        if duplicates:
            raise DuplicateKnowledgeError(duplicates)
        draft = KnowledgeDraft(
            knowledge_ref=ref,
            knowledge_type=str(proposal["knowledge_type"]),
            title=str(proposal["title"]),
            body=body,
            tags=list(proposal.get("tags", [])),
            sources=list(proposal["sources"]),
            provenance={"origin": "agent", "agent": agent.metadata()["agent"]},
            generated={"by": "agent"},
            stale_after=None if stale_after is None else str(stale_after),
        )
        try:
            self.repository.create_candidate(
                draft, actor=agent.actor, event_metadata=agent.metadata()
            )
        except sqlite3.IntegrityError as error:  # concurrent proposal won the race
            raise TrustRejectedError(f"{ref} already exists") from error
        return self.assess(ref)

    def signal(self, signal: Mapping[str, Any], agent: AgentContext) -> TrustResult:
        with _refusing():
            return self._signal_unchecked(signal, agent)

    def _signal_unchecked(
        self, signal: Mapping[str, Any], agent: AgentContext
    ) -> TrustResult:
        self._require_open(agent)
        validate_contract(SIGNAL_SCHEMA, signal)
        ref = str(signal["knowledge_ref"])
        self._require_writable(ref)
        record = self._get(ref)
        if record.status in (KnowledgeStatus.DEPRECATED, KnowledgeStatus.ARCHIVED):
            raise TrustRejectedError(f"{ref} is {record.status.value}")
        kind = signal["kind"]
        note = signal.get("note")
        try:
            self._record_signal(record, agent, kind, signal, note)
        except StaleRevisionError as error:
            raise TrustRejectedError(
                f"{ref} changed concurrently; re-read and retry"
            ) from error
        # The signal is committed from here on. A failing policy step must not
        # be reported as a refusal (the agent would retry and duplicate it);
        # reconcile() finishes the policy later.
        try:
            self._apply_policy(ref)
        except (VaultError, sqlite3.Error):
            _LOG.warning(
                "policy step for %s failed after its signal committed; "
                "kasane-admin reconcile will retry",
                ref,
                exc_info=True,
            )
        return self.assess(ref)

    def _record_signal(
        self,
        record: KnowledgeRecord,
        agent: AgentContext,
        kind: str,
        signal: Mapping[str, Any],
        note: Any,
    ) -> None:
        ref = record.knowledge_ref
        if kind == "verification":
            self._verify(
                record, agent, VerificationOutcome(signal["verification"]), note
            )
        else:
            event_type = (
                EventType.CORROBORATED
                if kind == "corroboration"
                else EventType.OUTCOME_REPORTED
            )
            extra: dict[str, JsonValue] = {}
            if note is not None:
                extra["note"] = note
            if kind == "outcome":
                extra["outcome"] = signal["outcome"]
            self.repository.record_signal_event(
                ref,
                event_type,
                expected_revision=record.revision,
                actor=agent.actor,
                metadata=agent.metadata(**extra),
            )

    # Read side ---------------------------------------------------------

    def assess(self, knowledge_ref: str) -> TrustResult:
        record = self._get(knowledge_ref)
        events = self.repository.list_events(knowledge_ref)
        assessment = evaluate(
            record.status, record.verification_outcome, events, self.config
        )
        return TrustResult(record, assessment, events[-1].event_id if events else None)

    def similar(
        self,
        title: str,
        tags: Sequence[str],
        body: str,
        *,
        exclude_ref: str | None = None,
        threshold: float = DEFAULT_HINT_THRESHOLD,
    ) -> list[SimilarKnowledge]:
        """Similar live knowledge, limited to this writer's scopes.

        Out-of-scope records are never compared or disclosed: a scoped writer
        must not learn about, or be blocked by, knowledge it cannot touch.
        """

        records = [
            record
            for record in self.repository.list_knowledge()
            if self._in_scope(record.knowledge_ref)
        ]
        return find_similar(
            title, tags, body, records, exclude_ref=exclude_ref, threshold=threshold
        )

    def approval_queue(self) -> list[TrustResult]:
        results = [
            self.assess(record.knowledge_ref)
            for record in self.repository.list_knowledge()
            if record.status is KnowledgeStatus.VERIFIED
        ]
        return [result for result in results if result.assessment.approval_eligible]

    # Human operations (approval queue) --------------------------------

    def approve(self, knowledge_ref: str, *, note: str | None = None) -> TrustResult:
        with _refusing():
            return self._approve_unchecked(knowledge_ref, note=note)

    def _approve_unchecked(
        self, knowledge_ref: str, *, note: str | None = None
    ) -> TrustResult:
        current = self.assess(knowledge_ref)
        if not current.assessment.approval_eligible:
            raise TrustRejectedError(
                f"{knowledge_ref} is not approval-eligible (T2 required)"
            )
        self._operator_transition(
            current, KnowledgeStatus.CANONICAL, {"decision": "approve", "note": note}
        )
        return self.assess(knowledge_ref)

    def reject(self, knowledge_ref: str, *, reason: str) -> TrustResult:
        with _refusing():
            return self._reject_unchecked(knowledge_ref, reason=reason)

    def _reject_unchecked(self, knowledge_ref: str, *, reason: str) -> TrustResult:
        """Decline a T2 item in the approval queue. Not an emergency tool."""

        if not reason.strip():
            raise TrustRejectedError("reject requires a reason")
        current = self.assess(knowledge_ref)
        if not current.assessment.approval_eligible:
            raise TrustRejectedError(
                f"{knowledge_ref} is not in the approval queue (T2 required); "
                "use quarantine for emergencies"
            )
        self._operator_transition(
            current, KnowledgeStatus.ARCHIVED, {"decision": "reject", "reason": reason}
        )
        return self.assess(knowledge_ref)

    def reconcile(self) -> ReconcileResult:
        """Re-apply the policy to every live record; report changed and skipped.

        Signal recording and policy transitions are separate transactions, so
        a crash in between can leave a record behind its policy state. The
        policy is deterministic, so re-running it is always safe.
        """

        changed: list[str] = []
        skipped: list[str] = []
        for record in self.repository.list_knowledge():
            if record.status in (KnowledgeStatus.DEPRECATED, KnowledgeStatus.ARCHIVED):
                continue
            try:
                acted = self._apply_policy(record.knowledge_ref)
            except VaultError:
                skipped.append(record.knowledge_ref)  # surfaced, sweep continues
                continue
            if acted:
                changed.append(record.knowledge_ref)
        return ReconcileResult(changed=changed, skipped=skipped)

    # Emergency controls (operator only) -------------------------------

    def freeze(self, *, reason: str) -> None:
        """Refuse every agent write until unfreeze(). Operator actions continue."""

        self._log("FREEZE", reason)

    def unfreeze(self, *, reason: str) -> None:
        self._log("UNFREEZE", reason)

    def block(self, actor: str, *, reason: str) -> None:
        """Refuse all future writes from one writer identity."""

        if actor != actor.strip() or not actor or actor in RESERVED_ACTORS:
            # Whitespace would make a block that never matches any writer.
            raise TrustRejectedError(f"cannot block {actor!r}")
        self._log("BLOCK_ACTOR", reason, target=actor)

    def unblock(self, actor: str, *, reason: str) -> None:
        self._log("UNBLOCK_ACTOR", reason, target=actor)

    def quarantine(
        self,
        *,
        reason: str,
        actor: str | None = None,
        since: str | None = None,
        refs: Sequence[str] = (),
        apply: bool = False,
    ) -> QuarantinePlan:
        with _refusing():
            return self._quarantine_unchecked(
                reason=reason, actor=actor, since=since, refs=refs, apply=apply
            )

    def _quarantine_unchecked(
        self,
        *,
        reason: str,
        actor: str | None,
        since: str | None,
        refs: Sequence[str],
        apply: bool,
    ) -> QuarantinePlan:
        """Archive knowledge authored by ``actor`` (since ``since``) and ``refs``.

        Also reports live knowledge the actor vouched for (corroborations,
        passed verifications, successes), which may need review. Dry run unless
        ``apply``; applying is logged once as a QUARANTINE operator action.
        """

        if not reason.strip():
            raise TrustRejectedError("quarantine requires a reason")
        for ref in refs:
            parse_knowledge_ref(ref)  # malformed refs refused before any work
        if actor is None and not refs:
            raise TrustRejectedError("quarantine needs --actor or at least one --ref")
        cutoff = None if since is None else _parse_since(since)
        targets: list[str] = []
        vouched: list[str] = []
        for record in self.repository.list_knowledge():
            if record.status is KnowledgeStatus.ARCHIVED:
                continue
            events = self.repository.list_events(record.knowledge_ref)
            if record.knowledge_ref in refs or (
                actor is not None and _authored_by(events, actor, cutoff)
            ):
                targets.append(record.knowledge_ref)
            elif actor is not None and _vouched_by(events, actor, cutoff):
                vouched.append(record.knowledge_ref)
        unknown = [ref for ref in refs if ref not in targets]
        for ref in unknown:
            self._get(ref)  # raises for unknown refs; archived refs are no-ops
        archived: list[str] = []
        failed: list[str] = []
        if apply:
            try:
                for ref in targets:
                    try:
                        self._archive_now(ref, reason)
                        archived.append(ref)
                    except (StaleRevisionError, TrustRejectedError, VaultError):
                        failed.append(ref)
            finally:
                # Logged even if the sweep aborts (e.g. database locked), so
                # what was archived is always on record.
                done = set(archived) | set(failed)
                failed.extend([ref for ref in targets if ref not in done])
                self._log_quarantine(
                    reason, actor, since, refs, archived, failed, vouched
                )
        return QuarantinePlan(
            targets=targets,
            vouched=vouched,
            archived=archived,
            failed=failed,
            applied=apply,
        )

    def _log_quarantine(
        self,
        reason: str,
        actor: str | None,
        since: str | None,
        refs: Sequence[str],
        archived: list[str],
        failed: list[str],
        vouched: list[str],
    ) -> None:
        self._log(
            "QUARANTINE",
            reason,
            target=actor,
            details={
                "since": since,
                "refs": _json_list(refs),
                "archived": _json_list(archived),
                "failed": _json_list(failed),
                "vouched": _json_list(vouched),
            },
        )

    # Internals ---------------------------------------------------------

    def _require_open(self, agent: AgentContext) -> None:
        controls = self.repository.write_controls()
        if controls.frozen:
            raise TrustRejectedError("vault is frozen by the operator; writes refused")
        if agent.actor in controls.blocked_actors:
            raise TrustRejectedError(
                f"writer {agent.actor!r} is blocked by the operator"
            )

    def _log(
        self,
        action: str,
        reason: str,
        *,
        target: str | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        if not reason.strip():
            raise TrustRejectedError(f"{action.lower()} requires a reason")
        self.repository.record_operator_action(
            action, reason=reason, actor=OPERATOR_ACTOR, target=target, details=details
        )

    def _archive_now(self, knowledge_ref: str, reason: str) -> None:
        """Retire a record immediately: canonical goes via deprecated."""

        metadata: dict[str, JsonValue] = {"decision": "quarantine", "reason": reason}
        record = self._get(knowledge_ref)
        if record.status is KnowledgeStatus.CANONICAL:
            self.repository.transition_lifecycle(
                knowledge_ref,
                KnowledgeStatus.DEPRECATED,
                expected_revision=record.revision,
                actor=OPERATOR_ACTOR,
                event_metadata=metadata,
            )
            record = self._get(knowledge_ref)
        self.repository.transition_lifecycle(
            knowledge_ref,
            KnowledgeStatus.ARCHIVED,
            expected_revision=record.revision,
            actor=OPERATOR_ACTOR,
            event_metadata=metadata,
        )

    def _operator_transition(
        self,
        current: TrustResult,
        target: KnowledgeStatus,
        metadata: dict[str, JsonValue],
    ) -> None:
        """Apply a human decision only if nothing happened since it was checked.

        Signal events do not bump the revision, so the guard is on the last
        event: a failure reported after the check invalidates the decision.
        """

        try:
            self.repository.transition_lifecycle(
                current.record.knowledge_ref,
                target,
                expected_revision=current.record.revision,
                expected_last_event_id=current.last_event_id,
                actor=OPERATOR_ACTOR,
                event_metadata=metadata,
            )
        except StaleRevisionError as error:
            raise TrustRejectedError(
                f"{current.record.knowledge_ref} changed since it was checked; "
                "re-check the approval queue"
            ) from error

    def _verify(
        self,
        record: KnowledgeRecord,
        agent: AgentContext,
        outcome: VerificationOutcome,
        note: Any,
    ) -> None:
        if record.status is not KnowledgeStatus.CANDIDATE:
            raise TrustRejectedError(
                "only candidates take verifications; report outcomes instead"
            )
        author = self.assess(record.knowledge_ref).assessment.author
        if author is not None and (agent.actor, agent.session) == author.key:
            raise TrustRejectedError("self-verification is not allowed")
        self.repository.record_verification(
            record.knowledge_ref,
            expected_revision=record.revision,
            actor=agent.actor,
            outcome=outcome,
            verification={"basis": "agent", "note": note},
            event_metadata=agent.metadata(),
        )

    def _apply_policy(self, knowledge_ref: str) -> bool:
        """Drive the record toward its policy state; return whether anything changed.

        Best effort: under sustained contention the loop stops without raising,
        because the caller's own write has already committed. The next signal or
        ``reconcile()`` finishes the job, since the policy is deterministic.
        """

        acted = False
        for _ in range(_MAX_POLICY_STEPS):
            current = self.assess(knowledge_ref)
            action = current.assessment.action
            if action is PolicyAction.NONE:
                break
            try:
                acted = self._perform(current, action) or acted
            except StaleRevisionError:
                continue  # another writer moved first; re-evaluate
        return acted

    def _perform(self, current: TrustResult, action: PolicyAction) -> bool:
        """Apply one policy action; return True once anything was committed.

        Advancing actions (verify, promote) are guarded on the exact event log
        they were decided from, because any later failure must block them.
        Retiring actions (revalidate, demote, archive, deprecate) are monotone
        in failures: later events cannot invalidate them, so they are guarded
        on revision only and do not starve under signal contention.
        """

        record, assessment = current.record, current.assessment
        ref, revision = record.knowledge_ref, record.revision
        evidence: dict[str, JsonValue] = {
            "policy": action.value,
            "assessment": dict(assessment.summary()),
        }
        if action is PolicyAction.VERIFY_BY_CORROBORATION:
            self.repository.record_verification(
                ref,
                expected_revision=revision,
                expected_last_event_id=current.last_event_id,
                actor=POLICY_ACTOR,
                outcome=VerificationOutcome.PASSED,
                verification={"basis": "corroboration", **evidence},
            )
            # Committed. Promotion is a separate decision for the next pass,
            # which re-reads the log (PROMOTE_VERIFIED fires on PASSED +
            # corroboration threshold) and is itself guarded.
            return True
        if action is PolicyAction.PROMOTE_VERIFIED:
            self.repository.transition_lifecycle(
                ref,
                KnowledgeStatus.VERIFIED,
                expected_revision=revision,
                expected_last_event_id=current.last_event_id,
                actor=POLICY_ACTOR,
                event_metadata=evidence,
            )
            return True
        if action is PolicyAction.REQUEST_REVALIDATION:
            self.repository.record_signal_event(
                ref,
                EventType.REVALIDATION_REQUESTED,
                expected_revision=revision,
                actor=POLICY_ACTOR,
                metadata=evidence,
            )
            return True
        targets = {
            PolicyAction.DEMOTE: KnowledgeStatus.CANDIDATE,
            PolicyAction.DEPRECATE: KnowledgeStatus.DEPRECATED,
            PolicyAction.ARCHIVE: KnowledgeStatus.ARCHIVED,
        }
        self.repository.transition_lifecycle(
            ref,
            targets[action],
            expected_revision=revision,
            actor=POLICY_ACTOR,
            event_metadata=evidence,
        )
        return True

    def _require_writable(self, knowledge_ref: str) -> None:
        parse_knowledge_ref(knowledge_ref)  # malformed refs refused via _refusing
        if not self._in_scope(knowledge_ref):
            raise TrustRejectedError(
                f"{knowledge_ref} is outside the allowed write scopes"
            )

    def _in_scope(self, knowledge_ref: str) -> bool:
        try:
            namespace = parse_knowledge_ref(knowledge_ref).namespace
        except VaultError:
            return False
        if self.write_scopes is None:
            return True
        return any(
            namespace == scope or namespace.startswith(scope + "/")
            for scope in self.write_scopes
        )

    def _get(self, knowledge_ref: str) -> KnowledgeRecord:
        try:
            return self.repository.get_knowledge(knowledge_ref)
        except KnowledgeNotFoundError as error:
            raise TrustRejectedError(f"unknown knowledge: {knowledge_ref}") from error

    def _exists(self, knowledge_ref: str) -> bool:
        try:
            self.repository.get_knowledge(knowledge_ref)
        except KnowledgeNotFoundError:
            return False
        return True


def _require_aware_timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise TrustRejectedError(f"stale_after is not ISO 8601: {value}") from error
    if parsed.tzinfo is None:
        raise TrustRejectedError("stale_after must include a timezone")


@contextmanager
def _refusing() -> Iterator[None]:
    """Surface Vault-level refusals as TrustRejectedError (Vault unchanged)."""

    try:
        yield
    except StaleRevisionError as error:
        raise TrustRejectedError(
            f"changed concurrently; re-read and retry ({error})"
        ) from error
    except VaultError as error:
        raise TrustRejectedError(str(error)) from error


_POSITIVE = {
    (EventType.CORROBORATED, None),
    (EventType.VERIFICATION_RECORDED, "passed"),
    (EventType.OUTCOME_REPORTED, "success"),
}


def _parse_since(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise TrustRejectedError(f"since is not ISO 8601: {value}") from error
    if parsed.tzinfo is None:
        raise TrustRejectedError("since must include a timezone")
    return parsed


def parse_since(value: str) -> datetime:
    """Public for the admin CLI: an aware ISO 8601 cutoff or TrustRejectedError."""

    return _parse_since(value)


def _at_or_after(timestamp: str, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return True  # unparseable: err on the side of including it
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)  # never compare naive with aware
    return parsed >= cutoff


def _authored_by(events: Sequence[Any], actor: str, cutoff: datetime | None) -> bool:
    content = [
        e for e in events if e.event_type in (EventType.CREATED, EventType.UPDATED)
    ]
    return (
        bool(content)
        and content[-1].actor == actor
        and _at_or_after(content[-1].timestamp, cutoff)
    )


def _vouched_by(events: Sequence[Any], actor: str, cutoff: datetime | None) -> bool:
    for event in events:
        if event.actor != actor or not _at_or_after(event.timestamp, cutoff):
            continue
        outcome = (
            event.metadata.get("outcome") if isinstance(event.metadata, dict) else None
        )
        key = (
            event.event_type,
            None if event.event_type is EventType.CORROBORATED else outcome,
        )
        if key in _POSITIVE:
            return True
    return False


def _json_list(values: Sequence[str]) -> list[JsonValue]:
    return list(values)
