"""Jensen's own notes: read, answered and logged, from inside `agent-runner`.

Self-contained on purpose: piped into the `agent-runner` container's own `python3` on stdin
(`docker exec -i agent-runner python3 - <args>`, see `agent.watch.host.Host.runner_python`) rather than imported
there, so improve-jensen can change how it reads or answers notes without ever rebuilding or redeploying the
runner's image. `agent-runner`'s `python3` is 3.12 and can import only the standard library -- nothing from
`agent.*` here, not even the rest of this package.

Notes live at `/var/agent/feedback/<YYYY-MM-DD>-<slug>.md`, one per thing that got in Jensen's way; `CHANGES.md`
in the same folder is improve-jensen's own running reply log, never a note Jensen wrote, and `collect` always
leaves it out. It's also plain enough to import directly in tests, against a tmp folder, the same way
`core_collect.py` is tested directly against a tmp DB -- `--root` exists so a test (or, later, a different
install layout) can point every subcommand at something other than the real path.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = "/var/agent/feedback"
CHANGES_NAME = "CHANGES.md"
CHANGES_HEADING = "# What changed for Jensen"
MAX_CHANGES = 30

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse(value: str) -> datetime:
    return datetime.strptime(value, _FMT).replace(tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_FMT)


def _check_name(name: str) -> None:
    if "/" in name or ".." in name or not name.endswith(".md"):
        raise ValueError(f"bad note name: {name!r}")


def collect(root: str | Path, since: str, until: str) -> list[dict]:
    """Notes under `root` (excluding `CHANGES.md`) whose mtime falls in `(since, until]`, each as `{name, text,
    modified}` (`modified` is the file's mtime, ISO). A missing folder gives `[]`. A note that can't be stat'd
    or read (removed mid-scan, a permission hiccup) is skipped rather than failing the whole collection."""
    root = Path(root)
    if not root.is_dir():
        return []
    since_dt, until_dt = _parse(since), _parse(until)
    out = []
    for path in sorted(root.glob("*.md")):
        if path.name == CHANGES_NAME:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if not (since_dt < mtime <= until_dt):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        out.append({"name": path.name, "text": text, "modified": _iso(mtime)})
    return out


def reply(root: str | Path, name: str, status: str, now: datetime) -> None:
    """Append `\\nStatus (<ISO now>): <status>\\n` to `root/name`. Raises `ValueError` for a name containing `/`
    or `..`, or one not ending in `.md` -- checked before anything touches the filesystem."""
    _check_name(name)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / name).open("a") as fh:
        fh.write(f"\nStatus ({_iso(now)}): {status}\n")


def add_change(root: str | Path, text: str, now: datetime) -> None:
    """Prepend `- <YYYY-MM-DD>: <text>` to `root/CHANGES.md` (creating it with `CHANGES_HEADING` first if it
    doesn't exist yet), keeping only the newest `MAX_CHANGES` entries, newest first."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / CHANGES_NAME
    existing = []
    if path.is_file():
        existing = [line for line in path.read_text().splitlines() if line.startswith("- ")]
    date = now.astimezone(UTC).strftime("%Y-%m-%d")
    entries = ([f"- {date}: {text}"] + existing)[:MAX_CHANGES]
    path.write_text(CHANGES_HEADING + "\n\n" + "\n".join(entries) + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    collect_p = sub.add_parser("collect")
    collect_p.add_argument("--since", required=True)
    collect_p.add_argument("--until", required=True)
    collect_p.add_argument("--root", default=ROOT)

    reply_p = sub.add_parser("reply")
    reply_p.add_argument("--name", required=True)
    reply_p.add_argument("--status-b64", required=True)
    reply_p.add_argument("--now", required=True)
    reply_p.add_argument("--root", default=ROOT)

    change_p = sub.add_parser("change")
    change_p.add_argument("--text-b64", required=True)
    change_p.add_argument("--now", required=True)
    change_p.add_argument("--root", default=ROOT)

    args = parser.parse_args(argv)

    if args.command == "collect":
        print(json.dumps(collect(args.root, args.since, args.until)))
        return 0
    if args.command == "reply":
        status = base64.b64decode(args.status_b64).decode()
        reply(args.root, args.name, status, _parse(args.now))
        print("ok")
        return 0
    if args.command == "change":
        text = base64.b64decode(args.text_b64).decode()
        add_change(args.root, text, _parse(args.now))
        print("ok")
        return 0
    raise AssertionError(f"unhandled command: {args.command!r}")  # pragma: no cover - argparse already validated


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
