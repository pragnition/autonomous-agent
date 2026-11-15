import json

import httpx
import pytest

from agent import db
from agent.core.task_labeler import MODEL, HaikuUnavailable, TaskLabeler, make_haiku, parse_label
from agent.core.task_record import task_record
from agent_fixtures import make_config


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


class FakeHaiku:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    async def __call__(self, system, user, session_id):
        self.calls.append((system, user, session_id))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def answer(label, task=None, title=None, outcome="did it", finished=True):
    return json.dumps({"label": label, "task": task, "title": title, "outcome": outcome, "finished": finished})


def turn(conn, wid, convo, minute, text, *, requester="ann", **extra):
    conn.execute("INSERT OR IGNORE INTO conversations(id, channel, thread_ts, created_at, last_active_at) "
                 "VALUES (?, 'D-1', ?, '2026-11-15T09:00:00Z', '2026-11-15T09:00:00Z')", (convo, convo))
    ts = f"2026-11-15T10:{minute:02d}:00Z"
    payload = {"text": text, "actor": requester, "source": "slack", "requester": requester, **extra}
    conn.execute("INSERT INTO work_items(id, conversation_id, kind, payload, status, result, created_at, updated_at) "
                 "VALUES (?, ?, 'turn', ?, 'done', ?, ?, ?)",
                 (wid, convo, json.dumps(payload), json.dumps({"outcome": "done", "text": "ok"}), ts, ts))


def labeler(conn, tmp_path, haiku, capacity=True):
    async def has_capacity():
        return capacity
    return TaskLabeler(conn, make_config(tmp_path), complete=haiku, has_capacity=has_capacity,
                       tasks_dir=tmp_path / "tasks")


def label_of(conn, wid):
    row = conn.execute("SELECT label, task_id FROM request_labels WHERE work_item_id = ?", (wid,)).fetchone()
    return (row["label"], row["task_id"]) if row else (None, None)


def tries_of(conn, wid):
    row = conn.execute("SELECT tries FROM request_labels WHERE work_item_id = ?", (wid,)).fetchone()
    return row["tries"] if row else 0


def test_parse_label_tolerates_fences_and_prose():
    parsed = parse_label('Sure:\n```json\n{"label": "new", "title": "X", "outcome": "Y", "finished": true}\n```')
    assert (parsed.label, parsed.title, parsed.outcome, parsed.finished) == ("new", "X", "Y", True)
    assert parse_label('{"label": "maybe"}') is None and parse_label("no json") is None
    assert parse_label('{"label": "chat"') is None


async def test_sweep_labels_and_exports(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "study the codebase")
    turn(conn, "W2", "V-b", 1, "hi")
    haiku = FakeHaiku(answer("new", title="Debloat study"), answer("chat", outcome=None, finished=None))
    assert await labeler(conn, tmp_path, haiku).sweep() == 2
    assert (label_of(conn, "W1"), label_of(conn, "W2")) == (("new", "TK-0001"), ("chat", None))
    [line] = (tmp_path / "tasks" / "2026-11.jsonl").read_text().splitlines()
    assert json.loads(line)["title"] == "Debloat study"
    assert "study the codebase" in haiku.calls[0][1] and haiku.calls[0][2] == haiku.calls[1][2]  # one session


async def test_the_request_shows_earlier_requests_and_the_ids_to_use(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "fix the login")
    turn(conn, "W2", "V-a", 1, "also the logout")
    haiku = FakeHaiku(answer("new", title="Fix login"), answer("same", task="TK-0001"))
    await labeler(conn, tmp_path, haiku).sweep()
    user = haiku.calls[1][1]
    assert "fix the login" in user and "TK-0001" in user and "Fix login" in user
    assert label_of(conn, "W2") == ("same", "TK-0001")


async def test_continuation_is_same_as_its_parent_whatever_haiku_says(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "build it")
    turn(conn, "W2", "V-a", 1, "(carry on)", follows="W1")
    haiku = FakeHaiku(answer("new", title="Build it", finished=False), answer("chat", outcome="Built it"))
    await labeler(conn, tmp_path, haiku).sweep()
    assert label_of(conn, "W2") == ("same", "TK-0001")
    assert task_record(conn, "TK-0001")["outcome"] == "Built it"


async def test_unoffered_task_becomes_new_with_a_title_from_the_request(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "can you also check the login page on staging and tell me what is broken there")
    await labeler(conn, tmp_path, FakeHaiku(answer("same", task="TK-0042"))).sweep()
    assert label_of(conn, "W1") == ("new", "TK-0001")
    assert task_record(conn, "TK-0001")["title"] == "can you also check the login page on staging and tell me wha"


async def test_new_without_a_title_takes_the_request(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "run the e2e suite")
    await labeler(conn, tmp_path, FakeHaiku(answer("new", title=""))).sweep()
    assert task_record(conn, "TK-0001")["title"] == "run the e2e suite"


async def test_proxy_down_stops_without_counting_a_try(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "study")
    turn(conn, "W2", "V-a", 1, "more")
    haiku = FakeHaiku(HaikuUnavailable("429"))
    assert await labeler(conn, tmp_path, haiku).sweep() == 0
    assert tries_of(conn, "W1") == 0 and len(haiku.calls) == 1


