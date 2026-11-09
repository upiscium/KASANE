"""``kasane-mcp`` command line: ``serve`` (stdio MCP) and ``seed-fixture`` (dev)."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from kasane.mcp_server import WriteAccess, build_server
from kasane.retrieval.request import parse_retrieval_request
from kasane.retrieval.service import Level0RetrievalService
from kasane.trust import AgentContext, TrustPolicyConfig, TrustService
from kasane.vault.repository import VaultRepository

VAULT_ENV = "KASANE_VAULT_PATH"
INDEX_ENV = "KASANE_INDEX_PATH"
ACTOR_ENV = "KASANE_ACTOR"
MODEL_ENV = "KASANE_MODEL"
SESSION_ENV = "KASANE_SESSION"
SCOPES_ENV = "KASANE_WRITE_SCOPES"
DEFAULT_SCOPES_ENV = "KASANE_DEFAULT_SCOPES"
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[3] / "evaluation/level0/knowledge-fixture.json"
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kasane-mcp", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the MCP server over stdio")
    serve.add_argument("--vault", default=os.environ.get(VAULT_ENV))
    serve.add_argument("--index", default=os.environ.get(INDEX_ENV))
    serve.add_argument(
        "--default-scope",
        action="append",
        dest="default_scopes",
        help="scope used when a retrieve request omits it (repeatable), "
        "e.g. global and project/kasane",
    )
    serve.add_argument(
        "--allow-write",
        action="store_true",
        help="expose propose/corroborate/verify/report_outcome (off by default)",
    )
    serve.add_argument("--actor", default=os.environ.get(ACTOR_ENV))
    serve.add_argument("--model", default=os.environ.get(MODEL_ENV))
    serve.add_argument(
        "--session",
        default=os.environ.get(SESSION_ENV),
        help="independence key; defaults to a fresh id per server process",
    )
    serve.add_argument(
        "--write-scope",
        action="append",
        dest="write_scopes",
        help="allowed namespace for writes (repeatable), e.g. project/kasane",
    )

    seed = commands.add_parser(
        "seed-fixture", help="create a NEW dev Vault from the Level 0 fixture"
    )
    seed.add_argument("--vault", required=True)
    seed.add_argument("--fixture", default=str(DEFAULT_FIXTURE))

    args = parser.parse_args(argv)
    if args.command == "seed-fixture":
        from kasane.evaluation.level0 import seed_fixture_vault

        try:
            count = seed_fixture_vault(args.fixture, args.vault)
        except FileExistsError as exc:
            parser.exit(2, f"kasane-mcp: {exc}\n")
        print(f"seeded {args.vault} with {count} canonical records", file=sys.stderr)
        return 0

    vault = _require_vault(parser, args.vault)
    repository = VaultRepository(vault)
    # One policy config for retrieval labels and intake, so T2 means the
    # same thing in capsules and in the approval queue.
    policy = TrustPolicyConfig()
    service = Level0RetrievalService(repository, args.index, trust_policy=policy)
    service.rebuild_index()
    write = (
        _write_access(parser, args, repository, policy) if args.allow_write else None
    )
    default_scope = _default_scope(parser, args.default_scopes)
    server = build_server(service, write=write, default_scope=default_scope)

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    anyio.run(run)
    return 0


def _default_scope(
    parser: argparse.ArgumentParser, values: list[str] | None
) -> list[str] | None:
    if values is None and (env := os.environ.get(DEFAULT_SCOPES_ENV)) is not None:
        values = [value.strip() for value in env.split(",") if value.strip()]
        if not values:
            parser.exit(2, f"kasane-mcp: {DEFAULT_SCOPES_ENV} is set but empty\n")
    if not values:
        return None
    probe = {
        "schema_version": "0.1",
        "query": "probe",
        "mode": "fast",
        "scope": values,
        "budget": {"max_tokens": 1, "max_bytes": 1, "max_evidence_items": 1},
    }
    try:
        parse_retrieval_request(probe)  # fail at startup, not on every call
    except ValueError as exc:
        parser.exit(2, f"kasane-mcp: invalid --default-scope: {exc}\n")
    return values


def _write_access(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    repository: VaultRepository,
    policy: TrustPolicyConfig,
) -> WriteAccess:
    if not args.actor or not args.model:
        parser.exit(
            2,
            f"kasane-mcp: --allow-write needs --actor/{ACTOR_ENV} and "
            f"--model/{MODEL_ENV}: trust is counted per writer identity\n",
        )
    try:
        agent = AgentContext(
            actor=args.actor,
            session=args.session or f"proc-{uuid.uuid4().hex}",
            model=args.model,
        )
    except ValueError as exc:
        parser.exit(2, f"kasane-mcp: {exc}\n")
    scopes = args.write_scopes
    env_scopes = os.environ.get(SCOPES_ENV)
    if scopes is None and env_scopes is not None:
        scopes = [scope.strip() for scope in env_scopes.split(",") if scope.strip()]
        if not scopes:
            # Ambiguous: an empty value must not silently mean "everywhere".
            parser.exit(
                2,
                f"kasane-mcp: {SCOPES_ENV} is set but empty; unset it to allow "
                "all namespaces or list at least one scope\n",
            )
    return WriteAccess(TrustService(repository, policy, write_scopes=scopes), agent)


def _require_vault(parser: argparse.ArgumentParser, value: str | None) -> Path:
    if not value:
        parser.exit(2, f"kasane-mcp: set --vault or {VAULT_ENV}\n")
    path = Path(value).expanduser()
    if not path.is_file():
        # VaultRepository would silently create an empty Vault here.
        parser.exit(
            2,
            f"kasane-mcp: Vault DB not found: {path} "
            "(create one, or run `kasane-mcp seed-fixture` for a dev Vault)\n",
        )
    return path


if __name__ == "__main__":
    raise SystemExit(main())
