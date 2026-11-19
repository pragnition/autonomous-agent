"""Shim: puts `src` on `sys.path` and runs the deploy pipeline.

    python scripts/deploy.py [--wait-idle-s N] [--if-pending] [--dry-run]

See `agent.watch.deploy` for what it does: which services a change touches, waiting for an idle runner before
touching `agent`, the fast-forward and rebuild, and the live check afterward.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agent.watch.deploy import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
