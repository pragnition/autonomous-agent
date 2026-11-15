import json
import logging

import pytest

from agent import db
from agent.core.task_record import (Label, apply_label, context_for, count_try, export_months, month_of,
                                    origin_of, pending, task_record, work_seconds)
from agent_fixtures import make_config


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


def at(hhmm, day=15):
    return f"2026-11-{day:02d}T{hhmm}:00Z"


def item(conn, wid, convo, arrived, *, answered=None, started=None, status="done", text="hi", reply="ok",
         outcome="done", day=15, **payload):
    """A finished turn: queued at `arrived`, picked up at `started`, answered at `answered`."""
    conn.execute("INSERT OR IGNORE INTO conversations(id, channel, thread_ts, created_at, last_active_at) "
                 "VALUES (?, 'D-1', ?, ?, ?)", (convo, convo, at(arrived, day), at(arrived, day)))
    result = None if status == "failed" else json.dumps({"outcome": outcome, "text": reply})
    conn.execute("INSERT INTO work_items(id, conversation_id, kind, payload, status, result, created_at, updated_at) "
                 "VALUES (?, ?, 'turn', ?, ?, ?, ?, ?)",
                 (wid, convo, json.dumps({"text": text, **payload}), status, result, at(arrived, day),
                  at(answered or arrived, day)))
    if started:
        conn.execute("INSERT INTO events(ts, type, data) VALUES (?, 'session_started', ?)",
                     (at(started), json.dumps({"work_item": wid, "queued_at": at(arrived)})))
    return row(conn, wid)


def row(conn, wid):
    return conn.execute("SELECT * FROM work_items WHERE id = ?", (wid,)).fetchone()


def label(conn, r, lab, *, source="slack", requester="ann", follows=None):
    return apply_label(conn, r, lab, source=source, requester=requester, follows=follows)


def new(title, outcome="did it", finished=True):
    return Label("new", None, title, outcome, finished)


def same(task, outcome="did more", finished=True):
    return Label("same", task, None, outcome, finished)


CHAT = Label("chat", None, None, None, None)


def test_work_seconds_merges_overlaps():
    assert work_seconds([(at("10:00"), at("10:10")), (at("10:05"), at("10:20")), (at("11:00"), at("11:01"))]) == 1260
    assert work_seconds([]) == 0


def test_pending_skips_smoke_running_and_labeled(conn):
    item(conn, "W1", "V-a", "10:00")
    item(conn, "W2", "V-smoke-1", "10:01")
    item(conn, "W3", "V-b", "10:02")
    conn.execute("UPDATE work_items SET status = 'claimed' WHERE id = 'W3'")
    item(conn, "W4", "V-c", "10:03", status="failed")
    item(conn, "W5", "V-d", "10:04")
    label(conn, row(conn, "W5"), CHAT)
    assert [r["id"] for r in pending(conn)] == ["W1", "W4"]


def test_a_try_keeps_a_request_pending(conn):
    r = item(conn, "W1", "V-a", "10:00")
    assert count_try(conn, r, source="slack", requester="ann", follows=None) == 1
    assert count_try(conn, r, source="slack", requester="ann", follows=None) == 2
    assert [p["id"] for p in pending(conn)] == ["W1"]


def test_origin_of_reads_new_payloads(conn, config):
    r = item(conn, "W1", "V-a", "10:00", source="schedule", requester="carol", follows="W0")
    assert origin_of(conn, config, r) == ("schedule", "carol", "W0")


def test_origin_of_infers_old_rows(conn, config):
    hook = {"type": "AgentSessionEvent", "action": "created",
            "agentSession": {"id": "S1", "creator": {"name": "Ann A", "email": "ann@example.com"}}}
    rows = [item(conn, "W1", "V-lin", "10:00", text="Linear agent session event:\n" + json.dumps(hook, indent=2)),
            item(conn, "W2", "V-sch", "10:01", actor="ann", origin="[schedule nightly · 0 22 * * * · set up by Ann]"),
            item(conn, "W3", "V-dm", "10:02", actor="ann", origin="[from Ann (ann) · DM · https://s/p1]"),
            item(conn, "W4", "V-dm", "10:30", actor="ann", continuation=1)]
    assert [origin_of(conn, config, r) for r in rows] == [
        ("linear", "ann", None), ("schedule", "ann", None), ("slack", "ann", None), ("slack", "ann", "W3")]


def test_origin_of_a_conversation_linked_to_an_issue_is_linear(conn, config):
    r = item(conn, "W1", "V-lin", "10:00", text="Linear issue SAM-7: Export", actor="bob")
    conn.execute("INSERT INTO linear_issue_conversations(issue_id, conversation_id) VALUES ('i7', 'V-lin')")
    assert origin_of(conn, config, r) == ("linear", "bob", None)


def test_context_has_no_text_marker_for_an_empty_request_and_a_failed_turn(conn):
    r = item(conn, "W1", "V-a", "10:00", status="failed", text="")
    ctx = context_for(conn, r, "ann")
    assert (ctx.asked, ctx.reply, ctx.errored) == ("(no text)", "(no text)", True)


