"""Shim: puts `src` on `sys.path` and runs the watch CLI.

    python scripts/jensen_watch.py <subcommand> ...

See `agent.watch.cli` for what each subcommand does.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.watch.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
