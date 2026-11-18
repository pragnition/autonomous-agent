import pytest

from agent import db
from agent.core.sessions import HOLD_SCHEDULED
from agent.core.usage_watch import EMPTY, HOLD, HOT, NORMAL, OK, OUT, UsageWatch, account_state, pool_level
from agent.proxy import ProxyUnavailable

RESET_5H, RESET_7D = 1794571200, 1794906000


def acct(used_5h=0.1, rank=1, pace_delta=-0.1, verified=True, used_7d=0.3):
    return {"5h": [used_5h, RESET_5H], "7d": [used_7d, RESET_7D], "rank": rank, "pace_delta": pace_delta,
            "verified": verified, "status": "allowed" if rank else "rejected"}


class FakeProxy:
    def __init__(self, accounts):
        self.accounts, self.fail = accounts, False

    async def raw_usage(self):
        if self.fail:
            raise ProxyUnavailable("down")
        return {**self.accounts, "_all": {"pace": {}}}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def watch(conn, accounts, now=None):
    alerts, resumed = [], []

    async def alert(text):
        alerts.append(text)

    def resume(item_id):
        resumed.append(item_id)
        conn.execute("UPDATE limit_waits SET resumed_at = 'x' WHERE work_item_id = ?", (item_id,))
        return f"new-{item_id}"

    clock = now or [1794560000.0]
    w = UsageWatch(conn, FakeProxy(accounts), alert=alert, resume=resume, clock=lambda: clock[0])
    return w, alerts, resumed, clock


def event_types(conn):
    return [r[0] for r in conn.execute("SELECT type FROM events ORDER BY id")]


def test_account_states_with_hysteresis():
    assert account_state(acct(), None) == OK
    assert account_state(acct(rank=None), OK) == OUT
    assert account_state(acct(used_5h=0.85), OK) == HOT
    assert account_state(acct(pace_delta=0.06), OK) == HOT          # will run out before its 7-day reset
    assert account_state(acct(used_5h=0.80), OK) == OK
    assert account_state(acct(used_5h=0.80), HOT) == HOT            # leaves hot only below 75%
    assert account_state(acct(used_5h=0.70, pace_delta=0.02), HOT) == HOT   # ...and back on pace
    assert account_state(acct(used_5h=0.70), HOT) == OK
    assert account_state({"rank": 2}, None) == OK                   # missing fields aren't an error


def test_pool_levels():
    assert pool_level(3, 3) == NORMAL and pool_level(2, 4) == NORMAL
    assert pool_level(1, 4) == HOLD and pool_level(0, 4) == EMPTY
    assert pool_level(1, 1) == NORMAL   # a one-account pool doesn't hold its schedules forever


async def test_pool_down_to_one_ok_account_holds_scheduled_runs(conn):
    accounts = {"a": acct(), "b": acct()}
    w, alerts, _, _ = watch(conn, accounts)
    await w.tick()
    assert db.get_setting(conn, HOLD_SCHEDULED) == "0"
    accounts["b"] = acct(used_5h=0.9)
    await w.tick()
    assert db.get_setting(conn, HOLD_SCHEDULED) == "1"
    assert event_types(conn) == ["usage_account", "usage_pool"] and alerts == []
    accounts["b"] = acct(used_5h=0.5)
    await w.tick()
    assert db.get_setting(conn, HOLD_SCHEDULED) == "0"


async def test_empty_pool_alerts_once_with_the_earliest_reset(conn):
    accounts = {"a": acct(rank=None), "b": acct(used_5h=0.95)}
    w, alerts, _, _ = watch(conn, accounts)
    for _ in range(3):
        await w.tick()
    assert len(alerts) == 1 and alerts[0].startswith("Every Claude account is hot or out of capacity")
    assert "Earliest reset: " in alerts[0] and "unknown" not in alerts[0]
    accounts["a"] = acct()
    await w.tick()
    assert alerts[-1] == "Claude capacity is back: 1 account with room."


async def test_limit_cut_turns_resume_once_an_account_is_ok(conn):
    accounts = {"a": acct(rank=None)}
    w, _, resumed, _ = watch(conn, accounts)
    conn.execute("INSERT INTO limit_waits(work_item_id, conversation_id, error, created_at) "
                 "VALUES ('W-1', 'V-1', '429', '2026-11-18T00:00:00Z')")
    await w.tick()
    assert resumed == []
    accounts["a"] = acct()
    await w.tick()
    await w.tick()
    assert resumed == ["W-1"] and "usage_limit_resumed" in event_types(conn)


async def test_unverified_and_out_too_long_alert_once(conn):
    accounts = {"a": acct(), "b": acct()}
    w, alerts, _, clock = watch(conn, accounts)
    await w.tick()
    accounts["a"] = acct(rank=None, verified=False)
    accounts["b"] = acct(rank=None)
    await w.tick()
    assert any("Account a is no longer verified" in t for t in alerts)
    before = len(alerts)
    clock[0] += 5 * 3600 + 60
    await w.tick()
    await w.tick()
    out = [t for t in alerts[before:] if "no capacity for over 5 hours" in t]
    assert len(out) == 1 and out[0].startswith("Account b")   # an unverified account isn't expected to have any


async def test_state_survives_a_restart(conn):
    accounts = {"a": acct(rank=None), "b": acct(rank=None)}
    w, alerts, _, now = watch(conn, accounts)
    await w.tick()
    w2, alerts2, _, _ = watch(conn, accounts, now=now)
    await w2.tick()
    assert len(alerts) == 1 and alerts2 == []   # a restarted core doesn't repeat the alert


async def test_proxy_away_keeps_the_last_state(conn):
    w, alerts, _, _ = watch(conn, {"a": acct(), "b": acct(used_5h=0.9)})
    await w.tick()
    w.proxy.fail = True
    await w.tick()
    assert db.get_setting(conn, HOLD_SCHEDULED) == "1" and alerts == []
