"""Jensen's own notes: read, answered and logged, from inside `agent-control`.

Self-contained on purpose: piped into the `agent-control` container's own `python3` on stdin
(`docker exec -i agent-control python3 - <args>`, see `agent.watch.host.Host.runner_python`) rather than imported
there, so improve-jensen can change how it reads or answers notes without ever rebuilding or redeploying the
agent image. `agent-control`'s `python3` is 3.12 and can import only the standard library -- nothing from
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
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = "/var/agent/feedback"
CHANGES_NAME = "CHANGES.md"
CHANGES_HEADING = "# What changed for Jensen"
MAX_CHANGES = 30
# A note's text is capped at this many characters -- a runaway note must not blow out the digest the way an
# unbounded tool/error line could.
MAX_NOTE_CHARS = 4_000
_TRUNCATED_SUFFIX = "\n...[truncated]"

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
    modified}` (`modified` is the file's mtime, ISO; `text` capped at `MAX_NOTE_CHARS`, marked with
    `_TRUNCATED_SUFFIX` when cut). A missing folder gives `[]`.

    A note is read with `errors="replace"`, so one note with badly-encoded bytes is still returned (with the bad
    bytes replaced) rather than raising and losing every other note in the window along with it. Only a note
    that can't be stat'd or read *at all* (removed mid-scan, a permission error) is skipped, and such skips are
    counted and reported on stderr -- never raised, since one bad note must not cost the rest of the window."""
    root = Path(root)
    if not root.is_dir():
        return []
    since_dt, until_dt = _parse(since), _parse(until)
    out = []
    skipped = 0
    for path in sorted(root.glob("*.md")):
        if path.name == CHANGES_NAME:
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        except OSError:
            skipped += 1
            continue
        if not (since_dt < mtime <= until_dt):
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            skipped += 1
            continue
        if len(text) > MAX_NOTE_CHARS:
            text = text[:MAX_NOTE_CHARS] + _TRUNCATED_SUFFIX
        out.append({"name": path.name, "text": text, "modified": _iso(mtime)})
    if skipped:
        print(f"warning: skipped {skipped} note(s) that could not be read", file=sys.stderr)
    return out


def reply(root: str | Path, name: str, status: str, now: datetime) -> None:
    """Append `\\nStatus (<ISO now>): <status>\\n` to `root/name`, then restore the note's own mtime to what it
    was before the append -- otherwise answering a note would bump its mtime into the *next* window, and the
    next hourly `collect` would hand the now-answered note back as if it were new, forever. Raises `ValueError`
    for a name containing `/` or `..`, or one not ending in `.md` -- checked before anything touches the
    filesystem. Raises `FileNotFoundError` for a note that doesn't exist: replying never creates one."""
    _check_name(name)
    path = Path(root) / name
    before = path.stat()  # raises FileNotFoundError here, before any write, when the note doesn't exist
    with path.open("a") as fh:
        fh.write(f"\nStatus ({_iso(now)}): {status}\n")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def add_change(root: str | Path, text: str, now: datetime) -> None:
    """Prepend `- <YYYY-MM-DD>: <text>` to `root/CHANGES.md` (creating it with `CHANGES_HEADING` first if it
    doesn't exist yet), keeping only the newest `MAX_CHANGES` entries, newest first. `text` has its whitespace
    flattened to a single line first (`" ".join(text.split())`), so an embedded newline can't start what looks
    like its own, unprefixed entry in the file."""
    text = " ".join(text.split())
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
