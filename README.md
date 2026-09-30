# linear-cli

A small CLI adapter for Linear. Point it at a token; let an agent discover
what it can do from the tool and Linear's published GraphQL schema.

Not an SDK. Not a workflow engine. Good enough is good enough.

## Status

Scaffold only. Package folder exists; the client is not implemented yet.

## Setup

```bash
uv sync --group dev
```

## Checks

```bash
uv run ruff check .
uv run black --check .
uv run pytest
```

## Auth (planned)

Set `LINEAR_API_KEY` to a Linear personal API key, or pass an OAuth access
token as a Bearer token. See [Linear's GraphQL docs](https://linear.app/developers/graphql).

## Agent instructions

Shared policy is in [`AGENTS.md`](AGENTS.md). Claude Code loads it via
`CLAUDE.md`. Cursor has a thin rule that points at the same file. Codex reads
`AGENTS.md` natively.
