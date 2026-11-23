"""Hand Jensen a turn from the host, without going through Slack: an existing conversation, or a fresh one.

    python scripts/ask_jensen.py --conversation V-xyz --text-file prompt.md
    python scripts/ask_jensen.py --new --text-file -        # pipe the text on stdin

Queues it through agentd's own `ask` in the live install's `agent-control` container, marked as improve-jensen's
(its source and origin), and prints the queued work item id. A fresh conversation's id starts with `V-review-`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent.watch.host import CORE_CONTAINER, Host  # noqa: E402
from agent.watch.state import IMPROVE_JENSEN_SOURCE  # noqa: E402

ORIGIN = "improve-jensen review"
NEW_PREFIX = "V-review-"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--conversation", help="an existing conversation id")
    group.add_argument("--new", action="store_true", help="start a fresh V-review-<hex> conversation")
    parser.add_argument("--text-file", required=True, help="path to the text to send, or - for stdin")
    args = parser.parse_args(argv)

    text = sys.stdin.read() if args.text_file == "-" else Path(args.text_file).read_text()
    target = ["--conversation", args.conversation] if args.conversation else ["--new-prefix", NEW_PREFIX]
    command = ["docker", "exec", "-i", CORE_CONTAINER, "agentd", "ask", *target, "--text-file", "-",
               "--source", IMPROVE_JENSEN_SOURCE, "--origin", ORIGIN]
    result = Host.default().run(command, input=text, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr.strip() or f"agentd ask exited {result.returncode}", file=sys.stderr)
        return 1
    print(result.stdout.strip().splitlines()[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
