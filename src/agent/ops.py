"""Operations run on the laptop itself (`just stop` / `just resume`), for when Slack is not an option."""

from __future__ import annotations

from pathlib import Path

from agent import db, events


def stop(db_path: Path) -> list[str]:
    from agent.core.sessions import WorkQueue

    conn = db.connect(db_path)
    db.set_setting(conn, "paused", "1")
    cancelled = WorkQueue(conn).cancel_running()
    events.append(conn, "stop_all", task_id=None, actor="local", data={"cancelled": cancelled})
    return cancelled


def resume(db_path: Path) -> None:
    conn = db.connect(db_path)
    db.set_setting(conn, "paused", "0")
    events.append(conn, "resume_all", task_id=None, actor="local")
