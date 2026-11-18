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
    "wait",
    "echo hi; wait",
    "true && wait",
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
