"""The task record and its labels: Python's whole labeling sweep over a scripted scenario, with a stand-in for Haiku
that answers by the request's text. Run from rust/crates/agentd with PYTHONPATH=<repo>/src; writes labels.json."""
import asyncio
import json
import tempfile
from pathlib import Path

from agent import db
from agent.config import load_agent_config, load_packs
from agent.core import task_labeler as tl, task_record as tr

HERE = Path("tests/fixtures/dashboard")
config_dir = Path("../../../examples/config")
config = load_agent_config(config_dir / "agent.toml")
projects = {name: pack.project for name, pack in load_packs(config_dir, config).items()}

linear = "Linear agent session event:" + json.dumps({
    "action": "created", "agentSession": {"id": "s1", "creator": {"email": "alice@example.com", "name": "Alice"},
                                          "issue": {"identifier": "SAM-7", "title": "Crash on save"},
                                          "comment": {"body": "please look"}}})
linear_stranger = "Linear agent session event:" + json.dumps({
    "action": "prompted", "agentActivity": {"user": {"email": "x@else.com", "name": "Xu"}, "content": {"body": "and now?"}},
    "agentSession": {"id": "s2", "issue": {"identifier": "SAM-9", "title": ""}}})
convos = [("C1", "D1", None), ("C2", "C2", "sample"), ("C3", None, None), ("V-smoke-1", None, None), ("C4", None, None)]
items = [
    # id, conversation, status, created, updated, payload, result
    ("W01", "C1", "done", "2026-11-18T01:00:00Z", "2026-11-18T01:01:00Z",
     {"text": "hi", "source": "slack", "requester": "sam", "origin": "[DM from sam https://slack.example/p1]"},
     {"text": "hello"}),
    ("W02", "C1", "done", "2026-11-18T02:00:00Z", "2026-11-18T02:30:00Z",
     {"text": "build the login page", "source": "slack", "requester": "sam",
      "origin": "[DM https://slack.example/p2 more https://slack.example/p3]"},
     {"text": "Opened https://github.com/example-org/agent-sample/pull/5 and https://linear.app/ex/issue/SAM-3/x"}),
    ("W03", "C1", "done", "2026-11-18T02:20:00Z", "2026-11-18T02:40:00Z",
     {"text": "status?", "source": "slack", "requester": "sam"}, {"text": "half done"}),
    ("W04", "C2", "failed", "2026-11-18T03:00:00Z", "2026-11-18T03:05:00Z",
     {"text": "fix SAM-12 crash in example-org/agent-sample", "actor": "alice"}, {"text": "", "outcome": "error"}),
    ("W05", "C3", "done", "2026-11-18T04:00:00Z", "2026-11-18T05:00:00Z", {"text": linear}, {"text": "fixed it"}),
    ("W06", "C1", "done", "2026-11-18T06:00:00Z", "2026-11-18T06:10:00Z",
     {"text": "carry on", "continuation": True, "actor": "sam"}, {"text": "carried on"}),
    ("W07", "C4", "done", "2026-11-18T07:00:00Z", "2026-11-18T07:01:00Z",
     {"text": "nightly report", "origin": "[schedule nightly]"}, {"text": "report posted"}),
    ("W08", "V-smoke-1", "done", "2026-11-18T08:00:00Z", "2026-11-18T08:01:00Z", {"text": "smoke"}, {"text": "ok"}),
    ("W09", "C1", "done", "2026-11-18T09:00:00Z", "2026-11-18T09:00:00Z", {"text": "also this"},
     {"text": "", "read_by": "W06"}),
    ("W10", "C1", "queued", "2026-11-18T10:00:00Z", "2026-11-18T10:00:00Z", {"text": "later"}, None),
    ("W11", "C2", "cancelled", "2026-11-18T11:00:00Z", "2026-11-18T11:00:00Z",
     {"text": "never mind the sample thing", "source": "slack", "requester": "alice"}, None),
    ("W12", "C3", "done", "2026-11-18T12:00:00Z", "2026-11-18T12:30:00Z", {"text": linear_stranger}, {"text": "on it"}),
    ("W13", "C1", "done", "2026-11-18T13:00:00Z", "2026-11-18T13:01:00Z",
     {"text": "garbled please", "source": "slack", "requester": "sam"}, {"text": "?"}),
    ("W14", "C2", "done", "2026-11-18T14:00:00Z", "2026-11-18T14:20:00Z",
     {"text": "handover", "source": "slack", "requester": "alice", "follows": "W04"}, {"text": "picked up"}),
    ("W15", "C5", "done", "2026-11-18T15:00:00Z", "2026-11-18T15:01:00Z",
     {"text": "boom", "source": "slack", "requester": "bob"}, {"text": "x"}),
]
answers = {  # what the stand-in answers for each request's text
    "hi": '{"label": "chat"}',
    "build the login page": 'Sure: ```json\n{"label": "new", "title": "Login page", "outcome": "PR opened", '
                            '"finished": true, "project": "Sample"}\n```',
    "status?": '{"label": "same", "task": "TK-0001", "outcome": "reported progress", "finished": false}',
    "fix SAM-12 crash in example-org/agent-sample": '{"label": "new", "title": "", "outcome": "failed", '
                                                   '"finished": false, "project": "nope"}',
    "SAM-7 Crash on save: please look": '{"label": "new", "title": "Fix crash on save", "outcome": "fixed", '
                                        '"finished": true, "project": null}',
    "carry on": '{"label": "new", "title": "ignored", "outcome": "kept going", "finished": true}',
    "nightly report": '{"label": "same", "task": "TK-0999", "outcome": "posted", "finished": true}',
    "SAM-9: and now?": '{"label": "new", "title": "Stranger work", "outcome": "started", "finished": null, '
                       '"project": "sample"}',
    "garbled please": "no json here",
    "handover": '{"label": "chat"}',
    "never mind the sample thing": '{"label": "chat"}',
}
calls = []


