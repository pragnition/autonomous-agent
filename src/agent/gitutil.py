"""Git helpers shared by core's git host and the runner."""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from pathlib import Path


class GitError(Exception):
    pass


def slugify(title: str, max_len: int = 40) -> str:
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
    return slug[:max_len].strip("-") or "task"


def branch_name(agent_name: str, task_id: str, title: str) -> str:
    return f"{agent_name}/{task_id}-{slugify(title)}"


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-2000:]}")
    return proc.stdout


def credential_args(token: str | None) -> tuple[list[str], dict | None]:
    """git -c args and env for a token, via a credential helper reading the environment: the token never appears
    on a command line other users could see in `ps`."""
    if not token:
        return [], None
    return (["-c", "credential.helper=",
             "-c", "credential.helper=!f() { echo username=x-access-token; echo password=$AGENT_PUSH_TOKEN; }; f"],
            {**os.environ, "AGENT_PUSH_TOKEN": token})
