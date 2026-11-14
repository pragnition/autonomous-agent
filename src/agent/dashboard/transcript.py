"""A session's activity, read from runs/<work item>.jsonl.

Each line is one Claude Agent SDK message as the runner forwarded it (a dataclass as a dict: SystemMessage,
AssistantMessage, UserMessage, RateLimitEvent, ResultMessage), or {"type", "truncated"} for one over 20 kB; core
stamps each with "_ts" when it arrives. Lines become timeline entries; nothing here ever raises on a strange line.
"""

from __future__ import annotations

import json
import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

INPUT_CHARS = 2000
RESULT_CHARS = 4000
RAW_CHARS = 500
SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt")
MIN_SECRET = 8

Scrub = Callable[[str], str]


def zero_tokens() -> dict:
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}


def tokens_of(usage: dict | None) -> dict:
    u = usage if isinstance(usage, dict) else {}

    def n(key: str) -> int:
        value = u.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    return {"in": n("input_tokens"), "out": n("output_tokens"), "cache_read": n("cache_read_input_tokens"),
            "cache_write": n("cache_creation_input_tokens")}


def make_scrubber(secrets: list[str]) -> Scrub:
    values = sorted({s for s in secrets if s and len(s) >= MIN_SECRET}, key=len, reverse=True)
    if not values:
        return lambda text: text
    pattern = re.compile("|".join(re.escape(v) for v in values))
    return lambda text: pattern.sub("[secret]", text)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _clean_input(value, scrub: Scrub):
    if isinstance(value, str):
        return _cut(scrub(value), INPUT_CHARS)  # scrub before cutting: a cut must never land inside a secret
    if isinstance(value, dict):
        return {k: _clean_input(v, scrub) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_input(v, scrub) for v in value]
    return value


def _summary(name: str, args: dict) -> str:
    for key in SUMMARY_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().splitlines()[0][:300]
    return name


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, dict) and block.get("type") == "tool_reference":
                parts.append(f"[{block.get('tool_name', 'tool')}]")
            elif isinstance(block, dict) and block.get("type") == "image":
                parts.append("[image]")
            else:
                parts.append(json.dumps(block, default=str))
        return "\n".join(parts)
    return "" if content is None else json.dumps(content, default=str)


def parse_record(record: dict, line: int, scrub: Scrub) -> list[dict]:
    ts = record.get("_ts") if isinstance(record.get("_ts"), str) else None
    out: list[dict] = []

    def add(kind: str, **fields) -> None:
        out.append({"id": f"{line}:{len(out)}", "ts": ts, "kind": kind, **fields})

    def raw() -> list[dict]:
        out.clear()
        add("raw", text=_cut(scrub(json.dumps(record, default=str)), RAW_CHARS))
        return out

    kind = record.get("type")
    if "truncated" in record:
        return raw()
    content = record.get("content")
    if kind == "SystemMessage":
        return out
    if kind == "AssistantMessage":
        if not isinstance(content, list):
            return raw()
        for block in content:
            if not isinstance(block, dict) or "thinking" in block:
                continue
            if isinstance(block.get("text"), str):
                add("text", role="assistant", text=scrub(block["text"]))
            elif "name" in block and "id" in block:
                args = _clean_input(block.get("input") if isinstance(block.get("input"), dict) else {}, scrub)
                add("tool_call", tool=str(block["name"]), tool_use_id=str(block["id"]),
                    summary=_summary(str(block["name"]), args), input=args)
        return out
    if kind == "UserMessage":
        if isinstance(content, str):
            add("text", role="user", text=scrub(content))
            return out
        if not isinstance(content, list):
            return raw()
        for block in content:
            if isinstance(block, dict) and "tool_use_id" in block:
                add("tool_result", tool_use_id=str(block["tool_use_id"]), is_error=bool(block.get("is_error")),
                    content=_cut(scrub(_flatten(block.get("content"))), RESULT_CHARS))
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                add("text", role="user", text=scrub(block["text"]))
        return out
    if kind == "ResultMessage":
        add("result", subtype=record.get("subtype"), turns=record.get("num_turns"), usage=tokens_of(record.get("usage")),
            is_error=bool(record.get("is_error")), text=_cut(scrub(str(record.get("result") or "")), RESULT_CHARS))
        return out
    if kind == "RateLimitEvent":
        info = record.get("rate_limit_info") if isinstance(record.get("rate_limit_info"), dict) else {}
        add("rate_limit", text=f"{info.get('rate_limit_type', 'limit')}: {info.get('status', '?')}")
        return out
    return raw()


