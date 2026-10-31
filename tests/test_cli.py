import io
import json
import os
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http.client import HTTPMessage, IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Self
from urllib.error import HTTPError, URLError

import pytest

import linear_cli.cli as cli_mod
from linear_cli.cli import (
    OAuthAccessToken,
    PersonalApiKey,
    _HttpPost,
    _HttpReply,
    _stdlib_post,
    main,
)

VIEWER_QUERY = "query { viewer { id } }"
VIEWER_BODY = b'{"query":"query { viewer { id } }","variables":{}}'
VIEWER_STDOUT = """\
{
  "data": {
    "viewer": {
      "id": "u1"
    }
  }
}
"""


def _run(
    argv: list[str],
    *,
    environ: dict[str, str] | None = None,
    stdin: str = "",
    reply: _HttpReply | None = None,
    post: _HttpPost | None = None,
) -> tuple[int, str, str, list[dict[str, object]]]:
    calls: list[dict[str, object]] = []

    def fake_post(
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> _HttpReply:
        calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": body,
                "timeout_seconds": timeout_seconds,
            }
        )
        if reply is None:
            raise AssertionError("unexpected HTTP POST")
        return reply

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = main(
        argv,
        environ={} if environ is None else environ,
        stdin=io.StringIO(stdin),
        stdout=stdout,
        stderr=stderr,
        post=fake_post if post is None else post,
    )
    return code, stdout.getvalue(), stderr.getvalue(), calls


def test_help() -> None:
    code, out, err, calls = _run(["--help"])
    assert code == 0
    assert err == ""
    assert calls == []
    assert (
        out.splitlines()[0]
        == "usage: linear-cli [-h] [--version] {schema,graphql,issues,comment,issue} ..."
    )
    assert "schema" in out
    assert "graphql" in out


def test_version() -> None:
    code, out, err, calls = _run(["--version"])
    assert code == 0
    assert err == ""
    assert calls == []
    assert out == "0.1.1\n"
    assert out.strip() == package_version("linear-cli")


def test_bad_argv_skips_package_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(cli_mod, "package_version", missing)
    code, out, err, calls = _run(["a", "a"])
    assert code == 2
    assert out == ""
    assert calls == []
    assert "PackageNotFoundError" not in err


def test_version_falls_back_to_pyproject(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(cli_mod, "package_version", missing)
    code, out, err, calls = _run(["--version"])
    assert code == 0
    assert err == ""
    assert calls == []
    assert out == "0.1.1\n"


def test_version_unknown_without_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(cli_mod, "package_version", missing)
    monkeypatch.setattr(cli_mod, "_pyproject_version", lambda: None)
    code, out, err, calls = _run(["--version"])
    assert code == 0
    assert err == ""
    assert calls == []
    assert out == "unknown\n"


def test_schema_help() -> None:
    code, out, err, calls = _run(["schema", "--help"])
    assert code == 0
    assert err == ""
    assert calls == []
    assert "schema" in out


def test_graphql_help() -> None:
    code, out, err, calls = _run(["graphql", "--help"])
    assert code == 0
    assert err == ""
    assert calls == []
    assert out.splitlines()[0].startswith("usage: linear-cli graphql [-h] ")
    assert "[--variables JSON]" in out
    assert "[--compact]" in out
    assert "[--data]" in out
    assert "DOCUMENT" in out


def test_missing_credential() -> None:
    code, out, err, calls = _run(["schema"])
    assert code == 2
    assert out == ""
    assert calls == []
    assert "LINEAR_API_KEY" in err
    assert "LINEAR_OAUTH_TOKEN" in err


def test_both_credentials() -> None:
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test", "LINEAR_OAUTH_TOKEN": "tok"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "lin_api_test" not in err
    assert "tok" not in err


def test_personal_key_query() -> None:
    code, out, err, calls = _run(
        ["graphql", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(
            200,
            "OK",
            b'{"data":{"viewer":{"id":"u1"}}}',
        ),
    )
    assert code == 0
    assert err == ""
    assert out == VIEWER_STDOUT
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://api.linear.app/graphql"
    assert call["timeout_seconds"] == 30.0
    headers = call["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "lin_api_test"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
    assert call["body"] == VIEWER_BODY


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8765/graphql",
        "http://localhost:8765/graphql",
        "http://[::1]:8765/graphql",
        "https://localhost/graphql",
    ],
)
def test_local_graphql_endpoint(url: str) -> None:
    code, out, err, calls = _run(
        ["graphql", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "local-dev-key", "LINEAR_GRAPHQL_URL": url},
        reply=_HttpReply(200, "OK", b'{"data":{"viewer":{"id":"u1"}}}'),
    )
    assert (code, out, err) == (0, VIEWER_STDOUT, "")
    assert calls[0]["url"] == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        "https://example.com/graphql",
        "http://api.linear.app/graphql",
        "file:///tmp/graphql",
        "http://127.0.0.1:bad/graphql",
        "http://127.0.0.1:99999/graphql",
        "http://127.0.0.1:0/graphql",
        "http://[::1/graphql",
        "http://local-dev-key@localhost/graphql",
        "http://localhost/graphql#fragment",
        " http://localhost/graphql",
        "http://localhost/\n",
    ],
)
def test_invalid_graphql_endpoint_fails_before_http(url: str) -> None:
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "local-dev-key", "LINEAR_GRAPHQL_URL": url},
    )
    assert code == 2
    assert out == ""
    assert "LINEAR_GRAPHQL_URL must be" in err
    assert "local-dev-key" not in err
    assert calls == []


def test_oauth_token_query() -> None:
    code, out, err, calls = _run(
        ["graphql", VIEWER_QUERY],
        environ={"LINEAR_OAUTH_TOKEN": "tok"},
        reply=_HttpReply(200, "OK", b'{"data":{"viewer":{"id":"u1"}}}'),
    )
    assert code == 0
    assert err == ""
    assert out == VIEWER_STDOUT
    assert len(calls) == 1
    headers = calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer tok"
    assert calls[0]["body"] == VIEWER_BODY


def test_schema_introspection() -> None:
    payload = {"data": {"__schema": {"queryType": {"name": "Query"}}}}
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(200, "OK", json.dumps(payload).encode()),
    )
    assert code == 0
    assert err == ""
    assert out == json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    assert len(calls) == 1
    body = calls[0]["body"]
    assert isinstance(body, bytes)
    decoded = json.loads(body)
    assert decoded["variables"] == {}
    assert "__schema" in decoded["query"]
    assert "FullType" in decoded["query"]
    assert "isRepeatable" in decoded["query"]
    assert "specifiedByURL" in decoded["query"]
    headers = calls[0]["headers"]
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "lin_api_test"


