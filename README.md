# linear-cli

A small CLI adapter for Linear. Point it at a token; let an agent discover
what it can do from the tool and Linear's published GraphQL schema.

Not an SDK. Not a workflow engine. Good enough is good enough.

## Install

```bash
uv tool install git+https://github.com/chewcorp/linear-cli@v0.1.1
uv tool upgrade linear-cli
```

`pipx install` accepts the same git URL.

Set exactly one of `LINEAR_API_KEY` or `LINEAR_OAUTH_TOKEN`. `LINEAR_API_KEY` is the raw personal API key, never `Bearer`. Hermes cron jobs need `LINEAR_API_KEY` in `env_passthrough`.

## Status

The CLI authenticates, fetches the live schema, and executes GraphQL.

```bash
linear-cli --help
linear-cli schema
linear-cli graphql DOCUMENT
linear-cli graphql --variables JSON DOCUMENT
linear-cli comment ISSUE --body-file PATH
linear-cli issue get ISSUE [--comments-since ISO]
linear-cli issue move ISSUE --state NAME
```

`DOCUMENT` is a GraphQL document. Use `-` to read that document from stdin.
When you omit `--variables`, the CLI sends `{}`.

## Shortcuts

```bash
linear-cli comment CCP-123 --body-file notes.md
printf 'Shipped in #12.\n' | linear-cli comment CCP-123 --body-file -
linear-cli issue get CCP-123
linear-cli issue get CCP-123 --comments-since 2026-10-16T00:00:00Z
linear-cli issue move CCP-123 --state "In Review"
```

`comment` prints the new comment's URL.
`issue get` prints one JSON object.
`--comments-since` adds the comments created at or after that time.
`issue move` matches the state name exactly on the issue's team and prints `Old → New`.
An unknown issue, an unknown state, or a name that matches two states exits 1 and changes nothing.

## Read changes since the last wake

```bash
linear-cli issues changed --team CCP,CPB,BRD --state-file ~/.linear-last-wake
```

The first run writes the current time to that file and prints nothing.
Each later run prints one JSON line per issue updated after the stored time, across every named team.
It then writes the time the run started.
A failed run leaves the file unchanged, so the next run repeats the window.
Pass `--since 2026-10-17T09:00:00Z` instead of `--state-file` when you want one window and no file.
Add `--with-comments` to include comments created in that same window.

`graphql --paginate` follows one connection that declares `$after` and selects `pageInfo { hasNextPage endCursor }`, then prints those pages as one result.

## Setup

```bash
uv sync --group dev
```

For API-dependent development without a Linear account or network access, use
the [local Linear sandbox](docs/local-services.md). It provides a real HTTP
GraphQL endpoint with synthetic, resettable data. No Docker or database is needed.

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
