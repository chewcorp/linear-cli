import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator

import pytest
from mock_linear import create_server

from linear_cli.cli import _stdlib_post


@pytest.fixture
def sandbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with create_server(0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/graphql"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def run_cli(
    url: str,
    *args: str,
    stdin: str = "",
    oauth: bool = False,
    token: str | None = None,
):
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"LINEAR_API_KEY", "LINEAR_OAUTH_TOKEN", "LINEAR_GRAPHQL_URL"}
    }
    env["LINEAR_GRAPHQL_URL"] = url
    env["LINEAR_OAUTH_TOKEN" if oauth else "LINEAR_API_KEY"] = (
        token
        if token is not None
        else ("local-dev-token" if oauth else "local-dev-key")
    )
    return subprocess.run(
        [sys.executable, "-m", "linear_cli", *args],
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_sandbox_schema_and_auth_modes(sandbox: str) -> None:
    result = run_cli(sandbox, "schema")
    assert result.returncode == 0, result.stderr
    schema = json.loads(result.stdout)["data"]["__schema"]
    assert schema["queryType"]["name"] == "Query"
    assert schema["mutationType"]["name"] == "Mutation"
    assert any(t["name"] == "Issue" for t in schema["types"])
    for oauth in (False, True):
        result = run_cli(
            sandbox, "graphql", "{ viewer { id name displayName } }", oauth=oauth
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["data"]["viewer"] == {
            "id": "local-user",
            "name": "Local Developer",
            "displayName": "Local",
        }


def test_sandbox_rejects_wrong_credentials(sandbox: str) -> None:
    for oauth in (False, True):
        result = run_cli(
            sandbox, "graphql", "{ viewer { id } }", oauth=oauth, token="wrong-token"
        )
        assert result.returncode == 1
        assert "Use sandbox credentials" in result.stderr


def test_sandbox_issue_mutations_persist(sandbox: str) -> None:
    result = run_cli(sandbox, "issue", "get", "LOCAL-1")
    assert result.returncode == 0, result.stderr
    issue = json.loads(result.stdout)
    assert issue["identifier"] == "LOCAL-1"
    assert issue["state"] == "Todo"
    assert issue["labels"] == ["sandbox"]

    result = run_cli(sandbox, "issue", "move", "LOCAL-1", "--state", "In Review")
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "Todo → In Review\n",
        "",
    )
    result = run_cli(sandbox, "issue", "get", "LOCAL-1")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["updatedAt"] == "2026-01-01T00:00:01Z"
    result = run_cli(
        sandbox, "comment", "LOCAL-1", "--body-file", "-", stdin="Local QA comment\n"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "https://linear.example/issue/LOCAL-1#comment-1"
    result = run_cli(
        sandbox,
        "graphql",
        "--variables",
        '{"id":"LOCAL-1"}',
        "query($id: String!) { issue(id: $id) { "
        "state { name } updatedAt comments { nodes { body } } } }",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data"]["issue"] == {
        "state": {"name": "In Review"},
        "updatedAt": "2026-01-01T00:00:02Z",
        "comments": {"nodes": [{"body": "Local QA comment\n"}]},
    }


def test_sandbox_errors_do_not_mutate_issue(sandbox: str) -> None:
    for args, diagnostic in [
        (("issue", "get", "LOCAL-404"), "unknown issue LOCAL-404"),
        (
            ("issue", "move", "LOCAL-1", "--state", "Missing"),
            "unknown state 'Missing'",
        ),
        (("graphql", "{ viewer { missingField } }"), "Cannot query field"),
        (
            (
                "graphql",
                (
                    'mutation { issueUpdate(id: "local-issue", '
                    'input: {stateId: "missing"}) { success } }'
                ),
            ),
            "Unknown issue or state",
        ),
    ]:
        result = run_cli(sandbox, *args)
        assert result.returncode == 1
        assert diagnostic in result.stderr
    result = run_cli(sandbox, "issue", "get", "LOCAL-1")
    assert result.returncode == 0, result.stderr
    issue = json.loads(result.stdout)
    assert issue["state"] == "Todo"
    assert issue["updatedAt"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("path", "token", "body", "status"),
    [
        ("/graphql", "wrong-key", b'{"query":"{ viewer { id } }"}', 401),
        ("/missing", "local-dev-key", b"{}", 404),
        ("/graphql", "local-dev-key", b"{", 400),
        ("/graphql", "local-dev-key", b"[]", 400),
        ("/graphql", "local-dev-key", b'{"query": 1}', 400),
        ("/graphql", "local-dev-key", b'{"query":"{viewer{id}}","variables":[]}', 400),
    ],
)
def test_sandbox_rejects_bad_http_requests(
    sandbox: str, path: str, token: str, body: bytes, status: int
) -> None:
    reply = _stdlib_post(
        url=sandbox.removesuffix("/graphql") + path,
        headers={"Content-Type": "application/json", "Authorization": token},
        body=body,
        timeout_seconds=5,
    )
    assert reply.status == status
    assert json.loads(reply.body)["errors"]


@pytest.mark.parametrize(
    ("states_args", "expected"),
    [
        ("", ["Todo", "In Review", "Done"]),
        ("(first: 2)", ["Todo", "In Review"]),
        ("(filter: null)", ["Todo", "In Review", "Done"]),
        ("(filter: {name: null})", ["Todo", "In Review", "Done"]),
        ("(filter: {name: {}})", ["Todo", "In Review", "Done"]),
        ("(filter: {name: {eq: null}})", ["Todo", "In Review", "Done"]),
        ('(filter: {name: {eq: "In Review"}})', ["In Review"]),
    ],
)
def test_sandbox_state_filters(
    sandbox: str, states_args: str, expected: list[str]
) -> None:
    query = (
        "query($id: String!) { issue(id: $id) { team { states"
        + states_args
        + " { nodes { name } } } } }"
    )
    result = run_cli(sandbox, "graphql", "--variables", '{"id":"LOCAL-1"}', query)
    assert result.returncode == 0, result.stderr
    nodes = json.loads(result.stdout)["data"]["issue"]["team"]["states"]["nodes"]
    assert [node["name"] for node in nodes] == expected


def test_sandbox_states_reject_negative_first(sandbox: str) -> None:
    result = run_cli(
        sandbox,
        "graphql",
        "--variables",
        '{"id":"LOCAL-1"}',
        "query($id: String!) { issue(id: $id) "
        "{ team { states(first: -1) { nodes { name } } } } }",
    )
    assert result.returncode == 1
    assert "first must be non-negative" in result.stderr
