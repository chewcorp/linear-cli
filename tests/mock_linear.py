"""Loopback-only Linear sandbox. Run with: uv run python tests/mock_linear.py."""

import argparse
import itertools
import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from graphql import GraphQLError, build_schema, graphql_sync

# shortcut: only the documented sandbox operations, extend alongside their tests.
SCHEMA = """
type Query {
  viewer: User!
  issue(id: String!): Issue
}
type Mutation {
  commentCreate(input: CommentCreateInput!): CommentPayload!
  issueUpdate(id: String!, input: IssueUpdateInput!): IssuePayload!
}
type User { id: ID!, name: String!, displayName: String! }
type Issue {
  id: ID!, identifier: String!, title: String!, description: String,
  state: WorkflowState!, assignee: User, priority: Int, estimate: Float,
  cycle: Cycle, labels(first: Int): LabelConnection!, parent: Issue,
  url: String!, updatedAt: String!, team: Team!, comments: CommentConnection!
}
type WorkflowState { id: ID!, name: String! }
type Team {
  states(first: Int, filter: WorkflowStateFilter): WorkflowStateConnection!
}
input WorkflowStateFilter { name: StringComparator }
input StringComparator { eq: String }
type WorkflowStateConnection { nodes: [WorkflowState!]! }
type Cycle { name: String, number: Int }
type Label { name: String! }
type LabelConnection { nodes: [Label!]! }
type Comment { url: String!, body: String!, user: User! }
type CommentConnection { nodes: [Comment!]! }
input CommentCreateInput { issueId: String!, body: String! }
type CommentPayload { success: Boolean!, comment: Comment }
input IssueUpdateInput { stateId: String! }
type IssuePayload { success: Boolean! }
"""


def create_server(port: int = 8765) -> HTTPServer:
    schema = build_schema(SCHEMA)
    user = {"id": "local-user", "name": "Local Developer", "displayName": "Local"}
    states = [
        {"id": "todo", "name": "Todo"},
        {"id": "review", "name": "In Review"},
        {"id": "done", "name": "Done"},
    ]
    comments: list[dict] = []
    # Deterministic clock: every successful mutation advances updatedAt a second.
    mutations = itertools.count(1)

    def next_updated_at() -> str:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        stamp = base + timedelta(seconds=next(mutations))
        return stamp.isoformat().replace("+00:00", "Z")

    def team_states(info, first=None, filter=None):
        # An omitted, empty, or null name comparator imposes no constraint.
        comparator = filter.get("name") if isinstance(filter, dict) else None
        eq = comparator.get("eq") if isinstance(comparator, dict) else None
        if first is not None and first < 0:
            raise GraphQLError("first must be non-negative")
        matched = [state for state in states if eq is None or state["name"] == eq]
        return {"nodes": matched[:first]}

    issue = {
        "id": "local-issue",
        "identifier": "LOCAL-1",
        "title": "Try the local Linear sandbox",
        "description": "Synthetic data. Restart the server to reset changes.",
        "state": states[0],
        "priority": 3,
        "estimate": 1,
        "labels": {"nodes": [{"name": "sandbox"}]},
        "url": "https://linear.example/issue/LOCAL-1",
        "updatedAt": "2026-01-01T00:00:00Z",
        "team": {"states": team_states},
        "comments": {"nodes": comments},
    }

    def find_issue(info, id):
        return issue if id in {issue["id"], issue["identifier"]} else None

    def comment_create(info, input):
        if find_issue(info, input["issueId"]) is None:
            raise GraphQLError("Unknown issue")
        comment = {
            "url": f"{issue['url']}#comment-{len(comments) + 1}",
            "body": input["body"],
            "user": user,
        }
        comments.append(comment)
        issue["updatedAt"] = next_updated_at()
        return {"success": True, "comment": comment}

    def issue_update(info, id, input):
        state = next((s for s in states if s["id"] == input["stateId"]), None)
        if find_issue(info, id) is None or state is None:
            raise GraphQLError("Unknown issue or state")
        issue["state"] = state
        issue["updatedAt"] = next_updated_at()
        return {"success": True}

    root = {
        "viewer": user,
        "issue": find_issue,
        "commentCreate": comment_create,
        "issueUpdate": issue_update,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/graphql":
                self.reply(404, {"errors": [{"message": "Use /graphql"}]})
                return
            if self.headers.get("Authorization") not in {
                "local-dev-key",
                "Bearer local-dev-token",
            }:
                self.reply(401, {"errors": [{"message": "Use sandbox credentials"}]})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_048_576:
                    raise ValueError
                payload = json.loads(self.rfile.read(length))
                if (
                    not isinstance(payload, dict)
                    or not isinstance(payload.get("query"), str)
                    or not isinstance(payload.get("variables", {}), dict)
                    or not isinstance(payload.get("operationName", ""), str)
                ):
                    raise TypeError
            except (TypeError, ValueError, UnicodeDecodeError):
                self.reply(
                    400, {"errors": [{"message": "Invalid GraphQL JSON request"}]}
                )
                return
            result = graphql_sync(
                schema,
                payload["query"],
                root_value=root,
                variable_values=payload.get("variables", {}),
                operation_name=payload.get("operationName"),
            )
            self.reply(200, result.formatted)

        def reply(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return HTTPServer(("127.0.0.1", port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with create_server(args.port) as server:
        print(
            f"Local Linear API: http://127.0.0.1:{server.server_port}/graphql",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
