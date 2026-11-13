import json

from agent.dashboard.transcript import live_stats, make_scrubber, parse_record, read_entries

USAGE = {"input_tokens": 4, "cache_creation_input_tokens": 421, "cache_read_input_tokens": 17592, "output_tokens": 24}
INIT = {"type": "SystemMessage", "subtype": "init", "data": {"type": "system", "subtype": "init", "session_id": "s-1"}}


def assistant(content, message_id="msg_1", usage=USAGE):
    return {"type": "AssistantMessage", "content": content, "message_id": message_id, "model": "m", "usage": usage,
            "parent_tool_use_id": None, "error": None, "session_id": "s-1", "stop_reason": None, "uuid": "u"}


def tool_result(tool_use_id, content, is_error=None):
    return {"type": "UserMessage", "content": [{"tool_use_id": tool_use_id, "content": content, "is_error": is_error}],
            "uuid": "u", "parent_tool_use_id": None}


RESULT = {"type": "ResultMessage", "subtype": "success", "num_turns": 5, "total_cost_usd": 0.0762, "is_error": False,
          "result": "Done.", "session_id": "s-1",
          "usage": {"input_tokens": 10, "cache_creation_input_tokens": 6934, "cache_read_input_tokens": 65109,
                    "output_tokens": 384}}


def parse(record, line=0):
    return parse_record(record, line, str)


def test_bash_call_summary_is_command():
    [e] = parse(assistant([{"id": "t1", "name": "Bash", "input": {"command": "pytest -q", "description": "tests"}}]))
    assert e["kind"] == "tool_call" and e["tool"] == "Bash" and e["tool_use_id"] == "t1"
    assert e["summary"] == "pytest -q" and e["input"]["command"] == "pytest -q"


def test_edit_call_summary_is_path():
    [e] = parse(assistant([{"id": "t2", "name": "Edit",
                            "input": {"file_path": "src/a.py", "old_string": "x", "new_string": "y"}}]))
    assert e["summary"] == "src/a.py"


def test_unknown_tool_summary_falls_back_to_name():
    [e] = parse(assistant([{"id": "t3", "name": "mcp__agent__status", "input": {}}]))
    assert e["summary"] == "mcp__agent__status"


def test_long_input_strings_are_cut():
    [e] = parse(assistant([{"id": "t4", "name": "Write", "input": {"file_path": "a", "content": "x" * 5000}}]))
    assert len(e["input"]["content"]) <= 2001


def test_thinking_skipped():
    entries = parse(assistant([{"thinking": "hmm", "signature": "sig"}, {"text": "Hello"}]))
    assert [e["kind"] for e in entries] == ["text"] and entries[0]["text"] == "Hello"


def test_entry_ids_are_line_and_index():
    entries = parse(assistant([{"text": "a"}, {"text": "b"}]), line=7)
    assert [e["id"] for e in entries] == ["7:0", "7:1"]


def test_tool_result_error_flag_and_cut():
    [e] = parse(tool_result("t1", [{"type": "text", "text": "boom " * 2000}], is_error=True))
    assert e["kind"] == "tool_result" and e["tool_use_id"] == "t1" and e["is_error"] is True
    assert e["content"].startswith("boom") and len(e["content"]) <= 4001


def test_tool_result_string_content():
    [e] = parse(tool_result("t1", "ok"))
    assert e["content"] == "ok" and e["is_error"] is False


def test_user_prompt_string_is_text():
    [e] = parse({"type": "UserMessage", "content": "please fix the login", "uuid": "u"})
    assert e == {"id": "0:0", "ts": None, "kind": "text", "role": "user", "text": "please fix the login"}


def test_result_entry_tokens():
    [e] = parse(RESULT)
    assert e["kind"] == "result" and e["subtype"] == "success" and e["turns"] == 5 and e["cost_usd"] == 0.0762
    assert e["usage"] == {"in": 10, "out": 384, "cache_read": 65109, "cache_write": 6934}
    assert e["text"] == "Done."


