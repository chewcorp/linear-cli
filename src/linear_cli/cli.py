from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import tomllib
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.client import IncompleteRead
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Final, Protocol, TextIO, TypeAlias, assert_never
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

LINEAR_GRAPHQL_URL: Final[str] = "https://api.linear.app/graphql"
HTTP_TIMEOUT_SECONDS: Final[float] = 30.0
_API_KEY_ENV: Final[str] = "LINEAR_API_KEY"
_OAUTH_ENV: Final[str] = "LINEAR_OAUTH_TOKEN"
_ISSUE_PAGE_SIZE: Final[int] = 50
_COMMENT_PAGE_SIZE: Final[int] = 50
_COMMENT_BODY_LIMIT: Final[int] = 280
# shortcut: 500 pages, raise this if a real walk is longer
_MAX_PAGES: Final[int] = 500
_AFTER_DEFINITION: Final[re.Pattern[str]] = re.compile(r"\$after\s*:")
_ISSUE_FIELDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("identifier", ("identifier",)),
    ("title", ("title",)),
    ("state", ("state", "name")),
    ("assignee", ("assignee", "displayName")),
    ("priority", ("priority",)),
    ("estimate", ("estimate",)),
    ("cycle", ("cycle", "number")),
    ("updatedAt", ("updatedAt",)),
    ("url", ("url",)),
)
_COMMENT_FIELDS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("author", ("user", "displayName")),
    ("createdAt", ("createdAt",)),
    ("body", ("body",)),
)

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

# shortcut: one page of 50, paginate when a caller needs the rest.
_SHORTCUT_PAGE_SIZE: Final[int] = 50

_ISSUE_ID_QUERY: Final[str] = """
query LinearCliIssueId($id: String!) {
  issue(id: $id) {
    id
  }
}
"""

_COMMENT_CREATE: Final[str] = """
mutation LinearCliCommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment {
      url
    }
  }
}
"""

_ISSUE_GET_QUERY: Final[str] = f"""
query LinearCliIssueGet($id: String!) {{
  issue(id: $id) {{
    identifier
    title
    description
    state {{ name }}
    assignee {{ name displayName }}
    priority
    estimate
    cycle {{ name number }}
    labels(first: {_SHORTCUT_PAGE_SIZE}) {{ nodes {{ name }} }}
    parent {{ identifier }}
    url
    updatedAt
  }}
}}
"""

_MOVE_LOOKUP_QUERY: Final[str] = f"""
query LinearCliMoveLookup($id: String!, $state: String!) {{
  issue(id: $id) {{
    id
    state {{ name }}
    team {{
      states(first: {_SHORTCUT_PAGE_SIZE}, filter: {{ name: {{ eq: $state }} }}) {{
        nodes {{ id name }}
      }}
    }}
  }}
}}
"""

