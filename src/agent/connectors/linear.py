"""Linear: intake of issues labelled with the agent's name, and status sync back."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Awaitable, Callable

import httpx

from agent import tasks
from agent.config import AgentConfig, Pack
from agent.tasks import Task

LABELLED_ISSUES = """
query LabelledIssues($label: String!, $team: String!) {
  issues(first: 50, filter: {labels: {name: {eq: $label}}, team: {key: {eq: $team}},
                             state: {type: {nin: ["completed", "canceled"]}}}) {
    nodes { id identifier title description url creator { email } labels { nodes { name } } }
  }
}"""
TEAM_STATES = """
query TeamStates($team: String!) {
  teams(filter: {key: {eq: $team}}) { nodes { id states { nodes { id name } } } }
}"""
ISSUE_UPDATE = """
mutation IssueUpdate($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: {stateId: $stateId}) { success }
}"""
COMMENT_CREATE = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: {issueId: $issueId, body: $body}) { success }
}"""
AGENT_ACTIVITY = """
mutation AgentActivityCreate($input: AgentActivityCreateInput!) {
  agentActivityCreate(input: $input) { success }
}"""
ISSUE = """
query Issue($id: String!) {
  issue(id: $id) { identifier title description url state { name }
                   comments(first: 30) { nodes { body createdAt user { name } } } }
}"""


class LinearClient:
    """Core's Linear calls. With the agent's Linear app installed, `token` returns its OAuth token and calls show as
    the agent; until then (token returns None) they use the personal API key."""

    def __init__(self, api_key: str, http: httpx.AsyncClient | None = None, url: str = "https://api.linear.app/graphql",
                 token: Callable[[], Awaitable[str | None]] | None = None):
        self.http, self.url, self.api_key, self.token = http or httpx.AsyncClient(timeout=30), url, api_key, token
        self._states: dict[str, dict[str, str]] = {}

    async def _headers(self) -> dict:
        app_token = await self.token() if self.token is not None else None
        auth = f"Bearer {app_token}" if app_token else self.api_key  # personal keys: no "Bearer"
        return {"Authorization": auth, "Content-Type": "application/json"}

    async def _gql(self, operation: str, query: str, variables: dict) -> dict:
        resp = await self.http.post(self.url, headers=await self._headers(),
                                    json={"query": query, "variables": variables, "operationName": operation})
        resp.raise_for_status()
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"Linear {operation}: {data['errors']}")
        return data["data"]

    async def labelled_issues(self, label: str, team_key: str) -> list[dict]:
        nodes = (await self._gql("LabelledIssues", LABELLED_ISSUES, {"label": label, "team": team_key}))["issues"]["nodes"]
        return [{"id": n["id"], "identifier": n["identifier"], "title": n["title"],
                 "description": n.get("description") or "", "url": n.get("url"),
                 "creator_email": (n.get("creator") or {}).get("email"),
                 "labels": [l["name"] for l in (n.get("labels") or {}).get("nodes", [])]} for n in nodes]

    async def _team_states(self, team_key: str) -> dict[str, str]:
        if team_key not in self._states:
            teams = (await self._gql("TeamStates", TEAM_STATES, {"team": team_key}))["teams"]["nodes"]
            self._states[team_key] = {s["name"]: s["id"] for t in teams for s in t["states"]["nodes"]}
        return self._states[team_key]

    async def team_state_names(self, team_key: str) -> set[str]:
        return set(await self._team_states(team_key))

    async def set_state(self, issue_id: str, team_key: str, state_name: str) -> None:
        state_id = (await self._team_states(team_key)).get(state_name)
        if state_id is None:
            raise RuntimeError(f"Linear team {team_key} has no state named {state_name!r}")
        await self._gql("IssueUpdate", ISSUE_UPDATE, {"id": issue_id, "stateId": state_id})

    async def issue(self, issue_id: str) -> dict:
        node = (await self._gql("Issue", ISSUE, {"id": issue_id}))["issue"]
        return {"identifier": node["identifier"], "title": node["title"], "description": node.get("description") or "",
                "url": node.get("url"), "state": (node.get("state") or {}).get("name"),
                "comments": [{"author": (c.get("user") or {}).get("name", "someone"), "body": c["body"],
                              "at": c.get("createdAt")} for c in (node.get("comments") or {}).get("nodes", [])]}

    async def comment(self, issue_id: str, body: str) -> None:
        await self._gql("CommentCreate", COMMENT_CREATE, {"issueId": issue_id, "body": body})

    async def agent_activity(self, session_id: str, type: str, body: str) -> None:
        """An activity on an agent session: thought, response, error or elicitation."""
        await self._gql("AgentActivityCreate", AGENT_ACTIVITY,
                        {"input": {"agentSessionId": session_id, "content": {"type": type, "body": body}}})


_LINK = re.compile(r"^Linear: (https?://\S+)\s*\Z", re.M)


def issue_link(description: str) -> str | None:
    """The issue's URL, from the last line the poller adds to a task's description."""
    found = _LINK.search(description or "")
    return found.group(1) if found else None


class LinearPoller:
    def __init__(self, conn: sqlite3.Connection, client: LinearClient, *, config: AgentConfig,
                 packs: dict[str, Pack], agent_label: str):
        self.conn, self.client, self.config, self.packs, self.label = conn, client, config, packs, agent_label

    def _known(self, issue_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM tasks WHERE linear_issue_id = ? LIMIT 1", (issue_id,)).fetchone() is not None

    async def poll(self) -> list[str]:
        created = []
        for pack in self.packs.values():
            project = pack.project
            for issue in await self.client.labelled_issues(self.label, project.linear_team):
                if self._known(issue["id"]):
                    continue  # one task per issue; follow-ups go through the task's Slack thread
                person = self.config.person_by_linear(issue["creator_email"])
                kind = "bug" if any(l.lower() == "bug" for l in issue["labels"]) else "feature"
                description = f"{issue['description']}\n\nLinear: {issue['url']}".strip()
                task = tasks.create_task(
                    self.conn, project=project.name, title=issue["title"], description=description, kind=kind,
                    source="linear", source_ref=issue["identifier"], requester=person.key if person else None,
                    owner=person.key if person else project.owner, slack_channel=project.slack_channel,
                    linear_issue_id=issue["id"])
                created.append(task.id)
        return created

    async def sync(self, task: Task) -> None:
        pack = self.packs.get(task.project)
        if pack is None or not task.linear_issue_id:
            return
        state = pack.project.linear_status.get(task.status.value)
        if state:
            await self.client.set_state(task.linear_issue_id, pack.project.linear_team, state)
        if task.status.value == "ready_for_review" and task.pr_url:
            await self.client.comment(task.linear_issue_id, f"Draft PR ready for review: {task.pr_url}")
