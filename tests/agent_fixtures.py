"""Shared fakes and builders for the core tests (tools, conversations, payloads)."""

from __future__ import annotations

import uuid
from pathlib import Path

from agent import db
from agent.config import Pack, ProjectConfig, load_agent_config

AGENT_TOML = """name = "agent"

[people.ann]
slack = "U1"
github = "ann-gh"
linear = "ann@example.com"
role = "admin"

[people.bob]
slack = "U2"
github = "bob-gh"
linear = "bob@example.com"
role = "member"

[cli.azure]
command = "az"
login = ["az", "login", "--use-device-code"]
login_check = ["az", "account", "show"]
"""


def make_config(tmp_path: Path, extra: str = ""):
    path = tmp_path / "agent.toml"
    path.write_text(AGENT_TOML + extra)
    return load_agent_config(path)


def make_pack(name: str = "sample", **over) -> Pack:
    fields = dict(name=name, repo="o/r", default_branch="main", owner="ann", slack_channel="C-SAMPLE",
                  linear_team="SAM", linear_status={"working": "In Progress", "ready_for_review": "In Review",
                                                    "merged": "Done", "failed": "Todo"},
                  secrets_worker=["SAMPLE_TEST_USER"])
    fields.update(over)
    return Pack(ProjectConfig(**fields))


class FakeSlack:
    def __init__(self):
        self.posts, self.updates, self.reactions, self.ephemerals, self.uploads, self.opened = [], [], [], [], [], []
        self.replies: list[dict] = []
        self.unreacted: list[dict] = []
        self.history: list[dict] = []  # a channel's top-level messages, each with a ts
        self._ts = 100

    async def chat_postMessage(self, **kw):
        self._ts += 1
        self.posts.append(kw)
        return {"channel": kw.get("channel"), "ts": f"{self._ts}.0"}

    async def chat_update(self, **kw):
        self.updates.append(kw)
        return {"ok": True}

    async def reactions_add(self, **kw):
        self.reactions.append(kw)

    async def reactions_remove(self, **kw):
        self.unreacted.append(kw)

    async def chat_postEphemeral(self, **kw):
        self.ephemerals.append(kw)

    async def conversations_replies(self, **kw):
        return {"messages": self.replies[: kw.get("limit", 50)]}

    async def conversations_history(self, **kw):
        """Newest first, like Slack; `oldest` is exclusive unless `inclusive` is set."""
        oldest = float(kw.get("oldest") or 0)
        keep = [m for m in self.history
                if float(m["ts"]) > oldest or (kw.get("inclusive") and float(m["ts"]) == oldest)]
        return {"messages": sorted(keep, key=lambda m: float(m["ts"]), reverse=True)[: kw.get("limit", 100)]}

    async def conversations_open(self, **kw):
        self.opened.append(kw)
        return {"channel": {"id": f"D-{kw['users']}"}}

    async def files_upload_v2(self, **kw):
        self.uploads.append(kw)
        return {"ok": True}


class FakeNotifier:
    def __init__(self):
        self.changed, self.asked = [], []

    async def task_changed(self, task):
        self.changed.append((task.id, task.status.value))

    async def ask(self, task, question):
        self.asked.append((task.id, question))


def add_conversation(conn, *, project: str | None = "sample", channel: str | None = "C1",
                     thread_ts: str | None = "1.0", task_id: str | None = None, is_primary: int = 1,
                     conversation_id: str | None = None) -> str:
    cid = conversation_id or f"V-{uuid.uuid4().hex[:10]}"
    now = db.utcnow()
    conn.execute("INSERT INTO conversations(id, channel, thread_ts, project, task_id, is_primary, created_at, "
                 "last_active_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (cid, channel, thread_ts, project, task_id, is_primary, now, now))
    return cid


def add_item(queue, conversation_id: str, *, actor: str | None = "ann", unattended: bool = False,
             kind: str = "turn", text: str = "hi", **extra) -> str:
    return queue.enqueue(conversation_id, kind, {"text": text, "actor": actor, "unattended": unattended, **extra})