_ISSUE_MOVE: Final[str] = """
mutation LinearCliIssueMove($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
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
    compact: bool
    data_only: bool
    paginate: bool


@dataclass(frozen=True, slots=True)
class Since:
    at: datetime


@dataclass(frozen=True, slots=True)
class Resume:
    at: datetime
    state_file: Path


@dataclass(frozen=True, slots=True)
class Baseline:
    state_file: Path


Window: TypeAlias = Since | Resume | Baseline


@dataclass(frozen=True, slots=True)
class IssuesChanged:
    teams: tuple[str, ...]
    window: Window
    with_comments: bool


@dataclass(frozen=True, slots=True)
class IssuesReady:
    teams: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IssuesStale:
    teams: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GetIssue:
    issue: str
    comments_since: str | None


@dataclass(frozen=True, slots=True)
class PostComment:
    issue: str
    body: str


@dataclass(frozen=True, slots=True)
class MoveIssue:
    issue: str
    state: str


Shortcut: TypeAlias = GetIssue | PostComment | MoveIssue

Invocation: TypeAlias = (
    FetchSchema
    | ExecuteOperation
    | IssuesChanged
    | IssuesReady
    | IssuesStale
    | Shortcut
)


@dataclass(frozen=True, slots=True)
class _IssueId:
    value: str


@dataclass(frozen=True, slots=True)
class _Move:
    issue: _IssueId
    state_id: str
    old: str
    new: str


@dataclass(frozen=True, slots=True)
class _Page:
    nodes: list[JsonValue]
    has_next_page: bool
    end_cursor: str | None


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


class GraphQLErrors(RemoteFailure):
    def __init__(self, response: GraphQLResponse) -> None:
        super().__init__("GraphQL response contains errors")
        self.response = response


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


def graphql_url_from_env(environ: Mapping[str, str]) -> str:
    url = environ.get("LINEAR_GRAPHQL_URL", LINEAR_GRAPHQL_URL)
    if url == LINEAR_GRAPHQL_URL:
        return url
    try:
        parsed = urlsplit(url)
        valid = (
            not any(char.isspace() for char in url)
            and parsed.scheme in {"http", "https"}
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and (parsed.port is None or parsed.port > 0)
        )
    except ValueError:
        valid = False
    if not valid:
        raise LocalInputError(
            "LINEAR_GRAPHQL_URL must be the Linear endpoint or an HTTP(S) URL "
            "on localhost, 127.0.0.1, or ::1"
        )
    return url


def parse_invocation(
    argv: Sequence[str],
    stdin: TextIO,
    stdout: TextIO,
    stderr: TextIO,
) -> Invocation:
    if "--version" in argv:
        stdout.write(f"{_package_version()}\n")
        raise _ParserExit(0)
    parser = _bind_parser(stdout, stderr)(
        prog="linear-cli",
        description="Use Linear through its published GraphQL schema.",
        epilog="Set LINEAR_GRAPHQL_URL to a loopback URL for local API development.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show the package version and exit",
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
        "--compact",
        action="store_true",
        help="print minified JSON",
    )
    graphql.add_argument(
        "--data",
        action="store_true",
        help="print only the data object",
    )
    graphql.add_argument(
        "--paginate",
        action="store_true",
        help="follow one $after connection and merge its nodes",
    )
    graphql.add_argument(
        "document",
        metavar="DOCUMENT",
        help="GraphQL document, or - to read stdin",
    )
    issues = commands.add_parser("issues", help="issue convenience commands")
    issue_commands = issues.add_subparsers(dest="issues_command", required=True)
    changed = issue_commands.add_parser(
        "changed",
        help="issues updated since a time",
    )
    changed.add_argument("--team", required=True, help="comma-separated team keys")
    window = changed.add_mutually_exclusive_group(required=True)
    window.add_argument("--since", help="ISO-8601 time with a timezone")
    window.add_argument(
        "--state-file",
        help="watermark file, written only after a successful run",
    )
    changed.add_argument(
        "--with-comments",
        action="store_true",
        help="include comments created since the same time",
    )
    ready = issue_commands.add_parser(
        "ready",
        help="frontier: unstarted, estimated, specced, unblocked, unclaimed issues",
    )
    ready.add_argument("--team", required=True, help="comma-separated team keys")
    stale = issue_commands.add_parser(
        "stale",
        help="cards past a time-in-state threshold, one JSON line per rule tripped",
        description=(
            "Rules: claimed-no-pr (Claimed 24h, no PR), claimed-has-pr (Claimed with "
            "a PR), in-progress-no-pr (In Progress 24h, no PR), dead-delegate "
            "(unstarted, delegate set, no PR, no update 24h), duplicate-pr (two or "
            "more open or merged PRs), quiet-in-review (In Review, no update 72h)."
        ),
    )
    stale.add_argument("--team", required=True, help="comma-separated team keys")
    comment = commands.add_parser("comment", help="post a comment on an issue")
    comment.add_argument(
        "issue", metavar="ISSUE", help="issue identifier, e.g. CCP-123"
    )
    comment.add_argument(
        "--body-file",
        required=True,
        metavar="PATH",
        help="markdown file for the comment body, or - to read stdin",
    )
    issue = commands.add_parser("issue", help="read or move one issue")
    issue_verbs = issue.add_subparsers(dest="issue_command", required=True)
    get = issue_verbs.add_parser("get", help="print one issue as JSON")
    get.add_argument("issue", metavar="ISSUE", help="issue identifier, e.g. CCP-123")
    get.add_argument(
        "--comments-since",
        metavar="ISO",
        help="include comments created at or after this ISO-8601 time",
    )
    move = issue_verbs.add_parser("move", help="move an issue to a workflow state")
    move.add_argument("issue", metavar="ISSUE", help="issue identifier, e.g. CCP-123")
    move.add_argument(
        "--state",
        required=True,
        metavar="NAME",
        help="exact workflow state name on the issue's team",
    )
    namespace = parser.parse_args(list(argv))
    if namespace.command == "schema":
        return FetchSchema()
    if namespace.command == "issues" and namespace.issues_command == "ready":
        return IssuesReady(_team_keys(namespace.team))
    if namespace.command == "issues" and namespace.issues_command == "stale":
        return IssuesStale(_team_keys(namespace.team))
    if namespace.command == "issues":
        return IssuesChanged(
            teams=_team_keys(namespace.team),
            window=_window(namespace.since, namespace.state_file),
            with_comments=namespace.with_comments,
        )
    if namespace.command == "comment":
        return PostComment(namespace.issue, _body(namespace.body_file, stdin))
    if namespace.command == "issue" and namespace.issue_command == "get":
        return GetIssue(namespace.issue, _comments_since(namespace.comments_since))
    if namespace.command == "issue":
        return MoveIssue(namespace.issue, namespace.state)
    document = _document(namespace.document, stdin)
    if namespace.paginate:
        _require_after(document)
    return ExecuteOperation(
        GraphQLOperation(document=document, variables=_variables(namespace.variables)),
        compact=namespace.compact,
        data_only=namespace.data,
        paginate=namespace.paginate,
    )


@dataclass(slots=True)
class GraphQLClient:
    credential: Credential
    post: _HttpPost
    url: str = LINEAR_GRAPHQL_URL

    def execute(self, operation: GraphQLOperation) -> GraphQLResponse:
        secret = self.credential.value
        try:
            body = json.dumps(
                {"query": operation.document, "variables": operation.variables},
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except ValueError:
            raise LocalInputError("--variables must be a JSON object") from None
        try:
            reply = self.post(
                url=self.url,
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

    def paginate(self, operation: GraphQLOperation) -> GraphQLResponse:
        _require_after(operation.document)
        seen: set[str] = set()
        current = operation
        first: GraphQLResponse | None = None
        path: tuple[str, ...] | None = None
        nodes: list[JsonValue] = []
        page: _Page | None = None
        for _ in range(_MAX_PAGES):
            sent = current.variables.get("after")
            if isinstance(sent, str) and sent in seen:
                raise RemoteFailure("pagination cursor did not advance")
            if isinstance(sent, str):
                seen.add(sent)
            response = self.execute(current)
            if response.has_errors:
                return response
            if first is None:
                first = response
                path, page = _one_connection(_data(response))
            else:
                assert path is not None
                page = _read_page(_data(response), path)
            nodes.extend(page.nodes)
            if not page.has_next_page:
                assert first is not None and path is not None
                return GraphQLResponse(
                    _with_connection(first.payload, path, nodes, page.end_cursor)
                )
            cursor = page.end_cursor
            if cursor is None or cursor == "" or cursor in seen:
                raise RemoteFailure("pagination cursor did not advance")
            variables = dict(current.variables)
            variables["after"] = cursor
            current = GraphQLOperation(operation.document, variables)
        raise RemoteFailure(f"pagination stopped after {_MAX_PAGES} pages")

    def data(self, operation: GraphQLOperation) -> JsonObject:
        response = self.execute(operation)
        if response.has_errors:
            raise GraphQLErrors(response)
        return _data(response)


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
        client = GraphQLClient(credential, post, graphql_url_from_env(environ))
    except LocalInputError as exc:
        _write_line(stderr, str(exc))
        return 2
    compact = False
    data_only = False
    paginate = False
    match invocation:
        case FetchSchema():
            operation = GraphQLOperation(INTROSPECTION_QUERY, {})
        case ExecuteOperation(
            operation=operation,
            compact=compact,
            data_only=data_only,
            paginate=paginate,
        ):
            pass
        case IssuesReady(teams=teams) | IssuesStale(teams=teams):
            run = (
                _issues_ready if isinstance(invocation, IssuesReady) else _issues_stale
            )
            try:
                return run(client, teams, stdout)
            except GraphQLErrors as exc:
                return _report(stderr, exc.response)
            except RemoteFailure as exc:
                _write_line(stderr, str(exc))
                return 1
        case IssuesChanged() as command:
            try:
                return _issues_changed(client, command, stdout, stderr)
            except (RemoteFailure, OSError) as exc:
                _write_line(stderr, str(exc))
                return 1
            except LocalInputError as exc:
                _write_line(stderr, str(exc))
                return 2
        case GetIssue() | PostComment() | MoveIssue() as shortcut:
            try:
                line = _shortcut(client, shortcut)
            except GraphQLErrors as exc:
                return _report(stderr, exc.response)
            except RemoteFailure as exc:
                _write_line(stderr, str(exc))
                return 1
            stdout.write(f"{line}\n")
            return 0
    try:
        response = client.paginate(operation) if paginate else client.execute(operation)
    except RemoteFailure as exc:
        _write_line(stderr, str(exc))
        return 1
    except LocalInputError as exc:
        _write_line(stderr, str(exc))
        return 2
    shown = response.payload.get("data") if data_only else response.payload
    if not (data_only and response.has_errors and shown is None):
        if compact:
            rendered = json.dumps(shown, ensure_ascii=False, separators=(",", ":"))
        else:
            rendered = json.dumps(shown, indent=2, ensure_ascii=False)
        stdout.write(rendered)
        stdout.write("\n")
    return _report(stderr, response)


def _require_after(document: str) -> None:
    if _AFTER_DEFINITION.search(document) is None:
        raise LocalInputError("--paginate needs a document that declares $after")


def _data(response: GraphQLResponse) -> JsonObject:
    data = response.payload.get("data")
    if not isinstance(data, dict):
        raise RemoteFailure("GraphQL response has no data object")
    return data


def _as_page(value: JsonValue) -> _Page | None:
    if not isinstance(value, dict):
        return None
    nodes = value.get("nodes")
    page_info = value.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        return None
    if "hasNextPage" not in page_info or "endCursor" not in page_info:
        return None
    has_next = page_info.get("hasNextPage")
    end_cursor = page_info.get("endCursor")
    if not isinstance(has_next, bool):
        return None
    if end_cursor is not None and not isinstance(end_cursor, str):
        return None
    return _Page(nodes, has_next, end_cursor)


def _connection_paths(
    value: JsonValue, path: tuple[str, ...]
) -> list[tuple[tuple[str, ...], _Page]]:
    if not isinstance(value, dict):
        return []
    found: list[tuple[tuple[str, ...], _Page]] = []
    page = _as_page(value)
    if page is not None:
        found.append((path, page))
    for key, child in value.items():
        if isinstance(child, dict):
            found.extend(_connection_paths(child, path + (key,)))
    return found


def _one_connection(data: JsonObject) -> tuple[tuple[str, ...], _Page]:
    found = _connection_paths(data, ())
    if len(found) != 1:
        raise RemoteFailure(
            f"pagination needs exactly one connection, found {len(found)}"
        )
    return found[0]


def _read_page(data: JsonObject, path: tuple[str, ...]) -> _Page:
    value: JsonValue = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise RemoteFailure("pagination connection missing on a later page")
        value = value[key]
    page = _as_page(value)
    if page is None:
        raise RemoteFailure("pagination connection missing on a later page")
    return page


def _with_connection(
    payload: JsonObject,
    path: tuple[str, ...],
    nodes: list[JsonValue],
    end_cursor: str | None,
) -> JsonObject:
    cloned = json.loads(json.dumps(payload))
    cursor: JsonValue = cloned["data"]
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            raise RemoteFailure("pagination connection missing on a later page")
        cursor = cursor[key]
    if not isinstance(cursor, dict):
        raise RemoteFailure("pagination connection missing on a later page")
    page_info = cursor.get("pageInfo")
    if not isinstance(page_info, dict):
        raise RemoteFailure("pagination connection missing on a later page")
    cursor["nodes"] = nodes
    page_info["hasNextPage"] = False
    page_info["endCursor"] = end_cursor
    return cloned


def _team_keys(raw: str) -> tuple[str, ...]:
    keys: list[str] = []
    for part in raw.split(","):
        key = part.strip()
        if key and key not in keys:
            keys.append(key)
    if not keys:
        raise LocalInputError("--team needs at least one team key")
    return tuple(keys)


def _instant(text: str, source: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise LocalInputError(
            f"{source} must be an ISO-8601 time with a timezone"
        ) from None
    if parsed.tzinfo is None:
        raise LocalInputError(f"{source} must be an ISO-8601 time with a timezone")
    return parsed.astimezone(UTC)


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _window(since: str | None, state_file: str | None) -> Window:
    if since is not None:
        return Since(_instant(since, "--since"))
    if state_file is None:
        raise LocalInputError("pass --since or --state-file")
    path = Path(state_file).expanduser()
    if not path.exists():
        return Baseline(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LocalInputError(f"cannot read state file: {exc.strerror}") from None
    stripped = text.strip()
    if stripped == "":
        raise LocalInputError("state file is empty")
    return Resume(_instant(stripped, "state file"), path)


def _field_selection(path: tuple[str, ...]) -> str:
    head, *rest = path
    if not rest:
        return head
    return f"{head} {{ {' '.join(rest)} }}"


def _selection(fields: tuple[tuple[str, tuple[str, ...]], ...]) -> str:
    return " ".join(_field_selection(path) for _, path in fields)


def _issues_changed_operation(
    teams: tuple[str, ...],
    since: datetime,
    *,
    until: datetime | None = None,
) -> GraphQLOperation:
    updated = "gt: $since"
    declared = "$after: String, $since: DateTimeOrDuration!"
    variables: JsonObject = {
        "after": None,
        "since": _iso(since),
        "teams": list(teams),
    }
    if until is not None:
        updated = "gt: $since, lte: $until"
        declared = (
            "$after: String, $since: DateTimeOrDuration!, $until: DateTimeOrDuration!"
        )
        variables["until"] = _iso(until)
    document = (
        f"query IssuesChanged({declared}, $teams: [String!]!) {{ "
        f"issues(first: {_ISSUE_PAGE_SIZE}, after: $after, orderBy: updatedAt, "
        f"filter: {{updatedAt: {{{updated}}}, team: {{key: {{in: $teams}}}}}}) {{ "
        f"nodes {{ {_selection(_ISSUE_FIELDS)} }} "
        "pageInfo { hasNextPage endCursor } } }"
    )
    return GraphQLOperation(document, variables)


def _issue_comments_operation(identifier: str, since: datetime) -> GraphQLOperation:
    document = (
        "query IssueComments($id: String!, $after: String, "
        "$since: DateTimeOrDuration!) { "
        "issue(id: $id) { "
        f"comments(first: {_COMMENT_PAGE_SIZE}, after: $after, "
        "filter: {createdAt: {gt: $since}}) { "
        f"nodes {{ {_selection(_COMMENT_FIELDS)} }} "
        "pageInfo { hasNextPage endCursor } } } }"
    )
    return GraphQLOperation(
        document,
        {"id": identifier, "after": None, "since": _iso(since)},
    )


def _at(node: JsonValue, path: tuple[str, ...]) -> JsonValue:
    value = node
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
        if value is None:
            return None
    return value


def _project(
    node: JsonValue, fields: tuple[tuple[str, tuple[str, ...]], ...]
) -> JsonObject:
    if not isinstance(node, dict):
        raise RemoteFailure("issue node was not an object")
    return {name: _at(node, path) for name, path in fields}


def _comment_lines(node: JsonValue) -> list[JsonObject]:
    if not isinstance(node, dict):
        raise RemoteFailure("issue node was not an object")
    raw = node.get("comments")
    if not isinstance(raw, dict):
        raise RemoteFailure("issue comments were missing")
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        raise RemoteFailure("issue comments were missing")
    lines: list[JsonObject] = []
    for item in nodes:
        projected = _project(item, _COMMENT_FIELDS)
        body = projected.get("body")
        if isinstance(body, str):
            projected["body"] = body[:_COMMENT_BODY_LIMIT]
        lines.append(projected)
    return lines


def _issue_line(node: JsonValue, with_comments: bool) -> str:
    projected = _project(node, _ISSUE_FIELDS)
    if with_comments:
        projected["comments"] = _comment_lines(node)
    return json.dumps(projected, ensure_ascii=False, separators=(",", ":"))


def _identifier(node: JsonValue) -> str:
    if not isinstance(node, dict):
        raise RemoteFailure("issue node was not an object")
    identifier = node.get("identifier")
    if not isinstance(identifier, str) or identifier == "":
        raise RemoteFailure("issue identifier was missing")
    return identifier


def _all_comments(
    client: GraphQLClient, operation: GraphQLOperation
) -> list[JsonValue]:
    response = client.paginate(operation)
    if response.has_errors:
        raise GraphQLErrors(response)
    page = _read_page(_data(response), ("issue", "comments"))
    if page.has_next_page:
        raise RemoteFailure("comment page was not complete")
    return list(page.nodes)


def _node_with_comments(
    client: GraphQLClient, node: JsonValue, since: datetime
) -> JsonValue:
    if not isinstance(node, dict):
        raise RemoteFailure("issue node was not an object")
    copied = dict(node)
    copied["comments"] = {
        "nodes": _all_comments(
            client, _issue_comments_operation(_identifier(node), since)
        )
    }
    return copied


def _write_watermark(path: Path, at: datetime) -> None:
    fd, name = tempfile.mkstemp(prefix=".linear-wake-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_iso(at) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except OSError:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _issues_changed(
    client: GraphQLClient,
    command: IssuesChanged,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    now = datetime.now(UTC)
    state_file: Path | None
    match command.window:
        case Baseline(state_file):
            _write_watermark(state_file, now)
            return 0
        case Since(at):
            state_file = None
        case Resume(at, state_file):
            pass
    response = client.paginate(
        _issues_changed_operation(
            command.teams,
            at,
            until=now if state_file is not None else None,
        )
    )
    if response.has_errors:
        return _report(stderr, response)
    page = _read_page(_data(response), ("issues",))
    try:
        nodes = [
            _node_with_comments(client, node, at) if command.with_comments else node
            for node in page.nodes
        ]
    except GraphQLErrors as exc:
        return _report(stderr, exc.response)
    lines = [_issue_line(node, command.with_comments) for node in nodes]
    if lines:
        stdout.write("\n".join(lines) + "\n")
    if state_file is not None:
        _write_watermark(state_file, now)
    return 0


# Links per issue; a card with more is left off the frontier, not half-checked.
_READY_LINK_PAGE: Final[int] = 20
# Russell's decisions live on the BRD team; an undecided BRD blocker holds the work.
_RUSSELL_TEAM: Final[str] = "BRD"
_RUSSELL_DECIDED: Final[frozenset[str]] = frozenset({"Approved", "Adjudicated"})
_READY_QUERY: Final[str] = f"""
query IssuesReady($after: String, $teams: [String!]!) {{
  issues(first: {_ISSUE_PAGE_SIZE}, after: $after, filter: {{
    team: {{ key: {{ in: $teams }} }}
    state: {{ type: {{ eq: "unstarted" }} }}
    assignee: {{ null: true }}
    estimate: {{ null: false }}
  }}) {{
    nodes {{
      identifier title priority estimate url description
      delegate {{ id }}
      labels(first: {_READY_LINK_PAGE}) {{
        nodes {{ name parent {{ name }} }} pageInfo {{ hasNextPage }}
      }}
      inverseRelations(first: {_READY_LINK_PAGE}) {{
        nodes {{
          type issue {{ team {{ key }} state {{ name }} completedAt canceledAt }}
        }}
        pageInfo {{ hasNextPage }}
      }}
      attachments(first: {_READY_LINK_PAGE}) {{
        nodes {{ metadata }} pageInfo {{ hasNextPage }}
      }}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""


def _open(issue: JsonValue) -> bool:
    return _at(issue, ("completedAt",)) is None and _at(issue, ("canceledAt",)) is None


def _gates(blocker: JsonValue) -> bool:
    # A decided BRD card stays in a started state until its work lands, so it no longer gates.
    decided = _at(blocker, ("state", "name")) in _RUSSELL_DECIDED
    return _open(blocker) and not (
        _at(blocker, ("team", "key")) == _RUSSELL_TEAM and decided
    )


def _ready(node: JsonValue) -> bool:
    if not isinstance(node, dict) or node.get("delegate") is not None:
        return False
    description = node.get("description")
    if not isinstance(description, str) or "done when" not in description.lower():
        return False
    if "russell's action" in description.lower():
        return False
    links = ("labels", "inverseRelations", "attachments")
    if any(_at(node, (key, "pageInfo", "hasNextPage")) is not False for key in links):
        return False
    if any(
        _at(label, ("parent", "name")) == "agent"
        for label in _nodes(node.get("labels"))
    ):
        return False
    if any(
        _at(relation, ("type",)) == "blocks" and _gates(_at(relation, ("issue",)))
        for relation in _nodes(node.get("inverseRelations"))
    ):
        return False
    # A merged PR already linked means the card was delivered and only awaits closure.
    return not any(
        _at(attachment, ("metadata", "status")) == "merged"
        for attachment in _nodes(node.get("attachments"))
    )


def _priority_rank(node: JsonValue) -> int:
    priority = _at(node, ("priority",))
    return priority if isinstance(priority, int) and priority > 0 else 5


def _issues_ready(client: GraphQLClient, teams: tuple[str, ...], stdout: TextIO) -> int:
    response = client.paginate(
        GraphQLOperation(_READY_QUERY, {"after": None, "teams": list(teams)})
    )
    if response.has_errors:
        raise GraphQLErrors(response)
    nodes = [n for n in _read_page(_data(response), ("issues",)).nodes if _ready(n)]
    nodes.sort(key=_priority_rank)
    fields = (
        ("identifier", ("identifier",)),
        ("title", ("title",)),
        ("priority", ("priority",)),
        ("estimate", ("estimate",)),
        ("url", ("url",)),
    )
    lines = [
        json.dumps(_project(n, fields), ensure_ascii=False, separators=(",", ":"))
        for n in nodes
    ]
    if lines:
        stdout.write("\n".join(lines) + "\n")
    return 0


_STALE_QUERY: Final[str] = f"""
query IssuesStale($after: String, $teams: [String!]!) {{
  issues(first: {_ISSUE_PAGE_SIZE}, after: $after, filter: {{
    team: {{ key: {{ in: $teams }} }}
    state: {{ type: {{ in: ["unstarted", "started"] }} }}
  }}) {{
    nodes {{
      identifier title estimate url updatedAt
      state {{ name type }}
      delegate {{ id }}
      attachments(first: {_READY_LINK_PAGE}) {{
        nodes {{ url metadata }} pageInfo {{ hasNextPage }}
      }}
      history(first: {_READY_LINK_PAGE}) {{
        nodes {{ createdAt toState {{ name }} }}
      }}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""


def _linked_prs(node: JsonValue) -> list[JsonValue]:
    # A closed-unmerged PR no longer delivers the card, so it does not count as linked.
    return [
        attachment
        for attachment in _nodes(_at(node, ("attachments",)))
        if "/pull/" in str(_at(attachment, ("url",)) or "")
        and _at(attachment, ("metadata", "status")) != "closed"
    ]


def _entered_state(node: JsonValue) -> datetime:
    state = _at(node, ("state", "name"))
    entries = [
        _when(_at(entry, ("createdAt",)))
        for entry in _nodes(_at(node, ("history",)))
        if _at(entry, ("toState", "name")) == state
    ]
    # shortcut: only the first history page is read; past it, updatedAt (later, so
    # it under-flags rather than over-flags). Page history if cards outgrow it.
    return max(entries, default=_updated(node))


def _updated(node: JsonValue) -> datetime:
    return _when(_at(node, ("updatedAt",)))


def _when(value: JsonValue) -> datetime:
    try:
        return _instant(str(value), "Linear timestamp")
    except LocalInputError:
        raise RemoteFailure(f"Linear returned a bad timestamp: {value!r}") from None


def _stale_rules(node: JsonValue, now: datetime) -> list[tuple[str, datetime, float]]:
    """(rule, since, threshold hours) for each rule the card trips."""
    if _at(node, ("attachments", "pageInfo", "hasNextPage")) is not False:
        return []
    prs = len(_linked_prs(node))
    state = _at(node, ("state", "name"))
    hits: list[tuple[str, datetime, float]] = []
    if state == "Claimed":
        hits.append(
            ("claimed-has-pr", _entered_state(node), 0)
            if prs
            else ("claimed-no-pr", _entered_state(node), 24)
        )
    elif state == "In Progress" and not prs:
        hits.append(("in-progress-no-pr", _entered_state(node), 24))
    elif state == "In Review":
        hits.append(("quiet-in-review", _updated(node), 72))
    elif (
        _at(node, ("state", "type")) == "unstarted"
        and _at(node, ("delegate",)) is not None
        and not prs
    ):
        hits.append(("dead-delegate", _updated(node), 24))
    if prs >= 2:
        hits.append(("duplicate-pr", _updated(node), 0))
    return [
        (rule, since, threshold)
        for rule, since, threshold in hits
        if (now - since).total_seconds() / 3600 >= threshold
    ]


def _issues_stale(client: GraphQLClient, teams: tuple[str, ...], stdout: TextIO) -> int:
    response = client.paginate(
        GraphQLOperation(_STALE_QUERY, {"after": None, "teams": list(teams)})
    )
    if response.has_errors:
        raise GraphQLErrors(response)
    now = datetime.now(UTC)
    rows: list[JsonObject] = []
    for node in _read_page(_data(response), ("issues",)).nodes:
        for rule, since, threshold in _stale_rules(node, now):
            row = _project(
                node,
                (
                    ("identifier", ("identifier",)),
                    ("title", ("title",)),
                    ("estimate", ("estimate",)),
                    ("url", ("url",)),
                ),
            )
            hours = (now - since).total_seconds() / 3600 - threshold
            row.update(rule=rule, since=_iso(since), hours_over=round(hours, 1))
            rows.append(row)
    rows.sort(key=lambda row: -(_number(row["estimate"]) or 0))
    if rows:
        stdout.write(
            "\n".join(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                for row in rows
            )
            + "\n"
        )
    return 0


def _shortcut(client: GraphQLClient, command: Shortcut) -> str:
    match command:
        case GetIssue():
            return _issue_json(
                client, command, client.data(_issue_get_operation(command))
            )
        case PostComment(issue=issue, body=body):
            issue_id = _issue_id(issue, client.data(_issue_id_operation(issue)))
            return _comment_url(client.data(_comment_operation(issue_id, body)))
        case MoveIssue():
            move = _move(command, client.data(_move_lookup_operation(command)))
            return _moved_line(move, client.data(_move_operation(move)))
    assert_never(command)


def _issue(identifier: str, data: JsonObject) -> JsonObject:
    match data:
        case {"issue": dict() as issue}:
            return issue
        case {"issue": None}:
            raise RemoteFailure(f"unknown issue {identifier}")
    raise RemoteFailure("issue lookup returned no issue field")


def _issue_id_operation(identifier: str) -> GraphQLOperation:
    return GraphQLOperation(_ISSUE_ID_QUERY, {"id": identifier})


def _issue_id(identifier: str, data: JsonObject) -> _IssueId:
    match _issue(identifier, data):
        case {"id": str(value)} if value:
            return _IssueId(value)
    raise RemoteFailure("issue lookup returned no id")


def _comment_operation(issue: _IssueId, body: str) -> GraphQLOperation:
    return GraphQLOperation(_COMMENT_CREATE, {"issueId": issue.value, "body": body})


def _comment_url(data: JsonObject) -> str:
    match data:
        case {"commentCreate": {"success": True, "comment": {"url": str(url)}}} if url:
            return url
        case {"commentCreate": {"success": False}}:
            raise RemoteFailure("commentCreate returned success: false")
    raise RemoteFailure("commentCreate returned no comment URL")


def _issue_get_operation(command: GetIssue) -> GraphQLOperation:
    return GraphQLOperation(_ISSUE_GET_QUERY, {"id": command.issue})


def _issue_get_comments_operation(issue: str, since: str) -> GraphQLOperation:
    document = (
        "query IssueGetComments($id: String!, $after: String, "
        "$since: DateTimeOrDuration!) { "
        "issue(id: $id) { "
        f"comments(first: {_COMMENT_PAGE_SIZE}, after: $after, "
        "filter: {createdAt: {gte: $since}}) { "
        "nodes { url createdAt body user { name displayName } } "
        "pageInfo { hasNextPage endCursor } } } }"
    )
    return GraphQLOperation(document, {"id": issue, "after": None, "since": since})


def _issue_json(client: GraphQLClient, command: GetIssue, data: JsonObject) -> str:
    issue = _issue(command.issue, data)
    labels: list[str] = []
    for label in _nodes(issue.get("labels")):
        name = _at(label, ("name",))
        if isinstance(name, str):
            labels.append(name)
    shown: JsonObject = {
        "identifier": _required_string(issue, "identifier"),
        "title": _required_string(issue, "title"),
        "description": _text_or_none(issue.get("description")),
        "state": _text_or_none(_at(issue, ("state", "name"))),
        "assignee": _person(issue.get("assignee")),
        "priority": _number(issue.get("priority")),
        "estimate": _number(issue.get("estimate")),
        "cycle": _cycle(issue.get("cycle")),
        "labels": labels,
        "parent": _text_or_none(_at(issue, ("parent", "identifier"))),
        "url": _required_string(issue, "url"),
        "updatedAt": _required_string(issue, "updatedAt"),
    }
    if command.comments_since is not None:
        shown["comments"] = [
            _comment(node)
            for node in _all_comments(
                client,
                _issue_get_comments_operation(command.issue, command.comments_since),
            )
        ]
    return json.dumps(shown, ensure_ascii=False, separators=(",", ":"))


def _required_string(node: JsonObject, key: str) -> str:
    value = node.get(key)
    if isinstance(value, str):
        return value
    raise RemoteFailure(f"issue lookup returned no {key}")


def _text_or_none(value: JsonValue) -> str | None:
    if isinstance(value, str):
        return value
    return None


def _number(value: JsonValue) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _nodes(value: JsonValue) -> list[JsonValue]:
    match value:
        case {"nodes": list(nodes)}:
            return nodes
    return []


def _comment(node: JsonValue) -> JsonObject:
    if not isinstance(node, dict):
        raise RemoteFailure("issue comments returned an unexpected shape")
    return {
        "url": _text_or_none(node.get("url")),
        "createdAt": _text_or_none(node.get("createdAt")),
        "body": _text_or_none(node.get("body")),
        "user": _person(node.get("user")),
    }


def _person(user: JsonValue) -> str | None:
    match user:
        case {"displayName": str(display)} if display:
            return display
        case {"name": str(name)} if name:
            return name
    return None


def _cycle(cycle: JsonValue) -> str | None:
    match cycle:
        case {"name": str(name)} if name:
            return name
        case {"number": int(number)} if not isinstance(number, bool):
            return str(number)
        case {"number": float(number)} if number.is_integer():
            return str(int(number))
        case {"number": float(number)}:
            return str(number)
    return None


def _move_lookup_operation(command: MoveIssue) -> GraphQLOperation:
    return GraphQLOperation(
        _MOVE_LOOKUP_QUERY, {"id": command.issue, "state": command.state}
    )


def _move(command: MoveIssue, data: JsonObject) -> _Move:
    match _issue(command.issue, data):
        case {
            "id": str(issue_id),
            "state": {"name": str(old)},
            "team": {"states": {"nodes": list(states)}},
        }:
            pass
        case _:
            raise RemoteFailure("move lookup returned an unexpected shape")
    match states:
        case [{"id": str(state_id), "name": str(new)}]:
            return _Move(_IssueId(issue_id), state_id, old, new)
        case []:
            raise RemoteFailure(f"unknown state {command.state!r} for {command.issue}")
        case [_, _, *_]:
            raise RemoteFailure(
                f"ambiguous state {command.state!r} for {command.issue}: "
                f"{len(states)} matches"
            )
    raise RemoteFailure("move lookup returned an unexpected shape")


def _move_operation(move: _Move) -> GraphQLOperation:
    return GraphQLOperation(
        _ISSUE_MOVE, {"id": move.issue.value, "stateId": move.state_id}
    )


def _moved_line(move: _Move, data: JsonObject) -> str:
    match data:
        case {"issueUpdate": {"success": True}}:
            return f"{move.old} → {move.new}"
    raise RemoteFailure("issueUpdate returned success: false")


def _report(stderr: TextIO, response: GraphQLResponse) -> int:
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
    _reject_nonfinite(value)
    return value


def _body(path: str, stdin: TextIO) -> str:
    try:
        text = stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise LocalInputError(f"cannot read {path}: {exc.strerror}") from None
    except UnicodeDecodeError:
        raise LocalInputError(f"{path} is not UTF-8") from None
    if not text.strip():
        raise LocalInputError("comment body is empty")
    return text


def _comments_since(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        raise LocalInputError("--comments-since must be an ISO-8601 time") from None
    return raw


def _authorization(credential: Credential) -> str:
    match credential:
        case PersonalApiKey(value):
            return value
        case OAuthAccessToken(value):
            return f"Bearer {value}"
    raise RemoteFailure("unsupported credential")


def _envelope(reply: _HttpReply) -> JsonObject | None:
    if 300 <= reply.status < 400:
        return None
    payload = _json_object(reply.body)
    if payload is None:
        return None
    if reply.status == 200:
        return payload
    errors = payload.get("errors")
    if isinstance(errors, list) and len(errors) > 0:
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


def _reject_nonfinite(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise LocalInputError("--variables must be a JSON object")
    if isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite(item)


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


def _package_version() -> str:
    try:
        return package_version("linear-cli")
    except PackageNotFoundError:
        return _pyproject_version() or "unknown"


def _pyproject_version() -> str | None:
    path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(project, dict):
        return None
    version = project.get("version")
    if isinstance(version, str) and version:
        return version
    return None


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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        raise HTTPError(req.full_url, code, msg, headers, fp)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_OPENER = urllib.request.build_opener(_NoRedirect)


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
        with _OPENER.open(request, timeout=timeout_seconds) as response:
            return _HttpReply(
                status=response.status,
                reason=str(response.reason or ""),
                body=response.read(),
            )
    except IncompleteRead as err:
        raise RemoteFailure(_collapse(str(err), "")) from None
    except HTTPError as err:
        # HTTPError subclasses URLError, so it must be handled before URLError.
        try:
            payload = err.read()
        except (OSError, IncompleteRead) as read_err:
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
