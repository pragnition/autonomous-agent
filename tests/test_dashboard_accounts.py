from agent.dashboard.accounts import HOURS, accounts_view, history_view

AGENT = ("agent-core", "agent-sandbox")


def toks(i=0, o=0, cr=0, cw=0):
    return {"in": i, "out": o, "cache_read": cr, "cache_write": cw}


def thr(n429=0, limit=0, n5xx=0, up=0):
    return {"5m": {"429": 0, "limit": 0, "5xx": 0, "upstream_error": 0, "retry": 0},
            "60m": {"429": n429, "limit": limit, "5xx": n5xx, "upstream_error": up, "retry": 0}}


def lat(requests=0, p50=None, p90=None, tok_s=None):
    return {"5m": {"requests": 0, "ttft_p50": None, "ttft_p90": None, "tok_s": None},
            "60m": {"requests": requests, "ttft_p50": p50, "ttft_p90": p90, "tok_s": tok_s}}


RAW = {
    "harbor": {"5h": [0.0, 1794583800], "7d": [0.03, 1794906000], "status": "allowed", "active": 0, "verified": True,
               "target_7d": 0.439, "pace_delta": -0.409, "pace": "behind", "cap_5h": 5.9, "rank": 4,
               "tokens_60m": toks(), "throughput": thr(), "latency": lat(),
               "by_origin": {"host": {"active": 0, "tokens_60m": toks()},
                             "agent-sandbox": {"active": 0, "tokens_60m": toks()}}},
    "badger": {"5h": [0.06, 1794571200], "7d": [0.55, 1794585600], "status": "allowed", "active": 4,
               "verified": True, "target_7d": 0.6, "pace_delta": -0.05, "pace": "on", "cap_5h": 2.0, "rank": 1,
               "tokens_60m": toks(100, 50, 1000, 200), "throughput": thr(2, 1, 0, 1), "latency": lat(6, 0.77, 1.35, 92),
               "by_origin": {"host": {"active": 1, "tokens_60m": toks(10, 5, 100, 20)},
                             "agent-core": {"active": 0, "tokens_60m": toks(0, 0, 0, 0)},
                             "agent-sandbox": {"active": 3, "tokens_60m": toks(90, 45, 900, 180)}}},
    "comet": {"status": "", "active": 0, "verified": False},
    "_all": {"pace": {"used_7d": 0.201, "target_7d": 0.443, "delta": -0.242, "state": "behind",
                      "accounts": {"ahead": 0, "on": 1, "behind": 7}, "unknown": 2},
             "tokens_60m": toks(100, 50, 1000, 200), "session_burn": 0.0025,
             "burn": {"1m": toks(1, 1, 1, 1), "60m": toks(100, 50, 1000, 200), "today": toks(200, 80, 2000, 300),
                      "today_since": 1794499200.0, "7d": toks(900, 300, 9000, 1000), "since": 1794561000.0,
                      "hourly": [[1794560400.0, 0], [1794564000.0, 160858], [1794567600.0, None]]},
             "by_origin": {"host": {"tokens_60m": toks(10, 5, 100, 20), "latency": lat(1)},
                           "agent-core": {"tokens_60m": toks(), "latency": lat()},
                           "agent-sandbox": {"tokens_60m": toks(90, 45, 900, 180), "latency": lat(5)}}},
}


def test_accounts_sorted_by_rank_nulls_last():
    view = accounts_view(RAW, AGENT)
    assert view["available"] is True and view["error"] is None
    assert [a["code"] for a in view["accounts"]] == ["badger", "harbor", "comet"]


def test_util_and_reset_pairs():
    a = {x["code"]: x for x in accounts_view(RAW, AGENT)["accounts"]}
    assert (a["badger"]["util_5h"], a["badger"]["reset_5h"]) == (0.06, 1794571200)
    assert (a["badger"]["util_7d"], a["badger"]["reset_7d"]) == (0.55, 1794585600)
    assert a["comet"]["util_5h"] is None and a["comet"]["reset_7d"] is None and a["comet"]["rank"] is None
    assert a["badger"]["pace"] == "on" and a["badger"]["active"] == 4 and a["badger"]["cap_5h"] == 2.0


