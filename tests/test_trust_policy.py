from __future__ import annotations

from typing import Any

import pytest

from kasane.trust import PolicyAction, Tier, TrustPolicyConfig, evaluate
from kasane.vault.models import (
    EventType,
    KnowledgeEvent,
    KnowledgeStatus,
    VerificationOutcome,
)

REF = "vault://global/policy/example"
C, V, K = KnowledgeStatus.CANDIDATE, KnowledgeStatus.VERIFIED, KnowledgeStatus.CANONICAL


def ev(
    event_type: EventType,
    actor: str = "agent",
    session: str = "s1",
    model: str = "m1",
    **extra: Any,
) -> KnowledgeEvent:
    metadata: dict[str, Any] = {"agent": {"session": session, "model": model}, **extra}
    return KnowledgeEvent("id", REF, 1, actor, "t", event_type, metadata)


def created(session: str = "author") -> KnowledgeEvent:
    return ev(EventType.CREATED, session=session)


def test_candidate_with_nothing_is_t0_and_idle() -> None:
    result = evaluate(C, None, [created()])
    assert (result.tier, result.action) == (Tier.T0, PolicyAction.NONE)


def test_two_independent_corroborations_verify() -> None:
    events = [created(), ev(EventType.CORROBORATED, session="a")]
    assert evaluate(C, None, events).action is PolicyAction.NONE
    events.append(ev(EventType.CORROBORATED, session="b"))
    assert evaluate(C, None, events).action is PolicyAction.VERIFY_BY_CORROBORATION


def test_repeats_from_one_session_and_author_do_not_count() -> None:
    events = [
        created("author"),
        ev(EventType.CORROBORATED, session="a"),
        ev(EventType.CORROBORATED, session="a"),
        ev(EventType.CORROBORATED, session="author"),
    ]
    result = evaluate(C, None, events)
    assert result.corroborations == 1
    assert result.action is PolicyAction.NONE


def test_reserved_actors_never_count_as_independent() -> None:
    events = [
        created(),
        ev(EventType.CORROBORATED, actor="policy", session="x"),
        ev(EventType.CORROBORATED, actor="operator", session="y"),
    ]
    assert evaluate(C, None, events).corroborations == 0


def test_independent_passed_verification_promotes() -> None:
    events = [
        created(),
        ev(EventType.VERIFICATION_RECORDED, session="v", outcome="passed"),
    ]
    result = evaluate(C, VerificationOutcome.PASSED, events)
    assert result.action is PolicyAction.PROMOTE_VERIFIED


def test_failure_blocks_then_archives_candidate() -> None:
    events = [
        created(),
        ev(EventType.CORROBORATED, session="a"),
        ev(EventType.CORROBORATED, session="b"),
        ev(EventType.OUTCOME_REPORTED, session="f1", outcome="failure"),
    ]
    assert evaluate(C, None, events).action is PolicyAction.NONE
    events.append(ev(EventType.VERIFICATION_RECORDED, session="f2", outcome="rejected"))
    assert evaluate(C, None, events).action is PolicyAction.ARCHIVE


def test_verified_becomes_approval_eligible_after_two_independent_successes() -> None:
    events = [
        created(),
        ev(EventType.OUTCOME_REPORTED, session="u1", outcome="success"),
    ]
    first = evaluate(V, None, events)
    assert (first.tier, first.approval_eligible) == (Tier.T1, False)
    events.append(ev(EventType.OUTCOME_REPORTED, session="u2", outcome="success"))
    second = evaluate(V, None, events)
    assert (second.tier, second.approval_eligible) == (Tier.T2, True)
    assert second.action is PolicyAction.NONE  # policy never reaches canonical


def test_canonical_failures_revalidate_then_deprecate() -> None:
    events = [
        created(),
        ev(EventType.OUTCOME_REPORTED, session="f1", outcome="failure"),
    ]
    assert evaluate(K, None, events).action is PolicyAction.REQUEST_REVALIDATION
    events.append(ev(EventType.REVALIDATION_REQUESTED, actor="policy"))
    assert evaluate(K, None, events).action is PolicyAction.NONE
    events.append(ev(EventType.OUTCOME_REPORTED, session="f2", outcome="failure"))
    assert evaluate(K, None, events).action is PolicyAction.DEPRECATE


def test_content_update_resets_counts_and_author() -> None:
    events = [
        created("author"),
        ev(EventType.CORROBORATED, session="a"),
        ev(EventType.UPDATED, session="editor"),
        ev(EventType.CORROBORATED, session="b"),
    ]
    result = evaluate(C, None, events)
    assert result.corroborations == 1
    assert result.author is not None and result.author.session == "editor"


def test_distinct_models_mode_excludes_author_model() -> None:
    config = TrustPolicyConfig(require_distinct_models=True)
    events = [
        created(),  # model m1
        ev(EventType.CORROBORATED, session="a", model="m1"),
        ev(EventType.CORROBORATED, session="b", model="m2"),
    ]
    assert evaluate(C, None, events, config).corroborations == 1
    events.append(ev(EventType.CORROBORATED, session="c", model="m3"))
    result = evaluate(C, None, events, config)
    assert result.action is PolicyAction.VERIFY_BY_CORROBORATION


def test_terminal_states_have_no_tier() -> None:
    for status in (KnowledgeStatus.DEPRECATED, KnowledgeStatus.ARCHIVED):
        result = evaluate(status, None, [created()])
        assert (result.tier, result.action) == (None, PolicyAction.NONE)


def test_config_rejects_non_positive_thresholds() -> None:
    with pytest.raises(ValueError):
        TrustPolicyConfig(successes_for_approval=0)
