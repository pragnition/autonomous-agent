"""Expected outputs of core's pure rules, for agentd's stage 3 tests. Run from rust/crates/agentd with
PYTHONPATH=<repo>/src."""
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import db, tasks
from agent.config import load_agent_config
from agent.connectors.linear import issue_link
from agent.core import conversations as cv, notifier, parking, schedules, spawned, usage_watch
from agent.core.cli_admin import parse_device_code
from agent.core.linear_sessions import linear_requester
from agent.core.slack_markdown import markdown_chunks
from agent.persona import load_persona, render_persona
from agent.proxy import SoftCap
from agent.scrub import scrub
from agent.secrets import load_secrets
from agent.tokens import estimate_tokens

out = {}
config = load_agent_config(Path("../../../examples/config/agent.toml"))

secret = "s3cret-value-0123456789"
out["scrub"] = [{"text": t, "out": scrub(t, [secret, "short", ""])} for t in [
    f"token {secret} and sk-ant-api03-abcdefghijklmnop and ghp_{'a' * 36}",
    "github_pat_" + "b" * 30 + " xoxb-1234567890-abcdef lin_api_" + "c" * 30 + " AKIAABCDEFGHIJKLMNOP",
    "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY----- short stays",
]]

env = Path("tests/fixtures/intake/secrets.env")
env.write_text('# comment\nA=1\nexport B="two words" # c\nC=\'x\' trailing\nD=plain # note\nE=a#b\nnot a line\n F = spaced \n')
out["secrets"] = load_secrets(env)

statuses = [s.value for s in tasks.Status]
conn = db.connect(":memory:")
db.create_schema(conn)
moves = []
for start in statuses:
    for to in statuses:
        conn.execute("DELETE FROM tasks")
        conn.execute("INSERT INTO tasks(id, project, title, kind, source, status, created_at, updated_at) "
                     "VALUES ('T-0001', 'p', 't', 'feature', 'slack', ?, 'x', 'x')", (start,))
        try:
            tasks.transition(conn, "T-0001", tasks.Status(to), actor="a")
            moves.append([start, to, True])
        except tasks.IllegalTransition:
            moves.append([start, to, False])
out["transitions"] = moves

persona = render_persona(load_persona(None), name="agent")
out["persona"] = {"text": persona, "tokens": estimate_tokens(persona)}

out["origin_line"] = [cv.origin_line("DM", who="Sam (sam)", link="https://x/1"), cv.origin_line("Linear SAM-7"),
                      cv.origin_line("#general thread", who="sam")]
out["model_named"] = [{"name": n, "out": list(cv.model_named(config.models, n))} for n in
                      ["opus", "Opus 4.6", "sonnet", "default", "default model", "fable", "haiku 4.5",
                       config.models.default, "gpt-4", "claude"]]
out["use_model"] = [{"text": t, "match": (m.group(1) if (m := cv._USE_MODEL.search(t)) else None),
                     "fable": bool(cv._FABLE.search(t))} for t in
                    ["use opus", "please use sonnet 4.5 for this", "Thanks. Use the API", "don't use opus",
                     "ok, now use default.", "use default settings", "can you use claude-opus-4-6", "I said use fable!",
                     "make the labeler use haiku 4.5"]]
out["unknown_model_reply"] = cv.unknown_model_reply(config.models, " gpt-4 ")
out["failure_reply"] = [{"outcome": o, "error": e, "out": cv.failure_reply(o, e)} for o, e in
                        [("timeout", "no result within 3600 s"), ("timeout", None), ("stalled", "no progress for 1800 s"),
                         ("interrupted", "interrupted"), ("error", None), ("error", "  boom\n")]]
out["limit"] = [{"error": e, "cut": cv.cut_by_limit("error", e), "reply": cv.limit_wait_reply(e)} for e in
                ["429 ['rate limited']", "account lotus is rejected", "Rate-limit hit", "None ['boom']", None]]
out["message_files"] = cv.message_files({"files": [{"id": "F1", "name": "a.png", "mimetype": "image/png", "size": 10},
                                                   {"id": "F2", "mode": "tombstone"}, {"name": "no id"},
                                                   {"id": "F3", "size": "7"}]})

text = "intro line\n" + "\n".join(f"line {i} " + "x" * 30 for i in range(12)) + "\n```\n" + \
       "\n".join(f"code {i} " + "y" * 30 for i in range(10)) + "\n```\nafter\n" + "z" * 300 + "\nend"
out["markdown_chunks"] = [{"limit": n, "out": markdown_chunks(text, n)} for n in (120, 400, 12000)]

tz = ZoneInfo("Asia/Singapore")
now = datetime(2026, 11, 23, 15, 0, tzinfo=tz).timestamp()
out["parse_until"] = {"now": now, "cases": [{"text": t, "out": parking.parse_until(t, now=now, tz=tz)} for t in
                                            ["14:30", "15:00", "15:01", "0:05", "25:00", "12:60", "2026-11-25T10:00",
                                             "2026-11-25T10:00:00+00:00", "tomorrow", " 09:15 "]]}
