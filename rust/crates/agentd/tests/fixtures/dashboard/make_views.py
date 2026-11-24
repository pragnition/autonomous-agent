"""The dashboard's account and transcript views from Python, for agentd's stage 4 tests. Run from
rust/crates/agentd with PYTHONPATH=<repo>/src; writes views.json and runs.jsonl."""
import json
from pathlib import Path

from agent.dashboard import accounts as a, transcript as t
from agent.proxy import SoftCap

HERE = Path("tests/fixtures/dashboard")
NOW = 1_795_000_000.0
out = {"now": NOW}
origins = ("agent-sandbox", "agent-control")
raw = {
    "alpha": {"status": "ok", "rank": 1, "verified": True, "active": 2, "5h": [0.5, NOW + 3600], "7d": [0.2, NOW + 86400],
              "pace": "ahead", "target_7d": 0.3, "pace_delta": 0.1, "cap_5h": 0.65,
              "tokens_60m": {"in": 10, "out": 20, "cache_read": 30, "cache_write": 4},
              "by_origin": {"agent-sandbox": {"tokens_60m": {"in": 5, "out": 6}, "active": 1, "latency": {"60m": {"requests": 3}}},
                            "laptop": {"tokens_60m": {"in": 1, "out": 2, "cache_read": "x"}, "active": 1}},
              "throughput": {"60m": {"429": 2, "5xx": 1}}, "latency": {"60m": {"requests": 7, "ttft_p50": 0.5, "tok_s": 40}}},
    "beta": {"status": "limited", "rank": None, "verified": False, "5h": [0.9, NOW + 100], "pace": "weird", "rank_x": 1,
             "by_origin": {"agent-control": {"tokens_60m": {"in": 9}}}},
    "gamma": {"status": "ok", "rank": 0, "5h": "bad", "7d": [None, None]},
    "odd": "not a dict",
    "_all": {"pace": {"used_7d": 0.4, "target_7d": 0.5, "delta": -0.1, "state": "behind", "accounts": {"ahead": 1, "on": 1},
                      "unknown": 1},
             "burn": {"today": {"in": 100}, "7d": {"out": 5}, "since": NOW - 7200, "hourly": [[NOW - 3600, 5], [1], "x"]},
             "tokens_60m": {"in": 15}, "session_burn": 0.02,
             "by_origin": {"laptop": {"tokens_60m": {"in": 1}, "latency": {"60m": {"requests": 2}}},
                           "agent-sandbox": {"tokens_60m": {"out": 3}}}},
}
cap = SoftCap(0.65, 5400)
out["accounts"] = {"raw": raw, "origins": list(origins), "soft_cap": [0.65, 5400],
                   "view": a.accounts_view(raw, origins, soft_cap=cap, now=NOW),
                   "missing": a.accounts_view(None, origins, error="proxy unreachable: refused")}
rows = [{"t": NOW - 300 * k, "accounts": {"alpha": {"tokens": {"in": k, "out": 1}}, "beta": {"tokens": {"cache_read": 2}}},
         "origins": {"agent-sandbox": {"tokens": {"in": 1}}, "laptop": {"tokens": {"out": k}}},
         "all": {"requests": 3, "429": 1, "limit": 1, "5xx": 0, "upstream_error": 2}} for k in range(0, 80, 7)]
rows += [{"t": NOW - 7 * 86400}, {"t": "x"}, "not a row", {"t": NOW + 10 ** 6}]
out["history"] = {"rows": rows, "cases": [{"hours": h, "view": a.history_view(rows, h, origins, now=NOW)}
                                          for h in (6, 24, 168, 720)],
                  "bucket_seconds": [[h, a.bucket_seconds(h)] for h in (1, 6, 24, 25, 168, 169, 720)]}

