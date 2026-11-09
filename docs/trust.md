# Trust lifecycle

KASANE stores agent-authored procedural knowledge. Agents write it and earn
trust for it; a human approves only the top tier. Decision record: Issue #40.

## Tiers

| Tier | Vault status | Reached when | Decided by |
| --- | --- | --- | --- |
| T0 | `candidate` | Proposed with at least one source | Agent |
| T1 | `verified` | 2 independent corroborations, or 1 independent `passed` verification; no failures | Policy |
| T2 | `verified`, approval-eligible | 2 independent `success` outcomes; no failures | Policy |
| T3 | `canonical` | Human Approve of a T2 item | Human |

Thresholds live in `TrustPolicyConfig` and are not part of the wire contract.

- **Independence** is counted per distinct `(actor, session)`; the author of the
  current content never counts for its own corroboration, verification, or
  success. `require_distinct_models=True` counts distinct models instead,
  excluding the author's model.
- Verification and lifecycle events bump the record revision but do not reset
  counts; a content change or demotion does.
- **Failures** (failure outcomes, failed/rejected verifications) are counted by
  distinct `(actor, session)`. Any failure blocks advancement (strict for now;
  a success/failure ratio rule is planned once real data exists).
  - Candidate: two failures archive it.
  - Verified: two failures **demote it to candidate** (`DEMOTED`); the recorded
    verification is cleared and a fresh trial starts. If the same content fails
    a second trial, it is archived.
  - Canonical: one failure emits `REVALIDATION_REQUESTED` (still served); two
    deprecate it.
- **Counting window** for each trial starts at the latest content change or
  demotion, whichever is later.
- The reserved actors `policy` and `operator` are never counted as independent.
- The policy (`kasane.trust.policy.evaluate`) is a pure function of status and
  events. It never sets `canonical`.

## Contracts

- `schemas/knowledge-proposal.schema.json` — what an agent submits. `knowledge_type`
  is one of `procedure`, `design`, `guidance`, `pitfall`, `constraint`. Body is
  capped at 4,000 characters / 12,000 UTF-8 bytes and grows only if operation
  shows the cap is too tight. Situation conditions are not accepted until #42;
  agents state the situation in the body.
- `schemas/knowledge-signal.schema.json` — `corroboration`, `verification`
  (candidates only), or `outcome`.

Agent identity (`actor`, `session`, `model`) comes from server configuration,
never from these payloads. It is declared, not authenticated: suitable for a
single-owner self-hosted Vault, not a security boundary.

## Duplicates

A proposal whose lexical similarity (Jaccard over words and CJK bigrams of
title, tags and body prefix) to any live knowledge is ≥ 0.5 is refused with the
matching refs, so the agent corroborates instead. Retired (deprecated/archived)
knowledge never blocks, and only knowledge inside the writer's scopes is
compared or disclosed. The check is advisory, not transactional: two concurrent
near-duplicate proposals can both be created. The threshold is tentative: paraphrases measured 0.58
(Japanese) and 0.78 (English); same-topic but different advice measured 0.27.

## Known limits

- Two sessions of the same model can make correlated errors; enable
  `require_distinct_models` once more than one model writes to the Vault.
- Stored text becomes context for future agents. Size bounds, required sources,
  write-scope allowlists, and (with #41) trust labels are the mitigations.
- No Refine/send-back action in this version.
