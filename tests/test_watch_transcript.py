import json

from agent.watch.transcript import TurnFacts, normalise_error, turn_facts


def _line(**obj) -> str:
    return json.dumps(obj)


def _assistant(content, usage=None, parent_tool_use_id=None, message_id="m1"):
    return _line(type="AssistantMessage", message_id=message_id, parent_tool_use_id=parent_tool_use_id,
                 usage=usage or {}, content=content)


def _tool_use(tool_id, name, input):
    return {"id": tool_id, "name": name, "input": input}


def _result(tool_use_id, content, is_error=None, parent_tool_use_id=None):
    return _line(type="UserMessage", parent_tool_use_id=parent_tool_use_id,
                 content=[{"tool_use_id": tool_use_id, "content": content, "is_error": is_error}])


def test_context_first_and_peak_main_thread_only():
    lines = [
        _assistant([_tool_use("t1", "Bash", {"command": "ls"})],
                   usage={"input_tokens": 20000, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 1000}),
        _assistant([_tool_use("t2", "Bash", {"command": "ls"})], parent_tool_use_id="sub1",
                   usage={"input_tokens": 900000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
        _assistant([_tool_use("t3", "Bash", {"command": "ls"})],
                   usage={"input_tokens": 45000, "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 1000}),
    ]
    facts = turn_facts(lines)
    assert (facts.context_first, facts.context_peak) == (24000, 50000)


def test_delegation_counts():
    lines = [
        _assistant([_tool_use("t1", "Bash", {"command": "ls"})]),
        _assistant([_tool_use("t2", "Bash", {"command": "pwd"})]),
        _assistant([_tool_use("t3", "Bash", {"command": "date"})]),
        _assistant([_tool_use("t4", "Task", {"description": "lane 1"})]),
    ]
    facts = turn_facts(lines)
    assert facts.tool_calls == 4 and facts.subagent_calls == 1


def test_agent_tool_counts_as_subagent():
    lines = [_assistant([_tool_use("t1", "Agent", {"description": "lane"})])]
    facts = turn_facts(lines)
    assert facts.tool_calls == 1 and facts.subagent_calls == 1


def test_inline_code_chars_counts_write_and_heredocs():
    heredoc_body = "x" * 50
    heredoc_command = "cat > a.py <<'EOF'\n" + heredoc_body + "\nEOF"
    lines = [
        _assistant([_tool_use("t1", "Write", {"file_path": "a.py", "content": "y" * 100})]),
        _assistant([_tool_use("t2", "Bash", {"command": heredoc_command})]),
        _assistant([_tool_use("t3", "Bash", {"command": "ls -la"})]),
    ]
    facts = turn_facts(lines)
    assert facts.inline_code_chars == 100 + len(heredoc_command) + 0


def test_heredoc_marker_inside_body_is_not_a_redirect():
    # The ">" only shows up inside the heredoc's body text, after the marker, so this isn't a file write.
    command = "cat <<'EOF'\nfoo > bar\nEOF"
    lines = [_assistant([_tool_use("t1", "Bash", {"command": command})])]
    facts = turn_facts(lines)
    assert facts.inline_code_chars == 0


def test_tool_errors_normalised_and_missing_tools():
    lines = [
        _assistant([_tool_use("t1", "Read", {"file_path": "/var/agent/x/1.txt"})]),
        _result("t1", "/var/agent/x/1.txt: No such file", is_error=True),
        _assistant([_tool_use("t2", "Read", {"file_path": "/tmp/y/2.txt"})]),
        _result("t2", "/tmp/y/2.txt: No such file", is_error=True),
        _assistant([_tool_use("t3", "Bash", {"command": "ss -tlnp"})]),
        _result("t3", "bash: ss: command not found", is_error=True),
        _assistant([_tool_use("t4", "Bash", {"command": "python3 -c 'import PIL'"})]),
        _result("t4", "ModuleNotFoundError: No module named 'PIL'", is_error=True),
    ]
    facts = turn_facts(lines)
    assert facts.tool_errors[0] == facts.tool_errors[1]
    assert "ss" in facts.missing_tools
    assert "PIL" in facts.missing_tools


def test_lost_background_and_still_running():
    lines = [
        _line(type="TaskStartedMessage", data={"task_id": "t1", "task_type": "subagent"}),
        _line(type="TaskStartedMessage", data={"task_id": "t2", "task_type": "subagent"}),
        _line(type="TaskStartedMessage", data={"task_id": "t3", "task_type": "subagent"}),
        _line(type="TaskNotificationMessage",
              data={"task_id": "t1", "status": "stopped",
                    "summary": 'Background agent "Lane 1" didn\'t finish before the previous session ended'}),
        _line(type="TaskNotificationMessage", data={"task_id": "t2", "status": "completed", "summary": "done"}),
        _line(type="ResultMessage", subtype="success"),
    ]
    facts = turn_facts(lines)
    assert facts.lost_background == 1
    assert facts.still_running == 1


def test_lost_background_by_usage_limit():
    lines = [
        _line(type="TaskStartedMessage", data={"task_id": "t1", "task_type": "subagent"}),
        _line(type="TaskNotificationMessage",
              data={"task_id": "t1", "status": "failed", "summary": "request failed with status 429"}),
        _line(type="ResultMessage", subtype="success"),
    ]
    facts = turn_facts(lines)
    assert facts.lost_background == 1 and facts.still_running == 0


def test_clones_skills_prs():
    lines = [
        _assistant([_tool_use("t1", "Bash", {"command": "gh repo clone org/repo -- -q"})]),
        _assistant([_tool_use("t2", "Bash", {"command": "git clone https://github.com/org/two.git"})]),
        _assistant([_tool_use("t3", "Skill", {"skill": "superpowers:writing-plans"})]),
        _line(type="SystemMessage", subtype="code_change_published",
              data={"url": "https://github.com/org/repo/pull/1", "action": "created"}),
        _line(type="SystemMessage", subtype="code_change_published",
              data={"url": "https://github.com/org/repo/pull/2", "action": "merged"}),
    ]
    facts = turn_facts(lines)
    assert facts.clones == ["org/repo", "org/two"]
    assert facts.skills == ["superpowers:writing-plans"]
    assert facts.prs_created == ["https://github.com/org/repo/pull/1"]


def test_tool_and_error_lines_are_capped():
    lines = [
        _assistant([_tool_use("t1", "Bash", {"command": "x" * 1000})]),
        _result("t1", "y" * 1000),
        _assistant([_tool_use("t2", "Bash", {"command": "echo boom"})]),
        _result("t2", "e" * 2000, is_error=True),
    ]
    facts = turn_facts(lines)
    assert facts.tool_lines[0] == f"Bash: {'x' * 300} -> {'y' * 300}"
    assert facts.error_lines[0] == "e" * 500 and len(facts.error_lines[0]) == 500


def test_skips_bad_lines():
    lines = ["not json", '{"type": "Assistant', _assistant([_tool_use("t1", "Bash", {"command": "ls"})])]
    facts = turn_facts(lines)
    assert facts.tool_calls == 1


def test_normalise_error_strips_paths_hex_and_digits():
    a = normalise_error("/var/agent/x/1.txt: No such file")
    b = normalise_error("/tmp/y/2.txt: No such file")
    assert a == b == "<path> no such file"
    assert normalise_error("object deadbeef12 not found") == "object <id> not found"
    assert normalise_error("retry after 12345678 ms") == "retry after <n> ms"


def test_turn_facts_returns_dataclass_with_defaults_on_empty_input():
    facts = turn_facts([])
    assert isinstance(facts, TurnFacts)
    assert facts.context_first == 0 and facts.context_peak == 0
    assert facts.tool_errors == [] and facts.missing_tools == []
