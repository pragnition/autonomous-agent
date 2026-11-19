"""The memory notes a yardstick run left behind, removed from inside `agent-runner` when the run ends.

Self-contained on purpose, like `runner_feedback.py`: piped into the `agent-runner` container's own `python3` on
stdin (`docker exec -i agent-runner python3 - <args>`, see `agent.watch.host.Host.runner_python`), so it imports
only the standard library. `--root` exists so a test can point it at a tmp folder instead of the real one.

Jensen's memory is a flat folder (`/var/agent/memory`): one `*.md` file per fact, and `MEMORY.md`, the index, with
one line per fact naming its file. Yardstick turns run with that real memory, so a turn that saves a note anyway
would carry a task's answers into the next run and into people's work. Two calls:

- `--root R --snapshot FILE`, as the run starts: records the names of the notes that exist now in `FILE`.
- `--root R --since EPOCH --marker M [--existing-file FILE]`, as the run ends: removes each note modified at or
  after `EPOCH` whose text contains `M` (`/var/agent/yardstick/`), and the index lines naming those files.

A note Jensen wrote for real work must never go (it can be written during the run too: Jensen keeps serving people
while the yardstick runs). Only the marker singles a note out, and with `--existing-file`, a note that was already
there when the run started is kept even if it was touched during the run and now mentions the marker: deleting it
would lose everything it held before. A snapshot that can't be read keeps every note. Prints a JSON report
(`removed`, `index_lines`, `kept`), which the caller reduces to counts: note names can carry people's names.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = "/var/agent/memory"
INDEX = "MEMORY.md"


def _notes(root: Path) -> list[Path]:
    """The fact files directly in `root`: regular `*.md` files, never the index and never a symlink."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.suffix == ".md" and p.name != INDEX and p.is_file() and not p.is_symlink())


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def snapshot(root: Path, out: Path) -> dict:
    names = [p.name for p in _notes(root)]
    out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(out, json.dumps({"notes": names}))
    return {"notes": len(names)}


def _existing(existing_file: Path) -> set[str] | None:
    try:
        names = json.loads(existing_file.read_text()).get("notes")
    except Exception:
        return None
    return {name for name in names if isinstance(name, str)} if isinstance(names, list) else None


def _mentions(path: Path, marker: str) -> bool:
    try:
        return marker in path.read_text(errors="replace")
    except OSError:  # a note that can't be read is never removed
        return False


def _names_one_of(line: str, names: list[str]) -> bool:
    """Whether `line` names one of the files as a whole name, so removing `new-marked.md` leaves a line about
    `renew-marked.md` alone."""
    return any(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", line) for name in names)


def _drop_index_lines(index: Path, names: list[str]) -> int:
    if not names or not index.is_file():
        return 0
    lines = index.read_text(errors="replace").splitlines(keepends=True)
    kept = [line for line in lines if not _names_one_of(line, names)]
    if len(kept) != len(lines):
        _atomic_write(index, "".join(kept))
    return len(lines) - len(kept)


def clean(root: Path, since: float, marker: str, existing_file: Path | None = None) -> dict:
    """Remove the notes modified at or after `since` that contain `marker` -- except, with `existing_file`, any note
    it names (it existed before the run), or every note when it can't be read -- and their index lines."""
    root = Path(root)
    existing = _existing(Path(existing_file)) if existing_file is not None else None
    removed: list[str] = []
    kept: list[str] = []
    for note in _notes(root):
        try:
            modified = note.stat().st_mtime
        except OSError:
            continue
        if modified < since or not _mentions(note, marker):
            continue
        if existing_file is not None and (existing is None or note.name in existing):
            kept.append(note.name)
            continue
        try:
            note.unlink()
        except OSError:
            kept.append(note.name)
            continue
        removed.append(note.name)
    return {"removed": removed, "index_lines": _drop_index_lines(root / INDEX, removed), "kept": kept}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=ROOT)
    parser.add_argument("--snapshot", help="record the notes that exist now in this file, and do nothing else")
    parser.add_argument("--since", type=float)
    parser.add_argument("--marker")
    parser.add_argument("--existing-file")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if args.snapshot:
        print(json.dumps(snapshot(root, Path(args.snapshot))))
        return 0
    if args.since is None or not args.marker:
        parser.error("--since and --marker are required unless --snapshot is given")
    existing_file = Path(args.existing_file) if args.existing_file else None
    print(json.dumps(clean(root, args.since, args.marker, existing_file)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
