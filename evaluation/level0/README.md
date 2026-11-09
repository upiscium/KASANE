# Level 0 evaluation baseline

This directory freezes a reproducible measurement of the Issue #6 Level 0 implementation. It is an evaluation artifact, not a production retrieval tuning surface.

## Dataset structure

- `knowledge-fixture.json`: 28 synthetic/public Knowledge records, including canonical targets, lexical distractors, scope lookalikes, and intentionally stale/noncanonical/conditional records.
- `golden-queries.json`: 16 schema-valid requests covering 16 query classes. Every entry declares `query_id`, `query_class`, `request`, `expected_relevant_refs`, `expected_no_match`, and notes; optional irrelevant/ineligible refs support failure analysis.
- `budget-profiles.json`: explicit small, normal, and large profiles over the identical query corpus.
- `baseline.json`: canonical deterministic report, including fixture/query/profile and production-implementation hashes.
  The snapshot test compares the **whole** report, including `implementation_sha256`:
  any change to the hashed retrieval files (`budget.py`, `eligibility.py`, `index.py`,
  `models.py`, `request.py`, `service.py`, plus `trust/policy.py`) must regenerate this file in the same change,
  and reviewers check that only intended fields moved (#37).
- The reference run is **canonical-only** (`min_trust: "T3"` is applied by the evaluator);
  lower trust tiers are covered by unit tests (#41).

The fixture contains no runtime Vault content, private user data, or secrets.

## Reproduction

```text
uv run python -m kasane.evaluation.level0
uv run pytest tests/test_level0_evaluation.py -v
```

The runner uses fresh temporary canonical/index databases per profile, a fixed evaluation clock, and `test:unicode-codepoint-v1` where one Unicode code point equals one exact token. The canonical report excludes wall-clock latency. Production retrieval still exposes an observational latency surface, but no latency value or threshold affects this baseline.

## Metric definitions

Recall, precision, and MRR are macro-averaged over the 13 queries with non-empty expected relevant sets. No-match accuracy compares the observed `insufficient_evidence` terminal result with `expected_no_match` across all 16 queries, so false no-match responses for expected matches are counted as errors.

- **Recall@K:** fraction of each query's expected relevant refs emitted in the first K selected refs.
- **Precision@K:** relevant refs in the first K divided by K; returning fewer than K therefore reflects the profile's output constraint.
- **MRR:** reciprocal rank of the first relevant selected ref, or zero when none is emitted.
- **Selected count / Capsule bytes:** caller-visible output burden, averaged over all queries.
- **Exact tokens:** budget report usage under the deterministic code-point tokenizer.

Failure lists are evaluation-only observations:

- `candidate_generation`: expected relevant ref never entered the lexical candidate set.
- `ranking_outside_top3`: eligible lexical candidate ranked below the evaluation top-3 target.
- `budget_or_evidence_cap`: relevant candidate ranked within top 3 but was not emitted.
- `eligibility_gate`: expected ineligible lexical candidate was intentionally removed by scope, lifecycle, freshness, or applicability policy.

## Budget profiles and numeric baseline

| Metric | small | normal | large |
| --- | ---: | ---: | ---: |
| max tokens / evidence | 900 / 1 | 1800 / 3 | 4000 / 6 |
| Recall@1 | 0.730769 | 0.730769 | 0.730769 |
| Recall@3 | 0.730769 | 0.769231 | 0.769231 |
| Precision@1 | 0.769231 | 0.769231 | 0.769231 |
| Precision@3 | 0.256410 | 0.282051 | 0.282051 |
| MRR | 0.769231 | 0.769231 | 0.788462 |
| No-match accuracy | 0.875000 | 0.875000 | 0.875000 |
| Mean selected count | 0.687500 | 1.187500 | 1.312500 |
| Mean Capsule bytes/tokens | 625.500000 | 759.125000 | 839.625000 |
| Max exact tokens | 830 | 1521 | 2664 |

The small profile records one budget/evidence-cap failure for the multiple-relevant query. Normal and large remove that failure. Increasing the budget also increases selected distractors and caller-visible size; it does not repair lexical candidate misses or top-3 ranking quality.

## Interpretation

### Strong classes

Exact title, exact tag, technical identifier, body lexical, multi-term, common-noise, distractor-heavy, expanded-form, and project/global scope queries all place their expected target first in the normal profile. Near-lookalike, true no-match, and stale/noncanonical/conditional cases correctly return no eligible evidence.

### Weak classes

- **Synonym/paraphrase:** `q08` never generates the effect-time validation candidate.
- **Abbreviation:** `q09` does not connect `TLS` with the expanded “Transport Layer Security” record.
- **Ranking-sensitive:** `q15` generates the relevant live-deployment ref, but three candidates with the same primary marker and additional lexical overlap rank above it. The query's “real systems” intent is semantically aligned with the target's “live customer-facing deployments” context and opposed to the distractors' toy, benchmark, and classroom contexts.
- **Multiple relevant refs:** `q11` has only 0.5 Recall@1 and loses one relevant ref under the small evidence cap.

The 0.875 no-match accuracy is driven by the synonym and abbreviation misses being reported as no evidence even though the golden set expects a relevant result.

### Failure separation

At the normal profile, the report records:

- 2 candidate-generation misses (`q08`, `q09`)
- 1 ranking-outside-top-3 miss (`q15`)
- 0 budget-induced misses (small has 1)
- 6 intentionally gated lexical refs across `q07` and `q14`

Eligibility exclusions are expected policy behavior, not Level 1 recall defects.

## Level 1 recommendation

**C. hybrid** is the measured recommendation.

Candidate expansion is needed for the synonym and abbreviation classes because no reranker can recover refs absent from the lexical candidate set. Reranking is independently needed for the ranking-sensitive case where the relevant ref is already present but below top 3 despite observable semantic alignment between the query's real-system intent and the target's live customer-facing deployment context. A candidate-expansion-only Level 1 would leave ordering weakness unchanged, while reranking-only would leave two unsupported semantic queries unchanged.

This recommendation records the current weakness; it does not change Level 0 ranking, tokenization, FTS construction, eligibility, or Capsule assembly.