async def test_three_bad_answers_make_chat(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "study")
    lab = labeler(conn, tmp_path, FakeHaiku("nope", "still no", "{bad json"))
    for _ in range(3):
        await lab.sweep()
    assert label_of(conn, "W1") == ("chat", None)


async def test_no_capacity_makes_no_call(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "study")
    haiku = FakeHaiku()
    assert await labeler(conn, tmp_path, haiku, capacity=False).sweep() == 0 and haiku.calls == []


async def test_one_requesters_failure_doesnt_hold_up_another(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "study", requester="ann")
    turn(conn, "W2", "V-b", 1, "deploy it", requester="bob")
    haiku = FakeHaiku("garbage", answer("new", title="Deploy"))
    await labeler(conn, tmp_path, haiku).sweep()
    assert label_of(conn, "W1") == (None, None) and label_of(conn, "W2") == ("new", "TK-0001")


async def test_a_stuck_request_holds_up_only_its_requesters_later_ones(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "study")
    turn(conn, "W2", "V-c", 1, "and this")
    haiku = FakeHaiku("garbage")
    await labeler(conn, tmp_path, haiku).sweep()
    assert label_of(conn, "W2") == (None, None) and len(haiku.calls) == 1


async def test_poke_runs_a_sweep_in_the_background(conn, tmp_path):
    import asyncio
    turn(conn, "W1", "V-a", 0, "study")
    lab = labeler(conn, tmp_path, FakeHaiku(answer("new", title="Study")))
    lab.poke()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if label_of(conn, "W1")[0]:
            break
    assert label_of(conn, "W1") == ("new", "TK-0001")


async def test_make_haiku_sends_the_proxy_headers():
    seen = {}

    def handler(request):
        seen.update(path=request.url.path, headers=request.headers, body=json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": '{"label": "chat"}'}]})

    complete = make_haiku("http://proxy:1/", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await complete("sys", "user", "sid-1") == '{"label": "chat"}'
    assert seen["path"] == "/v1/messages" and seen["body"]["model"] == MODEL == "claude-haiku-4-5"
    assert seen["body"]["system"] == "sys" and seen["body"]["messages"] == [{"role": "user", "content": "user"}]
    h = seen["headers"]
    assert h["authorization"] == "Bearer sk-ant-oat01-container-via-host-proxy"
    assert (h["anthropic-version"], h["anthropic-beta"], h["x-claude-code-session-id"]) == (
        "2023-06-01", "oauth-2025-04-20", "sid-1")


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_make_haiku_raises_unavailable_on_limits_and_outages(status):
    complete = make_haiku("http://proxy:1", httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json={}))))
    with pytest.raises(HaikuUnavailable):
        await complete("s", "u", "sid")


async def test_make_haiku_raises_unavailable_when_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")
    complete = make_haiku("http://proxy:1", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(HaikuUnavailable):
        await complete("s", "u", "sid")


# review fixes

async def test_a_hand_over_waits_while_its_parent_turn_is_still_running(conn, tmp_path):
    turn(conn, "P1", "V-a", 0, "split this up")
    conn.execute("UPDATE work_items SET status = 'claimed', result = NULL WHERE id = 'P1'")
    turn(conn, "C1", "V-child", 1, "the brief", follows="P1")
    haiku = FakeHaiku(answer("new", title="Split it"), answer("chat", outcome="Did the child part"))
    lab = labeler(conn, tmp_path, haiku)
    assert await lab.sweep() == 0 and haiku.calls == []
    conn.execute("UPDATE work_items SET status = 'done', result = ? WHERE id = 'P1'",
                 (json.dumps({"outcome": "done", "text": "handed it over"}),))
    await lab.sweep()
    assert (label_of(conn, "P1"), label_of(conn, "C1")) == (("new", "TK-0001"), ("same", "TK-0001"))


async def test_an_unexpected_error_neither_counts_a_try_nor_stops_other_requesters(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "study", requester="ann")
    turn(conn, "W2", "V-b", 1, "deploy it", requester="bob")
    haiku = FakeHaiku(RuntimeError("boom"), answer("new", title="Deploy"))
    assert await labeler(conn, tmp_path, haiku).sweep() == 1
    assert (tries_of(conn, "W1"), label_of(conn, "W2")) == (0, ("new", "TK-0001"))


async def test_capacity_is_checked_before_each_call(conn, tmp_path):
    turn(conn, "W1", "V-a", 0, "study")
    turn(conn, "W2", "V-a", 1, "and more")
    answers = iter([True, True, False])

    async def has_capacity():
        return next(answers, False)

    haiku = FakeHaiku(answer("new", title="Study"), answer("same", task="TK-0001"))
    lab = TaskLabeler(conn, make_config(tmp_path), complete=haiku, has_capacity=has_capacity,
                      tasks_dir=tmp_path / "tasks")
    assert await lab.sweep() == 1 and len(haiku.calls) == 1


async def test_make_haiku_treats_a_non_json_body_as_a_bad_answer():
    complete = make_haiku("http://proxy:1", httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>oops</html>"))))
    assert await complete("s", "u", "sid") == ""
