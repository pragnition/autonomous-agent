"""Hand Jensen a turn from the host, without going through Slack: an existing conversation, or a fresh one.

    python scripts/ask_jensen.py --conversation V-xyz --text-file prompt.md
    python scripts/ask_jensen.py --new --text-file -        # pipe the text on stdin

Pipes `core_ask.py` into the live install's `agent-control` container (`agent.watch.host.Host.core_python`) and
prints the queued work item id.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agent.watch.host import Host  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--conversation", help="an existing conversation id")
    group.add_argument("--new", action="store_true", help="start a fresh V-review-<hex> conversation")
    parser.add_argument("--text-file", required=True, help="path to the text to send, or - for stdin")
    args = parser.parse_args(argv)

    text = sys.stdin.read() if args.text_file == "-" else Path(args.text_file).read_text()
    text_b64 = base64.b64encode(text.encode()).decode()

    core_args = ["--text-b64", text_b64]
    if args.conversation:
        core_args = ["--conversation", args.conversation, *core_args]

    item_id = Host.default().core_python("core_ask.py", core_args).strip()
    print(item_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
