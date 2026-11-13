"""Worktrees inside a sandbox (spec v2 §5): one local clone of core's git endpoint per project, and one repo per
conversation that borrows its objects (`clone --reference`). Each conversation's repo is a real `.git` directory
its session user owns, so it can commit without touching root-owned files. The sandbox token rides in git's
environment, never on a command line or in a repo's config."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path

from agent.gitutil import GitError, git

SCRATCH = (".agent-progress.md", ".agent-out/")  # session notes and saved tool output, never committed


class Worktrees:
    def __init__(self, root: Path, git_base: str, token_for_git: Callable[[], str]):
        self.root, self.git_base, self.token_for_git = Path(root), git_base.rstrip("/"), token_for_git

    def env(self) -> dict:
        return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {self.token_for_git()}",
                "GIT_CONFIG_KEY_1": "safe.directory", "GIT_CONFIG_VALUE_1": "*"}

    def clone_path(self, project: str) -> Path:
        return self.root / "clones" / project

    def ensure_clone(self, project: str) -> Path:
        path = self.clone_path(project)
        if (path / ".git").exists():
            git(path, "fetch", "-q", "--prune", "origin", env=self.env())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            git(path.parent, "clone", "-q", f"{self.git_base}/git/{project}.git", str(path), env=self.env())
        return path

    def prepare(self, conversation_id: str, project: str, branch: str, base: str) -> tuple[Path, bool]:
        """The conversation's repo on `branch`, created from origin/<base> the first time."""
        path = self.root / "work" / conversation_id
        if (path / ".git").exists():
            return path, False
        clone = self.ensure_clone(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            git(path.parent, "clone", "-q", "--no-checkout", "--reference", str(clone),
                f"{self.git_base}/git/{project}.git", str(path), env=self.env())
            git(path, "checkout", "-q", "-B", branch, f"origin/{base}", env=self.env())
        except GitError:
            shutil.rmtree(path, ignore_errors=True)  # a half-made repo would be "prepared" next time
            raise
        with (path / ".git" / "info" / "exclude").open("a") as f:
            f.write("".join(f"{name}\n" for name in SCRATCH))
        return path, True

    def prepare_read_only(self, project: str, base: str) -> Path:
        clone = self.ensure_clone(project)
        path = self.root / "ro" / project
        if (path / ".git").exists():
            git(path, "checkout", "-q", "--detach", f"origin/{base}", env=self.env())
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            git(clone, "worktree", "add", "-q", "--detach", str(path), f"origin/{base}", env=self.env())
        return path

    def push(self, worktree: Path, branch: str) -> str:
        git(worktree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=self.env())
        return git(worktree, "rev-parse", "HEAD", env=self.env()).strip()

    def head(self, worktree: Path) -> str:
        return git(worktree, "rev-parse", "HEAD", env=self.env()).strip()

    def remove(self, conversation_id: str, project: str) -> None:
        shutil.rmtree(self.root / "work" / conversation_id, ignore_errors=True)
