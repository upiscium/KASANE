# KASANE architecture v0.1

KASANE returns the smallest sufficient task context within an explicit budget. It is not an Agent-facing raw top-k chunk server. The primary integration artifact is a **Context Capsule**; a future raw-retrieval surface may exist only for bounded debugging, evaluation, or administration.

## Contract-first boundary

The versioned JSON Schemas in [`schemas/`](../schemas/) are the single source of truth (SSOT) for the v0.1 request, capsule, and bounded protocol-error wire contracts. Documentation explains their semantics but must not redefine fields. There are no Python wire models in v0.1.

```text
Retrieval Request
    -> retrieval policy (mode, levels, scope)
    -> knowledge selection and bounded synthesis
    -> Context Capsule
```

The contracts intentionally do not select a persistence engine, index, embedding model, reranker, LLM, transport, or access-control mechanism.

## Stable public identities

Knowledge uses a logical URI with an explicit namespace and a nested knowledge path:

```text
vault://global/<knowledge-path>
vault://project/<project>/<knowledge-path>
vault://research/<topic>/<knowledge-path>
vault://private/<scope>/<knowledge-path>
```

Examples include `vault://global/agent-development/verify-before-pass` and `vault://project/terreate/buffer-write-contract`. Each path segment is logical and stable; the URI must not expose database row IDs, vector-store IDs, file offsets, or other physical keys. Evidence IDs are capsule-local handles that point to bounded excerpts and provenance; they do not replace the logical knowledge identity.

Retrieval scope selectors use the same namespace paths without the `vault://` prefix, for example `global`, `project/terreate`, `research/execution-integrity`, or `private/team-a`. A selector narrows requested retrieval and never grants access; authorization remains an independent boundary.

## Authority and OKF

- **OKF** is a durable, portable knowledge representation for interchange.
- **Context Capsule** is a task-scoped, derived retrieval artifact.

A capsule is not an OKF document and does not acquire canonical authority merely by being returned. Promotion to canonical knowledge is a separate, future workflow. Conversely, OKF import/export does not define retrieval ranking or capsule budgeting.

## Retrieval levels

Levels describe observable cost/capability classes, not fixed algorithms:

| Level | Contract meaning |
| --- | --- |
| 0 | Deterministic metadata and lexical retrieval. |
| 1 | Higher-cost retrieval assistance such as reranking or embedding may be used. |
| 2 | Agentic Librarian reasoning may be used. |

The returned capsule records the highest level used and the escalation path. Implementations may change algorithms inside a level without changing the public contract.

## Non-goals for v0.1

SQLite/Vault Core persistence, FTS, vector databases, embeddings, rerankers, Librarian implementation, query decomposition, HTTP, ACL, canonical promotion, and full OKF import/export are outside this contract task. A minimal stdio MCP surface was added later; see [`mcp.md`](mcp.md).
