from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol, TextIO, TypeAlias
from urllib.error import HTTPError, URLError

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

LINEAR_GRAPHQL_URL: Final[str] = "https://api.linear.app/graphql"
HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_API_KEY_ENV: Final[str] = "LINEAR_API_KEY"
_OAUTH_ENV: Final[str] = "LINEAR_OAUTH_TOKEN"

INTROSPECTION_QUERY: Final[str] = r"""
query LinearCliSchema {
  __schema {
    queryType { name }
    mutationType { name }
    subscriptionType { name }
    types { ...FullType }
    directives {
      name
      description
      locations
      args(includeDeprecated: true) { ...InputValue }
      isRepeatable
    }
  }
}

fragment FullType on __Type {
  kind
  name
  description
  specifiedByURL
  fields(includeDeprecated: true) {
    name
    description
    args(includeDeprecated: true) { ...InputValue }
    type { ...TypeRef }
    isDeprecated
    deprecationReason
  }
  inputFields(includeDeprecated: true) { ...InputValue }
  interfaces { ...TypeRef }
  enumValues(includeDeprecated: true) {
    name
    description
    isDeprecated
    deprecationReason
  }
  possibleTypes { ...TypeRef }
}

fragment InputValue on __InputValue {
  name
  description
  type { ...TypeRef }
  defaultValue
  isDeprecated
  deprecationReason
}

fragment TypeRef on __Type {
  kind
  name
  ofType {
    kind
    name
    ofType {
      kind
      name
      ofType {
        kind
        name
        ofType {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
              ofType {
                kind
                name
              }
            }
          }
        }
      }
    }
  }
}
"""


@dataclass(frozen=True, slots=True)
class PersonalApiKey:
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthAccessToken:
    value: str = field(repr=False)


Credential: TypeAlias = PersonalApiKey | OAuthAccessToken


@dataclass(frozen=True, slots=True)
class GraphQLOperation:
    document: str
    variables: JsonObject


@dataclass(frozen=True, slots=True)
class GraphQLResponse:
    payload: JsonObject

    @property
    def has_errors(self) -> bool:
        errors = self.payload.get("errors")
        return isinstance(errors, list) and len(errors) > 0


@dataclass(frozen=True, slots=True)
class FetchSchema:
    pass


@dataclass(frozen=True, slots=True)
class ExecuteOperation:
    operation: GraphQLOperation


Invocation: TypeAlias = FetchSchema | ExecuteOperation


@dataclass(frozen=True, slots=True)
class _HttpReply:
    status: int
    reason: str
    body: bytes


