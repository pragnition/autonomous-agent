"""The account pool as the dashboard shows it, from the proxy's /proxy/usage and /proxy/perf (claude_proxy/).

The proxy's shapes are read defensively: a missing or odd field becomes null or zero, never an error, so the
dashboard keeps working while the proxy changes.
"""

from __future__ import annotations

import time

HOURS = (6, 24, 168, 720)
ERROR_KINDS = ("429", "limit", "5xx", "upstream_error")
TOKEN_KINDS = ("in", "out", "cache_read", "cache_write")


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _tokens(value) -> dict:
    v = _d(value)
    return {k: _num(v.get(k)) or 0 for k in TOKEN_KINDS}


def _add(into: dict, more: dict) -> dict:
    for k in TOKEN_KINDS:
        into[k] += more[k]
    return into


def _zero() -> dict:
    return dict.fromkeys(TOKEN_KINDS, 0)


def _window(rec: dict, w: str) -> tuple[float | None, float | None]:
    pair = rec.get(w)
    if isinstance(pair, list) and len(pair) == 2:
        return _num(pair[0]), _num(pair[1])
    return None, None


def _account(code: str, rec: dict, agent_origins: tuple[str, ...]) -> dict:
    util_5h, reset_5h = _window(rec, "5h")
    util_7d, reset_7d = _window(rec, "7d")
    by_origin = {name: _tokens(_d(o).get("tokens_60m")) for name, o in _d(rec.get("by_origin")).items()}
    agent, people = _zero(), _zero()
    for name, tk in by_origin.items():
        _add(agent if name in agent_origins else people, tk)
    errors = _d(_d(rec.get("throughput")).get("60m"))
    latency = _d(_d(rec.get("latency")).get("60m"))
    rank = rec.get("rank")
    return {
        "code": code, "status": rec.get("status") or "", "rank": rank if isinstance(rank, int) else None,
        "verified": bool(rec.get("verified")), "active": _num(rec.get("active")) or 0,
        "util_5h": util_5h, "reset_5h": reset_5h, "util_7d": util_7d, "reset_7d": reset_7d,
        "pace": rec.get("pace") if rec.get("pace") in ("ahead", "on", "behind") else None,
        "target_7d": _num(rec.get("target_7d")), "pace_delta": _num(rec.get("pace_delta")),
        "cap_5h": _num(rec.get("cap_5h")),
        "tokens_60m": _tokens(rec.get("tokens_60m")), "agent_60m": agent, "people_60m": people,
        "by_origin": by_origin,
        "errors_60m": {k: _num(errors.get(k)) or 0 for k in ERROR_KINDS},
        "latency_60m": {"requests": _num(latency.get("requests")) or 0, "ttft_p50": _num(latency.get("ttft_p50")),
                        "ttft_p90": _num(latency.get("ttft_p90")), "tok_s": _num(latency.get("tok_s"))},
    }


def accounts_view(raw: dict | None, agent_origins: tuple[str, ...], error: str | None = None) -> dict:
    if not isinstance(raw, dict):
        return {"available": False, "error": error or "no usage from the proxy", "agent_origins": list(agent_origins),
                "pool": None, "accounts": [], "origins": []}
    accounts = [_account(code, _d(rec), agent_origins) for code, rec in raw.items() if code != "_all"]
    accounts.sort(key=lambda a: (a["rank"] is None, a["rank"] or 0, a["code"]))

    pool_raw = _d(raw.get("_all"))
    pace, burn = _d(pool_raw.get("pace")), _d(pool_raw.get("burn"))
    counts = _d(pace.get("accounts"))
    hourly = [[_num(p[0]), _num(p[1])] for p in burn.get("hourly") or [] if isinstance(p, list) and len(p) == 2]
    pool = {"used_7d": _num(pace.get("used_7d")), "target_7d": _num(pace.get("target_7d")),
            "delta": _num(pace.get("delta")), "state": pace.get("state"),
            "counts": {k: _num(counts.get(k)) or 0 for k in ("ahead", "on", "behind")},
            "unknown": _num(pace.get("unknown")) or 0, "tokens_60m": _tokens(pool_raw.get("tokens_60m")),
            "burn": {"today": _tokens(burn.get("today")), "week": _tokens(burn.get("7d")),
                     "since": _num(burn.get("since"))},
            "hourly": hourly, "session_burn": _num(pool_raw.get("session_burn"))}

    active: dict[str, int] = {}
    for rec in raw.values():
        for name, o in _d(_d(rec).get("by_origin")).items():
            active[name] = active.get(name, 0) + (_num(_d(o).get("active")) or 0)
    origins = [{"name": name, "is_agent": name in agent_origins, "active": active.get(name, 0),
                "tokens_60m": _tokens(_d(o).get("tokens_60m")),
                "requests_60m": _num(_d(_d(_d(o).get("latency")).get("60m")).get("requests")) or 0}
               for name, o in sorted(_d(pool_raw.get("by_origin")).items())]
    return {"available": True, "error": None, "agent_origins": list(agent_origins), "pool": pool,
            "accounts": accounts, "origins": origins}


def bucket_seconds(hours: int) -> int:
    return 300 if hours <= 24 else 3600 if hours <= 168 else 21600


def history_view(rows: list[dict], hours: int, agent_origins: tuple[str, ...], now: float | None = None) -> dict:
    """The proxy's 5-minute perf rows summed into fixed buckets covering the last `hours`, every bucket present."""
    size = bucket_seconds(hours)
    n = hours * 3600 // size
    last = int((now if now is not None else time.time()) // size) * size
    start = last - (n - 1) * size
    buckets = [{"t": start + i * size, "by_account": {}, "by_origin": {}, "agent": _zero(), "people": _zero(),
                "requests": 0, "errors": 0} for i in range(n)]
    accounts, origins = set(), set()
    for row in rows:
        t = _num(row.get("t")) if isinstance(row, dict) else None
        if t is None or not start <= t < last + size:
            continue
        b = buckets[int((t - start) // size)]
        for code, rec in _d(row.get("accounts")).items():
            accounts.add(code)
            _add(b["by_account"].setdefault(code, _zero()), _tokens(_d(rec).get("tokens")))
        for name, rec in _d(row.get("origins")).items():
            origins.add(name)
            tk = _tokens(_d(rec).get("tokens"))
            _add(b["by_origin"].setdefault(name, _zero()), tk)
            _add(b["agent"] if name in agent_origins else b["people"], tk)
        whole = _d(row.get("all"))
        b["requests"] += _num(whole.get("requests")) or 0
        b["errors"] += sum(_num(whole.get(k)) or 0 for k in ERROR_KINDS)
    return {"hours": hours, "bucket_s": size, "accounts": sorted(accounts), "origins": sorted(origins),
            "buckets": buckets}
