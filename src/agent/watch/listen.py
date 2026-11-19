"""The host listener (`python scripts/jensen_watch.py listen`, a systemd service on the host): improve-jensen's
clock and its ear in Slack, one loop every 15 seconds.

- The clock: GitHub's cron never fires the workflow, so the listener sends its `repository_dispatch` events through
  Jensen's `gh` (`watch` at :17 and `assess` at :10 every hour, `approvals` every 10 minutes, all UTC). `<state>/clock.json` keeps each type's last slot sent; each loop sends a type's most recent
  slot once when it's later than that, so a long stop catches up with one event, not a burst. The first start sends
  nothing and just records the slots.
- The threads: `approvals.answer_threads` answers new replies to the proposals in plain language, with Claude
  through the Claude Code CLI and the host's proxy (`claude`).
- It exits 0 when the live checkout's `HEAD` moves, so systemd restarts it on the new code.

Every Slack, `gh` or Claude failure is one stderr line (Slack's code or the exception's type, never a token or a
message's text) and the loop goes on. Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from agent.watch.approvals import answer_threads
from agent.watch.slack import Slack, SlackError, load_people, load_token
from agent.watch.state import FMT

DEFAULT_REPO = "pragnition/autonomous-agent"
WATCH, ASSESS, APPROVALS = "improve-jensen-watch", "improve-jensen-assess", "improve-jensen-approvals"
_LOOP_S = 15
_CLOCK_FILE = "clock.json"
_CLAUDE_MODEL = "claude-sonnet-5"
_CLAUDE_TIMEOUT_S = 90
# The proxy's improve-jensen origin, the same way the workflow's Claude steps reach it.
_CLAUDE_ENV = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18092",
               "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-container-via-host-proxy"}


def _warn(what: str, exc: BaseException) -> None:
    """One stderr line naming what failed and Slack's code or the exception's type, never its text."""
    detail = str(exc) if isinstance(exc, SlackError) else type(exc).__name__
    print(f"warning: {what} failed ({detail})", file=sys.stderr)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def latest_slots(now: datetime) -> dict[str, datetime]:
    """Each dispatch type's most recent slot at or before `now`."""
    now = now.astimezone(UTC).replace(second=0, microsecond=0)

    def hourly(minute: int) -> datetime:
        slot = now.replace(minute=minute)
        return slot if slot <= now else slot - timedelta(hours=1)

    return {WATCH: hourly(17), ASSESS: hourly(10), APPROVALS: now.replace(minute=now.minute - now.minute % 10)}


def tick(dispatch: Callable[[str], None], state_root: Path, now: datetime) -> None:
    """Send each type's latest slot once (`latest_slots`), recording it in `clock.json` only once it's sent. A type
    with nothing recorded yet (no `clock.json`) is recorded without sending."""
    path = Path(state_root) / _CLOCK_FILE
    try:
        sent = json.loads(path.read_text())
    except (OSError, ValueError):
        sent = {}
    sent = sent if isinstance(sent, dict) else {}
    changed = False
    for event_type, slot in latest_slots(now).items():
        stamp, last = slot.strftime(FMT), sent.get(event_type)
        if isinstance(last, str) and last >= stamp:
            continue
        if isinstance(last, str):
            try:
                dispatch(event_type)
            except Exception as exc:
                _warn(f"sending {event_type}", exc)
                continue
        sent[event_type] = stamp
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(sent))


def claude(host) -> Callable[[str, str], str]:
    """`ask(system, prompt) -> text`: one Claude turn through the Claude Code CLI and the host's proxy, with
    `system` in place of the default system prompt, no tools, the prompt on stdin. Raises on a non-zero exit, a
    timeout or an answer without a result."""
    def ask(system: str, prompt: str) -> str:
        result = host.run([str(Path.home() / ".local" / "bin" / "claude"), "-p", "--output-format", "json",
                           "--model", _CLAUDE_MODEL, "--system-prompt", system, "--tools", "", "--max-turns", "1"],
                          input=prompt, capture_output=True, text=True, timeout=_CLAUDE_TIMEOUT_S,
                          env={**os.environ, **_CLAUDE_ENV})
        if result.returncode != 0:
            raise RuntimeError(f"claude exited {result.returncode}")
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or data.get("is_error") or not isinstance(data.get("result"), str):
            raise ValueError("claude answered without a result")
        return data["result"]
    return ask


def _head(host) -> str | None:
    try:
        result = host.run(["git", "rev-parse", "HEAD"], cwd=host.live, capture_output=True, text=True)
    except Exception as exc:
        _warn("reading the live checkout's HEAD", exc)
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip() or None


def run(host, state_root: Path, repo: str, *, ask: Callable[[str, str], str] | None = None,
        make_slack: Callable[[str], Slack] = Slack, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep) -> int:
    """The listener's loop: the clock, then the threads, every `_LOOP_S` seconds, until the live checkout's `HEAD`
    moves (exit 0). `gh` is Jensen's (`host.jensen_gh`) against `repo`."""
    ask = ask or claude(host)

    def dispatch(event_type: str) -> None:
        host.jensen_gh(["api", "-X", "POST", f"repos/{repo}/dispatches", "-f", f"event_type={event_type}"])

    def gh(args: list[str]) -> str:
        return host.jensen_gh([*args, "-R", repo]).stdout or ""

    start = _head(host)
    while True:
        now = clock()
        try:
            tick(dispatch, state_root, now)
        except Exception as exc:
            _warn("keeping the clock", exc)
        try:
            answer_threads(make_slack(load_token(host.live)), gh, ask, dispatch, state_root,
                           load_people(host.live), now)
        except Exception as exc:
            _warn("answering the proposals' threads", exc)
        sleep(_LOOP_S)
        head = _head(host)
        if start is None:
            start = head
        elif head is not None and head != start:
            return 0