async def complete(system: str, user: str, session_id: str) -> str:
    lines = user.split("\n")
    asked = lines[next(i for i, l in enumerate(lines) if l.startswith("The request (")) + 1]
    calls.append({"asked": asked, "user": user})
    if asked == "boom":  # not the request's fault: it waits without using up a try
        raise RuntimeError("stand-in failure")
    return answers[asked]


async def capacity() -> bool:
    return True


def tables(conn) -> dict:
    labels = [dict(r) for r in conn.execute("SELECT * FROM request_labels ORDER BY work_item_id")]
    for r in labels:
        r.pop("labeled_at")
    return {"request_labels": labels,
            "task_records": [dict(r) for r in conn.execute("SELECT * FROM task_records ORDER BY id")]}


async def main() -> dict:
    conn = db.connect(":memory:")
    db.create_schema(conn)
    for cid, channel, project in convos:
        conn.execute("INSERT INTO conversations(id, channel, project, created_at, last_active_at) VALUES (?, ?, ?, ?, ?)",
                     (cid, channel, project, "2026-11-18T00:00:00Z", "2026-11-18T00:00:00Z"))
    conn.execute("INSERT INTO linear_issue_conversations(issue_id, conversation_id) VALUES ('i1', 'C3')")
    for i, c, s, a, u, p, r in items:
        conn.execute("INSERT INTO work_items(id, conversation_id, kind, payload, status, result, created_at, updated_at) "
                     "VALUES (?, ?, 'turn', ?, ?, ?, ?, ?)", (i, c, json.dumps(p), s, json.dumps(r) if r else None, a, u))
    for item, at in (("W02", "2026-11-18T02:05:00Z"), ("W02", "2026-11-18T02:10:00Z"), ("W05", "2026-11-18T04:30:00Z")):
        conn.execute("INSERT INTO events(ts, type, data) VALUES (?, 'session_started', ?)", (at, json.dumps({"work_item": item})))
    out = {"seed": {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
                    for t in ("conversations", "linear_issue_conversations", "work_items", "events")},
           "answers": dict(answers),  # a copy: the relabel step changes one
           "pending": [r["id"] for r in tr.pending(conn)],
           "origins": {r["id"]: list(tr.origin_of(conn, config, r)) for r in tr.pending(conn)},
           "hints": {r["id"]: tr.project_hints(conn, r, projects) for r in tr.pending(conn)}}
    tasks_dir = Path(tempfile.mkdtemp())
    labeler = tl.TaskLabeler(conn, config, complete=complete, has_capacity=capacity, tasks_dir=tasks_dir,
                             projects=projects)
    sweeps = []
    for _ in range(4):  # the unreadable answer is tried 3 times; the hand-over waits for its parent
        sweeps.append(await labeler.sweep())
    out["sweeps"] = sweeps
    out["calls"] = list(calls)  # a copy: the relabel step reuses the list
    out["after_sweeps"] = tables(conn)
    out["files"] = {p.name: p.read_text() for p in sorted(tasks_dir.iterdir())}
    # relabel: the task's first request becomes chat, so the task goes and its follow-ups are labeled again
    row = conn.execute("SELECT * FROM work_items WHERE id = 'W02'").fetchone()
    tr.apply_label(conn, row, tr.Label("chat", None, None, None, None), source="slack", requester="sam", follows=None)
    out["after_relabel"] = tables(conn)
    out["pending_after_relabel"] = [r["id"] for r in tr.pending(conn)]
    # an older labeler's label that Haiku can't relabel: after 3 unreadable answers it keeps its label
    conn.execute("UPDATE request_labels SET version = 1 WHERE work_item_id = 'W05'")
    answers["SAM-7 Crash on save: please look"] = "still not json"
    calls.clear()
    out["relabel_sweeps"] = [await labeler.sweep() for _ in range(3)]
    out["relabel_calls"] = calls
    out["after_relabel_sweeps"] = tables(conn)
    out["parse_label"] = [{"text": t, "out": (lambda l: None if l is None else [l.label, l.task, l.title, l.outcome,
                                                                               l.finished, l.project])(tl.parse_label(t))}
                          for t in ['{"label": "new", "title": " T ", "finished": "yes"}', 'x {"label": "same"} y',
                                    '{"label": "other"}', "{", "", '[1]', '{"label": "chat", "task": "  "}']]
    out["prompt"] = tl.PROMPT
    out["work_seconds"] = [{"in": spans, "out": tr.work_seconds(spans)} for spans in (
        [["2026-11-18T01:00:00Z", "2026-11-18T02:00:00Z"], ["2026-11-18T01:30:00Z", "2026-11-18T03:00:00Z"],
         ["2026-11-18T05:00:00Z", "2026-11-18T05:00:30Z"], [None, "2026-11-18T06:00:00Z"],
         ["2026-11-18T07:00:00Z", "2026-11-18T06:00:00Z"]], [])]
    return out


(HERE / "labels.json").write_text(json.dumps(asyncio.run(main()), indent=1) + "\n")
print("ok")
