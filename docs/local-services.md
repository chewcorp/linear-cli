# Local services

Linear is a hosted service, not a database you can run in Docker. This CLI has
no Postgres, Redis, or other local infrastructure dependencies. Use the small
GraphQL sandbox below for offline API development and QA. It never contacts
Linear, requires no account, and changes only synthetic in-memory data.

## Start the API

From the repository root, with Python 3.11+ and `uv` installed:

```bash
uv sync --frozen --group dev
uv run python tests/mock_linear.py
```

After the initial dependency install, the sandbox works offline. Wait for
`Local Linear API: http://127.0.0.1:8765/graphql`.
It binds only to loopback. If the port is busy, add `--port 8766` and change
the client URL below to match.

## Point the CLI at it

In a second terminal at the repository root, open a disposable shell so local
credentials and the endpoint do not replace your normal configuration:

```bash
bash
unset LINEAR_API_KEY LINEAR_OAUTH_TOKEN
export LINEAR_API_KEY=local-dev-key
export LINEAR_GRAPHQL_URL=http://127.0.0.1:8765/graphql
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"

uv run linear-cli graphql '{ viewer { id name displayName } }'
uv run linear-cli schema
uv run linear-cli issue get LOCAL-1
uv run linear-cli issue move LOCAL-1 --state "In Review"
printf 'Local QA comment\n' | uv run linear-cli comment LOCAL-1 --body-file -
uv run linear-cli graphql \
  '{ issue(id: "LOCAL-1") { state { name } comments { nodes { body } } } }'
```

The viewer is `Local Developer`. The issue starts in `Todo`; the move prints
`Todo → In Review`. The last query shows the new state and comment. The comment
URL is synthetic, not a functioning Linear page.

For OAuth header testing, run `unset LINEAR_API_KEY` and
`export LINEAR_OAUTH_TOKEN=local-dev-token`. The server accepts only these two
dummy credentials, not real Linear tokens. Invalid credentials return HTTP 401.
Unknown issues and invalid GraphQL fields produce normal CLI/GraphQL errors.

Run `exit` to leave the disposable client shell. Stop the server with Ctrl-C.
Restarting it resets the issue and removes comments; no files or containers
need cleanup. Normal CLI invocations still default to
`https://api.linear.app/graphql`. `LINEAR_GRAPHQL_URL` accepts that endpoint or
HTTP(S) URLs on `localhost`, `127.0.0.1`, or `::1`, and rejects other hosts,
embedded credentials, and fragments before making a request. Keep dummy
credentials scoped to the local endpoint.

## Scope and automated checks

This is a deliberately small test schema, not a copy of Linear's published
schema. It supports introspection, `viewer`, `issue get` (without
`--comments-since`), `comment`, `issue move`, and GraphQL queries using the
fields declared in `tests/mock_linear.py`. There is one issue, `LOCAL-1`
(internal ID `local-issue`), and three states: `Todo`, `In Review`, and `Done`.
Mutations persist until the server restarts.

`issues changed`, `issues ready`, pagination, comment time filters, and other
Linear APIs are not implemented. Unsupported fields fail GraphQL validation
rather than returning a canned success. To exercise another API locally, extend
the test schema and resolver data in `tests/mock_linear.py` and add an
integration test. Use the hosted API with a dedicated test workspace when
checking compatibility with Linear's full live schema or permissions.

```bash
uv run pytest tests/test_local_services.py
uv run ruff check .
uv run black --check .
uv run pytest
```

The integration tests start their own sandbox on an available loopback port,
invoke the CLI as a subprocess over real HTTP, and shut the server down. They
need no running sandbox, Linear credentials, or hosted API access, and run in
the existing CI test job.
