# KASANE

KASANE (「重ね」) is a self-hosted, layered canonical knowledge substrate for AI
systems — coding agents, local LLMs, companion AIs, and other consumers.

Instead of returning raw top-k chunks, KASANE returns a **Context Capsule**: the
smallest sufficient task context that fits an explicit token budget, so the
calling model spends as little context and reasoning as possible on knowledge
lookup.

- Architecture: [`docs/architecture.md`](docs/architecture.md)
- Wire contracts (SSOT): [`schemas/`](schemas/)
- Vault Core: [`docs/vault-core.md`](docs/vault-core.md)
- Level 0 retrieval: [`docs/level-0-retrieval.md`](docs/level-0-retrieval.md)
- MCP server (`kasane-mcp`): [`docs/mcp.md`](docs/mcp.md)
- Trust lifecycle (tiers, policy): [`docs/trust.md`](docs/trust.md)

## Package naming

| Item | Name |
| --- | --- |
| Project | KASANE |
| Python import package | `kasane` |
| Distribution | `kasane-knowledge` (PyPI `kasane` is an unrelated project) |

The former `agentknowledgevault` import package was removed without a
compatibility shim: it was never published and had no external consumers.
Schema `$id` values are URNs (`urn:kasane:schema:v0.1:<name>`): identifiers,
not fetchable URLs, so they do not depend on owning any domain.

## Development

Python environment managed by uv and Nix; tasks run through Just.

```sh
just project::check
```
