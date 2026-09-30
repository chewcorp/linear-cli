import io
import json
import os
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Mapping
from http.client import HTTPMessage, IncompleteRead
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    assert out.splitlines()[0] == "usage: linear-cli [-h] {schema,graphql} ..."
    assert "schema" in out
    assert "graphql" in out


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
    assert out.splitlines()[0] == (
        "usage: linear-cli graphql [-h] [--variables JSON] DOCUMENT"
    )
    assert "graphql" in out


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
        "linear-cli: GraphQL response contains errors\n" f"linear-cli: {message}\n"
    )
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