out["park_span"] = [parking._span(s) for s in (59, 60, 3599, 3600, 3660, 90061, -5)]

sched_text = """
[daily-report]
cron = "0 9 * * 1-5"
channel = "#general"
prompt = "Report"
by = "sam"

[Bad_Name]
cron = "0 9 * * *"
channel = "x"
prompt = "p"
by = "b"

[missing]
cron = "0 9 * * *"
channel = "x"

[never]
cron = "0 0 30 2 *"
channel = "x"
prompt = "p"
by = "b"

[six-fields]
cron = "0 0 9 * * *"
channel = "x"
prompt = "p"
by = "b"

[extra]
cron = "*/15 * * * *"
channel = "sam"
prompt = "p"
by = "b"
link = "https://x"
paused = true
color = "red"

[typed]
cron = "*/15 * * * *"
channel = "x"
prompt = "p"
by = "b"
paused = "yes"

[ok-paused]
cron = "*/15 * * * *"
channel = "sam"
prompt = "p"
by = "b"
paused = true
"""
parsed, errors = schedules.parse(sched_text)
out["schedules"] = {"valid": [s.__dict__ for s in parsed], "errors": errors,
                    "bad_toml": schedules.parse("[x\n")[1]}
when = datetime(2026, 11, 23, 9, 7, 30, tzinfo=tz)
out["cron"] = [{"cron": c, "latest": schedules.latest_slot(c, when).isoformat(),
                "next": [t.isoformat() for t in schedules.next_slots(c, when)]} for c in
               ["0 9 * * 1-5", "*/15 * * * *", "0 0 1 * 1", "30 8 * * *", "7 9 23 11 *", "0 12 * * 0"]]

soft = SoftCap()
t0 = 1_000_000.0
recs = [({"rank": None}, None), ({"rank": 0, "5h": [0.70, t0 + 4 * 3600]}, None),
        ({"rank": 0, "5h": [0.70, t0 + 3600]}, None), ({"rank": 0, "5h": [0.60, t0 + 4 * 3600]}, "hot"),
        ({"rank": 0, "5h": [0.50, t0 + 4 * 3600]}, "hot"), ({"rank": 0, "5h": [0.2, t0 + 4 * 3600], "pace_delta": 0.06}, None),
        ({"rank": 0, "5h": [0.2, t0 + 4 * 3600], "pace_delta": 0.01}, "hot"), ({"rank": 0, "5h": [0.2, None]}, None),
        ({"rank": 1, "5h": [0.9, t0 + 1000]}, "hot")]
out["account_state"] = [{"rec": r, "previous": p, "out": usage_watch.account_state(r, p, now=t0, soft_cap=soft)}
                        for r, p in recs]
out["next_reset"] = [usage_watch.next_reset(r) for r in [{"5h": [0.5, 10], "7d": [1.0, 20]},
                                                          {"5h": [0.5, 10], "7d": [0.5, 20]}, {}]]
out["pool_level"] = [[ok, v, usage_watch.pool_level(ok, v)] for ok, v in [(0, 3), (1, 1), (1, 3), (2, 3), (1, 0)]]

row = {"id": "A-1234abcd", "prompt": "Look into   the flaky\ntest " + "p" * 400}
out["report_text"] = [spawned.report_text(row, s, o, t) for s, o, t in
                      [("done", "done", "found it"), ("failed", "stalled", ""), ("done", "done", "r" * 20_005)]]

task = tasks.Task(id="T-0007", project="p", title="Fix it", description="", kind="bug", source="slack",
                  source_ref=None, requester=None, owner=None, status=tasks.Status.ready_for_review, branch=None,
                  pr_number=None, pr_url="https://github.com/o/r/pull/1", linear_issue_id=None, slack_channel=None,
                  slack_thread_ts=None, status_message_ts=None, created_at="", updated_at="")
out["status_text"] = [notifier.status_text(task), notifier.status_text(
    tasks.Task(**{**task.__dict__, "status": tasks.Status.merged, "pr_url": None}))]
out["issue_link"] = [issue_link(d) for d in ["body\n\nLinear: https://linear.app/x/issue/SAM-7", "no link",
                                             "Linear: https://a\nmore"]]
out["linear_requester"] = [linear_requester(b, config) for b in [
    {"action": "prompted", "agentActivity": {"user": {"email": "nobody@example.com", "name": "Pat"}}},
    {"action": "created", "agentSession": {"creator": {"name": "Robin"}}}, {"action": "created"}]]
out["device_code"] = [parse_device_code(t) for t in [
    "To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code ABCD-1234 to "
    "authenticate.", "nothing here"]]
Path("tests/fixtures/intake/expected.json").write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n")