def test_agent_vs_people_split():
    badger = accounts_view(RAW, AGENT)["accounts"][0]
    assert badger["agent_60m"] == toks(90, 45, 900, 180)
    assert badger["people_60m"] == toks(10, 5, 100, 20)
    assert badger["by_origin"]["agent-sandbox"] == toks(90, 45, 900, 180)
    assert badger["errors_60m"] == {"429": 2, "limit": 1, "5xx": 0, "upstream_error": 1}
    assert badger["latency_60m"] == {"requests": 6, "ttft_p50": 0.77, "ttft_p90": 1.35, "tok_s": 92}


def test_pool_and_origins():
    view = accounts_view(RAW, AGENT)
    pool = view["pool"]
    assert pool["used_7d"] == 0.201 and pool["state"] == "behind" and pool["counts"] == {"ahead": 0, "on": 1,
                                                                                         "behind": 7}
    assert pool["burn"]["today"] == toks(200, 80, 2000, 300) and pool["burn"]["week"] == toks(900, 300, 9000, 1000)
    assert pool["hourly"][1] == [1794564000.0, 160858]
    origins = {o["name"]: o for o in view["origins"]}
    assert origins["host"]["is_agent"] is False and origins["agent-sandbox"]["is_agent"] is True
    assert origins["agent-sandbox"]["active"] == 3 and origins["agent-sandbox"]["requests_60m"] == 5
    assert view["agent_origins"] == ["agent-core", "agent-sandbox"]


def test_unavailable_view():
    view = accounts_view(None, AGENT, error="proxy unreachable")
    assert view["available"] is False and view["error"] == "proxy unreachable"
    assert view["accounts"] == [] and view["origins"] == [] and view["pool"] is None


def test_garbage_fields_do_not_raise():
    view = accounts_view({"weird": {"5h": "nope", "tokens_60m": None, "by_origin": [], "throughput": 3},
                          "_all": "x"}, AGENT)
    assert view["accounts"][0]["util_5h"] is None and view["accounts"][0]["tokens_60m"] == toks()


def row(t, accounts, origins, requests=1, n429=0):
    return {"t": t, "end": t + 300, "all": {"requests": requests, "429": n429, "limit": 0, "5xx": 0,
                                            "upstream_error": 0},
            "accounts": {code: {"tokens": tk} for code, tk in accounts.items()},
            "origins": {name: {"tokens": tk} for name, tk in origins.items()}}


def test_history_bucket_sizes():
    assert HOURS == (6, 24, 168, 720)
    assert history_view([], 6, AGENT, now=36000)["bucket_s"] == 300
    assert history_view([], 24, AGENT, now=360000)["bucket_s"] == 300
    assert history_view([], 168, AGENT, now=3600000)["bucket_s"] == 3600
    assert history_view([], 720, AGENT, now=36000000)["bucket_s"] == 21600


def test_history_zero_fills_and_sums_rows_in_bucket():
    now = 100 * 3600
    rows = [row(now - 3 * 3600, {"badger": toks(1, 1)}, {"host": toks(1, 1)}, requests=2, n429=1),
            row(now - 3 * 3600 + 300, {"badger": toks(2, 0), "onyx": toks(5)}, {"agent-sandbox": toks(7)})]
    h = history_view(rows, 168, AGENT, now=now)
    assert h["bucket_s"] == 3600 and len(h["buckets"]) == 168
    ts = [b["t"] for b in h["buckets"]]
    assert ts == sorted(ts) and all(t % 3600 == 0 for t in ts)
    full = next(b for b in h["buckets"] if b["by_account"])
    assert full["by_account"] == {"badger": toks(3, 1), "onyx": toks(5)}
    assert full["requests"] == 3 and full["errors"] == 1
    assert sum(1 for b in h["buckets"] if not b["by_account"]) == 167
    assert h["accounts"] == ["badger", "onyx"] and h["origins"] == ["agent-sandbox", "host"]


def test_history_agent_people_totals():
    now = 100 * 3600
    h = history_view([row(now - 600, {}, {"host": toks(1, 2), "agent-core": toks(3), "agent-sandbox": toks(4)})],
                     6, AGENT, now=now)
    full = next(b for b in h["buckets"] if b["by_origin"])
    assert full["agent"] == toks(7) and full["people"] == toks(1, 2)


def test_history_skips_rows_outside_range_and_bad_rows():
    now = 100 * 3600
    h = history_view([row(now - 7 * 3600, {"badger": toks(1)}, {}), {"t": "x"}, "nope"], 6, AGENT, now=now)
    assert all(not b["by_account"] for b in h["buckets"])