def test_context_reads_a_linear_webhook_as_the_issue_and_the_prompt(conn):
    hook = {"action": "prompted", "agentSession": {"issue": {"identifier": "SAM-7", "title": "Export"}},
            "agentActivity": {"content": {"type": "prompt", "body": "also logout"}}}
    r = item(conn, "W1", "V-lin", "10:00", text="Linear agent session event:\n" + json.dumps(hook))
    assert context_for(conn, r, "ann").asked == "SAM-7 Export: also logout"


def test_context_lists_earlier_requests_and_the_requesters_recent_tasks(conn):
    label(conn, item(conn, "W1", "V-old", "07:00"), new("Debloat study"))                   # ann, 3h earlier
    label(conn, item(conn, "W2", "V-bob", "08:00"), new("Bob's thing"), requester="bob")
    label(conn, item(conn, "W3", "V-old", "06:00", day=14), new("Yesterday's"))             # over 24h ago
    label(conn, item(conn, "W4", "V-a", "09:00", text="hello"), CHAT)
    label(conn, item(conn, "W5", "V-a", "09:30", text="fix login"), new("Fix login"))
    r = item(conn, "W6", "V-a", "10:00", text="so what are the findings??")
    ctx = context_for(conn, r, "ann")
    assert [(e["label"], e["task"]) for e in ctx.earlier] == [("chat", None), ("new", "TK-0004")]
    assert [t["task"] for t in ctx.recent] == ["TK-0001"]


def test_apply_label_builds_a_task_across_requests(conn):
    w1 = item(conn, "W1", "V-a", "10:00", started="10:01", answered="10:11", text="study the codebase",
              origin="[from Ann (ann) · DM · https://slack/p1]")
    assert label(conn, w1, new("Debloat study", "Listed 12 unused modules", False)) == "TK-0001"
    w2 = item(conn, "W2", "V-b", "11:00", started="11:00", answered="11:05", text="so the findings?",
              reply="Posted them: <https://github.com/o/r/pull/7|PR #7> and https://linear.app/ws/issue/SAM-9/x",
              origin="[from Bob (bob) · DM · https://slack/p2]")
    assert label(conn, w2, same("TK-0001", "Posted the findings"), requester="bob") == "TK-0001"
    rec = task_record(conn, "TK-0001")
    assert (rec["title"], rec["source"], rec["requested_by"], rec["followed_up_by"]) == (
        "Debloat study", "slack", "ann", ["bob"])
    assert (rec["requests"], rec["finished"], rec["outcome"]) == (2, True, "Posted the findings")
    assert (rec["started_at"], rec["ended_at"], rec["jensen_time_s"]) == (at("10:00"), at("11:05"), 900)
    assert rec["where"] == ["https://slack/p1", "https://slack/p2"]
    assert rec["links"] == ["https://github.com/o/r/pull/7", "https://linear.app/ws/issue/SAM-9/x"]
    assert rec["steps"][1] == {"at": at("11:00"), "by": "bob", "asked": "so the findings?",
                               "outcome": "Posted the findings", "finished": True, "work_item": "W2"}


def test_time_without_a_pickup_counts_from_arrival(conn):
    label(conn, item(conn, "W1", "V-a", "10:00", answered="10:02"), new("Quick one"))
    assert task_record(conn, "TK-0001")["jensen_time_s"] == 120


def test_chat_makes_no_task(conn):
    assert label(conn, item(conn, "W1", "V-a", "10:00"), CHAT) is None
    assert conn.execute("SELECT COUNT(*) FROM task_records").fetchone()[0] == 0


def test_export_writes_the_touched_month_newest_first(conn, tmp_path):
    label(conn, item(conn, "W1", "V-a", "10:00"), new("First"))
    label(conn, item(conn, "W2", "V-b", "11:00"), new("Second"))
    w3 = item(conn, "W3", "V-c", "11:00")
    conn.execute("UPDATE work_items SET created_at = '2026-10-31T17:00:00Z' WHERE id = 'W3'")
    label(conn, row(conn, "W3"), new("October in UTC, November here"))
    out = tmp_path / "tasks"
    export_months(conn, out, {"2026-11"}, "Asia/Singapore")
    lines = (out / "2026-11.jsonl").read_text().splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["TK-0002", "TK-0001", "TK-0003"]
    assert not (out / "2026-10.jsonl").exists()


def test_month_of_uses_the_timezone():
    assert month_of("2026-10-31T17:00:00Z", "Asia/Singapore") == "2026-11"
    assert month_of("2026-10-31T15:00:00Z", "Asia/Singapore") == "2026-10"


def test_export_into_an_unwritable_folder_logs(conn, tmp_path, caplog):
    label(conn, item(conn, "W1", "V-a", "10:00"), new("First"))
    (tmp_path / "file").write_text("")
    with caplog.at_level(logging.ERROR):
        export_months(conn, tmp_path / "file" / "tasks", {"2026-11"}, "UTC")
    assert "couldn't write the task file for 2026-11" in caplog.text