def test_rate_limit_entry():
    [e] = parse({"type": "RateLimitEvent", "rate_limit_info": {"status": "allowed", "rate_limit_type": "five_hour"}})
    assert e["kind"] == "rate_limit" and "allowed" in e["text"] and "five_hour" in e["text"]


def test_system_message_yields_nothing():
    assert parse(INIT) == []


def test_truncated_and_unknown_become_raw():
    [t] = parse({"type": "AssistantMessage", "truncated": "x" * 3000})
    [u] = parse({"type": "Mystery", "foo": 1})
    assert t["kind"] == "raw" and len(t["text"]) <= 500
    assert u["kind"] == "raw" and "Mystery" in u["text"]


def test_malformed_content_becomes_raw():
    [e] = parse({"type": "AssistantMessage", "content": "not a list"})
    assert e["kind"] == "raw"


def test_entry_ts_from_stamp():
    [e] = parse({**assistant([{"text": "hi"}]), "_ts": "2026-11-13T10:00:00Z"})
    assert e["ts"] == "2026-11-13T10:00:00Z"
    assert parse(assistant([{"text": "hi"}]))[0]["ts"] is None


def test_scrubber_replaces_secrets():
    scrub = make_scrubber(["s3cr3t-value-123", "short", ""])
    assert scrub("token s3cr3t-value-123 and short") == "token [secret] and short"
    [e] = parse_record(assistant([{"id": "t", "name": "Bash", "input": {"command": "echo s3cr3t-value-123"}}]), 0,
                       scrub)
    assert "s3cr3t" not in json.dumps(e)


def write_lines(path, records, tail=""):
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + tail)


def test_read_entries_from_offset(tmp_path):
    f = tmp_path / "W-1.jsonl"
    write_lines(f, [INIT, assistant([{"text": "one"}])])
    entries, offset = read_entries(f)
    assert [e["text"] for e in entries] == ["one"] and offset == f.stat().st_size
    with f.open("a") as fh:
        fh.write(json.dumps(assistant([{"text": "two"}])) + "\n")
    more, offset2 = read_entries(f, offset)
    assert [e["text"] for e in more] == ["two"] and offset2 == f.stat().st_size
    assert more[0]["id"].startswith("2:")


def test_read_entries_leaves_partial_last_line(tmp_path):
    f = tmp_path / "W-1.jsonl"
    partial = json.dumps(assistant([{"text": "later"}]))
    write_lines(f, [assistant([{"text": "now"}])], tail=partial[:20])
    entries, offset = read_entries(f)
    assert [e["text"] for e in entries] == ["now"]
    with f.open("a") as fh:
        fh.write(partial[20:] + "\n")
    more, _ = read_entries(f, offset)
    assert [e["text"] for e in more] == ["later"]


def test_read_entries_bad_json_line_is_raw(tmp_path):
    f = tmp_path / "W-1.jsonl"
    f.write_text("{not json\n")
    [e], _ = read_entries(f)
    assert e["kind"] == "raw"


def test_read_entries_missing_file(tmp_path):
    assert read_entries(tmp_path / "nope.jsonl") == ([], 0)


def test_live_stats_dedupes_message_ids(tmp_path):
    f = tmp_path / "W-1.jsonl"
    write_lines(f, [INIT, assistant([{"thinking": ""}], "msg_1"), assistant([{"text": "a"}], "msg_1"),
                    assistant([{"text": "b"}], "msg_2")])
    s = live_stats(f)
    assert s.session_id == "s-1" and s.done is False and s.cost_usd is None
    assert s.tokens == {"in": 8, "out": 48, "cache_read": 2 * 17592, "cache_write": 2 * 421}
    assert s.turns == 2


def test_live_stats_result_wins(tmp_path):
    f = tmp_path / "W-1.jsonl"
    write_lines(f, [INIT, assistant([{"text": "a"}], "msg_1"), RESULT])
    s = live_stats(f)
    assert s.done is True and s.turns == 5 and s.cost_usd == 0.0762
    assert s.tokens == {"in": 10, "out": 384, "cache_read": 65109, "cache_write": 6934}


def test_live_stats_missing_file(tmp_path):
    s = live_stats(tmp_path / "nope.jsonl")
    assert s.session_id is None and s.tokens == {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
