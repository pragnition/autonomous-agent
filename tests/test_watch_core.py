import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agent import db
from agent.watch import core_collect

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


def test_collect_lists_the_turns_still_running(conn):
    # Whatever their updated_at: a running turn may already have opened a PR the window's search finds.
    since, until = _iso(T0), _iso(T0 + timedelta(hours=1))
    running = _insert_item(conn, conversation_id="V-a", status="claimed", updated_at=_iso(T0 - timedelta(hours=2)))
    _insert_item(conn, conversation_id="V-b", status="queued", updated_at=_iso(T0 + timedelta(minutes=5)))
    _insert_item(conn, conversation_id="V-c", status="done", updated_at=_iso(T0 + timedelta(minutes=5)))

    result = core_collect.collect(conn, since, until)

    assert result["running"] == [{"id": running, "conversation_id": "V-a"}]


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


_AGENTD_TURNS = """CREATE TABLE agentd_turns (work_item_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
    state TEXT NOT NULL, spec TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}', forwarded_seq INTEGER NOT NULL
    DEFAULT 0, result TEXT, started_at TEXT NOT NULL, ended_at TEXT)"""


def test_collect_adds_how_agentd_ran_each_turn(conn):
    # agentd's own record of a turn: how often its session had to start again, and its moves to another account.
    conn.execute(_AGENTD_TURNS)
    since, until = _iso(T0), _iso(T0 + timedelta(hours=1))
    moved = _insert_item(conn, conversation_id="V-a", status="done", updated_at=_iso(T0 + timedelta(minutes=10)))
    plain = _insert_item(conn, conversation_id="V-b", status="done", updated_at=_iso(T0 + timedelta(minutes=20)))
    conn.execute("INSERT INTO agentd_turns(work_item_id, conversation_id, state, spec, progress, started_at) "
                 "VALUES (?, 'V-a', 'reported', '{}', ?, 'x')",
                 (moved, json.dumps({"attempts": 3, "forks": 1, "crash_resumed": True, "fresh_retried": False,
                                     "prompt": "never copied", "session_id": "s"})))

    items = {item["id"]: item for item in core_collect.collect(conn, since, until)["items"]}

    assert items[moved]["agentd"] == {"attempts": 3, "forks": 1, "crash_resumed": True, "fresh_retried": False}
    assert "agentd" not in items[plain]


def test_collect_without_agentd_tables(conn):
    since, until = _iso(T0), _iso(T0 + timedelta(hours=1))
    item = _insert_item(conn, conversation_id="V-a", status="done", updated_at=_iso(T0 + timedelta(minutes=10)))
    assert [i["id"] for i in core_collect.collect(conn, since, until)["items"]] == [item]
