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
- **Counting window** starts at the latest content event (`CREATED`/`UPDATED`).
  Verification and lifecycle events bump the record revision but do not reset
  counts; a content change does.
- **Failures** (failure outcomes, failed/rejected verifications) are counted by
  distinct `(actor, session)`. Any failure blocks advancement. Two failures
  archive a candidate/verified item. On canonical knowledge, one failure emits
  `REVALIDATION_REQUESTED` and two deprecate it.
- The reserved actors `policy` and `operator` are never counted as independent.
- The policy (`kasane.trust.policy.evaluate`) is a pure function of status and
  events. It never sets `canonical`.

## Contracts

- `schemas/knowledge-proposal.schema.json` — what an agent submits.
- `schemas/knowledge-signal.schema.json` — `corroboration`, `verification`
  (candidates only), or `outcome`.

Agent identity (`actor`, `session`, `model`) comes from server configuration,
never from these payloads. It is declared, not authenticated: suitable for a
single-owner self-hosted Vault, not a security boundary.

## Known limits

- Two sessions of the same model can make correlated errors; enable
  `require_distinct_models` once more than one model writes to the Vault.
- Stored text becomes context for future agents. Size bounds, required sources,
  write-scope allowlists, and (with #41) trust labels are the mitigations.
- No Refine/send-back action in this version.
