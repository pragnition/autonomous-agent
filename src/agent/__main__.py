"""python -m agent core|runner|kpis|supervisor|doctor|stop|resume"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["kpis"]:  # pickup, PR and merge times, queue wait, merge rate
        from agent.core.kpis import main as show_kpis
        return show_kpis(argv[1:])
    if argv[:1] == ["runner"]:  # the agent container's runner
        from agent.runner.main import main as run_runner
        return run_runner(argv[1:])
    parser = argparse.ArgumentParser(prog="agent", description="Autonomous engineering agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("core", help="run the core service: Slack, the work queue and pollers")
    sub.add_parser("runner", help="run in the agent container: the runner that works on core's queue")
    sub.add_parser("kpis", help="show the KPIs: pickup, PR and merge times, queue wait, merge rate")
    sub.add_parser("supervisor", help="run the health watchdog")
    sub.add_parser("doctor", help="check config, secrets, the proxy, GitHub and Linear")
    sub.add_parser("stop", help="stop everything: pause new work and cancel what is running")
    sub.add_parser("resume", help="resume after a stop")
    command = parser.parse_args(argv).command
    if command == "core":
        from agent.core.app import main as run
    elif command == "supervisor":
        from agent.supervisor import main as run
    elif command in ("stop", "resume"):
        return _local(command)
    else:
        from agent.doctor import main as run
    return run() or 0


def _local(command: str) -> int:
    from agent import ops, paths

    db_path = paths.DB_DIR / "agent.db"
    if command == "resume":
        ops.resume(db_path)
    else:
        ops.stop(db_path)
    print(f"{command}: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
