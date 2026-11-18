import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agent import db
from agent.core.sessions import WorkQueue
from agent.watch import core_ask, core_collect

T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.strftime(FMT)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def _insert_item(conn, *, conversation_id, status, updated_at, payload=None, result=None) -> str:
    item_id = f"W-{uuid.uuid4().hex[:12]}"
    created_at = _iso(T0)
    conn.execute(
        "INSERT INTO work_items(id, conversation_id, kind, payload, status, result, delivered_into, "
        "created_at, updated_at) VALUES (?, ?, 'turn', ?, ?, ?, NULL, ?, ?)",
        (item_id, conversation_id, json.dumps(payload or {}), status,
         json.dumps(result) if result is not None else None, created_at, updated_at),
    )
    return item_id


class FakePayloads:
    """Stands in for `agent.core.payloads.Payloads`: `enqueue_turn` only calls `.turn(...)` and never reads the
    convo it's passed, so the fake ignores it and returns the bare text."""

    def __init__(self):
        self.calls = []

    def turn(self, convo, text, actor, *, unattended=False):
        self.calls.append((convo, text, actor, unattended))
        return {"text": text}


# -- core_collect --


def test_collect_window_statuses_and_smoke_excluded(conn):
    since, until = _iso(T0), _iso(T0 + timedelta(hours=1))

    inside = _insert_item(conn, conversation_id="V-a", status="done", updated_at=_iso(T0 + timedelta(minutes=30)))
    _insert_item(conn, conversation_id="V-b", status="claimed", updated_at=_iso(T0 + timedelta(minutes=30)))
    _insert_item(conn, conversation_id="V-c", status="done", updated_at=_iso(T0 - timedelta(minutes=5)))
    _insert_item(conn, conversation_id="V-smoke-x", status="done", updated_at=_iso(T0 + timedelta(minutes=10)))

    result = core_collect.collect(conn, since, until)

    assert {item["id"] for item in result["items"]} == {inside}
    assert result["claimed"] == 1


def test_collect_reduces_payload_and_reads_pool(conn):
    db.set_setting(conn, "usage_watch", json.dumps({"pool": "hold_scheduled"}))
    since, until = _iso(T0), _iso(T0 + timedelta(hours=1))
    payload = {"text": "hi", "actor": "u1", "unattended": True, "source": "slack", "origin": "o",
               "continuation": 1, "project": "sample", "system_prompt": "SECRET SYSTEM PROMPT",
               "resume_session_id": "s1", "test_secrets": {"K": "v"}}
    result_payload = {"text": "pong", "outcome": "done", "error": None, "extra": "drop-me"}
    _insert_item(conn, conversation_id="V-a", status="done", updated_at=_iso(T0 + timedelta(minutes=10)),
                 payload=payload, result=result_payload)

    out = core_collect.collect(conn, since, until)

    assert out["pool"] == "hold_scheduled"
    assert len(out["items"]) == 1
    item_payload = out["items"][0]["payload"]
    assert "system_prompt" not in item_payload and "resume_session_id" not in item_payload \
        and "test_secrets" not in item_payload
    assert item_payload == {"text": "hi", "actor": "u1", "unattended": True, "source": "slack", "origin": "o",
                             "continuation": 1, "project": "sample"}
    assert out["items"][0]["result"] == {"text": "pong", "outcome": "done", "error": None}


def test_collect_pool_defaults_to_normal(conn):
    since, until = _iso(T0), _iso(T0 + timedelta(hours=1))
    assert core_collect.collect(conn, since, until)["pool"] == "normal"


def test_collect_window_is_half_open(conn):
    """`(since, until]`: the instant equal to `since` is excluded, the instant equal to `until` is included."""
    since, until = _iso(T0), _iso(T0 + timedelta(hours=1))

    on_since = _insert_item(conn, conversation_id="V-since", status="done", updated_at=since)
    on_until = _insert_item(conn, conversation_id="V-until", status="done", updated_at=until)

    ids = {item["id"] for item in core_collect.collect(conn, since, until)["items"]}
    assert on_until in ids
    assert on_since not in ids


# -- core_ask --


def test_enqueue_into_existing_and_new_conversation(conn):
    now = db.utcnow()
    conn.execute("INSERT INTO conversations(id, channel, thread_ts, project, is_primary, created_at, "
                 "last_active_at) VALUES ('V-abc', 'C1', '1.0', NULL, 1, ?, ?)", (now, now))
    payloads = FakePayloads()

    item_id = core_ask.enqueue_turn(conn, payloads, "V-abc", "hello")
    item = WorkQueue(conn).get(item_id)
    assert item.conversation_id == "V-abc"
    assert item.payload == {"text": "hello", "source": "improve-jensen", "origin": "improve-jensen review"}
    # payloads.turn(convo, text, actor, *, unattended) — queued as an unattended turn, with no actor.
    _, call_text, call_actor, call_unattended = payloads.calls[-1]
    assert call_text == "hello" and call_actor is None and call_unattended is True

    new_item_id = core_ask.enqueue_turn(conn, payloads, None, "hi")
    new_item = WorkQueue(conn).get(new_item_id)
    assert new_item.conversation_id.startswith("V-review-")
    row = conn.execute("SELECT channel FROM conversations WHERE id = ?", (new_item.conversation_id,)).fetchone()
    assert row["channel"] is None

    with pytest.raises(KeyError):
        core_ask.enqueue_turn(conn, payloads, "V-missing", "x")


def test_enqueue_rolls_back_new_conversation_when_turn_fails(conn):
    class FailingPayloads:
        def turn(self, convo, text, actor, *, unattended=False):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        core_ask.enqueue_turn(conn, FailingPayloads(), None, "hi")

    rows = conn.execute("SELECT id FROM conversations WHERE id LIKE 'V-review-%'").fetchall()
    assert rows == []  # no orphan conversation left behind by the failed turn
