# Level 1 evaluation artifact

This directory is reserved for the independently generated `level1-evaluation-v1`
artifact. The Level 0 fixture and `evaluation/level0/baseline.json` are inputs and
are never updated by Level 1 evaluation.

Generate a report with `generate_level1_report` from
`kasane.evaluation.level1` and write it with `write_level1_report`.

Level 1 uses deterministic limits of 32 semantic candidates, 32 reranker
candidates/results, and a combined limit of exactly 2,048 UTF-8 bytes for each
reranker document (title and body together).
Provider failures are recorded only in internal diagnostics; the Context
Capsule schema is unchanged. Reports should be generated in a temporary
workspace and compared with the committed artifact.
