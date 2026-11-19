import json
import time
from datetime import datetime, timezone

from agent.runner.usage import SessionUsage

SID = "11111111-1111-4111-8111-111111111111"


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reply(mid, at, inp=1, out=10, read=1000, write=50):
    """One record of an API reply in the CLI's session log; the CLI writes one per content block, all with its id."""
    return {"type": "assistant", "timestamp": iso(at), "sessionId": SID,
            "message": {"id": mid, "model": "claude-opus-5-5", "content": [],
                        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                                  "cache_creation_input_tokens": write}}}


def log(home, *records, sid=SID, agent=None, raw=""):
    folder = home / "projects" / "-var-agent-scratch-V-1"
    path = folder / sid / "subagents" / f"agent-{agent}.jsonl" if agent else folder / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write("".join(json.dumps(r) + "\n" for r in records) + raw)
    return path


def test_every_reply_of_the_session_and_its_subagents_counts_once(tmp_path):
    now = time.time()
    log(tmp_path, reply("m1", now, out=3), reply("m1", now, out=12), {"type": "user", "timestamp": iso(now)},
        reply("m2", now + 1, read=2000), raw="not json\n")
    log(tmp_path, reply("a1", now + 2, inp=2, out=5, read=300, write=7), agent="a1")
    usage = SessionUsage(tmp_path)
    usage.watch(SID, now)
    assert usage.poll() == (1 + 1 + 2, 12 + 10 + 5, 1000 + 2000 + 300, 50 + 50 + 7)


def test_replies_from_before_the_run_began_are_left_out(tmp_path):
    began = time.time()
    log(tmp_path, reply("old", began - 60), reply("new", began + 1))
    log(tmp_path, reply("old-agent", began - 30), agent="a1")
    usage = SessionUsage(tmp_path)
    usage.watch(SID, began)
    assert usage.poll() == (1, 10, 1000, 50)


def test_each_poll_reads_on_from_the_last(tmp_path):
    now = time.time()
    path = log(tmp_path, reply("m1", now))
    usage = SessionUsage(tmp_path)
    usage.watch(SID, now)
    assert usage.poll() == (1, 10, 1000, 50)
    partial = json.dumps(reply("m3", now + 2))
    log(tmp_path, reply("m2", now + 1), raw=partial[:40])
    assert usage.poll() == (2, 20, 2000, 100)  # a line still being written waits for its end
    with path.open("a") as f:
        f.write(partial[40:] + "\n")
    log(tmp_path, reply("a1", now + 3), agent="a1")  # an agent that started since
    assert usage.poll() == (4, 40, 4000, 200)
    assert usage.poll() == (4, 40, 4000, 200)


def test_every_session_the_run_used_counts(tmp_path):
    now = time.time()
    other = "22222222-2222-4222-8222-222222222222"
    log(tmp_path, reply("m1", now))
    log(tmp_path, reply("m2", now + 5), sid=other)
    usage = SessionUsage(tmp_path)
    usage.watch(SID, now)
    usage.watch(other, now + 4)
    assert usage.poll() == (2, 20, 2000, 100)


def test_nothing_known_without_a_log(tmp_path):
    usage = SessionUsage(tmp_path)
    usage.watch(SID, time.time())
    assert usage.poll() is None
    assert SessionUsage(None).poll() is None
