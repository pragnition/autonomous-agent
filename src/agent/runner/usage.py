"""What a run's sessions used, counted from the CLI's own session logs.

The SDK's ResultMessage reports the main thread's usage only, a turn that stops without one (stalled, timed out,
interrupted) reports nothing, and a background agent's replies never reach the SDK stream at all. The CLI logs every
reply it gets: CLAUDE_CONFIG_DIR/projects/<project folder>/<sid>.jsonl for the main thread, and
<sid>/subagents/*.jsonl for each agent the session ran, foreground or background. The CLI writes one record per
content block of a reply, each with the reply's id and usage, so each reply counts once, at its largest numbers.

A resumed session's log holds its earlier runs too: only the replies from when this run took the session up count.
Each poll reads only what the logs gained since the last.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
Tokens = tuple[int, int, int, int]  # input, output, cache read, cache write


def _reply(line: bytes, since: float) -> tuple[str, list[int]] | None:
    """(reply id, its token counts) for an assistant record from `since` on, else None."""
    if b'"usage"' not in line:
        return None
    try:
        record = json.loads(line)
        message = record["message"]
        at = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).timestamp()
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if record.get("type") != "assistant" or not isinstance(message, dict) or at < since:
        return None
    usage = message.get("usage")
    if not isinstance(message.get("id"), str) or not isinstance(usage, dict):
        return None
    return message["id"], [n if isinstance(n := usage.get(k), int) and not isinstance(n, bool) else 0
                           for k in USAGE_FIELDS]


class SessionUsage:
    """The tokens a run's sessions used, from their CLI logs. Thread-safe: polled off the event loop."""

    def __init__(self, claude_home: Path | str | None):
        self.projects = Path(claude_home) / "projects" if claude_home else None
        self._since: dict[str, float] = {}  # session id -> when this run took it up (epoch seconds, to the ms)
        self._read: dict[Path, int] = {}  # log -> bytes read so far
        self._replies: dict[str, list[int]] = {}
        self._lock = threading.Lock()

    def watch(self, sid: str | None, since: float) -> None:
        """Count the session's replies from `since` on; a session already watched keeps its first start."""
        if sid:
            with self._lock:
                self._since.setdefault(sid, int(since * 1000) / 1000)  # the CLI stamps records to the ms

    def poll(self) -> Tokens | None:
        """The totals so far, reading what the logs gained since the last poll; None while no session has a log."""
        if self.projects is None:
            return None
        with self._lock:
            for sid, since in self._since.items():
                for path in [*self.projects.glob(f"*/{sid}.jsonl"), *self.projects.glob(f"*/{sid}/subagents/*.jsonl")]:
                    self._read_on(path, since)
            if not self._read:
                return None
            return tuple(map(sum, zip(*self._replies.values()))) or (0, 0, 0, 0)  # type: ignore[return-value]

    def _read_on(self, path: Path, since: float) -> None:
        start = self._read.get(path, 0)
        try:
            with path.open("rb") as f:
                f.seek(start)
                data = f.read()
        except OSError:
            return
        end = data.rfind(b"\n") + 1  # a last line without its newline is still being written: the next poll reads it
        self._read[path] = start + end
        for line in data[:end].splitlines():
            if (reply := _reply(line, since)) is not None:
                mid, counts = reply
                seen = self._replies.get(mid)
                self._replies[mid] = counts if seen is None else [max(a, b) for a, b in zip(seen, counts)]
