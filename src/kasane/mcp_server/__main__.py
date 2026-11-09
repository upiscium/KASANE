"""``kasane-mcp`` command line: ``serve`` (stdio MCP) and ``seed-fixture`` (dev)."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import anyio
from mcp.server.stdio import stdio_server

from kasane.mcp_server import build_server
from kasane.retrieval.service import Level0RetrievalService
from kasane.vault.repository import VaultRepository

VAULT_ENV = "KASANE_VAULT_PATH"
INDEX_ENV = "KASANE_INDEX_PATH"
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[3] / "evaluation/level0/knowledge-fixture.json"
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kasane-mcp", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the MCP server over stdio")
    serve.add_argument("--vault", default=os.environ.get(VAULT_ENV))
    serve.add_argument("--index", default=os.environ.get(INDEX_ENV))

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
    service = Level0RetrievalService(VaultRepository(vault), args.index)
    service.rebuild_index()
    server = build_server(service)

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream, server.create_initialization_options()
            )

    anyio.run(run)
    return 0


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
