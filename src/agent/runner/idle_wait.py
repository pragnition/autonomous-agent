"""A PreToolUse hook on Bash that turns away commands that only wait.

A session that launches background agents and then holds its turn open with a blocking wait (`timeout 290 tail -f
/dev/null`, `sleep 300`, ...) can't read anything: messages sent into it are only delivered between tool calls, so the
person waits minutes for an answer. Background agents and tasks already wake the session when they finish, and the
runner keeps the session open while they run (claude.py), so ending the turn is safe. Short sleeps, such as giving a
server a few seconds to come up, and anything run in the background still pass.
"""

from __future__ import annotations

import re

# The longest a foreground sleep may be, in seconds: enough for a server to come up, not enough to hold a turn open
MAX_SLEEP_S = 10

IDLE_WAIT_REASON = (
    "Blocked: this command only waits, and while a Bash call is running, no message from the people you work with can "
    "reach you. Background agents and tasks notify you when they finish, and the session stays open while they run, "
    "so end your turn now (say in a line what's still running) and let the notification wake you. To wait for a "
    "condition (a file, a port, a CI run), use the Monitor tool instead of a sleep or a polling loop.")

_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_SEP = r"(?:^|[;&|(){}`!]|\$\(|\b(?:then|do|else|timeout(?:\s+-\S+)*\s+\S+|nohup|exec|command|env|time)\s)"
# tail -f / --follow on a file that never grows: /dev/null or /dev/zero
_TAIL_NOTHING = re.compile(r"\btail\s+(?:-\S*\s+)*(?:-f|-F|--follow\S*)\s+(?:-\S*\s+)*/dev/(?:null|zero)\b")
_SLEEP = re.compile(_SEP + r"\s*sleep\s+((?:\d*\.?\d+[smhd]?\s*)+|inf(?:inity)?\b)")
_PY_SLEEP = re.compile(r"\b(?:time\.)?sleep\(\s*(\d*\.?\d+)\s*\)")
_READ_T = re.compile(_SEP + r"\s*read\s+(?:-\S+\s+)*-\w*t\s*(\d*\.?\d+)")
_BARE_WAIT = re.compile(_SEP + r"\s*wait\s*(?:$|[;&|)}])")


def _sleep_seconds(args: str) -> float:
    if args.startswith("inf"):
        return float("inf")
    total = 0.0
    for num, unit in re.findall(r"(\d*\.?\d+)([smhd]?)", args):
        total += float(num) * _UNITS[unit]
    return total


def idle_wait(command: str) -> bool:
    """Whether the command just holds the shell open: a follow of /dev/null, a sleep (or `read -t`) longer than
    MAX_SLEEP_S in any position, including inside `timeout`, a chain or a loop, or a bare `wait` with nothing started."""
    cmd = command.strip()
    if _TAIL_NOTHING.search(cmd):
        return True
    if any(_sleep_seconds(m.group(1).strip()) > MAX_SLEEP_S for m in _SLEEP.finditer(cmd)):
        return True
    if any(float(m.group(1)) > MAX_SLEEP_S for p in (_PY_SLEEP, _READ_T) for m in p.finditer(cmd)):
        return True
    # a bare `wait` waits on this shell's own background jobs; with none started before it (no lone `&`) it only
    # stands in for a wait on background agents, which it can't do
    for m in _BARE_WAIT.finditer(cmd):
        if not re.search(r"(?<![&>])&(?![&>])", cmd[:m.start() + 1]):
            return True
    return False


async def block_idle_waits(input_data: dict, tool_use_id: str | None, context) -> dict:
    """The PreToolUse hook: deny a foreground Bash command that only waits, with what to do instead."""
    tool_input = input_data.get("tool_input") or {}
    if input_data.get("tool_name") != "Bash" or tool_input.get("run_in_background"):
        return {}
    if not idle_wait(str(tool_input.get("command") or "")):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": IDLE_WAIT_REASON}}