secret = "sk-very-secret-value-123"
records = [
    {"type": "SystemMessage", "subtype": "init", "data": {"session_id": "sid-1"}, "_ts": "2026-11-20T01:00:00Z"},
    {"type": "AssistantMessage", "message_id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                              "cache_read_input_tokens": 100},
     "content": [{"thinking": "hmm"}, {"text": f"I will use {secret}"},
                 {"id": "tu1", "name": "Bash", "input": {"command": "ls -la\nsecond line", "timeout": 5,
                                                         "env": {"K": secret}, "list": ["x" * 2500]}}]},
    {"type": "AssistantMessage", "message_id": "m1", "usage": {"input_tokens": 10, "output_tokens": 5,
                                                              "cache_read_input_tokens": 100},
     "content": [{"id": "tu2", "name": "Read", "input": {"file_path": "/tmp/a.txt"}}, {"id": "tu3", "name": "Skill",
                                                                                      "input": {}}]},
    {"type": "AssistantMessage", "message_id": "m2", "usage": {"input_tokens": 1, "output_tokens": 2,
                                                              "cache_creation_input_tokens": 7, "x": True},
     "content": "not a list"},
    {"type": "UserMessage", "content": [
        {"tool_use_id": "tu1", "is_error": True, "content": [{"type": "text", "text": "out " + secret},
                                                              {"type": "tool_reference", "tool_name": "Grep"},
                                                              {"type": "image"}, {"type": "other", "n": 1}]},
        {"tool_use_id": "tu2", "content": "y" * 5000}, {"text": "a person's words"}]},
    {"type": "UserMessage", "content": "plain text from the user"},
    {"type": "UserMessage", "content": 5},
    {"type": "RateLimitEvent", "rate_limit_info": {"rate_limit_type": "five_hour", "status": "allowed_warning"}},
    {"type": "RateLimitEvent"},
    {"type": "RunUsage", "usage": {"input_tokens": 50, "output_tokens": 60, "cache_read_input_tokens": 70,
                                   "cache_creation_input_tokens": 80}},
    {"type": "AssistantMessage", "truncated": "x" * 30},
    {"type": "Mystery", "value": secret},
    {"type": "ResultMessage", "subtype": "success", "num_turns": 4, "is_error": False, "result": "done " + secret,
     "usage": {"input_tokens": 11, "output_tokens": 22}, "_ts": 5},
]
lines = [json.dumps(r) for r in records]
lines.insert(3, "not json at all")
lines.insert(5, "[1, 2]")
text = "\n".join(lines) + "\n" + '{"type": "UserMessage", "content": "half'
runs = HERE / "runs.jsonl"
runs.write_bytes(text.encode())
scrub = t.make_scrubber([secret, "short"])
entries, offset, line = t.read_from(runs, 0, 0, scrub)
mid = len(("\n".join(lines[:4]) + "\n").encode())
later, offset2, line2 = t.read_from(runs, mid, 4, scrub)
out["transcript"] = {"secret": secret, "entries": entries, "offset": offset, "line": line,
                     "from_mid": {"offset": mid, "entries": later, "end": offset2, "line": line2},
                     "lines_before": [[o, t.lines_before(runs, o)] for o in (0, 1, mid, offset, offset + 5)]}
cache = t.TranscriptCache(scrub, recent=5)
stats, recent = cache.follow(runs)
out["transcript"]["follow"] = {"stats": {"session_id": stats.session_id, "tokens": stats.tokens, "turns": stats.turns,
                                         "done": stats.done}, "recent": recent}
partial = HERE / "partial.jsonl"
partial.write_text(json.dumps(records[1]) + "\n" + json.dumps(records[2]) + "\n")
p_stats, _ = t.TranscriptCache(scrub).follow(partial)
partial.unlink()
out["transcript"]["no_result"] = {"session_id": p_stats.session_id, "tokens": p_stats.tokens, "turns": p_stats.turns,
                                  "done": p_stats.done}
(HERE / "views.json").write_text(json.dumps(out, indent=1) + "\n")
print("ok")
