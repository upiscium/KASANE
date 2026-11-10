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

Every returned knowledge ref carries a trust tier (T0 candidate … T3 canonical);
requests choose the floor with `min_trust` (default `T1`). Deprecated and
archived knowledge is never returned. Retrieval never writes canonical Vault
state; it only maintains the disposable derived lexical index.

The contract's default `scope` is `["global"]`, so project knowledge is invisible
unless a request names it. `serve --default-scope global --default-scope
project/kasane` (or `KASANE_DEFAULT_SCOPES=global,project/kasane`) sets the scope
used when a request omits it; an explicit request scope always wins. Invalid
values fail at startup.

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

**Independence and sessions:** without `--session`, every server process is a
new session, so an agent that restarts its MCP server counts as a *different*
independent participant and can corroborate its own earlier proposal. This is
the accepted weak-independence risk of #40; pin `--session` per agent, and
enable `require_distinct_models` once several models write.

**Scopes:** `--write-scope` also bounds duplicate checks and `similar` hints, so
a scoped writer never sees or is blocked by knowledge outside its scopes.
Omitting it allows writes to every namespace, including `private/*`.

**Emergency stop:** `kasane-admin freeze` refuses all agent writes;
`block`/`quarantine` handle a single writer (docs/trust.md).

**Discovery:** verified knowledge (T1/T2) is returned by default, so agents can
report outcomes on it. Candidates (T0) appear only with `min_trust: "T0"`; their
main route to corroboration is the near-duplicate refusal of `kasane_propose`.

## Running

```sh
# Real use: empty Vault (see "Getting started" below)
uv run kasane-admin init

# Development only: Vault seeded with synthetic fixture knowledge
uv run kasane-mcp seed-fixture --vault ~/.local/share/kasane/dev.db

# Serve over stdio
uv run kasane-mcp serve --vault ~/.local/share/kasane/dev.db
```

`--vault` / `KASANE_VAULT_PATH` is required, and the file must already exist:
the server fails at startup rather than silently creating an empty Vault.
`--index` / `KASANE_INDEX_PATH` optionally places the derived index (default:
next to the Vault). `seed-fixture` defaults to the in-repo fixture; pass
`--fixture` when running from an installed wheel.

## Getting started (real use)

1. **Create the Vault** (empty; safe to re-run):

   ```sh
   uv run --directory /path/to/KASANE kasane-admin init
   # -> vault: $KASANE_DATA_ROOT/vault.db, else $XDG_STATE_HOME/kasane/vault.db,
   #    else ~/.local/state/kasane/vault.db
   ```

   `kasane-admin` commands use the same default, so no variable is needed for
   them. Re-running `init` on an older Vault migrates it in place and reports
   `migrated_from`; copy the file first if you want a backup.

2. **Register the server** with your agent client. Set `KASANE_ACTOR` to a
   stable name per client and `KASANE_MODEL` to the model it runs; leave
   `KASANE_SESSION` unset so each client process is its own session.

   OpenCode (`opencode.json`; shape from the
   [OpenCode MCP docs](https://opencode.ai/docs/mcp-servers), which use
   `mcp.servers`, `environment` and `disabled`; check against your version):

   ```json
   {
     "mcp": {
       "servers": {
         "kasane": {
           "type": "local",
           "command": [
             "uv", "run", "--directory", "/path/to/KASANE", "kasane-mcp", "serve",
             "--allow-write",
             "--default-scope", "global", "--default-scope", "project/<project>"
           ],
           "environment": {
             "KASANE_VAULT_PATH": "/home/<you>/.local/state/kasane/vault.db",
             "KASANE_ACTOR": "opencode",
             "KASANE_MODEL": "<model-id>"
           }
         }
       }
     }
   }
   ```

   Claude Code: use `add-json`. The `claude mcp add -e KEY=VAL name -- cmd`
   form shown by `--help` mis-parses `-e` in some versions
   ([anthropics/claude-code#62332](https://github.com/anthropics/claude-code/issues/62332)).

   ```sh
   claude mcp add-json -s user kasane '{
     "type": "stdio",
     "command": "uv",
     "args": ["run", "--directory", "/path/to/KASANE", "kasane-mcp", "serve",
              "--allow-write", "--default-scope", "global",
              "--default-scope", "project/<project>"],
     "env": {
       "KASANE_VAULT_PATH": "/home/<you>/.local/state/kasane/vault.db",
       "KASANE_ACTOR": "claude-code",
       "KASANE_MODEL": "<model-id>"
     }
   }'
   ```

   Registering two different clients (or models) gives the trust model the
   independent writers it needs; one client alone can still reach T1/T2 via
   separate sessions, which is the documented weak-independence risk.

3. **Tell the agent how to use it.** Agents rarely call new tools unprompted.
   Add to the project's `AGENTS.md` / `CLAUDE.md`:

   ```markdown
   ## KASANE knowledge
   - Before designing or debugging, call `kasane_retrieve` with
     `{"schema_version": "0.1", "mode": "fast", "query": "<the task>",
     "budget": {"max_tokens": 2000, "max_bytes": 8000, "max_evidence_items": 5}}`.
     Treat T0–T2 items as hints to check, T3 as approved.
   - After applying a retrieved item, call `kasane_report_outcome` with
     success or failure and one line on the task. Report failures too.
   - When you establish a reusable procedure, design rule, situational fix,
     pitfall, or constraint, call `kasane_propose` with
     `vault://project/<project>/<kind>/<slug>`, at least one source, and the
     situation it applies to in the body. If it is refused as
     `near_duplicate`, corroborate the match only if you derived it yourself.
   - Never store secrets, credentials, or personal data.
   ```

4. **Watch it** with `kasane-admin` (uses the same default Vault as `init`;
   pass `--vault` if your clients point elsewhere):

   ```sh
   alias kasane-admin='uv run --directory /path/to/KASANE kasane-admin'
   kasane-admin status          # counts, gates, queue size
   kasane-admin events --limit 50
   kasane-admin queue           # T2 items waiting for your approve/reject
   ```

   Emergency: `kasane-admin freeze --reason "..."` (see docs/trust.md).

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

Level 1/2 over MCP, HTTP transport, and authentication. Scope selectors narrow retrieval but never
grant access, so do not expose this server to untrusted callers.
