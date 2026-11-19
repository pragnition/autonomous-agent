import pytest

from agent.runner.idle_wait import IDLE_WAIT_REASON, block_idle_waits, idle_wait


@pytest.mark.parametrize("command", [
    "timeout 290 tail -f /dev/null; echo waited",  # the one from issue #17
    "tail -f /dev/null",
    "tail -F /dev/null",
    "sleep 300",
    "sleep 5m",
    "sleep 1h 30m",
    "sleep infinity",
    "timeout 300 sleep 290",
    "cd repo && sleep 60 && echo done",
    "echo start; sleep 120",
    "(sleep 120)",
    "x=$(sleep 99)",
    "nohup sleep 100",
    "while true; do sleep 30; done",
    "read -t 300",
    "read -r -t 120 line",
    "python -c 'import time; time.sleep(200)'",
    # a wait hidden in a shell's -c script, behind timeout's own options, or on a line of its own
    "bash -c 'sleep 300'",
    'sh -c "cd /tmp && sleep 300"',
    "timeout 300 bash -c 'sleep 299'",
    "timeout -s KILL 300 sleep 299",
    "timeout -k 5 --preserve-status 5m sleep 299",
    "cd repo\nsleep 300",
    "docker exec core sh -c 'sleep 400'",
    'x="$(sleep 99)"',  # a command substitution runs even inside double quotes
])
def test_idle_waits_are_caught(command):
    assert idle_wait(command)


@pytest.mark.parametrize("command", [
    "sleep 2",
    "sleep 3 && curl -s localhost:3000",
    "npm run dev > dev.log 2>&1 & sleep 5; curl -s localhost:3000",
    "npm test & npm run lint & wait",
    "until curl -s localhost:3000; do sleep 1; done",
    "tail -f dev.log",
    "tail -n 50 /var/log/app.log",
    "echo sleep 300",
    "grep -n 'sleep(' src/app.py",
    "pytest -q",
    "git log --oneline -5",
    # a sleep that is only text: in a quoted argument, a commit message or a heredoc's body
    "grep -rn 'sleep(30)' src/",
    "sed -i 's/sleep(30)/sleep(1)/' app.py",
    "node -e 'await sleep(15000)'",
    'git commit -m "fix: drop the retry loop; sleep 15 was never enough"',
    "git commit -F - <<'EOF'\nfix: drop the retry loop; sleep 15 was never enough\nEOF",
    "cat > poll.py <<'EOF'\nimport time\nwhile True:\n    time.sleep(30)\nEOF\npython -m py_compile poll.py",
    "cat > wait.sh <<EOF\n#!/bin/sh\nsleep 300\nEOF",
    # a bare wait returns at once: each Bash call is a fresh shell with no jobs of its own
    "wait",
    "echo a >&2; wait",
    "true && wait",
])
def test_real_work_passes(command):
    assert not idle_wait(command)


async def test_hook_denies_a_foreground_idle_wait_with_guidance():
    out = await block_idle_waits({"tool_name": "Bash", "tool_input": {"command": "timeout 290 tail -f /dev/null"}},
                                 "t1", None)
    decision = out["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse" and decision["permissionDecision"] == "deny"
    assert decision["permissionDecisionReason"] == IDLE_WAIT_REASON
    assert "end your turn" in IDLE_WAIT_REASON and "Monitor" in IDLE_WAIT_REASON


async def test_hook_lets_other_commands_and_background_calls_through():
    assert await block_idle_waits({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}, "t1", None) == {}
    assert await block_idle_waits({"tool_name": "Bash", "tool_input": {"command": "sleep 300",
                                                                       "run_in_background": True}}, "t1", None) == {}
    assert await block_idle_waits({"tool_name": "Read", "tool_input": {"file_path": "/x"}}, "t1", None) == {}