def test_graphql_errors_on_http_200() -> None:
    payload = {
        "data": {"viewer": {"id": "u1"}},
        "errors": [{"message": "one"}, {"message": "two"}],
    }
    code, out, err, calls = _run(
        ["graphql", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(200, "OK", json.dumps(payload).encode()),
    )
    assert code == 1
    assert out == json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    assert err == (
        "linear-cli: GraphQL response contains errors\n"
        "linear-cli: one\n"
        "linear-cli: two\n"
    )
    assert "lin_api_test" not in err
    assert len(calls) == 1


def test_graphql_errors_on_http_400() -> None:
    message = 'Cannot query field "nope" on type "Query".'
    payload = {"errors": [{"message": message}]}
    code, out, err, calls = _run(
        ["graphql", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(400, "Bad Request", json.dumps(payload).encode()),
    )
    assert code == 1
    assert out == json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    assert err == (
        f"linear-cli: GraphQL response contains errors\nlinear-cli: {message}\n"
    )
    assert len(calls) == 1


def test_graphql_compact() -> None:
    code, out, err, calls = _run(
        ["graphql", "--compact", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(
            200,
            "OK",
            b'{ "data" : { "viewer" : { "id" : "u1" } } }',
        ),
    )
    assert code == 0
    assert err == ""
    assert out == '{"data":{"viewer":{"id":"u1"}}}\n'
    assert len(calls) == 1
    assert calls[0]["body"] == VIEWER_BODY


def test_graphql_data() -> None:
    code, out, err, calls = _run(
        ["graphql", "--data", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(200, "OK", b'{"data":{"viewer":{"id":"u1"}}}'),
    )
    assert code == 0
    assert err == ""
    assert out == json.dumps({"viewer": {"id": "u1"}}, indent=2) + "\n"
    assert len(calls) == 1


def test_graphql_compact_and_data() -> None:
    code, out, err, calls = _run(
        ["graphql", "--compact", "--data", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(
            200,
            "OK",
            b'{ "data" : { "viewer" : { "id" : "u1" } } }',
        ),
    )
    assert code == 0
    assert err == ""
    assert out == '{"viewer":{"id":"u1"}}\n'
    assert len(calls) == 1


def test_graphql_data_omits_null_on_errors() -> None:
    message = 'Cannot query field "nope" on type "Query".'
    payload = {"errors": [{"message": message}]}
    code, out, err, calls = _run(
        ["graphql", "--data", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(400, "Bad Request", json.dumps(payload).encode()),
    )
    assert code == 1
    assert out == ""
    assert err == (
        f"linear-cli: GraphQL response contains errors\nlinear-cli: {message}\n"
    )
    assert "null" not in out
    assert len(calls) == 1


def test_graphql_data_keeps_errors() -> None:
    payload = {
        "data": {"viewer": {"id": "u1"}},
        "errors": [{"message": "one"}, {"message": "two"}],
    }
    code, out, err, calls = _run(
        ["graphql", "--data", "--compact", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(200, "OK", json.dumps(payload).encode()),
    )
    assert code == 1
    assert out == '{"viewer":{"id":"u1"}}\n'
    assert err == (
        "linear-cli: GraphQL response contains errors\n"
        "linear-cli: one\n"
        "linear-cli: two\n"
    )
    assert "lin_api_test" not in err
    assert len(calls) == 1


def test_variables_object() -> None:
    code, out, err, calls = _run(
        ["graphql", "--variables", '{"id":"x"}', VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(200, "OK", b'{"data":{"viewer":{"id":"u1"}}}'),
    )
    assert code == 0
    assert err == ""
    assert out == VIEWER_STDOUT
    assert len(calls) == 1
    body = calls[0]["body"]
    assert isinstance(body, bytes)
    assert json.loads(body)["variables"] == {"id": "x"}
    assert json.loads(body)["query"] == VIEWER_QUERY


def test_variables_array_rejected() -> None:
    code, out, err, calls = _run(
        ["graphql", "--variables", "[]", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "JSON object" in err


def test_variables_invalid_json() -> None:
    code, out, err, calls = _run(
        ["graphql", "--variables", "{", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "JSON object" in err
    assert "{" not in err


def test_stdin_document() -> None:
    code, out, err, calls = _run(
        ["graphql", "-"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        stdin="query { viewer { id } }\n",
        reply=_HttpReply(200, "OK", b'{"data":{"viewer":{"id":"u1"}}}'),
    )
    assert code == 0
    assert err == ""
    assert out == VIEWER_STDOUT
    assert len(calls) == 1
    assert calls[0]["body"] == VIEWER_BODY


def test_missing_document() -> None:
    code, out, err, calls = _run(
        ["graphql"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "DOCUMENT" in err


def test_empty_document() -> None:
    code, out, err, calls = _run(
        ["graphql", "   "],
        environ={"LINEAR_API_KEY": "lin_api_test"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert err == "linear-cli: GraphQL document is empty\n"


def test_empty_stdin_document() -> None:
    code, out, err, calls = _run(
        ["graphql", "-"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        stdin=" \n",
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert err == "linear-cli: GraphQL document is empty\n"


def test_bearer_prefix_rejected() -> None:
    code, out, err, calls = _run(
        ["graphql", VIEWER_QUERY],
        environ={"LINEAR_OAUTH_TOKEN": "Bearer super-secret"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "super-secret" not in err


def test_whitespace_credential_rejected() -> None:
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin api"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "lin api" not in err


def test_padded_credential_rejected() -> None:
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "  lin_api_test  "},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "lin_api_test" not in err


def test_blank_companion_credential_rejected() -> None:
    code, out, _err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test", "LINEAR_OAUTH_TOKEN": " "},
    )
    assert code == 2
    assert out == ""
    assert calls == []


def test_http_500_excerpt_redacts_secret() -> None:
    body = b"lin_api_test\n" + (b"word " * 80)
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(500, "Server Error", body),
    )
    assert code == 1
    assert out == ""
    assert len(calls) == 1
    assert "lin_api_test" not in err
    assert err.startswith("linear-cli: HTTP 500 ")
    excerpt = err.removeprefix("linear-cli: HTTP 500 ").removesuffix("\n")
    assert "\n" not in excerpt
    assert len(excerpt) == 200
    assert excerpt.startswith("REDACTED")


def test_http_200_not_json() -> None:
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(200, "OK", b"<html>"),
    )
    assert code == 1
    assert out == ""
    assert err == "linear-cli: response was not a JSON object\n"
    assert len(calls) == 1


def test_credential_repr_omits_secret() -> None:
    assert "lin_api_test" not in repr(PersonalApiKey("lin_api_test"))
    assert "super-secret" not in repr(OAuthAccessToken("super-secret"))


def test_stdlib_post_success(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status = 200
        reason = "OK"

        def read(self) -> bytes:
            return b'{"data":{"viewer":{"id":"u1"}}}'

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    class _Opener:
        def open(
            self, request: urllib.request.Request, timeout: float = 0
        ) -> _Response:
            assert request.full_url == "https://api.linear.app/graphql"
            assert request.get_method() == "POST"
            assert request.get_header("Authorization") == "lin_api_test"
            assert request.get_header("Content-type") == "application/json"
            assert request.get_header("Accept") == "application/json"
            assert timeout == 30.0
            assert isinstance(request.data, bytes)
            assert b"__schema" in request.data
            return _Response()

    monkeypatch.setattr(cli_mod, "_OPENER", _Opener())
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        post=_stdlib_post,
    )
    assert calls == []
    assert code == 0
    assert err == ""
    assert out == VIEWER_STDOUT


def test_stdlib_post_reads_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    message = 'Cannot query field "nope" on type "Query".'
    payload = {"errors": [{"message": message}]}

    class _Opener:
        def open(self, request: urllib.request.Request, timeout: float = 0) -> object:
            assert timeout == 30.0
            raise HTTPError(
                request.full_url,
                400,
                "Bad Request",
                HTTPMessage(),
                io.BytesIO(json.dumps(payload).encode()),
            )

    monkeypatch.setattr(cli_mod, "_OPENER", _Opener())
    code, out, err, _calls = _run(
        ["graphql", VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        post=_stdlib_post,
    )
    assert code == 1
    assert out == json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    assert message in err
    assert "GraphQL response contains errors" in err
    assert "lin_api_test" not in err


def test_stdlib_post_sanitizes_urlerror(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Opener:
        def open(self, request: urllib.request.Request, timeout: float = 0) -> object:
            assert request.get_header("Authorization") == "lin_api_test"
            assert timeout == 30.0
            raise URLError("connection refused")

    monkeypatch.setattr(cli_mod, "_OPENER", _Opener())
    code, out, err, _calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        post=_stdlib_post,
    )
    assert code == 1
    assert out == ""
    assert err == "linear-cli: connection refused\n"
    assert "lin_api_test" not in err


def test_http_503_with_empty_errors_is_failure() -> None:
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        reply=_HttpReply(503, "Service Unavailable", b'{"errors":[],"data":{}}'),
    )
    assert code == 1
    assert out == ""
    assert err.startswith("linear-cli: HTTP 503 ")
    assert len(calls) == 1


def test_overflow_variables_rejected() -> None:
    code, out, err, calls = _run(
        ["graphql", "--variables", '{"n":1e400}', VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "JSON object" in err


def test_nested_overflow_variables_rejected() -> None:
    code, out, err, calls = _run(
        ["graphql", "--variables", '{"n":[1e400]}', VIEWER_QUERY],
        environ={"LINEAR_API_KEY": "lin_api_test"},
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "JSON object" in err


def test_incomplete_read_is_remote_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        status = 200
        reason = "OK"

        def read(self) -> bytes:
            raise IncompleteRead(b"partial", 100)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    class _Opener:
        def open(
            self, request: urllib.request.Request, timeout: float = 0
        ) -> _Response:
            return _Response()

    monkeypatch.setattr(cli_mod, "_OPENER", _Opener())
    code, out, err, calls = _run(
        ["schema"],
        environ={"LINEAR_API_KEY": "lin_api_test"},
        post=_stdlib_post,
    )
    assert calls == []
    assert code == 1
    assert out == ""
    assert err.startswith("linear-cli: ")
    assert "Traceback" not in err
    assert "lin_api_test" not in err


def test_stdlib_post_does_not_follow_redirect() -> None:
    seen: list[tuple[str, str | None]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            seen.append((self.path, self.headers.get("Authorization")))
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            if self.path == "/graphql":
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{self.server.server_address[1]}/stolen",
                )
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"data":{}}')

        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        reply = _stdlib_post(
            url=f"http://127.0.0.1:{port}/graphql",
            headers={
                "Authorization": "lin_api_test",
                "Content-Type": "application/json",
            },
            body=b"{}",
            timeout_seconds=5.0,
        )
        assert reply.status == 302
        assert seen == [("/graphql", "lin_api_test")]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_module_help() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"LINEAR_API_KEY", "LINEAR_OAUTH_TOKEN"}
    }
    proc = subprocess.run(
        [sys.executable, "-m", "linear_cli", "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0
    assert "schema" in proc.stdout
    assert "graphql" in proc.stdout
    assert "issues" in proc.stdout
    assert "comment" in proc.stdout
    assert "{schema,graphql,issues,comment,issue}" in proc.stdout


_KEY = {"LINEAR_API_KEY": "lin_api_test"}
_PAGE_QUERY = (
    "query ($after: String) { issues(first: 2, after: $after) "
    "{ nodes { identifier } pageInfo { hasNextPage endCursor } } }"
)


def _connection(
    nodes: list[dict[str, object]], cursor: str | None, has_next: bool
) -> dict[str, object]:
    return {
        "data": {
            "issues": {
                "nodes": nodes,
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            }
        }
    }


def _serve(
    payloads: list[dict[str, object]],
) -> tuple[_HttpPost, list[dict[str, object]]]:
    sent: list[dict[str, object]] = []
    remaining = list(payloads)

    def post(
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> _HttpReply:
        del url, headers, timeout_seconds
        sent.append(json.loads(body))
        if not remaining:
            raise AssertionError("unexpected HTTP POST")
        return _HttpReply(200, "OK", json.dumps(remaining.pop(0)).encode())

    return post, sent


def _issue(
    identifier: str,
    *,
    team: str = "CCP",
    comments: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    node: dict[str, object] = {
        "identifier": identifier,
        "title": f"Title {identifier}",
        "state": {"name": "Todo"},
        "assignee": None,
        "priority": 0,
        "estimate": None,
        "cycle": None,
        "updatedAt": "2026-10-17T12:00:00.000Z",
        "url": f"https://linear.app/chewcorp/issue/{identifier}",
    }
    if comments is not None:
        node["comments"] = {"nodes": comments}
    node["team"] = team
    return node


def test_paginate_merges_nodes() -> None:
    post, sent = _serve(
        [
            _connection([{"identifier": "CCP-1"}], "c1", True),
            _connection([{"identifier": "CPB-2"}], "c2", False),
        ]
    )
    code, out, err, calls = _run(
        ["graphql", "--paginate", "--data", "--compact", _PAGE_QUERY],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 0
    assert err == ""
    assert json.loads(out) == {
        "issues": {
            "nodes": [{"identifier": "CCP-1"}, {"identifier": "CPB-2"}],
            "pageInfo": {"hasNextPage": False, "endCursor": "c2"},
        }
    }
    assert sent[1]["variables"] == {"after": "c1"}


def test_paginate_rejects_document_without_after() -> None:
    code, out, err, calls = _run(
        ["graphql", "--paginate", "query { viewer { id } }"],
        environ=_KEY,
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "$after" in err


def test_paginate_ignores_connections_inside_nodes() -> None:
    payload = _connection(
        [
            {
                "identifier": "CCP-1",
                "comments": {
                    "nodes": [{"body": "hi"}],
                    "pageInfo": {"hasNextPage": False, "endCursor": "comment-cursor"},
                },
            }
        ],
        None,
        False,
    )
    post, _sent = _serve([payload])
    code, out, err, calls = _run(
        ["graphql", "--paginate", "--data", "--compact", _PAGE_QUERY],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 0
    assert err == ""
    assert json.loads(out)["issues"]["nodes"][0]["identifier"] == "CCP-1"


def test_paginate_rejects_two_connections() -> None:
    payload = _connection([], None, False)
    assert isinstance(payload["data"], dict)
    payload["data"]["projects"] = {
        "nodes": [],
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
    post, _sent = _serve([payload])
    code, out, err, calls = _run(
        ["graphql", "--paginate", _PAGE_QUERY],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 1
    assert out == ""
    assert "exactly one connection, found 2" in err


def test_paginate_stops_when_cursor_repeats() -> None:
    post, sent = _serve(
        [
            _connection([{"identifier": "CCP-1"}], "a", True),
            _connection([{"identifier": "CCP-2"}], "b", True),
            _connection([{"identifier": "CCP-3"}], "a", True),
        ]
    )
    code, out, err, calls = _run(
        ["graphql", "--paginate", _PAGE_QUERY],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 1
    assert out == ""
    assert "cursor did not advance" in err
    assert len(sent) == 3


def test_paginate_does_not_merge_an_error_page() -> None:
    post, _sent = _serve(
        [
            _connection([{"identifier": "CCP-1"}], "c1", True),
            {
                "errors": [{"message": "boom"}],
                "data": _connection([{"identifier": "CCP-2"}], None, False)["data"],
            },
        ]
    )
    code, out, err, calls = _run(
        ["graphql", "--paginate", "--compact", _PAGE_QUERY],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 1
    assert "CCP-1" not in out
    assert "boom" in err


def test_issues_changed_filters_named_teams() -> None:
    nodes = [_issue("CCP-1"), _issue("BRD-2", team="BRD")]
    post, sent = _serve([_connection(nodes, None, False)])
    code, out, err, calls = _run(
        [
            "issues",
            "changed",
            "--team",
            " CCP, CPB, CCP, BRD ",
            "--since",
            "2026-10-17T09:00:00Z",
        ],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 0
    assert err == ""
    lines = [json.loads(line) for line in out.splitlines()]
    assert [line["identifier"] for line in lines] == ["CCP-1", "BRD-2"]
    assert list(lines[0]) == [
        "identifier",
        "title",
        "state",
        "assignee",
        "priority",
        "estimate",
        "cycle",
        "updatedAt",
        "url",
    ]
    assert lines[0]["state"] == "Todo"
    assert lines[0]["assignee"] is None
    assert lines[0]["priority"] == 0
    assert lines[0]["cycle"] is None
    body = sent[0]
    assert body["variables"] == {
        "after": None,
        "since": "2026-10-17T09:00:00.000Z",
        "teams": ["CCP", "CPB", "BRD"],
    }
    query = body["query"]
    assert isinstance(query, str)
    assert "updatedAt: {gt: $since}" in query
    assert "team: {key: {in: $teams}}" in query
    assert "orderBy: updatedAt" in query
    assert "comments(" not in query


def test_issues_changed_state_file_advances_only_after_success(tmp_path: Path) -> None:
    state = tmp_path / "wake"
    state.write_text("2026-10-17T09:00:00.000Z\n")
    post, sent = _serve([_connection([_issue("CCP-1")], None, False)])
    before = datetime.now(UTC)
    code, out, err, calls = _run(
        ["issues", "changed", "--team", "CCP", "--state-file", str(state)],
        environ=_KEY,
        post=post,
    )
    after = datetime.now(UTC)
    assert calls == []
    assert code == 0
    assert err == ""
    assert json.loads(out)["identifier"] == "CCP-1"
    stored = datetime.fromisoformat(state.read_text().strip())
    assert before - timedelta(milliseconds=5) <= stored <= after
    assert sent[0]["variables"]["since"] == "2026-10-17T09:00:00.000Z"


def test_state_file_pages_stop_at_the_run_start(tmp_path: Path) -> None:
    state = tmp_path / "wake"
    state.write_text("2026-10-17T09:00:00.000Z\n")
    post, sent = _serve(
        [
            _connection([_issue("CCP-1")], "c1", True),
            _connection([_issue("CCP-2")], "c2", False),
        ]
    )
    code, out, err, calls = _run(
        ["issues", "changed", "--team", "CCP", "--state-file", str(state)],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 0
    assert err == ""
    assert [json.loads(line)["identifier"] for line in out.splitlines()] == [
        "CCP-1",
        "CCP-2",
    ]
    written = state.read_text().strip()
    assert sent[0]["variables"]["since"] == "2026-10-17T09:00:00.000Z"
    assert sent[0]["variables"]["until"] == written
    assert sent[1]["variables"]["until"] == written
    assert sent[1]["variables"]["after"] == "c1"
    for body in sent:
        query = body["query"]
        assert isinstance(query, str)
        assert "updatedAt: {gt: $since, lte: $until}" in query


def test_issues_changed_leaves_state_file_on_failure(tmp_path: Path) -> None:
    state = tmp_path / "wake"
    original = "2026-10-17T09:00:00.000Z\n"
    state.write_text(original)
    post, sent = _serve(
        [
            _connection([_issue("CCP-1")], "c1", True),
            {"errors": [{"message": "boom"}]},
        ]
    )
    code, out, err, calls = _run(
        ["issues", "changed", "--team", "CCP", "--state-file", str(state)],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 1
    assert out == ""
    assert "boom" in err
    assert state.read_text() == original
    assert sent[1]["variables"]["after"] == "c1"


def test_issues_changed_baselines_a_missing_state_file(tmp_path: Path) -> None:
    state = tmp_path / "wake"
    before = datetime.now(UTC)
    code, out, err, calls = _run(
        ["issues", "changed", "--team", "CCP,CPB,BRD", "--state-file", str(state)],
        environ=_KEY,
    )
    after = datetime.now(UTC)
    assert code == 0
    assert out == ""
    assert err == ""
    assert calls == []
    stored = datetime.fromisoformat(state.read_text().strip())
    assert before - timedelta(milliseconds=5) <= stored <= after


def test_issues_changed_does_not_baseline_without_a_token(tmp_path: Path) -> None:
    state = tmp_path / "wake"
    code, out, err, calls = _run(
        ["issues", "changed", "--team", "CCP", "--state-file", str(state)],
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "LINEAR_API_KEY" in err
    assert not state.exists()


def test_issues_changed_rejects_a_naive_since() -> None:
    code, out, err, calls = _run(
        ["issues", "changed", "--team", "CCP", "--since", "2026-10-17T09:00:00"],
        environ=_KEY,
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert "timezone" in err


def test_issues_changed_rejects_a_bad_state_file(tmp_path: Path) -> None:
    state = tmp_path / "wake"
    state.write_text("\n")
    code, out, err, calls = _run(
        ["issues", "changed", "--team", "CCP", "--state-file", str(state)],
        environ=_KEY,
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert state.read_text() == "\n"
    assert "empty" in err


def _comment_connection(
    nodes: list[dict[str, object]], cursor: str | None, has_next: bool
) -> dict[str, object]:
    return {
        "data": {
            "issue": {
                "comments": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                }
            }
        }
    }


def _reply(payload: dict[str, object]) -> _HttpReply:
    return _HttpReply(200, "OK", json.dumps(payload).encode())


def test_issues_changed_includes_comments_since_the_watermark() -> None:
    sent: list[dict[str, object]] = []

    def post(
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> _HttpReply:
        del url, headers, timeout_seconds
        parsed = json.loads(body)
        sent.append(parsed)
        variables = parsed["variables"]
        assert isinstance(variables, dict)
        if "teams" in variables:
            return _reply(_connection([_issue("CCP-9")], None, False))
        return _reply(
            _comment_connection(
                [
                    {
                        "body": "x" * 300,
                        "createdAt": "2026-10-17T12:30:00.000Z",
                        "user": {"displayName": "Ada"},
                    }
                ],
                None,
                False,
            )
        )

    code, out, err, calls = _run(
        [
            "issues",
            "changed",
            "--team",
            "CCP",
            "--since",
            "2026-10-17T09:00:00+00:00",
            "--with-comments",
        ],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 0
    assert err == ""
    line = json.loads(out)
    assert line["comments"] == [
        {
            "author": "Ada",
            "createdAt": "2026-10-17T12:30:00.000Z",
            "body": "x" * 280,
        }
    ]
    assert sent[1]["variables"]["id"] == "CCP-9"
    assert sent[1]["variables"]["since"] == "2026-10-17T09:00:00.000Z"


def test_with_comments_returns_every_comment_page(tmp_path: Path) -> None:
    state = tmp_path / "wake"
    original = "2026-10-17T09:00:00.000Z\n"
    state.write_text(original)
    sent: list[dict[str, object]] = []

    def post(
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> _HttpReply:
        del url, headers, timeout_seconds
        parsed = json.loads(body)
        sent.append(parsed)
        variables = parsed["variables"]
        assert isinstance(variables, dict)
        if "teams" in variables:
            return _reply(_connection([_issue("CCP-51")], None, False))
        after = variables.get("after")
        if after is None:
            nodes = [
                {
                    "body": f"c{index}",
                    "createdAt": "2026-10-17T12:00:00.000Z",
                    "user": {"displayName": "Ada"},
                }
                for index in range(50)
            ]
            return _reply(_comment_connection(nodes, "c1", True))
        assert after == "c1"
        return _reply(
            _comment_connection(
                [
                    {
                        "body": "c50",
                        "createdAt": "2026-10-17T12:00:00.000Z",
                        "user": {"displayName": "Ada"},
                    }
                ],
                "c2",
                False,
            )
        )

    before = datetime.now(UTC)
    code, out, err, calls = _run(
        [
            "issues",
            "changed",
            "--team",
            "CCP",
            "--state-file",
            str(state),
            "--with-comments",
        ],
        environ=_KEY,
        post=post,
    )
    after = datetime.now(UTC)
    assert calls == []
    assert code == 0
    assert err == ""
    line = json.loads(out)
    assert [comment["body"] for comment in line["comments"]] == [
        f"c{index}" for index in range(51)
    ]
    assert line["updatedAt"] == "2026-10-17T12:00:00.000Z"
    stored = datetime.fromisoformat(state.read_text().strip())
    assert before - timedelta(milliseconds=5) <= stored <= after
    assert state.read_text() != original
    comment_requests = [body for body in sent if "teams" not in body["variables"]]
    assert [body["variables"]["after"] for body in comment_requests] == [None, "c1"]
    assert {body["variables"]["since"] for body in comment_requests} == {
        original.strip()
    }
    assert {body["variables"]["id"] for body in comment_requests} == {"CCP-51"}


def test_comment_page_failure_leaves_the_state_file(tmp_path: Path) -> None:
    state = tmp_path / "wake"
    original = "2026-10-17T09:00:00.000Z\n"
    state.write_text(original)

    def post(
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> _HttpReply:
        del url, headers, timeout_seconds
        parsed = json.loads(body)
        variables = parsed["variables"]
        assert isinstance(variables, dict)
        if "teams" in variables:
            return _reply(_connection([_issue("CCP-51")], None, False))
        if variables.get("after") is None:
            return _reply(
                _comment_connection(
                    [
                        {
                            "body": "c0",
                            "createdAt": "2026-10-17T12:00:00.000Z",
                            "user": {"displayName": "Ada"},
                        }
                    ],
                    "c1",
                    True,
                )
            )
        return _reply({"errors": [{"message": "comment page failed"}]})

    code, out, err, calls = _run(
        [
            "issues",
            "changed",
            "--team",
            "CCP",
            "--state-file",
            str(state),
            "--with-comments",
        ],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code == 1
    assert out == ""
    assert "comment page failed" in err
    assert state.read_text() == original


def _posted(sent: list[dict[str, object]]) -> list[tuple[str, object]]:
    return [
        (str(body["query"]).split()[1].split("(")[0], body["variables"])
        for body in sent
    ]


def _move_lookup(states: list[dict[str, object]]) -> dict[str, object]:
    return {
        "data": {
            "issue": {
                "id": "issue-uuid",
                "state": {"name": "Todo"},
                "team": {"states": {"nodes": states}},
            }
        }
    }


def _shown_issue(**overrides: object) -> dict[str, object]:
    issue: dict[str, object] = {
        "identifier": "CCP-123",
        "title": "Write the shortcuts",
        "description": "markdown or null",
        "state": {"name": "In Review"},
        "assignee": {"name": "ada", "displayName": ""},
        "priority": 2,
        "estimate": 3,
        "cycle": {"name": "", "number": 12},
        "labels": {"nodes": [{"name": "cli"}]},
        "parent": {"identifier": "CCP-100"},
        "url": "https://linear.app/chewcorp/issue/CCP-123",
        "updatedAt": "2026-10-16T12:00:00.000Z",
    }
    issue.update(overrides)
    return {"data": {"issue": issue}}


_COMMENT_URL = "https://linear.app/chewcorp/comment/1"


def _comment_created(url: str | None = _COMMENT_URL, *, success: bool = True) -> dict:
    comment = None if url is None else {"url": url}
    return {"data": {"commentCreate": {"success": success, "comment": comment}}}


def test_comment_posts_file_body_to_the_resolved_issue(tmp_path: Path) -> None:
    body = "  Shipped in #12.\n"
    path = tmp_path / "notes.md"
    path.write_text(body)
    post, sent = _serve(
        [
            {"data": {"issue": {"id": "issue-uuid"}}},
            _comment_created(),
        ]
    )
    code, out, err, calls = _run(
        ["comment", "CCP-123", "--body-file", str(path)],
        environ=_KEY,
        post=post,
    )
    assert (code, out, err) == (0, f"{_COMMENT_URL}\n", "")
    assert calls == []
    assert _posted(sent) == [
        ("LinearCliIssueId", {"id": "CCP-123"}),
        ("LinearCliCommentCreate", {"issueId": "issue-uuid", "body": body}),
    ]


def test_comment_reads_body_from_stdin() -> None:
    body = "From stdin.\n"
    post, sent = _serve(
        [
            {"data": {"issue": {"id": "issue-uuid"}}},
            _comment_created(),
        ]
    )
    code, out, err, calls = _run(
        ["comment", "CCP-123", "--body-file", "-"],
        environ=_KEY,
        stdin=body,
        post=post,
    )
    assert (code, out, err) == (0, f"{_COMMENT_URL}\n", "")
    assert calls == []
    assert _posted(sent) == [
        ("LinearCliIssueId", {"id": "CCP-123"}),
        ("LinearCliCommentCreate", {"issueId": "issue-uuid", "body": body}),
    ]


def test_comment_unknown_issue_posts_nothing(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("hello\n")
    post, sent = _serve([{"data": {"issue": None}}])
    code, out, err, calls = _run(
        ["comment", "CCP-999", "--body-file", str(path)],
        environ=_KEY,
        post=post,
    )
    assert (code, out, err) == (1, "", "linear-cli: unknown issue CCP-999\n")
    assert calls == []
    assert _posted(sent) == [("LinearCliIssueId", {"id": "CCP-999"})]


def test_comment_lookup_errors_stop_before_the_write(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("hello\n")
    post, sent = _serve([{"errors": [{"message": "nope"}]}])
    code, out, err, calls = _run(
        ["comment", "CCP-123", "--body-file", str(path)],
        environ=_KEY,
        post=post,
    )
    assert code == 1
    assert out == ""
    assert err == ("linear-cli: GraphQL response contains errors\nlinear-cli: nope\n")
    assert calls == []
    assert _posted(sent) == [("LinearCliIssueId", {"id": "CCP-123"})]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"data": {"commentCreate": {"success": False}}},
            "commentCreate returned success: false",
        ),
        (
            {"data": {"commentCreate": {"success": True, "comment": None}}},
            "commentCreate returned no comment URL",
        ),
    ],
)
def test_comment_write_failures(
    tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    path = tmp_path / "notes.md"
    path.write_text("hello\n")
    post, sent = _serve([{"data": {"issue": {"id": "issue-uuid"}}}, payload])
    code, out, err, calls = _run(
        ["comment", "CCP-123", "--body-file", str(path)],
        environ=_KEY,
        post=post,
    )
    assert (code, out, err) == (1, "", f"linear-cli: {message}\n")
    assert calls == []
    assert [name for name, _variables in _posted(sent)] == [
        "LinearCliIssueId",
        "LinearCliCommentCreate",
    ]


def test_comment_missing_file_is_local(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"
    code, out, err, calls = _run(
        ["comment", "CCP-123", "--body-file", str(missing)],
        environ=_KEY,
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert err == f"linear-cli: cannot read {missing}: No such file or directory\n"


def test_comment_unreadable_file_is_local(tmp_path: Path) -> None:
    code, out, err, calls = _run(
        ["comment", "CCP-123", "--body-file", str(tmp_path)],
        environ=_KEY,
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert err == f"linear-cli: cannot read {tmp_path}: Is a directory\n"


def test_comment_non_utf8_body_is_local(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_bytes(b"\xff")
    code, out, err, calls = _run(
        ["comment", "CCP-123", "--body-file", str(path)],
        environ=_KEY,
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert err == f"linear-cli: {path} is not UTF-8\n"


@pytest.mark.parametrize(
    ("kind", "body"),
    [("file", ""), ("file", " \n\t"), ("stdin", ""), ("stdin", " \n\t")],
)
def test_comment_blank_body_is_local(tmp_path: Path, kind: str, body: str) -> None:
    if kind == "file":
        path = tmp_path / "notes.md"
        path.write_text(body)
        argv_path = str(path)
        stdin = ""
    else:
        argv_path = "-"
        stdin = body
    code, out, err, calls = _run(
        ["comment", "CCP-123", "--body-file", argv_path],
        environ=_KEY,
        stdin=stdin,
    )
    assert (code, out, calls) == (2, "", [])
    assert err == "linear-cli: comment body is empty\n"


def test_issue_get_prints_one_flat_object() -> None:
    post, sent = _serve([_shown_issue()])
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123"],
        environ=_KEY,
        post=post,
    )
    expected = {
        "identifier": "CCP-123",
        "title": "Write the shortcuts",
        "description": "markdown or null",
        "state": "In Review",
        "assignee": "ada",
        "priority": 2,
        "estimate": 3,
        "cycle": "12",
        "labels": ["cli"],
        "parent": "CCP-100",
        "url": "https://linear.app/chewcorp/issue/CCP-123",
        "updatedAt": "2026-10-16T12:00:00.000Z",
    }
    assert (code, err, calls) == (0, "", [])
    assert out == json.dumps(expected, ensure_ascii=False, separators=(",", ":")) + "\n"
    assert "comments" not in json.loads(out)
    assert _posted(sent) == [("LinearCliIssueGet", {"id": "CCP-123"})]
    query = sent[0]["query"]
    assert isinstance(query, str)
    assert "labels(first: 50)" in query
    assert "comments(" not in query


def test_issue_get_prints_one_compact_line() -> None:
    post, _sent = _serve([_shown_issue()])
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123"],
        environ=_KEY,
        post=post,
    )
    assert (code, err, calls) == (0, "", [])
    assert out.endswith("\n")
    assert "\n" not in out[:-1]
    loaded = json.loads(out)
    assert loaded["identifier"] == "CCP-123"
    assert loaded["title"] == "Write the shortcuts"
    assert "comments" not in loaded
    assert out == json.dumps(loaded, ensure_ascii=False, separators=(",", ":")) + "\n"


def test_issue_get_comments_since_returns_every_page() -> None:
    def comment(index: int) -> dict[str, object]:
        return {
            "url": f"https://linear.app/comment/{index}",
            "createdAt": "2026-10-16T00:00:00.000Z",
            "body": f"c{index}",
            "user": {"name": "ada", "displayName": "Ada"},
        }

    post, _sent = _serve(
        [
            _shown_issue(),
            _comment_connection([comment(index) for index in range(50)], "c1", True),
            _comment_connection([comment(50)], "c2", False),
        ]
    )
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123", "--comments-since", "2026-10-16T00:00:00Z"],
        environ=_KEY,
        post=post,
    )
    assert (code, err, calls) == (0, "", [])
    assert out.endswith("\n") and "\n" not in out[:-1]
    loaded = json.loads(out)
    assert [item["body"] for item in loaded["comments"]] == [
        f"c{index}" for index in range(51)
    ]


def test_issue_get_comment_page_failure_prints_nothing() -> None:
    post, _sent = _serve(
        [
            _shown_issue(),
            _comment_connection(
                [
                    {
                        "url": "https://linear.app/comment/0",
                        "createdAt": "2026-10-16T00:00:00.000Z",
                        "body": "c0",
                        "user": {"name": "ada", "displayName": "Ada"},
                    }
                ],
                "c1",
                True,
            ),
            {"errors": [{"message": "comment page failed"}]},
        ]
    )
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123", "--comments-since", "2026-10-16T00:00:00Z"],
        environ=_KEY,
        post=post,
    )
    assert calls == []
    assert code != 0
    assert out == ""
    assert "comment page failed" in err


def test_issue_get_nulls_absent_fields() -> None:
    post, _sent = _serve(
        [
            _shown_issue(
                description=None,
                state=None,
                assignee=None,
                estimate=None,
                cycle=None,
                parent=None,
                labels=None,
            )
        ]
    )
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123"],
        environ=_KEY,
        post=post,
    )
    expected = {
        "identifier": "CCP-123",
        "title": "Write the shortcuts",
        "description": None,
        "state": None,
        "assignee": None,
        "priority": 2,
        "estimate": None,
        "cycle": None,
        "labels": [],
        "parent": None,
        "url": "https://linear.app/chewcorp/issue/CCP-123",
        "updatedAt": "2026-10-16T12:00:00.000Z",
    }
    assert (code, err, calls) == (0, "", [])
    assert out == json.dumps(expected, ensure_ascii=False, separators=(",", ":")) + "\n"


def test_issue_get_comments_since() -> None:
    since = "2026-10-16T00:00:00Z"
    post, sent = _serve(
        [
            _shown_issue(assignee={"name": "ada", "displayName": "Ada"}),
            _comment_connection(
                [
                    {
                        "url": "https://linear.app/comment/b",
                        "createdAt": "2026-10-16T02:00:00.000Z",
                        "body": "second",
                        "user": {"name": "ada", "displayName": "Ada"},
                    },
                    {
                        "url": "https://linear.app/comment/a",
                        "createdAt": "2026-10-16T01:00:00.000Z",
                        "body": "first",
                        "user": {"name": "grace", "displayName": ""},
                    },
                    {
                        "url": "https://linear.app/comment/c",
                        "createdAt": "2026-10-16T03:00:00.000Z",
                        "body": "third",
                        "user": {"name": "", "displayName": ""},
                    },
                ],
                None,
                False,
            ),
        ]
    )
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123", "--comments-since", since],
        environ=_KEY,
        post=post,
    )
    assert (code, err, calls) == (0, "", [])
    loaded = json.loads(out)
    assert loaded["assignee"] == "Ada"
    assert loaded["comments"] == [
        {
            "url": "https://linear.app/comment/b",
            "createdAt": "2026-10-16T02:00:00.000Z",
            "body": "second",
            "user": "Ada",
        },
        {
            "url": "https://linear.app/comment/a",
            "createdAt": "2026-10-16T01:00:00.000Z",
            "body": "first",
            "user": "grace",
        },
        {
            "url": "https://linear.app/comment/c",
            "createdAt": "2026-10-16T03:00:00.000Z",
            "body": "third",
            "user": None,
        },
    ]
    assert _posted(sent) == [
        ("LinearCliIssueGet", {"id": "CCP-123"}),
        (
            "IssueGetComments",
            {"id": "CCP-123", "after": None, "since": since},
        ),
    ]
    query = sent[1]["query"]
    assert isinstance(query, str)
    assert "gte: $since" in query
    assert "gt: $since" not in query


def test_issue_get_accepts_a_naive_since() -> None:
    raw = "2026-10-16T00:00:00"
    post, sent = _serve([_shown_issue(), _comment_connection([], None, False)])
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123", "--comments-since", raw],
        environ=_KEY,
        post=post,
    )
    assert code == 0
    assert err == ""
    assert calls == []
    assert json.loads(out)["comments"] == []
    assert sent[1]["variables"] == {"id": "CCP-123", "after": None, "since": raw}


def test_issue_get_rejects_unparseable_since() -> None:
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123", "--comments-since", "yesterday"],
        environ=_KEY,
    )
    assert code == 2
    assert out == ""
    assert calls == []
    assert err == "linear-cli: --comments-since must be an ISO-8601 time\n"


def test_issue_get_unknown_issue() -> None:
    post, sent = _serve([{"data": {"issue": None}}])
    code, out, err, calls = _run(
        ["issue", "get", "CCP-999"],
        environ=_KEY,
        post=post,
    )
    assert (code, out, err) == (1, "", "linear-cli: unknown issue CCP-999\n")
    assert calls == []
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("cycle", "expected"),
    [
        ({"name": "Cycle 12", "number": 12}, "Cycle 12"),
        ({"name": "", "number": 12}, "12"),
        ({"name": "", "number": 12.0}, "12"),
        ({"name": None, "number": 12.5}, "12.5"),
        ({"name": "", "number": True}, None),
        ({"name": "", "number": False}, None),
        (None, None),
        ({"number": "12"}, None),
    ],
)
def test_issue_get_renders_cycle(cycle: object, expected: object) -> None:
    post, _sent = _serve([_shown_issue(cycle=cycle)])
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123"],
        environ=_KEY,
        post=post,
    )
    assert (code, err, calls) == (0, "", [])
    assert json.loads(out)["cycle"] == expected


@pytest.mark.parametrize(
    ("priority", "estimate", "expected_priority", "expected_estimate"),
    [
        (2, 3, 2, 3),
        (0, 0.0, 0, 0.0),
        (2.5, 1.5, 2.5, 1.5),
        (True, False, None, None),
        ("2", "3", None, None),
    ],
)
def test_issue_get_keeps_only_numeric_priority_and_estimate(
    priority: object,
    estimate: object,
    expected_priority: object,
    expected_estimate: object,
) -> None:
    post, _sent = _serve([_shown_issue(priority=priority, estimate=estimate)])
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123"],
        environ=_KEY,
        post=post,
    )
    assert (code, err, calls) == (0, "", [])
    loaded = json.loads(out)
    assert loaded["priority"] == expected_priority
    assert loaded["estimate"] == expected_estimate


@pytest.mark.parametrize("key", ["identifier", "title", "url", "updatedAt"])
def test_issue_get_rejects_a_non_string_field(key: str) -> None:
    post, sent = _serve([_shown_issue(**{key: None})])
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123"],
        environ=_KEY,
        post=post,
    )
    assert (code, out, calls) == (1, "", [])
    assert err == f"linear-cli: issue lookup returned no {key}\n"
    assert len(sent) == 1


def test_issue_move_prints_old_and_new_state() -> None:
    post, sent = _serve(
        [
            _move_lookup([{"id": "state-review", "name": "In Review"}]),
            {"data": {"issueUpdate": {"success": True}}},
        ]
    )
    code, out, err, calls = _run(
        ["issue", "move", "CCP-123", "--state", "In Review"],
        environ=_KEY,
        post=post,
    )
    assert (code, out, err) == (0, "Todo → In Review\n", "")
    assert calls == []
    assert _posted(sent) == [
        ("LinearCliMoveLookup", {"id": "CCP-123", "state": "In Review"}),
        ("LinearCliIssueMove", {"id": "issue-uuid", "stateId": "state-review"}),
    ]
    query = sent[0]["query"]
    assert isinstance(query, str)
    assert "name: { eq: $state }" in query
    assert "states(first: 50, filter: { name: { eq: $state } })" in query


@pytest.mark.parametrize(
    ("states", "message"),
    [
        ([], "unknown state 'Done' for CCP-123"),
        (
            [{"id": "s1", "name": "Done"}, {"id": "s2", "name": "Done"}],
            "ambiguous state 'Done' for CCP-123: 2 matches",
        ),
    ],
)
def test_issue_move_needs_exactly_one_state(
    states: list[dict[str, object]], message: str
) -> None:
    post, sent = _serve([_move_lookup(states)])
    code, out, err, calls = _run(
        ["issue", "move", "CCP-123", "--state", "Done"],
        environ=_KEY,
        post=post,
    )
    assert (code, out, err) == (1, "", f"linear-cli: {message}\n")
    assert calls == []
    assert _posted(sent) == [
        ("LinearCliMoveLookup", {"id": "CCP-123", "state": "Done"}),
    ]


def test_issue_move_unknown_issue() -> None:
    post, sent = _serve([{"data": {"issue": None}}])
    code, out, err, calls = _run(
        ["issue", "move", "CCP-999", "--state", "Done"],
        environ=_KEY,
        post=post,
    )
    assert (code, out, err) == (1, "", "linear-cli: unknown issue CCP-999\n")
    assert calls == []
    assert _posted(sent) == [
        ("LinearCliMoveLookup", {"id": "CCP-999", "state": "Done"}),
    ]


def test_issue_move_lookup_errors_stop_before_the_write() -> None:
    post, sent = _serve([{"errors": [{"message": "nope"}]}])
    code, out, err, calls = _run(
        ["issue", "move", "CCP-123", "--state", "Done"],
        environ=_KEY,
        post=post,
    )
    assert code == 1
    assert out == ""
    assert err == ("linear-cli: GraphQL response contains errors\nlinear-cli: nope\n")
    assert calls == []
    assert _posted(sent) == [
        ("LinearCliMoveLookup", {"id": "CCP-123", "state": "Done"}),
    ]


def test_issue_move_update_failure() -> None:
    post, sent = _serve(
        [
            _move_lookup([{"id": "state-review", "name": "In Review"}]),
            {"data": {"issueUpdate": {"success": False}}},
        ]
    )
    code, out, err, calls = _run(
        ["issue", "move", "CCP-123", "--state", "In Review"],
        environ=_KEY,
        post=post,
    )
    assert (code, out, err) == (
        1,
        "",
        "linear-cli: issueUpdate returned success: false\n",
    )
    assert calls == []
    assert [name for name, _variables in _posted(sent)] == [
        "LinearCliMoveLookup",
        "LinearCliIssueMove",
    ]


def test_shortcut_transport_failure() -> None:
    code, out, err, calls = _run(
        ["issue", "get", "CCP-123"],
        environ=_KEY,
        reply=_HttpReply(500, "Server Error", b"boom"),
    )
    assert (code, out, err) == (1, "", "linear-cli: HTTP 500 boom\n")
    assert len(calls) == 1


def _ready_issue(identifier: str, **overrides: object) -> dict[str, object]:
    node: dict[str, object] = {
        "identifier": identifier,
        "title": f"Title {identifier}",
        "priority": 3,
        "estimate": 2,
        "url": f"https://linear.app/chewcorp/issue/{identifier}",
        "description": "Task: x\nCriteria: y\nDone when: z",
        "delegate": None,
    }
    for key in ("labels", "inverseRelations", "attachments"):
        node[key] = {"nodes": [], "pageInfo": {"hasNextPage": False}}
    for key, value in overrides.items():
        if isinstance(value, dict) and "nodes" in value:
            value = {"pageInfo": {"hasNextPage": False}, **value}
        node[key] = value
    return node


def _closed(team: str = "CCP") -> dict[str, object]:
    return {"team": {"key": team}, "completedAt": "2026-10-17T00:00:00Z"}


def _open_issue(team: str = "CCP", state: str = "Todo") -> dict[str, object]:
    return {
        "team": {"key": team},
        "state": {"name": state},
        "completedAt": None,
        "canceledAt": None,
    }


def _blocked_by(issue: dict[str, object]) -> dict[str, object]:
    return {"nodes": [{"type": "blocks", "issue": issue}]}


def test_issues_ready_keeps_only_the_frontier() -> None:
    nodes = [
        _ready_issue("CCP-1", priority=0),
        _ready_issue("CCP-2", priority=2),
        _ready_issue("CCP-3", delegate={"id": "cursor"}),
        _ready_issue("CCP-4", description="no acceptance text"),
        _ready_issue("CCP-5", description="Done when: x. This is Russell's action."),
        _ready_issue(
            "CCP-6",
            labels={"nodes": [{"name": "pe", "parent": {"name": "agent"}}]},
        ),
        _ready_issue(
            "CCP-7",
            inverseRelations={"nodes": [{"type": "blocks", "issue": _open_issue()}]},
        ),
        _ready_issue(
            "CCP-8",
            inverseRelations={"nodes": [{"type": "blocks", "issue": _closed()}]},
            labels={"nodes": [{"name": "harness", "parent": None}]},
        ),
        _ready_issue(
            "CCP-9",
            inverseRelations=_blocked_by(_open_issue("BRD", "Under Consideration")),
        ),
        _ready_issue(
            "CCP-10", inverseRelations=_blocked_by(_open_issue("BRD", "Approved"))
        ),
        _ready_issue(
            "CCP-11", attachments={"nodes": [{"metadata": {"status": "merged"}}]}
        ),
        _ready_issue(
            "CCP-12",
            inverseRelations={"nodes": [], "pageInfo": {"hasNextPage": True}},
        ),
        _ready_issue(
            "CCP-13",
            inverseRelations={
                "nodes": [{"type": "related", "issue": _open_issue("BRD")}]
            },
        ),
    ]
    post, sent = _serve([_connection(nodes, None, False)])
    code, out, err, calls = _run(
        ["issues", "ready", "--team", "CCP"], environ=_KEY, post=post
    )
    assert calls == []
    assert (code, err) == (0, "")
    lines = [json.loads(line) for line in out.splitlines()]
    assert [line["identifier"] for line in lines] == [
        "CCP-2",
        "CCP-8",
        "CCP-10",
        "CCP-13",
        "CCP-1",
    ]
    assert list(lines[0]) == ["identifier", "title", "priority", "estimate", "url"]
    body = sent[0]
    assert body["variables"] == {"after": None, "teams": ["CCP"]}
    query = body["query"]
    assert isinstance(query, str)
    assert 'state: { type: { eq: "unstarted" } }' in query
    assert "assignee: { null: true }" in query
    assert "estimate: { null: false }" in query


def test_issues_ready_reports_graphql_errors() -> None:
    post, _ = _serve([{"errors": [{"message": "bad filter"}], "data": None}])
    code, out, err, _ = _run(
        ["issues", "ready", "--team", "CCP"], environ=_KEY, post=post
    )
    assert code == 1
    assert out == ""
    assert "bad filter" in err


def _ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat()


def _pr(number: int, status: str = "open") -> dict[str, object]:
    return {
        "url": f"https://github.com/chewcorp/x/pull/{number}",
        "metadata": {"status": status},
    }


def _stale_issue(
    identifier: str,
    state: str,
    *,
    entered: float = 30,
    updated: float = 1,
    prs: list[dict[str, object]] | None = None,
    **overrides: object,
) -> dict[str, object]:
    unstarted = state in {"Ready", "Todo"}
    node: dict[str, object] = {
        "identifier": identifier,
        "title": f"Title {identifier}",
        "estimate": 1,
        "url": f"https://linear.app/chewcorp/issue/{identifier}",
        "updatedAt": _ago(updated),
        "state": {"name": state, "type": "unstarted" if unstarted else "started"},
        "delegate": None,
        "attachments": {"nodes": prs or [], "pageInfo": {"hasNextPage": False}},
        "history": {
            "nodes": [
                {"createdAt": _ago(entered + 5), "toState": {"name": "Ready"}},
                {"createdAt": _ago(entered), "toState": {"name": state}},
            ]
        },
    }
    node.update(overrides)
    return node


def test_issues_stale_flags_each_rule() -> None:
    nodes = [
        _stale_issue("CCP-1", "Claimed", estimate=3),
        _stale_issue("CCP-2", "Claimed", entered=2, prs=[_pr(1)]),
        _stale_issue("CCP-3", "In Progress", estimate=5),
        _stale_issue("CCP-4", "Ready", updated=30, delegate={"id": "cursor"}),
        _stale_issue("CCP-5", "In Review", prs=[_pr(2), _pr(3, "merged")]),
        _stale_issue("CCP-6", "In Review", updated=80, prs=[_pr(4)]),
        # Clean: young claims, PR-backed work, a closed PR, an undelegated card.
        _stale_issue("CCP-7", "Claimed", entered=2),
        _stale_issue("CCP-8", "In Progress", prs=[_pr(5)]),
        _stale_issue("CCP-9", "In Review", prs=[_pr(6), _pr(7, "closed")]),
        _stale_issue("CCP-10", "Ready", updated=30),
        _stale_issue("CCP-11", "In Progress", entered=30, history={"nodes": []}),
        _stale_issue("CCP-12", "In Review", prs=[_pr(8), _pr(8)]),
        _stale_issue("CCP-13", "In Review", prs=[_pr(9, "merged"), _pr(10, "merged")]),
    ]
    post, sent = _serve([_connection(nodes, None, False)])
    code, out, err, calls = _run(
        ["issues", "stale", "--team", "CCP"], environ=_KEY, post=post
    )
    assert calls == []
    assert (code, err) == (0, "")
    lines = [json.loads(line) for line in out.splitlines()]
    assert [(line["identifier"], line["rule"]) for line in lines] == [
        ("CCP-3", "in-progress-no-pr"),
        ("CCP-1", "claimed-no-pr"),
        ("CCP-2", "claimed-has-pr"),
        ("CCP-4", "dead-delegate"),
        ("CCP-5", "duplicate-pr"),
        ("CCP-6", "quiet-in-review"),
    ]
    assert list(lines[0]) == [
        "identifier",
        "title",
        "estimate",
        "url",
        "rule",
        "since",
        "hours_over",
    ]
    assert 5.9 <= lines[0]["hours_over"] <= 6.1
    assert 7.9 <= lines[5]["hours_over"] <= 8.1
    assert sent[0]["variables"] == {"after": None, "teams": ["CCP"]}
    assert 'type: { in: ["unstarted", "started"] }' in sent[0]["query"]


def test_issues_stale_reports_graphql_errors() -> None:
    post, _ = _serve([{"errors": [{"message": "bad filter"}], "data": None}])
    code, out, err, _ = _run(
        ["issues", "stale", "--team", "CCP"], environ=_KEY, post=post
    )
    assert (code, out) == (1, "")
    assert "bad filter" in err
