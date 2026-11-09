# MCP server

`kasane-mcp` exposes Level 0 retrieval to agents over MCP (stdio). It has one
tool:

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

Write tools (proposing candidates, verification, promotion), Level 1/2,
HTTP transport, and authentication. Scope selectors narrow retrieval but never
grant access, so do not expose this server to untrusted callers.
