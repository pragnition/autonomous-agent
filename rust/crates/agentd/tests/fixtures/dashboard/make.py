"""A seeded database and the Python read models' outputs on it, for agentd's stage 4 tests. Run from
rust/crates/agentd with PYTHONPATH=<repo>/src; writes seed.json (rows per table) and expected.json."""
import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agent import db
from agent.core import kpis
from agent.dashboard import queries as q

HERE = Path("tests/fixtures/dashboard")
NOW = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
TZ = "Asia/Singapore"


def ts(**ago) -> str:
    return (NOW - timedelta(**ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


conn = db.connect(":memory:")
db.create_schema(conn)
linear_event = "Linear agent session event:" + json.dumps({"agentSession": {"issue": {
    "identifier": "SAM-7", "title": "Crash on save", "url": "https://linear.app/x/issue/SAM-7/crash"}}})
rows = {
    "tasks": [
        dict(id="T-0001", project="sample", title="Fix login bug", kind="bug", source="slack", requester="sam",
             status="ready_for_review", pr_number=12, pr_url="https://github.com/o/r/pull/12", created_at=ts(days=3),
             updated_at=ts(days=1)),
        dict(id="T-0002", project="sample", title="Crash on save", kind="bug", source="linear", requester="kim",
             status="in_progress", created_at=ts(days=2), updated_at=ts(days=2)),
    ],
    "conversations": [
        dict(id="C-dm", channel="D111", thread_ts="1700000000.000100", created_at=ts(days=5), last_active_at=ts(hours=1)),
        dict(id="C-thr", channel="C222", thread_ts="1700000001.000200", project="sample", task_id="T-0001",
             created_at=ts(days=3), last_active_at=ts(days=1)),
        dict(id="C-lin", project="sample", task_id="T-0002", created_at=ts(days=2), last_active_at=ts(days=2)),
        dict(id="C-hand", channel="C222", thread_ts="1700000001.000200", is_primary=0, created_at=ts(days=1),
             last_active_at=ts(hours=20)),
        dict(id="C-sched", created_at=ts(hours=30), last_active_at=ts(hours=30)),
    ],
    "settings": [dict(key="linear_session:abc", value="C-lin"), dict(key="paused", value="1")],
}
items = [
    # id, conversation, kind, status, payload, result, created (hours ago), updated (hours ago)
    ("W-1", "C-dm", "turn", "done", {"text": "hi there\nsecond line", "actor": "sam", "system_prompt": "x"},
     {"text": "hello", "outcome": "answered"}, 120, 119),
    ("W-2", "C-dm", "turn", "failed", {"text": "x" * 200, "actor": "sam"}, {"text": "", "outcome": "error"}, 30, 29),
    ("W-3", "C-dm", "turn", "claimed", {"text": "   \n  build the thing  ", "actor": "sam"}, None, 2, 1),
    ("W-4", "C-dm", "turn", "queued", {"text": "and then this", "actor": "sam"}, None, 1, 1),
    ("W-5", "C-thr", "turn", "done", {"text": "please fix login", "actor": "sam"}, {"text": "PR open"}, 72, 70),
    ("W-6", "C-thr", "turn", "delivered", {"text": "also check tests", "actor": "kim"}, None, 24, 24),
    ("W-7", "C-lin", "turn", "done", {"text": linear_event, "actor": "kim"}, {"text": "fixed"}, 48, 47),
    ("W-8", "C-hand", "turn", "cancelled", {"text": "carry on", "actor": "agent"}, None, 20, 19),
    ("W-9", "C-sched", "turn", "done", {"text": "nightly report", "origin": "[schedule nightly]"}, {"text": "ok"},
     30, 29.5),
    ("W-10", None, "turn", "done", {"text": "", "actor": "system"}, None, 3, 2.5),
    ("W-11", "C-sched", "turn", "failed", {"text": "nightly report"}, {"text": "boom"}, 6, 5.9),
]
rows["work_items"] = [dict(id=i, conversation_id=c, kind=k, status=s, payload=json.dumps(p),
                           result=json.dumps(r) if r is not None else None, created_at=ts(hours=a),
                           updated_at=ts(hours=b)) for i, c, k, s, p, r, a, b in items]
rows["runs"] = [
    dict(id="W-1", kind="turn", status="done", account_code="alpha", turns=3, input_tokens=100, output_tokens=50,
         cache_read_tokens=1000, cache_write_tokens=200, outcome="answered", started_at=ts(hours=120),
         ended_at=ts(hours=119)),
    dict(id="W-2", kind="turn", status="failed", account_code="beta", turns=1, input_tokens=10, output_tokens=5,
         error="boom", outcome="error", started_at=ts(hours=30), ended_at=ts(hours=29)),
    dict(id="W-5", task_id="T-0001", kind="turn", status="done", account_code="alpha", turns=7, input_tokens=700,
         output_tokens=900, cache_read_tokens=5000, cache_write_tokens=600, project="sample", started_at=ts(hours=72),
         ended_at=ts(hours=70)),
    dict(id="W-7", kind="turn", status="done", account_code="beta", turns=4, input_tokens=400, output_tokens=300,
         project="sample", started_at=ts(hours=48), ended_at=ts(hours=47)),
    dict(id="W-9", kind="turn", status="done", turns=2, input_tokens=20, output_tokens=2_500_000,
         project="other", started_at=ts(hours=30), ended_at=ts(hours=29.5)),
    dict(id="R-old", kind="turn", status="done", turns=1, input_tokens=1, output_tokens=1, started_at=ts(days=40)),
]
ev = []
for item, hours in (("W-1", 119.9), ("W-1", 119.8), ("W-3", 1.5), ("W-5", 71), ("W-6", 23), ("W-7", 47.9),
                    ("W-9", 29.9)):
    ev.append(dict(ts=ts(hours=hours), type="session_started", actor=None, data=json.dumps({"work_item": item})))
ev += [
    dict(ts=ts(hours=10), type="stop_all", actor="sam", data=json.dumps({"cancelled": []})),
    dict(ts=ts(hours=9), type="resume_all", actor="local", data=None),
    dict(ts=ts(hours=8), type="stop_all", actor="tailscale:pat@example.com",
         data=json.dumps({"cancelled": ["W-8"], "via": "dashboard"})),
    dict(ts=ts(hours=7), type="task_created", task_id="T-0001", actor="sam", data=json.dumps({"x": 1})),
    dict(ts=ts(hours=6), type="task_moved", task_id="T-0002", actor="linear", data="not json"),
    dict(ts=ts(hours=5), type="session_cancelled", actor="kim", data=json.dumps({"work_item": "W-8"})),
    dict(ts=ts(hours=4), type="ask", actor="kim", data=json.dumps({"work_item": "W-7"})),
    dict(ts=ts(hours=3), type="note", actor="kim", data=json.dumps([1, 2])),
    dict(ts=ts(hours=2), type="note", actor="system", data=None),
    dict(ts=ts(hours=1), type="llm_caps_changed", actor="tailscale:pat@example.com", data=json.dumps({"daily_usd": 5})),
]
rows["events"] = ev
rows["task_records"] = []
for tid, src, project, start_h, work, fin in (("TK-0001", "slack", "sample", 1, 600, 1),
                                               ("TK-0002", "slack", None, 9.5, 120, 0),
                                               ("TK-0003", "linear", "sample", 50, 4000, 1),
                                               ("TK-0004", "schedule", None, 100, 30, None),
                                               ("TK-0005", "slack", "other", 10 * 24, 90000, 1),
                                               ("TK-0006", "slack", "sample", 3, 300, 1)):
    record = {"id": tid, "title": f"Task {tid}", "source": src, "project": project, "started_at": ts(hours=start_h)}
    rows["task_records"].append(dict(id=tid, source=src, project=project, requested_by="sam", started_at=ts(hours=start_h),
                                     ended_at=ts(hours=start_h - 0.5), work_time_s=work, finished=fin,
                                     record=json.dumps(record)))
rows["agent_prs"] = [
    dict(repo="o/r", number=12, url="u", title="t", head="sam-7-fix", base="main", opened_at=ts(days=3),
         merged_at=ts(days=1), updated_at=ts(hours=1)),
    dict(repo="o/r", number=13, opened_at=ts(days=20), merged_at=ts(days=15), updated_at=ts(hours=1)),
    dict(repo="o/r", number=14, opened_at=ts(days=1), updated_at=ts(hours=1)),
]
rows["linear_issues"] = [
    dict(issue_id="i1", identifier="SAM-7", handed_at=ts(days=2), state_type="completed", completed_at=ts(hours=12)),
    dict(issue_id="i2", identifier="SAM-8", handed_at=ts(days=2), state_type="started"),
]
rows["llm_usage"] = [
    dict(at=ts(hours=h), upstream=u, model=m, status=s, input_tokens=i, cached_tokens=c, output_tokens=o, cost_usd=usd,
         cache_write_tokens=w)
    for h, u, m, s, i, c, o, usd, w in (
        (1, "foundry", "gpt-5.6-terra", 200, 1000, 200, 300, 0.012, 0),
        (2, "foundry", "gpt-5.6-terra", 500, 0, 0, 0, 0.0, 0),
        (9.9, "foundry", "gpt-6-luna", 200, 5000, 0, 100, 0.03, 10),
        (50, "openai", "o4", 200, 10, 0, 10, 0.001, 0),
        (24 * 25, "foundry", "gpt-5.6-terra", 200, 99, 0, 99, 1.5, 0))]

for table, table_rows in rows.items():
    for r in table_rows:
        conn.execute(f"INSERT INTO {table}({', '.join(r)}) VALUES ({', '.join('?' * len(r))})", tuple(r.values()))

out = {"now": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "tz": TZ}


def plain(v):
    return q._plain(v)


out["kpis"] = []
for days in (1, 7, 30):
    k = kpis.compute_kpis(conn, since=NOW - timedelta(days=days), until=NOW)
    out["kpis"].append({"days": days, "view": q._kpis_dict(k), "text": kpis.format_kpis(k, "Jensen")})
w = kpis.usage_windows(conn, NOW, TZ)
out["usage_windows"] = {"view": plain(w), "text": kpis.format_usage_windows(w)}
out["format_tokens"] = [[n, kpis.format_tokens(n)] for n in (0, 999, 1000, 1500, 999_999, 1_000_000, 2_340_000)]
out["format_duration"] = [[n, kpis.format_duration(n)] for n in (0, 59, 60, 3599, 3600, 3661, 86399, 86400, 200000)]
out["kpi_view"] = [{"days": d, "view": q.kpi_view(conn, d, NOW, TZ)} for d in (1, 7, 30)]

filters = [{}, {"state": "running"}, {"state": "queued"}, {"state": "done"}, {"state": "failed"},
           {"project": "sample"}, {"source": "linear"}, {"source": "handover"}, {"source": "slack"},
           {"source": "other"}, {"account": "alpha"}, {"q": "login"}, {"q": "nightly", "state": "done"},
           {"limit": 3, "offset": 2}, {"limit": 2, "offset": 100}]
out["list_sessions"] = []
out["list_conversations"] = []
for f in filters:
    sf = q.SessionFilter(**f)
    sessions, total = q.list_sessions(conn, sf)
    out["list_sessions"].append({"filter": f, "rows": sessions, "total": total})
    groups, total = q.list_conversations(conn, sf)
    out["list_conversations"].append({"filter": f, "groups": groups, "total": total,
                                      "rows": [q.conversation_row(g, NOW) for g in groups]})
out["get_conversation"] = [{"ref": ref, "out": q.get_conversation(conn, ref)} for ref in ("C-dm", "W-6", "W-10", "nope")]
out["get_session"] = [{"id": i, "out": q.get_session(conn, i)} for i in ("W-7", "W-10", "nope")]
out["overview_counts"] = q.overview_counts(conn, NOW, TZ)
out["paused_by"] = q.paused_by(conn)
out["list_events"] = [{"args": [t, n, b], "out": q.list_events(conn, t, n, b)}
                      for t, n, b in ((None, 100, None), ("stop_all", 100, None), (None, 3, None), (None, 5, 10))]

(HERE / "seed.json").write_text(json.dumps(rows, indent=1) + "\n")
(HERE / "expected.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
print("ok")
