"""The note that starts a fresh session in place of one that can't be resumed on the account it would run on.

A Claude session is never resumed on a different account from the one it ran on, and its full history is never
loaded into the fresh session by any other route either. The runner reads the earlier session's record itself and
puts a short digest in the note: the session id, the text of its last few assistant messages (capped) and the files
it edited or wrote. The note gives the record's path for looking up one specific detail (grep, tail), and says not to
read it whole or resume it. read_thread and the conversation folder's own state are how the session catches up.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path

LAST_MESSAGES = 3  # the earlier session's last assistant messages put in the digest
MAX_DIGEST_CHARS = 4000  # their text in total, at most
MAX_FILES = 40  # the files it edited or wrote, at most (the latest ones)
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}


def session_file(claude_home: Path | str | None, sid: str) -> str:
    """Where the CLI keeps the session's record: CLAUDE_CONFIG_DIR/projects/<project folder>/<sid>.jsonl. The real
    path when it's there, otherwise the pattern to look under."""
    if not claude_home:
        return f"<CLAUDE_CONFIG_DIR>/projects/*/{sid}.jsonl"
    home = Path(claude_home)
    found = sorted((home / "projects").glob(f"*/{sid}.jsonl")) if (home / "projects").is_dir() else []
    return str(found[0]) if found else str(home / "projects" / "*" / f"{sid}.jsonl")


def _assistant_blocks(record: dict, sid: str) -> list | None:
    """The content blocks of one of the earlier session's own assistant messages, from either record: the runner's
    transcript (an AssistantMessage, tagged with its session) or the CLI's session log (type "assistant"). None for
    anything else, a subagent's messages included."""
    if record.get("type") == "AssistantMessage":
        if record.get("parent_tool_use_id") or record.get("session_id") not in (None, sid):
            return None
        return record.get("content") if isinstance(record.get("content"), list) else None
    if record.get("type") == "assistant" and not record.get("isSidechain"):
        content = (record.get("message") or {}).get("content")
        return content if isinstance(content, list) else None
    return None


def _read_record(path: Path, sid: str) -> tuple[list[str], list[str]]:
    """The text of the session's last assistant messages and the files it edited or wrote, reading the record a line
    at a time: only the digest is kept, never the record."""
    texts: deque[str] = deque(maxlen=LAST_MESSAGES)
    files: dict[str, None] = {}
    with path.open(errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            blocks = _assistant_blocks(record, sid) if isinstance(record, dict) else None
            if not blocks:
                continue
            text = "\n".join(b["text"] for b in blocks if isinstance(b, dict) and isinstance(b.get("text"), str)
                             and b.get("type", "text") == "text").strip()
            if text:
                texts.append(text)
            for b in blocks:
                if isinstance(b, dict) and b.get("name") in EDIT_TOOLS and isinstance(b.get("input"), dict):
                    target = b["input"].get("file_path") or b["input"].get("notebook_path")
                    if isinstance(target, str) and target:
                        files.pop(target, None)
                        files[target] = None
    return list(texts), list(files)[-MAX_FILES:]


def _cap(texts: list[str]) -> list[str]:
    """Each message cut to its share of MAX_DIGEST_CHARS, so the digest stays short however long they were."""
    share = MAX_DIGEST_CHARS // max(len(texts), 1)
    return [t if len(t) <= share else t[:share - 1].rstrip() + "…" for t in texts]


def digest(sid: str, records: list[Path | str]) -> tuple[list[str], list[str]]:
    """The digest of the earlier session from the first of its records that has any of its messages."""
    for path in records:
        p = Path(path)
        if not p.is_file():
            continue
        try:
            texts, files = _read_record(p, sid)
        except OSError:
            continue
        if texts or files:
            return _cap(texts), files
    return [], []


def handoff_note(sid: str, claude_home: Path | str | None, transcript: Path | str | None = None) -> str:
    log = session_file(claude_home, sid)
    records = [str(transcript)] if transcript else []
    records.append(log)
    texts, files = digest(sid, records)
    if texts:
        last = "\n\n".join(f"[{i}] {t}" for i, t in enumerate(texts, 1))
        summary = f"Its last messages, oldest first:\n\n{last}\n\n"
    else:
        summary = "There is no digest of its messages (its record wasn't found or had none).\n\n"
    summary += f"Files it edited or wrote: {', '.join(files)}\n\n" if files else ""
    where = " or ".join(records)
    return (f"(This is a fresh session. Your earlier session in this conversation, {sid}, ran on another Claude "
            "account, and a session is never resumed on a different account, so you don't have its history. Here "
            f"is a short digest of it.\n\n{summary}"
            "Catch up with read_thread and from the conversation folder's own state (git status, your notes there), "
            "then carry on with the task. Don't resume the earlier session (no /resume, no --resume) and don't read "
            f"or load its record whole. If you need one specific detail from it, look it up in {where} with grep "
            "or tail.)\n\n")
