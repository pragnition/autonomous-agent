"""The note that starts a fresh session in place of one that can't be resumed on the account it would run on.

A Claude session is never resumed on a different account from the one it ran on. To carry the work over, a fresh
session (new id, empty history) is told which earlier session to pick up from and where its record is, and reads it
itself.
"""

from __future__ import annotations

from pathlib import Path


def session_file(claude_home: Path | str | None, sid: str) -> str:
    """Where the CLI keeps the session's record: CLAUDE_CONFIG_DIR/projects/<project folder>/<sid>.jsonl. The real
    path when it's there, otherwise the pattern to look under."""
    if not claude_home:
        return f"<CLAUDE_CONFIG_DIR>/projects/*/{sid}.jsonl"
    home = Path(claude_home)
    found = sorted((home / "projects").glob(f"*/{sid}.jsonl")) if (home / "projects").is_dir() else []
    return str(found[0]) if found else str(home / "projects" / "*" / f"{sid}.jsonl")


def handoff_note(sid: str, claude_home: Path | str | None, transcript: Path | str | None = None) -> str:
    where = f"the Claude Code session log {session_file(claude_home, sid)}"
    if transcript:
        where += f" and the runner's transcript {transcript}"
    return (f"(This is a fresh session. Your earlier session in this conversation, {sid}, ran on another Claude "
            f"account, and a session is never resumed on a different account, so you don't have its history. Its "
            f"record is {where}. Read it to see what you had done and where you stopped, call read_thread to catch "
            "up on the conversation so far, then carry on with the task.)\n\n")
