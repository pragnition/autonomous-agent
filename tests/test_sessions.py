from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from agent import db
from agent.config import Limits
from agent.core.sessions import Gate, WorkQueue, mint_token, resolve_token, revoke_for_work_item
from agent.proxy import ProxyUnavailable

T0 = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def status_of(conn, item_id):
    return conn.execute("SELECT status FROM work_items WHERE id = ?", (item_id,)).fetchone()[0]


def utc_in(seconds):
    return datetime.now(UTC) + timedelta(seconds=seconds)


class FakeProxy:
    def __init__(self, capacity=True, fail=False):
        self.capacity, self.fail = capacity, fail

    async def usage(self):
        if self.fail:
            raise ProxyUnavailable("down")
        return SimpleNamespace(has_capacity=lambda: self.capacity)


def cfg(sessions=3):
    return SimpleNamespace(limits=Limits(max_concurrent_sessions=sessions))


def test_one_claimed_item_per_conversation(conn):
    q = WorkQueue(conn)
    a = q.enqueue("c1", "turn", {"text": "1"})
    q.enqueue("c1", "turn", {"text": "2"})
    assert q.claim().id == a
    assert q.claim() is None                  # the second message waits for the first
    q.complete(a, {"text": "ok"})
    assert q.claim().payload["text"] == "2"


def test_other_conversations_are_not_blocked(conn):
    q = WorkQueue(conn)
    q.enqueue("c1", "turn", {"text": "1"})
    b = q.enqueue("c2", "turn", {"text": "x"})
    q.claim()
    assert q.claim().id == b


def test_expired_lease_requeues_then_fails(conn):
    q = WorkQueue(conn, lease_s=1, max_attempts=2)
    i = q.enqueue("c", "turn", {})
    q.claim(now=T0)
    assert q.claim(now=T0 + timedelta(seconds=5)).attempts == 1
    assert q.claim(now=T0 + timedelta(seconds=10)) is None
    assert status_of(conn, i) == "failed"


def test_heartbeat_extends_lease_and_reports_cancel(conn):
    q = WorkQueue(conn, lease_s=10)
    i = q.enqueue("c", "turn", {})
    q.claim(now=T0)
    assert q.heartbeat(i, now=T0 + timedelta(seconds=8)) is True
    assert q.claim(now=T0 + timedelta(seconds=15)) is None    # still leased after the heartbeat
    q.cancel(i)
    assert q.heartbeat(i) is False


def test_lost_lease_revokes_the_session_token(conn):
    q = WorkQueue(conn, lease_s=1)
    i = q.enqueue("c", "turn", {})
    q.claim(now=T0)
    tok = mint_token(conn, "session", work_item_id=i, ttl_s=3600)
    q.claim(now=T0 + timedelta(seconds=5))
    assert resolve_token(conn, tok) is None


def test_complete_and_cancel(conn):
    q = WorkQueue(conn)
    a = q.enqueue("c1", "turn", {})
    b = q.enqueue("c2", "turn", {})
    q.claim()
    q.complete(a, {"text": "done"})
    assert q.get(a).status == "done" and q.get(a).result == {"text": "done"}
    q.cancel(b)
    assert status_of(conn, b) == "cancelled"
    q.cancel(a)
    assert status_of(conn, a) == "done"                                      # finished items stay finished


def test_token_revoked_and_expired(conn):
    t = mint_token(conn, "session", work_item_id="w1", ttl_s=60)
    assert resolve_token(conn, t).work_item_id == "w1"
    assert resolve_token(conn, t).kind == "session"
    revoke_for_work_item(conn, "w1")
    assert resolve_token(conn, t) is None
    t2 = mint_token(conn, "session", work_item_id="w2", ttl_s=1)
    assert resolve_token(conn, t2, now=utc_in(5)) is None
    assert resolve_token(conn, "not-a-token") is None


def test_tokens_are_stored_hashed(conn):
    t = mint_token(conn, "runner", ttl_s=60)
    assert conn.execute("SELECT COUNT(*) FROM tokens WHERE hash = ?", (t,)).fetchone()[0] == 0


async def test_gate(conn):
    db.set_setting(conn, "paused", "1")
    assert (await Gate(conn, cfg(), FakeProxy()).can_start()) == (False, "paused")
    db.set_setting(conn, "paused", "0")
    assert (await Gate(conn, cfg(), FakeProxy(capacity=False)).can_start()) == (False, "waiting for capacity")
    assert (await Gate(conn, cfg(), FakeProxy(fail=True)).can_start()) == (False, "waiting for capacity")
    assert (await Gate(conn, cfg(), FakeProxy()).can_start()) == (True, "")


async def test_gate_busy(conn):
    q = WorkQueue(conn)
    q.enqueue("c1", "turn", {})
    q.claim()
    assert (await Gate(conn, cfg(sessions=1), FakeProxy()).can_start()) == (False, "busy")


async def test_stale_claims_are_reaped_by_the_gate(conn):
    q = WorkQueue(conn, lease_s=1)
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    for i in range(3):
        q.enqueue(f"c{i}", "turn", {})
        q.claim(now=long_ago)
    q.enqueue("c9", "turn", {})
    assert (await Gate(conn, cfg(sessions=3), FakeProxy()).can_start()) == (True, "")
    assert q.claim() is not None


def test_cancel_running(conn):
    q = WorkQueue(conn)
    a = q.enqueue("c1", "turn", {})
    b = q.enqueue("c2", "turn", {})
    q.claim()
    assert q.cancel_running() == [a] and q.get(b).status == "queued"


def test_has_claimable(conn):
    q = WorkQueue(conn)
    assert not q.has_claimable()
    q.enqueue("c1", "turn", {})
    q.enqueue("c2", "turn", {})
    assert q.has_claimable()
    q.claim()
    assert q.has_claimable()    # c2's turn is untouched
    q.claim()
    assert not q.has_claimable()


def test_has_claimable_excludes_a_conversations_second_turn(conn):
    q = WorkQueue(conn)
    q.enqueue("c1", "turn", {})
    q.enqueue("c1", "turn", {})
    q.claim()
    assert not q.has_claimable()    # c1 already has a claimed item


def test_has_claimable_includes_an_expired_claim(conn):
    """An expired claim must not make its conversation invisible forever — only a still-live claim blocks —
    otherwise nothing would ever call claim() (whose own reap requeues it) to free the conversation up again."""
    q = WorkQueue(conn)
    q.enqueue("c1", "turn", {})
    q.claim(now=T0)
    q.enqueue("c1", "turn", {})    # the follow-up, queued behind the still-live claim
    assert not q.has_claimable(now=T0 + timedelta(seconds=10))    # lease (90s) still live
    assert q.has_claimable(now=T0 + timedelta(seconds=100))       # lease has expired
