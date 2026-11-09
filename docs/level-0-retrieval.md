# Level 0 deterministic retrieval

Level 0 is the always-available retrieval path. It uses no LLM, embedding, vector database, reranker, semantic query rewriting, or Librarian process:

```text
Retrieval Request
  -> logical scope filter
  -> trust-tier filter (min_trust)
  -> fail-closed freshness filter
  -> safe lexical search
  -> deterministic ranking
  -> bounded evidence selection
  -> Context Capsule
```

The existing schemas remain the wire-contract SSOT. Python dataclasses represent internal service results and diagnostics only; the caller-facing Capsule and protocol error are ordinary JSON objects with exactly the schema fields.

## Eligibility

Each request sets `min_trust` (default `T1`; see [`trust.md`](trust.md)). Records below it are excluded before ranking: `T3` admits only `canonical`; `T2` adds verified records that are approval-eligible (computed from the event log); `T1` adds all verified records; `T0` adds candidates. Deprecated and archived records are never returned at any tier. A passed verification outcome alone does not raise a candidate's tier; only its lifecycle status and recorded signals do.

Every returned knowledge ref carries a compact `trust` label (`{"tier": "T1"}`, plus `"revalidation": true` for canonical knowledge served pending revalidation), and the capsule `context` names each item's tier. Trust breaks ties only: ranking keys are, in order, exact title, exact tag, exact topic, lexical score, **trust**, scope specificity, ref. A weakly matching canonical record never outranks a strongly matching verified one.

The reference evaluation (`evaluation/level0`) runs canonical-only (`min_trust: "T3"`).

Scope selectors match the `vault://` URI with the scheme removed. A selector matches either the complete logical path or a prefix ending at a `/` segment boundary. Omitted scope is `global`. Scope narrows retrieval and does not grant authorization.

`stale_after = null` remains eligible on freshness grounds. A timezone-aware timestamp at or before the injected clock is stale. An absent timezone, invalid timestamp, or otherwise unparseable value is excluded without aborting retrieval and increments malformed-freshness diagnostics.

Vault Core does not define machine-evaluable `applies_when` or `counterconditions` predicates. Level 0 therefore does not infer their meaning. A canonical record with either field set to anything other than the defined empty values (`null`, `{}`, or `[]`) is fail-closed excluded and counted by internal applicability diagnostics. It is never returned as if it were unconditional, and arbitrary JSON is never converted into a guessed predicate or generated claim.

## Derived lexical index

`DerivedLexicalIndex` is a separate SQLite database containing an FTS5 table and one derived-state watermark. It is not a Vault migration, source of truth, or dependency of any Vault Core mutation. There are no canonical-table triggers that reference it.

Retrieval computes a deterministic fingerprint from canonical record identities, revisions, and statuses. A missing schema, changed fingerprint, deleted index, or unreadable index file causes the retrieval side to rebuild from `VaultRepository.list_knowledge()`. `rebuild_index()` provides the explicit rebuild operation. Deleting the index cannot delete Knowledge and cannot prevent create, update, verification, lifecycle, or event transactions in the canonical database.

Caller query text is Unicode-tokenized into alphanumeric terms. Retrieval constructs a quoted FTS expression from those terms instead of passing caller text through as FTS query language. Quotes, punctuation, whitespace, and operator-like words therefore cannot inject operators or cause FTS syntax errors.

FTS5 `bm25` supplies lexical relevance with internal field weights. Exact normalized title and tag matches, exact requested-topic matches, and scope specificity participate in the internal sort key. Final ties use `knowledge_ref`. The formula is deliberately not part of the public wire contract, while ordering remains deterministic for the same canonical state, request, and implementation configuration.

## Capsule assembly and budgets

Level 0 emits bounded source excerpts, never an unbounded body top-k dump. Each evidence item points to a capsule-local knowledge handle whose entry contains the logical `vault://` URI and Vault revision. Provenance is copied only from an explicit source/handle shape that already fits the schema. Oversize opaque handles are skipped rather than truncated; if no valid source handle remains, the complete logical `vault://` URI is used as the traceable fallback. Level 0 does not invent critical facts, constraints, or pitfalls, so those arrays remain empty unless a future deterministic structured rule is separately specified.

Evidence is considered in deterministic rank order and capped by `max_evidence_items`. On overflow, lower-ranked evidence is removed and the Capsule becomes `degraded` with an explicit `insufficient_evidence` marker. No match returns a failed Capsule with empty synthesized content and explicit insufficient evidence. A matching result with `max_evidence_items = 0` is distinct: it returns a budget-limited failed Capsule that explicitly reports evidence-cap exhaustion and does not claim that retrieval found no match.

Budget accounting serializes the entire caller-visible Capsule except top-level `budget` using sorted keys, compact separators, UTF-8, and unescaped Unicode:

- A resolved injected tokenizer uses `exact_tokenizer` / `exact_tokens` and enforces `max_tokens`.
- An absent or unknown tokenizer uses `utf8_bytes` / `hard_bytes` and enforces `max_bytes` without claiming a token guarantee.
- Complete, degraded, and failed Capsules all fit the selected hard boundary.
- If the canonical minimum failed Capsule cannot fit, retrieval returns the schema-defined `budget_too_small_for_capsule` protocol error instead of a Capsule.

## Diagnostics and baseline

`RetrievalResult` keeps diagnostics outside the strict Capsule: candidate and selected counts, scope/lifecycle/applicability/stale exclusions, malformed freshness, index rebuild state and watermark, serialized Capsule bytes, and elapsed milliseconds. Tests use an injected monotonic clock to make the measurement surface reproducible. The fixture baseline verifies one selected result from one candidate, exact serialized-byte accounting, stable ranking before and after rebuild, and measurable latency without introducing a benchmark framework or making production performance claims.
