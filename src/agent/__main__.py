"""python -m agent services|supervisor"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["services"]:  # the agent's own long-running services, beside agentd's conversation containers
        from agent.services import services_main
        return services_main(argv[1:])
    parser = argparse.ArgumentParser(prog="agent", description="Autonomous engineering agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("services", help="keep the agent's own long-running services going")
    sub.add_parser("supervisor", help="run the health watchdog")
    parser.parse_args(argv)
    from agent.supervisor import main as run
    return run() or 0


if __name__ == "__main__":
    sys.exit(main())
