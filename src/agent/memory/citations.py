"""Code citations on facts: re-hashed before a fact is briefed, so a changed file makes the fact stale."""

from __future__ import annotations

import hashlib
from pathlib import Path


def _range(lines) -> tuple[int, int]:
    if isinstance(lines, str):
        start, _, end = lines.partition("-")
        return int(start), int(end or start)
    start, end = lines
    return int(start), int(end)


class CitationChecker:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root).resolve()

    @staticmethod
    def hash_lines(path: Path, start: int, end: int) -> str:
        lines = Path(path).read_text().splitlines()
        return hashlib.sha256("\n".join(lines[start - 1:end]).encode()).hexdigest()

    def is_current(self, citation: dict) -> bool:
        try:
            path = (self.repo_root / citation["path"]).resolve()
            if self.repo_root not in path.parents or not path.is_file():
                return False
            start, end = _range(citation["lines"])
            return self.hash_lines(path, start, end) == citation["sha256"]
        except (KeyError, ValueError, OSError, UnicodeDecodeError):
            return False