class _HttpPost(Protocol):
    def __call__(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> _HttpReply: ...


class LocalInputError(Exception):
    pass


class RemoteFailure(Exception):
    pass


class _ParserExit(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(str(status))
        self.status = status


def credential_from_env(environ: Mapping[str, str]) -> Credential:
    api_key = _token(environ, _API_KEY_ENV)
    oauth_token = _token(environ, _OAUTH_ENV)
    if api_key is not None and oauth_token is None:
        return PersonalApiKey(api_key)
    if oauth_token is not None and api_key is None:
        return OAuthAccessToken(oauth_token)
    raise LocalInputError(f"set exactly one of {_API_KEY_ENV} or {_OAUTH_ENV}")


def parse_invocation(
    argv: Sequence[str],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> Invocation:
    parser = _bind_parser(stdout, stderr)(
        prog="linear-cli",
        description="Use Linear through its published GraphQL schema.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="fetch the live GraphQL schema")
    graphql = commands.add_parser("graphql", help="execute one GraphQL document")
    graphql.add_argument(
        "--variables",
        default="{}",
        metavar="JSON",
        help="JSON object of variables",
    )
    graphql.add_argument(
        "document",
        metavar="DOCUMENT",
        help="GraphQL document, or - to read stdin",
    )
    namespace = parser.parse_args(list(argv))
    if namespace.command == "schema":
        return FetchSchema()
    return ExecuteOperation(
        GraphQLOperation(
            document=_document(namespace.document, stdin),
            variables=_variables(namespace.variables),
        )
    )


@dataclass(slots=True)
class GraphQLClient:
    credential: Credential
    post: _HttpPost

    def execute(self, operation: GraphQLOperation) -> GraphQLResponse:
        secret = self.credential.value
        body = json.dumps(
            {"query": operation.document, "variables": operation.variables},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            reply = self.post(
                url=LINEAR_GRAPHQL_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": _authorization(self.credential),
                },
                body=body,
                timeout_seconds=HTTP_TIMEOUT_SECONDS,
            )
        except RemoteFailure as exc:
            raise RemoteFailure(_collapse(str(exc), secret)) from None
        payload = _envelope(reply)
        if payload is None:
            raise RemoteFailure(_http_diagnostic(reply, secret))
        return GraphQLResponse(payload)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    post: _HttpPost | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    post = _stdlib_post if post is None else post
    try:
        invocation = parse_invocation(argv, stdin, stdout, stderr)
    except _ParserExit as exc:
        return exc.status
    except LocalInputError as exc:
        _write_line(stderr, str(exc))
        return 2
    try:
        credential = credential_from_env(environ)
    except LocalInputError as exc:
        _write_line(stderr, str(exc))
        return 2
    match invocation:
        case FetchSchema():
            operation = GraphQLOperation(INTROSPECTION_QUERY, {})
        case ExecuteOperation(operation=operation):
            pass
        case _:
            _write_line(stderr, "unknown command")
            return 2
    try:
        response = GraphQLClient(credential, post).execute(operation)
    except RemoteFailure as exc:
        _write_line(stderr, str(exc))
        return 1
    stdout.write(json.dumps(response.payload, indent=2, ensure_ascii=False))
    stdout.write("\n")
    if not response.has_errors:
        return 0
    _write_line(stderr, "GraphQL response contains errors")
    errors = response.payload.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if not isinstance(item, dict):
                continue
            message = item.get("message")
            if isinstance(message, str):
                _write_line(stderr, message)
    return 1


def _token(environ: Mapping[str, str], name: str) -> str | None:
    if name not in environ:
        return None
    raw = environ[name]
    value = raw.strip()
    if (
        value == ""
        or any(char.isspace() for char in raw)
        or value.lower().startswith("bearer ")
    ):
        raise LocalInputError(
            f"{name} must be a non-empty token with no whitespace and no Bearer prefix"
        )
    return value


def _document(document: str, stdin: TextIO) -> str:
    if document == "-":
        document = stdin.read()
    document = document.strip()
    if not document:
        raise LocalInputError("GraphQL document is empty")
    return document


def _variables(raw: str) -> JsonObject:
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        raise LocalInputError("--variables must be a JSON object") from None
    if not isinstance(value, dict):
        raise LocalInputError("--variables must be a JSON object")
    return value


def _authorization(credential: Credential) -> str:
    match credential:
        case PersonalApiKey(value):
            return value
        case OAuthAccessToken(value):
            return f"Bearer {value}"
    raise RemoteFailure("unsupported credential")


def _envelope(reply: _HttpReply) -> JsonObject | None:
    payload = _json_object(reply.body)
    if payload is None:
        return None
    if reply.status == 200 or "data" in payload or "errors" in payload:
        return payload
    return None


def _json_object(body: bytes) -> JsonObject | None:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        value = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        return None
    if isinstance(value, dict):
        return value
    return None


def _reject_constant(constant: str) -> None:
    raise json.JSONDecodeError("invalid JSON number", constant, 0)


def _http_diagnostic(reply: _HttpReply, secret: str) -> str:
    if reply.status == 200:
        return "response was not a JSON object"
    excerpt = _collapse(reply.body.decode("utf-8", errors="replace"), secret)
    if excerpt:
        return f"HTTP {reply.status} {excerpt}"
    return f"HTTP {reply.status}"


def _collapse(text: str, secret: str, limit: int = 200) -> str:
    if secret:
        text = text.replace(secret, "REDACTED")
    return " ".join(text.split())[:limit]


def _write_line(stream: TextIO, message: str) -> None:
    stream.write(f"linear-cli: {message}\n")


def _bind_parser(stdout: TextIO, stderr: TextIO) -> type[argparse.ArgumentParser]:
    class BoundParser(argparse.ArgumentParser):
        def print_help(self, file: TextIO | None = None) -> None:
            super().print_help(stdout if file is None else file)

        def print_usage(self, file: TextIO | None = None) -> None:
            super().print_usage(stderr if file is None else file)

        def exit(self, status: int | None = 0, message: str | None = None) -> None:
            if message:
                target = stdout if status == 0 else stderr
                target.write(message)
            raise _ParserExit(0 if status is None else status)

        def error(self, message: str) -> None:
            self.print_usage(stderr)
            self.exit(2, f"{self.prog}: error: {message}\n")

    return BoundParser


def _stdlib_post(
    *,
    url: str,
    headers: Mapping[str, str],
    body: bytes,
    timeout_seconds: float,
) -> _HttpReply:
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict(headers),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return _HttpReply(
                status=response.status,
                reason=str(response.reason or ""),
                body=response.read(),
            )
    except HTTPError as err:
        # HTTPError subclasses URLError, so it must be handled before URLError.
        try:
            payload = err.read()
        except OSError as read_err:
            raise RemoteFailure(_collapse(str(read_err), "")) from None
        finally:
            err.close()
        return _HttpReply(
            status=err.code,
            reason=str(err.reason or ""),
            body=payload,
        )
    except TimeoutError as err:
        raise RemoteFailure(_collapse(str(getattr(err, "reason", err)), "")) from None
    except URLError as err:
        raise RemoteFailure(_collapse(str(err.reason), "")) from None
    except OSError as err:
        raise RemoteFailure(_collapse(str(err), "")) from None
