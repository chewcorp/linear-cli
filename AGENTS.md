# linear-cli

A thin CLI so an agent with a Linear token can do Linear work. The published
GraphQL schema is the source of truth. Good enough is good enough.

## Rules

Karpathy's four, plus a stop condition:

1. **Think before coding.** State assumptions. Surface tradeoffs. Push back when
   a simpler path exists. If something is unclear, name it.
2. **Simplicity first.** Minimum code that solves the problem. Nothing
   speculative. No abstractions for one-use code. No flexibility that was not
   asked for.
3. **Surgical changes.** Touch only what the task requires. Clean up only your
   own mess. No drive-by refactors or comment churn.
4. **Goal-driven execution.** Define a check you can run. Loop until it passes.

**Good enough is good enough.** Stop when the goal is met. Do not gold-plate.

Skip the ceremony on typo-sized work.

## What this is

- A small Python CLI (`uv`, not a framework).
- Dynamic from Linear's published GraphQL API where possible:
  `https://api.linear.app/graphql` (introspection is supported).
  Docs: https://linear.app/developers/graphql
- Self-describing: an agent should be able to discover what it can do from the
  tool itself (`--help`, schema/describe), not from a second handbook.
- Auth is a token. Personal API keys go in `LINEAR_API_KEY` and are sent as
  `Authorization: <key>`. OAuth access tokens go in `LINEAR_OAUTH_TOKEN` and
  are sent as `Authorization: Bearer <token>`. Set exactly one.
- One HTTP client. One schema fetch. Commands derived from the spec, not a
  hand-maintained catalog of Linear.

## What this is not

- Not an SDK, ORM, or typed client generator.
- Not a workflow engine, plugin host, or governance layer.
- Not a copy of Linear's TypeScript SDK.

If a change needs a new layer, it is probably the wrong change.

## Layout

```
AGENTS.md                 # this file — shared policy
CLAUDE.md                 # Claude Code import of AGENTS.md
.cursor/rules/            # Cursor pointer at AGENTS.md
src/linear_cli/           # CLI, credential check, and GraphQL client
tests/                    # pytest
.github/workflows/ci.yml  # ruff, black, pytest
```

Codex reads `AGENTS.md` as-is. Do not fork these rules into a second file.

## Commands

```bash
uv sync --group dev
uv run ruff check .
uv run black --check .
uv run pytest
```

Format with `uv run black .`. Lint with `uv run ruff check .`. Do not enable
Ruff's formatter; Black owns style.

## Implementation notes

- The CLI authenticates, fetches the live schema, and executes one GraphQL document.
- Prefer introspection (or another published spec artifact) over vendoring a
  stale operation list. A cached schema is fine if it can be refreshed.
- Convenience verbs for common issue/project actions are OK if they stay thin
  wrappers over the spec, not a parallel API.
- Fail plainly. Do not invent retry frameworks, config hierarchies, or plugin
  hooks.
- Keep secrets out of the repo. `.env` is gitignored.
