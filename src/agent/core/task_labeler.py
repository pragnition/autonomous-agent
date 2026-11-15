"""The labeler: after the agent answers a request, Haiku says whether it was chat, a new task or the same task as an
earlier one, with a title and a one-line outcome. The agent does nothing for it.

Core calls Haiku through the Claude account proxy, like the sessions do, and only while the proxy has capacity, so
labeling never takes room from real work. A sweep labels every finished request that has no label yet: in arrival
order per requester (a label depends on that person's earlier requests), different requesters side by side. A
request Haiku can't be reached for waits for the next sweep; one it keeps answering badly becomes chat after
MAX_TRIES. Nothing here runs in a turn's path: a result only pokes a sweep in the background.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from agent.core.task_record import (NO_TEXT, Label, RequestContext, apply_label, context_for, count_try,
                                    export_months, month_of, origin_of, pending, project_hints, task_record)
from agent.proxy import PLACEHOLDER_TOKEN

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TRIES = 3
TITLE_CHARS = 60
LABELS = {"chat", "new", "same"}

PROMPT = """You sort the requests a team sends their AI engineer into a record of the work it did. You get one
request, the agent's reply to it, and what came before. Answer with one JSON object and nothing else:

{"label": "chat" | "new" | "same", "task": "<id>" | null, "title": "<3-8 words>" | null,
 "outcome": "<one sentence>" | null, "finished": true | false | null, "project": "<name>" | null}

label:
- "chat": the request asks for no work at all. Only greetings ("hi", "yo"), thanks, small talk, "you alive?", "how's
  it going?", or a question about the agent itself that it answers from what it already knows. When in doubt, it is
  not chat.
- "new": the request asks the agent to do something: build, fix, run, test, check, find out, install, set up, report,
  investigate, or message someone ("ask Sam what's up", "tell the channel"). A different piece of work is "new" even
  in the same conversation and even about the same project. Whether the agent managed it doesn't change the label:
  work it couldn't or wouldn't do is still a task, with finished false.
