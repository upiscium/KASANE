# MCP server

`kasane-mcp` exposes KASANE to agents over MCP (stdio). Read-only by default,
with one tool:

| Tool | Input | Output |
| --- | --- | --- |
| `kasane_retrieve` | Retrieval Request v0.1 ([schema](../schemas/retrieval-request.schema.json)) | Context Capsule v0.1, or a `budget_too_small_for_capsule` protocol error |

The tool input schema is the request schema file itself (minus `$id`), so the
schemas in `schemas/` remain the single source of truth. Tool output is the
exact wire artifact, returned both as structured content and as JSON text.

| Situation | `is_error` | Payload |
| --- | --- | --- |
| Capsule produced (including `insufficient_evidence`) | false | Context Capsule |
| Budget cannot hold any capsule | true | Retrieval error v0.1 |
| Malformed request | true | Plain text (v0.1 defines no wire error for this) |

Only `canonical` knowledge is retrievable. Retrieval never writes canonical
Vault state; it only maintains the disposable derived lexical index.

## Writing knowledge (`--allow-write`)

With `--allow-write`, four intake tools are added (trust rules: `docs/trust.md`):

| Tool | Input | Effect |
| --- | --- | --- |
| `kasane_propose` | Knowledge Proposal v0.1 ([schema](../schemas/knowledge-proposal.schema.json)) | New T0 candidate; result lists related knowledge (score ≥ 0.25). Near-duplicates (≥ 0.5) are refused with `error: near_duplicate` and the matching refs |
| `kasane_corroborate` | `knowledge_ref`, optional `note` | Independent confirmation |
| `kasane_verify` | `knowledge_ref`, `verification` (`passed`/`failed`/`rejected`), `note` | Candidates only; never your own |
| `kasane_report_outcome` | `knowledge_ref`, `outcome` (`success`/`failure`), `note` | After applying knowledge to a real task |

Signal tool inputs are subsets of the [signal contract](../schemas/knowledge-signal.schema.json);
the server adds `schema_version` and `kind` and validates the full signal.
Refusals (contract violations, self-verification, duplicates, scope) are tool
errors (`is_error: true`) and leave the Vault unchanged.

Identity is server configuration, never tool input:

| Flag | Env | Meaning |
| --- | --- | --- |
| `--actor` | `KASANE_ACTOR` | Writer name (required; `policy`/`operator` reserved) |
| `--model` | `KASANE_MODEL` | Model behind the writer (required) |
| `--session` | `KASANE_SESSION` | Independence key; default: fresh id per server process |
| `--write-scope` | `KASANE_WRITE_SCOPES` (comma-separated) | Allowed namespaces, e.g. `project/kasane`; omitted = all |

```sh
uv run kasane-mcp serve --vault ~/.local/state/kasane/vault.db \
  --allow-write --actor opencode --model <model-id> --write-scope project/kasane
```

**Before enabling writes in real use:** there is no emergency stop until
`kasane-admin freeze`/`quarantine` lands, and `kasane_retrieve` still serves only
canonical knowledge, so agents cannot yet discover candidates except through
the `similar` list returned by `kasane_propose` (trust-labeled retrieval: #41).

## Running

```sh
# Development Vault seeded from the Level 0 fixture (refuses existing files)
uv run kasane-mcp seed-fixture --vault ~/.local/share/kasane/dev.db

# Serve over stdio
uv run kasane-mcp serve --vault ~/.local/share/kasane/dev.db
```

`--vault` / `KASANE_VAULT_PATH` is required, and the file must already exist:
the server fails at startup rather than silently creating an empty Vault.
`--index` / `KASANE_INDEX_PATH` optionally places the derived index (default:
next to the Vault). `seed-fixture` defaults to the in-repo fixture; pass
`--fixture` when running from an installed wheel.

## Registering with a client

Adjust paths to your checkout and Vault. These snippets follow each client's
documented config shape but have not yet been exercised against every client
version.

OpenCode (`opencode.json`):

```json
{
  "mcp": {
    "kasane": {
      "type": "local",
      "command": ["uv", "run", "--directory", "/path/to/KASANE", "kasane-mcp", "serve"],
      "environment": { "KASANE_VAULT_PATH": "/path/to/dev.db" }
    }
  }
}
```

Claude Code:

```sh
claude mcp add kasane -e KASANE_VAULT_PATH=/path/to/dev.db \
  -- uv run --directory /path/to/KASANE kasane-mcp serve
```

## Example

Request:

```json
{
  "schema_version": "0.1",
  "query": "atomic write protocol",
  "mode": "fast",
  "budget": { "max_tokens": 1800, "max_bytes": 7200, "max_evidence_items": 3 }
}
```

Against the seeded dev Vault this returns a ~1.2 KB capsule whose
`knowledge_refs` are `vault://global/transactions/atomic-write` and
`vault://global/storage/database-write-lock`.

## Not yet supported

Human approval tooling (`kasane-admin`), Level 1/2, HTTP transport, and
authentication. Scope selectors narrow retrieval but never
grant access, so do not expose this server to untrusted callers.
