"""Worktrees inside a sandbox (spec v2 §5): one local clone of core's git endpoint per project, and one repo per
conversation that borrows its objects (`clone --reference`), inside that conversation's session folder. Each
conversation's repo is a real `.git` directory its session user owns, so it can commit without touching root-owned
files. The sandbox token rides in git's environment, never on a command line or in a repo's config."""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from agent.gitutil import GitError, git

SCRATCH = (".agent-progress.md", ".agent-out/")  # session notes and saved tool output, never committed


class CheckoutRefused(Exception):
    """What is already at a checkout's path isn't this conversation's checkout; it is left as it is."""


class Worktrees:
    def __init__(self, root: Path, git_base: str, token_for_git: Callable[[], str]):
        self.root, self.git_base, self.token_for_git = Path(root), git_base.rstrip("/"), token_for_git
        self._clone_locks: dict[str, threading.Lock] = {}

    def env(self) -> dict:
        return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {self.token_for_git()}",
                "GIT_CONFIG_KEY_1": "safe.directory", "GIT_CONFIG_VALUE_1": "*"}

    def clone_path(self, project: str) -> Path:
        return self.root / "clones" / project

    def ensure_clone(self, project: str) -> Path:
        """The project's shared clone, fetched. One at a time per project: conversations preparing their checkouts
        at once would otherwise clone into, or fetch in, the same directory together."""
        path = self.clone_path(project)
        with self._clone_locks.setdefault(project, threading.Lock()):
            if (path / ".git").exists():
                git(path, "fetch", "-q", "--prune", "origin", env=self.env())
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                git(path.parent, "clone", "-q", f"{self.git_base}/git/{project}.git", str(path), env=self.env())
        return path

    def prepare(self, path: Path, project: str, branch: str, base: str) -> tuple[Path, bool]:
        """The conversation's repo at `path` on `branch`, created from origin/<base> the first time. The session
        folder is the agent's to write in, so what is already at `path` is used only when it is this checkout (core's
        repo for the project, on the conversation's branch), and is otherwise refused and left alone. A new repo is
        made beside `path` and moved into place once it is complete, so a failure leaves nothing behind."""
        url = f"{self.git_base}/git/{project}.git"
        if (path / ".git").exists():
            self._check_reusable(path, url, branch)
            return path, False
        if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
            raise CheckoutRefused(f"{path} already exists and isn't a git checkout; move it out of the way to get "
                                  f"a checkout of {project} there")
        clone = self.ensure_clone(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.preparing-{uuid.uuid4().hex[:8]}"
        try:
            git(path.parent, "clone", "-q", "--no-checkout", "--reference", str(clone), url, str(tmp),
                env=self.env())
            git(tmp, "checkout", "-q", "-B", branch, f"origin/{base}", env=self.env())
            with (tmp / ".git" / "info" / "exclude").open("a") as f:
                f.write("".join(f"{name}\n" for name in SCRATCH))
            os.rename(tmp, path)  # replaces only a missing or empty folder
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)  # only what this call made
            raise
        return path, True

    def _check_reusable(self, path: Path, url: str, branch: str) -> None:
        try:
            origin = git(path, "remote", "get-url", "origin", env=self.env()).strip()
        except GitError:
            origin = ""
        if origin != url:
            raise CheckoutRefused(f"{path} is a git repo whose origin is {origin or 'not set'}, not {url}; it was "
                                  "left as it is")
        try:
            head = git(path, "symbolic-ref", "-q", "--short", "HEAD", env=self.env()).strip()
        except GitError:
            head = "a detached HEAD"
        if head != branch:
            raise CheckoutRefused(f"{path} is on {head}, not this conversation's branch {branch}; switch back with "
                                  f"`git -C {path} checkout {branch}`")

    def push(self, worktree: Path, branch: str) -> str:
        git(worktree, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=self.env())
        return git(worktree, "rev-parse", "HEAD", env=self.env()).strip()

    def head(self, worktree: Path) -> str:
        return git(worktree, "rev-parse", "HEAD", env=self.env()).strip()