- "same": the request is about one of the earlier tasks listed below and asks for no different piece of work: a
  follow-up on its progress or result ("so what are the findings??"), a correction, a nudge ("bro I logged you in
  already"), an answer to the agent's question, approval or edits of the agent's draft, or "done" after the person
  did a step the agent asked for. A task from another conversation is "same" only when the request clearly refers
  back to it; a request for different work is "new" even when it is on a similar subject.
task: for "same", the id of that earlier task. Use only an id listed below; if none fits, answer "new".
title: for "new", 3-8 words naming the work, e.g. "Debloat study of the web app codebase".
outcome: for "new" and "same", one sentence on what the agent did or delivered in this reply.
finished: for "new" and "same", whether the agent delivered what was asked. A PR opened, a deploy to staging, a
message sent or a report posted counts as finished even while it waits for review or merge. Not finished: it couldn't
do it, got stuck, gave up, stopped mid-work, or is waiting on input it needs to go on.
project: for "new" and "same", the name of the listed project when the work is on that product itself: its code,
builds, deployments, tests, Linear issues, bugs, users or roadmap. null for general work that belongs to no project,
even when a project is mentioned: messaging or introducing the agent to teammates, the agent's own access, setup,
tools and plugins, questions about the agent, and research or reports about agents or the team rather than the
product. The signals core found point at a project but don't decide it. The project never changes the label: a
general task is still "new" or "same", and installing or setting something up for the agent is a task, not chat.
For "chat", task, title, outcome, finished and project are null."""


class HaikuUnavailable(Exception):
    """The proxy or Anthropic can't answer now (unreachable, a limit, an outage): try again on a later sweep."""


Complete = Callable[[str, str, str], Awaitable[str]]


def make_haiku(base_url: str, http: httpx.AsyncClient | None = None) -> Complete:
    """complete(system, user, session_id) -> Haiku's text, through the proxy at base_url (core's PROXY_URL)."""
    base, client = base_url.rstrip("/"), http or httpx.AsyncClient(timeout=60)

    async def complete(system: str, user: str, session_id: str) -> str:
        try:
            resp = await client.post(f"{base}/v1/messages", headers={
                "authorization": f"Bearer {PLACEHOLDER_TOKEN}", "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20", "x-claude-code-session-id": session_id},
                json={"model": MODEL, "max_tokens": 400, "system": system,
                      "messages": [{"role": "user", "content": user}]})
        except httpx.HTTPError as e:
            raise HaikuUnavailable(f"proxy unreachable: {e}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise HaikuUnavailable(f"proxy answered {resp.status_code}")
        if resp.status_code >= 400:  # a request it will never take: counts as a bad answer, so it ends as chat
            log.warning("Haiku refused a label request: %s %s", resp.status_code, resp.text[:300])
            return ""
        try:
            content = resp.json().get("content") or []
        except ValueError:  # not an answer at all: a bad one, so it ends as chat if it keeps happening
            log.warning("the proxy answered a label request with something that isn't JSON: %s", resp.text[:300])
            return ""
        return "".join(b.get("text", "") for b in content if b.get("type") == "text")

    return complete


def parse_label(text: str) -> Label | None:
    """The one JSON object in Haiku's answer, fences and prose around it allowed."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("label") not in LABELS:
        return None
    text_of = lambda key: data[key].strip() if isinstance(data.get(key), str) and data[key].strip() else None  # noqa: E731
    finished = data.get("finished")
    return Label(data["label"], text_of("task"), text_of("title"), text_of("outcome"),
                 finished if isinstance(finished, bool) else None, text_of("project"))


def _where(project) -> str:
    parts = [f"repo {project.repo}" if getattr(project, "repo", "") else "",
             f"Slack {project.slack_channel}" if getattr(project, "slack_channel", "") else "",
             f"Linear team {project.linear_team}" if getattr(project, "linear_team", "") else ""]
    return ", ".join(p for p in parts if p)


def render(ctx: RequestContext, *, source: str, requester: str | None, projects: dict | None = None,
           hints: list[str] | None = None) -> str:
    """The user message: the projects, what came before, the ids Haiku may use, the request and the reply."""
    lines = ["Projects:"]
    lines += [f"- {name}: {_where(p)}" for name, p in sorted((projects or {}).items())] or \
        ["- none configured (project is always null)"]
    lines.append(f"Signals core found for this request: {', '.join(hints or []) or 'none'}")
    lines.append("Earlier requests in this conversation (oldest first):")
    for e in ctx.earlier:
        what = f"{e['task']} \"{e['title']}\"" if e["task"] else "chat"
        lines.append(f"- [{what}{', ' + e['project'] if e.get('project') else ''}] {e['asked']}")
    if not ctx.earlier:
        lines.append("- none")
    lines.append("This person's other tasks from the last 24 hours:")
    for t in ctx.recent:
        lines.append(f"- {t['task']} \"{t['title']}\" ({t.get('project') or 'general'}; latest: "
                     f"{t['outcome'] or 'no outcome yet'})")
    if not ctx.recent:
        lines.append("- none")
    ids = list(dict.fromkeys([e["task"] for e in ctx.earlier if e["task"]] + [t["task"] for t in ctx.recent]))
    lines += [f"Task ids you may use for \"same\": {', '.join(ids) or 'none'}", "",
              f"The request (from {requester or 'someone'}, via {source}):", ctx.asked, "",
              "The agent's reply" + (" (the turn ended in an error)" if ctx.errored else "") + ":", ctx.reply]
    return "\n".join(lines)


_STOP = object()  # this requester's later requests wait for the next sweep


class TaskLabeler:
    def __init__(self, conn: sqlite3.Connection, config, *, complete: Complete,
                 has_capacity: Callable[[], Awaitable[bool]], tasks_dir: Path, projects: dict | None = None,
                 concurrency: int = 4, max_tries: int = MAX_TRIES):
        self.conn, self.config, self.complete, self.has_capacity = conn, config, complete, has_capacity
        self.projects = projects or {}   # name -> ProjectConfig: what a task's project may be
        self.tasks_dir, self.concurrency, self.max_tries = Path(tasks_dir), concurrency, max_tries
        self._lock = asyncio.Lock()
        self._running: asyncio.Task | None = None
        self._again = False

    def poke(self) -> None:
        """A request was just answered: sweep in the background, once more after the running sweep if there is one."""
        if self._running is not None and not self._running.done():
            self._again = True
            return
        self._running = asyncio.get_running_loop().create_task(self._sweeps())

    async def _sweeps(self) -> None:
        while True:
            self._again = False
            try:
                await self.sweep()
            except Exception:
                log.exception("labeling requests failed")
            if not self._again:
                return

    async def sweep(self) -> int:
        """Label what can be labeled now; how many requests got a label."""
        async with self._lock:
            rows = pending(self.conn)
            if not rows:
                return 0
            if not await self._capacity():
                return 0
            groups = self._groups(rows)
            session, gate, months, labeled = str(uuid.uuid4()), asyncio.Semaphore(self.concurrency), set(), []

            async def run(group: list) -> None:
                async with gate:
                    for row, source, requester, follows in group:
                        task_id = await self._label(row, source, requester, follows, session, months)
                        if task_id is _STOP:
                            return
                        labeled.append(row["id"])
                        if task_id:
                            months.add(month_of(task_record(self.conn, task_id)["started_at"], self.config.timezone))

            # every group finishes before the lock is released, even one that failed
            for failure in await asyncio.gather(*(run(g) for g in groups), return_exceptions=True):
                if isinstance(failure, Exception):
                    log.error("labeling a requester's requests failed", exc_info=failure)
            if months:
                export_months(self.conn, self.tasks_dir, months, self.config.timezone)
            if labeled:
                log.info("labeled %d request(s)", len(labeled))
            return len(labeled)

    def _groups(self, rows: list[sqlite3.Row]) -> list[list]:
        """Requests that can depend on each other, each group in arrival order: a label reads the earlier requests of
        its conversation and its requester's recent tasks, so requests sharing either (or a hand-over's parent's
        conversation) are labeled one after another. Unrelated groups run side by side."""
        parent: dict[str, str] = {}

        def root(key: str) -> str:
            while parent.setdefault(key, key) != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        items = []
        for row in rows:
            source, requester, follows = origin_of(self.conn, self.config, row)
            keys = [f"c:{row['conversation_id']}"] + ([f"r:{requester}"] if requester else [])
            if follows and (up := self.conn.execute("SELECT conversation_id FROM work_items WHERE id = ?",
                                                    (follows,)).fetchone()):
                keys.append(f"c:{up['conversation_id']}")
            for key in keys[1:]:
                parent[root(key)] = root(keys[0])
            items.append((keys[0], (row, source, requester, follows)))
        groups: dict[str, list] = {}
        for key, item in items:
            groups.setdefault(root(key), []).append(item)
        return list(groups.values())

    async def _capacity(self) -> bool:
        """Whether the proxy has room: labeling only runs on capacity real work isn't using."""
        try:
            return await self.has_capacity()
        except Exception:
            log.warning("couldn't ask the proxy for capacity; labeling waits", exc_info=True)
            return False

    async def _label(self, row: sqlite3.Row, source: str, requester: str | None, follows: str | None,
                     session: str, months: set[str]):
        """Label one request: its task id, None for chat, or _STOP when it has to wait. A relabeled request adds the
        month of the task it was in to `months`, so a task it leaves is rewritten too."""
        fixed = None
        if follows:
            parent = self.conn.execute(
                "SELECT w.conversation_id, l.label, l.task_id FROM work_items w LEFT JOIN request_labels l "
                "ON l.work_item_id = w.id WHERE w.id = ?", (follows,)).fetchone()
            if parent is not None and parent["label"] is not None:
                fixed = parent["task_id"]
            elif parent is not None and not (parent["conversation_id"] or "").startswith("V-smoke"):
                return _STOP  # the turn it carries on is still running, or finished and not labeled yet
        if not await self._capacity():
            return _STOP
        ctx = context_for(self.conn, row, requester)
        hints = project_hints(self.conn, row, self.projects)
        offered = {e["task"] for e in ctx.earlier if e["task"]} | {t["task"] for t in ctx.recent}
        try:
            answer = await self.complete(PROMPT, render(ctx, source=source, requester=requester,
                                                        projects=self.projects, hints=hints), session)
        except HaikuUnavailable as e:
            log.info("labeling waits: %s", e)
            return _STOP
        except Exception:  # not the request's fault: it waits for the next sweep without using up a try
            log.exception("asking Haiku to label %s failed", row["id"])
            return _STOP
        before = self.conn.execute("SELECT * FROM request_labels WHERE work_item_id = ?", (row["id"],)).fetchone()
        label = parse_label(answer)
        if label is None:
            tries = count_try(self.conn, row, source=source, requester=requester, follows=follows)
            if tries < self.max_tries:
                return _STOP
            if before is not None and before["label"] is not None:  # a relabel that keeps failing: keep what it was
                log.warning("gave up relabeling %s after %d unreadable answers; it keeps its label", row["id"], tries)
                label = Label(before["label"], before["task_id"], before["title"], before["outcome"],
                              None if before["finished"] is None else bool(before["finished"]), before["project"])
            else:
                log.warning("gave up labeling %s after %d unreadable answers; it counts as chat", row["id"], tries)
                label = Label("same", fixed, None, None, None) if fixed else Label("chat", None, None, None, None)
        elif fixed:
            label = Label("same", fixed, None, label.outcome, label.finished, label.project)
        elif label.label == "same" and label.task not in offered:
            label = Label("new", None, None, label.outcome, label.finished, label.project)
        if label.label == "new" and not label.title:
            title = ctx.asked if ctx.asked != NO_TEXT else "Untitled"
            label = Label("new", None, title[:TITLE_CHARS].strip(), label.outcome, label.finished, label.project)
        label = Label(label.label, label.task, label.title, label.outcome, label.finished,
                      self._project(row, label.project, hints))
        if before is not None and before["task_id"] and (old := task_record(self.conn, before["task_id"])):
            months.add(month_of(old["started_at"], self.config.timezone))
        return apply_label(self.conn, row, label, source=source, requester=requester, follows=follows)

    def _project(self, row: sqlite3.Row, named: str | None, hints: list[str]) -> str | None:
        """Haiku's project when it's a configured one; a name that isn't falls back to what core can see. None stays
        general: general work can happen in a project's channel too."""
        if not named:
            return None
        known = {name.lower(): name for name in self.projects}
        return known.get(named.lower()) or (hints[0] if hints else None)
