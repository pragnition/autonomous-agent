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
                                    export_months, month_of, origin_of, pending, task_record)
from agent.proxy import PLACEHOLDER_TOKEN

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TRIES = 3
TITLE_CHARS = 60
LABELS = {"chat", "new", "same"}

PROMPT = """You sort the requests a team sends their AI engineer into a record of the work it did. You get one
request, the agent's reply to it, and what came before. Answer with one JSON object and nothing else:

{"label": "chat" | "new" | "same", "task": "<id>" | null, "title": "<3-8 words>" | null,
 "outcome": "<one sentence>" | null, "finished": true | false | null}

label:
- "chat": the request asks for no work at all. Only greetings ("hi", "yo"), thanks, small talk, "you alive?", "how's
  it going?", or a question about the agent itself that it answers from what it already knows. When in doubt, it is
  not chat.
- "new": the request asks the agent to do something: build, fix, run, test, check, find out, install, set up, report,
  investigate, or message someone ("ask Sam what's up", "tell the channel"). A different piece of work is "new" even
  in the same conversation and even about the same project.
- "same": the request is about one of the earlier tasks listed below and asks for no different piece of work: a
  follow-up on its progress or result ("so what are the findings??"), a correction, a nudge ("bro I logged you in
  already"), an answer to the agent's question, approval or edits of the agent's draft, or "done" after the person
  did a step the agent asked for.
task: for "same", the id of that earlier task. Use only an id listed below; if none fits, answer "new".
title: for "new", 3-8 words naming the work, e.g. "Debloat study of the web app codebase".
outcome: for "new" and "same", one sentence on what the agent did or delivered in this reply.
finished: for "new" and "same", whether the agent delivered what was asked. A PR opened, a deploy to staging, a
message sent or a report posted counts as finished even while it waits for review or merge. Not finished: it couldn't
do it, got stuck, gave up, stopped mid-work, or is waiting on input it needs to go on.
For "chat", task, title, outcome and finished are null."""


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
                 finished if isinstance(finished, bool) else None)


def render(ctx: RequestContext, *, source: str, requester: str | None) -> str:
    """The user message: what came before, the ids Haiku may use, the request and the reply."""
    lines = ["Earlier requests in this conversation (oldest first):"]
    for e in ctx.earlier:
        what = f"{e['task']} \"{e['title']}\"" if e["task"] else "chat"
        lines.append(f"- [{what}] {e['asked']}")
    if not ctx.earlier:
        lines.append("- none")
    lines.append("This person's other tasks from the last 24 hours:")
    for t in ctx.recent:
        lines.append(f"- {t['task']} \"{t['title']}\" (latest: {t['outcome'] or 'no outcome yet'})")
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
                 has_capacity: Callable[[], Awaitable[bool]], tasks_dir: Path, concurrency: int = 4,
                 max_tries: int = MAX_TRIES):
        self.conn, self.config, self.complete, self.has_capacity = conn, config, complete, has_capacity
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
            groups: dict[str | None, list] = {}
            for row in rows:
                source, requester, follows = origin_of(self.conn, self.config, row)
                groups.setdefault(requester, []).append((row, source, requester, follows))
            session, gate, months, labeled = str(uuid.uuid4()), asyncio.Semaphore(self.concurrency), set(), []

            async def run(group: list) -> None:
                async with gate:
                    for row, source, requester, follows in group:
                        task_id = await self._label(row, source, requester, follows, session)
                        if task_id is _STOP:
                            return
                        labeled.append(row["id"])
                        if task_id:
                            months.add(month_of(task_record(self.conn, task_id)["started_at"], self.config.timezone))

            # every group finishes before the lock is released, even one that failed
            for failure in await asyncio.gather(*(run(g) for g in groups.values()), return_exceptions=True):
                if isinstance(failure, Exception):
                    log.error("labeling a requester's requests failed", exc_info=failure)
            if months:
                export_months(self.conn, self.tasks_dir, months, self.config.timezone)
            if labeled:
                log.info("labeled %d request(s)", len(labeled))
            return len(labeled)

    async def _capacity(self) -> bool:
        """Whether the proxy has room: labeling only runs on capacity real work isn't using."""
        try:
            return await self.has_capacity()
        except Exception:
            log.warning("couldn't ask the proxy for capacity; labeling waits", exc_info=True)
            return False

    async def _label(self, row: sqlite3.Row, source: str, requester: str | None, follows: str | None,
                     session: str):
        """Label one request: its task id, None for chat, or _STOP when it has to wait."""
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
        offered = {e["task"] for e in ctx.earlier if e["task"]} | {t["task"] for t in ctx.recent}
        try:
            answer = await self.complete(PROMPT, render(ctx, source=source, requester=requester), session)
        except HaikuUnavailable as e:
            log.info("labeling waits: %s", e)
            return _STOP
        except Exception:  # not the request's fault: it waits for the next sweep without using up a try
            log.exception("asking Haiku to label %s failed", row["id"])
            return _STOP
        label = parse_label(answer)
        if label is None:
            tries = count_try(self.conn, row, source=source, requester=requester, follows=follows)
            if tries < self.max_tries:
                return _STOP
            log.warning("gave up labeling %s after %d unreadable answers; it counts as chat", row["id"], tries)
            label = Label("same", fixed, None, None, None) if fixed else Label("chat", None, None, None, None)
        elif fixed:
            label = Label("same", fixed, None, label.outcome, label.finished)
        elif label.label == "same" and label.task not in offered:
            label = Label("new", None, None, label.outcome, label.finished)
        if label.label == "new" and not label.title:
            title = ctx.asked if ctx.asked != NO_TEXT else "Untitled"
            label = Label("new", None, title[:TITLE_CHARS].strip(), label.outcome, label.finished)
        return apply_label(self.conn, row, label, source=source, requester=requester, follows=follows)