def read_from(path: Path, offset: int, line: int, scrub: Scrub = str) -> tuple[list[dict], int, int]:
    """Entries for the complete lines from byte `offset`, which is the start of line number `line`, and the cursor
    (offset, line) where the next read starts. A last line without its newline is still being written: it is left
    for the next read. Only the new bytes are read, so a stream that follows a long transcript stays cheap."""
    try:
        with Path(path).open("rb") as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return [], offset, line
    end = data.rfind(b"\n") + 1
    entries: list[dict] = []
    for raw_line in data[:end].splitlines():
        try:
            record = json.loads(raw_line)
            if not isinstance(record, dict):
                raise ValueError
        except ValueError:
            record = {"type": "unreadable", "truncated": raw_line[:RAW_CHARS].decode(errors="replace")}
        entries.extend(parse_record(record, line, scrub))
        line += 1
    return entries, offset + end, line


def lines_before(path: Path, offset: int) -> int:
    try:
        with Path(path).open("rb") as f:
            return f.read(offset).count(b"\n") if offset else 0
    except OSError:
        return 0


@dataclass
class LiveStats:
    session_id: str | None = None
    tokens: dict = field(default_factory=zero_tokens)
    turns: int = 0
    done: bool = False


class _Totals:
    """What a session has used so far, fed one record at a time. The SDK splits one reply into several messages
    that carry the same usage, so usage counts once per message id; the ResultMessage, when it comes, is the real
    total."""

    def __init__(self):
        self.session_id: str | None = None
        self.by_message: dict[str, dict] = {}
        self.result: LiveStats | None = None

    def feed(self, record: dict) -> None:
        kind = record.get("type")
        if kind == "SystemMessage" and record.get("subtype") == "init":
            sid = (record.get("data") or {}).get("session_id")
            self.session_id = sid if isinstance(sid, str) else self.session_id
        elif kind == "AssistantMessage" and record.get("message_id"):
            self.by_message[record["message_id"]] = tokens_of(record.get("usage"))
        elif kind == "ResultMessage":
            self.result = LiveStats(tokens=tokens_of(record.get("usage")), turns=int(record.get("num_turns") or 0),
                                    done=True)

    def stats(self) -> LiveStats:
        if self.result is not None:
            return LiveStats(self.session_id, dict(self.result.tokens), self.result.turns, True)
        tokens = zero_tokens()
        for usage in self.by_message.values():
            for key, value in usage.items():
                tokens[key] += value
        return LiveStats(self.session_id, tokens, len(self.by_message))


@dataclass
class _Followed:
    offset: int = 0
    line: int = 0
    totals: _Totals = field(default_factory=_Totals)
    recent: deque = field(default_factory=deque)


class TranscriptCache:
    """Live numbers and the last few entries of the transcripts being watched, read incrementally: each call reads
    only what was appended since the last, so polling a long session costs what it added, not its whole length."""

    def __init__(self, scrub: Scrub = str, recent: int = 5, max_files: int = 256):
        self.scrub, self.keep, self.max_files = scrub, recent, max_files
        self._files: dict[Path, _Followed] = {}
        self._lock = threading.Lock()

    def follow(self, path: Path) -> tuple[LiveStats, list[dict]]:
        path = Path(path)
        with self._lock:
            state = self._files.pop(path, None) or _Followed(recent=deque(maxlen=self.keep))
            self._files[path] = state  # most recently followed last
            while len(self._files) > self.max_files:
                del self._files[next(iter(self._files))]
            try:
                with path.open("rb") as f:
                    f.seek(state.offset)
                    data = f.read()
            except OSError:
                return state.totals.stats(), list(state.recent)
            end = data.rfind(b"\n") + 1
            for raw_line in data[:end].splitlines():
                try:
                    record = json.loads(raw_line)
                    if not isinstance(record, dict):
                        raise ValueError
                except ValueError:
                    record = {"type": "unreadable", "truncated": raw_line[:RAW_CHARS].decode(errors="replace")}
                state.totals.feed(record)
                state.recent.extend(parse_record(record, state.line, self.scrub))
                state.line += 1
            state.offset += end
            return state.totals.stats(), list(state.recent)
