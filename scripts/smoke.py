"""End-to-end smoke test on a real install: queue one task on the `sample` pack and wait for its draft PR.

Run inside the core container with the stack up: `just smoke`. Needs real Slack, GitHub, Linear and proxy setup.
"""

from __future__ import annotations

import sys
import time

TIMEOUT_S = 45 * 60


def main() -> int:
    from agent import db, paths, tasks
    from agent.tasks import Status

    conn = db.connect(paths.DB_DIR / "agent.db")
    task = tasks.create_task(conn, project="sample", title="Smoke test: add a hello() function",
                             description="Add hello() returning 'hello' to hello.py, with a test.", kind="chore",
                             source="slack", source_ref=None, requester=None, owner=None)
    print(f"queued {task.id}; waiting for the dispatcher (up to {TIMEOUT_S // 60} min)", flush=True)
    deadline, last = time.time() + TIMEOUT_S, None
    while time.time() < deadline:
        task = tasks.get_task(conn, task.id)
        if task.status != last:
            print(f"{task.id}: {task.status.value}", flush=True)
            last = task.status
        if task.status == Status.ready_for_review:
            print(f"PASS: draft PR {task.pr_url}")
            return 0
        if task.status in (Status.failed, Status.needs_input, Status.cancelled):
            print(f"FAIL: {task.id} ended {task.status.value}")
            return 1
        time.sleep(10)
    print("FAIL: timed out")
    return 1


if __name__ == "__main__":
    sys.exit(main())
