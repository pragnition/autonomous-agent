"""A PreToolUse hook on Bash that turns away commands that only wait.

A session that launches background agents and then holds its turn open with a blocking wait (`timeout 290 tail -f
/dev/null`, `sleep 300`, ...) can't read anything: messages sent into it are only delivered between tool calls, so the
person waits minutes for an answer. Background agents already wake the session when they finish, and the runner
keeps the session open while they run (claude.py), so ending the turn is safe. Short sleeps, such as giving a
server a few seconds to come up, and anything run in the background still pass.

Only what the shell runs counts. Quoted strings (a grep pattern, a commit message, a `node -e` script) and heredoc
bodies (a file being written) are text, so a sleep in them passes; the script of `bash -c '...'` is run, so it is
checked like the command itself, and so is the script of `python -c '...'` for a `time.sleep`.
"""

from __future__ import annotations

import re

# The longest a foreground sleep may be, in seconds: enough for a server to come up, not enough to hold a turn open
MAX_SLEEP_S = 10

IDLE_WAIT_REASON = (
    "Blocked: this command only waits, and while a Bash call is running, no message from the people you work with can "
    "reach you. Background agents notify you when they finish, and your session stays open while they run, so end "
    "your turn now (say in a line what's still running) and let the notification wake you. To wait for a "
    "condition (a file, a port, a CI run), use the Monitor tool instead of a sleep or a polling loop.")

_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
# timeout with its options, their values and its duration, before the command it runs (timeout -s KILL 5m sleep 299)
_TIMEOUT = r"timeout(?:\s+(?:-\S+|\d[\d.]*[smhd]?|(?:SIG)?[A-Z]+))+"
# Where a command starts: the start of the text or of a line, after a control operator or an opening bracket, or
# after a word that runs the next one
_SEP = r"(?:^|[\n;&|(){}`!]|\$\(|\b(?:then|do|else|nohup|exec|command|env|time|" + _TIMEOUT + r")\s)"
# tail -f / --follow on a file that never grows: /dev/null or /dev/zero
_TAIL_NOTHING = re.compile(r"\btail\s+(?:-\S*\s+)*(?:-f|-F|--follow\S*)\s+(?:-\S*\s+)*/dev/(?:null|zero)\b")
_SLEEP = re.compile(_SEP + r"\s*sleep\s+((?:\d*\.?\d+[smhd]?\s*)+|inf(?:inity)?\b)")
_READ_T = re.compile(_SEP + r"\s*read\s+(?:-\S+\s+)*-\w*t\s*(\d*\.?\d+)")
# A quoted string's place in the command once it's taken out: \0<its index>\0
_QUOTED = r"\x00(\d+)\x00"
_SHELL_C = re.compile(r"\b(?:ba|da|k|z)?sh\s+(?:-\S+\s+)*-[a-z]*c[a-z]*\s+" + _QUOTED)
_PYTHON_C = re.compile(r"\bpython[\d.]*\s+(?:-\S+\s+)*-c\s+" + _QUOTED)
_PY_SLEEP = re.compile(r"\btime\.sleep\(\s*(\d*\.?\d+)\s*\)")
# A command substitution inside double quotes still runs: "$(sleep 99)"
_SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*\\?(['\"]?)([A-Za-z_][\w.-]*)\1")


def _sleep_seconds(args: str) -> float:
    if args.startswith("inf"):
        return float("inf")
    total = 0.0
    for num, unit in re.findall(r"(\d*\.?\d+)([smhd]?)", args):
        total += float(num) * _UNITS[unit]
    return total


def _drop_heredoc_bodies(command: str) -> str:
    """The command without the bodies of its heredocs: what a heredoc feeds a command is its input, not commands."""
    lines, ends = [], []
    for line in command.split("\n"):
        if ends:
            if line.lstrip("\t") == ends[0]:  # `<<-` allows the end line to be indented with tabs
                ends.pop(0)
            continue
        lines.append(line)
        ends = [m.group(2) for m in _HEREDOC.finditer(line)]
    return "\n".join(lines)


def _take_out_quotes(command: str) -> tuple[str, list[str], list[str]]:
    """(the command with each quoted string replaced by its place, the quoted strings, the command substitutions
    inside double quotes). A quoted string is one argument (a pattern, a message, a script), not a command."""
    out, quoted, substituted, i, n = [], [], [], 0, len(command)
    while i < n:
        c = command[i]
        if c == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        if c not in "'\"":
            out.append(c)
            i += 1
            continue
        j = i + 1
        while j < n and command[j] != c:
            j += 2 if c == '"' and command[j] == "\\" else 1
        text = command[i + 1:j]
        if c == '"':
            substituted += [a or b for a, b in _SUBST.findall(text)]
        out.append(f"\x00{len(quoted)}\x00")
        quoted.append(text)
        i = j + 1
    return "".join(out), quoted, substituted


def idle_wait(command: str) -> bool:
    """Whether the command just holds the shell open: a follow of /dev/null, or a sleep (or `read -t`) longer than
    MAX_SLEEP_S in any position the shell runs it, including inside `timeout`, a chain, a loop or a `sh -c` script."""
    cmd, quoted, substituted = _take_out_quotes(_drop_heredoc_bodies(command.strip()))
    if _TAIL_NOTHING.search(cmd):
        return True
    if any(_sleep_seconds(m.group(1).strip()) > MAX_SLEEP_S for m in _SLEEP.finditer(cmd)):
        return True
    if any(float(m.group(1)) > MAX_SLEEP_S for m in _READ_T.finditer(cmd)):
        return True
    if any(float(s.group(1)) > MAX_SLEEP_S for m in _PYTHON_C.finditer(cmd)
           for s in _PY_SLEEP.finditer(quoted[int(m.group(1))])):
        return True
    # what a shell runs from its -c script, or from a substitution inside double quotes, is checked the same way
    return any(idle_wait(script) for script in (*(quoted[int(m.group(1))] for m in _SHELL_C.finditer(cmd)),
                                               *substituted))


async def block_idle_waits(input_data: dict, tool_use_id: str | None, context) -> dict:
    """The PreToolUse hook: deny a foreground Bash command that only waits, with what to do instead."""
    tool_input = input_data.get("tool_input") or {}
    if input_data.get("tool_name") != "Bash" or tool_input.get("run_in_background"):
        return {}
    if not idle_wait(str(tool_input.get("command") or "")):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": IDLE_WAIT_REASON}}
