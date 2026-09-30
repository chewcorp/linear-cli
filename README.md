# linear-cli

A small CLI adapter for Linear. Point it at a token; let an agent discover
what it can do from the tool and Linear's published GraphQL schema.

Not an SDK. Not a workflow engine. Good enough is good enough.

## Status

The CLI authenticates, fetches the live schema, and executes GraphQL.

```bash
linear-cli --help
linear-cli schema
linear-cli graphql DOCUMENT
linear-cli graphql --variables JSON DOCUMENT
```

`DOCUMENT` is a GraphQL document. Use `-` to read that document from stdin.
When you omit `--variables`, the CLI sends `{}`.

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

## Auth

Set exactly one of these variables.

- `LINEAR_API_KEY` sends the personal API key as `Authorization: <key>`.
- `LINEAR_OAUTH_TOKEN` sends the OAuth access token as `Authorization: Bearer <token>`.

A blank value, a value that contains whitespace, or a value that already
starts with `Bearer ` in any capitalization is rejected. Leaving both unset,
or setting both, is an error. The CLI does not send a request in those cases.

See the [Linear GraphQL API](https://linear.app/developers/graphql).

## Agent instructions

Shared policy is in [`AGENTS.md`](AGENTS.md). Claude Code loads it via
`CLAUDE.md`. Cursor has a thin rule that points at the same file. Codex reads
`AGENTS.md` natively.
