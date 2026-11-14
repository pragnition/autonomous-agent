"""core's git endpoint: a bare mirror of each project that the agent container clones from and pushes to.

The agent clones and pushes pack repos through this endpoint. It may push only to refs/heads/<agent>/* here (a
pre-receive hook refuses everything else, including deletes and tags), and core pushes to GitHub from here when
open_pr is called.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from pathlib import Path

from aiohttp import web

from agent.gitutil import GitError, credential_args, git

PRE_RECEIVE = """#!/bin/sh
# Installed by core: the agent may push only to refs/heads/{agent}/*, and never delete.
while read old new ref; do
  case "$ref" in
    refs/heads/{agent}/*) ;;
    *) echo "refused: only refs/heads/{agent}/* may be pushed (got $ref)" >&2; exit 1 ;;
  esac
  if ! echo "$new" | grep -q '[^0]'; then
    echo "refused: deleting $ref" >&2; exit 1
  fi
done
"""


def _bearer_or_basic(header: str) -> str | None:
    if header.startswith("Bearer "):
        return header[7:]
    if header.startswith("Basic "):
        try:
            return base64.b64decode(header[6:]).decode().split(":", 1)[1]
        except (ValueError, IndexError):
            return None
    return None


class GitHost:
    def __init__(self, root: Path, *, agent_name: str, read_token: str | None,
                 upstream_url: Callable[[str], str] = lambda repo: f"https://github.com/{repo}.git",
                 auth: Callable[[str | None, str], bool] = lambda token, project: False):
        self.root, self.agent_name, self.read_token = Path(root), agent_name, read_token
        self.upstream_url, self.auth = upstream_url, auth
        self._repos: dict[str, str] = {}

    def path(self, project: str) -> Path:
        return self.root / f"{project}.git"

    def ensure(self, project: str, repo: str) -> Path:
        path = self.path(project)
        self._repos[project] = repo
        if not (path / "HEAD").exists():
            cred, env = credential_args(self.read_token)
            self.root.mkdir(parents=True, exist_ok=True)
            git(self.root, *cred, "clone", "-q", "--bare", self.upstream_url(repo), str(path), env=env)
            git(path, "config", "http.receivepack", "true")
        hook = path / "hooks" / "pre-receive"
        hook.write_text(PRE_RECEIVE.format(agent=self.agent_name))
        hook.chmod(0o755)
        return path

    def refresh(self, project: str) -> None:
        cred, env = credential_args(self.read_token)
        git(self.path(project), *cred, "fetch", "-q", "--prune", self.upstream_url(self._repos[project]),
            "+refs/heads/*:refs/heads/*", f"^refs/heads/{self.agent_name}/*", env=env)

    def tip(self, project: str, branch: str) -> str | None:
        try:
            return git(self.path(project), "rev-parse", "--verify", "-q", f"refs/heads/{branch}").strip() or None
        except GitError:
            return None

    def push_to_github(self, project: str, repo: str, branch: str, sha: str, write_token: str) -> None:
        if not branch.startswith(f"{self.agent_name}/"):
            raise GitError(f"refusing to push {branch!r}: only {self.agent_name}/* branches go to GitHub")
        cred, env = credential_args(write_token)
        git(self.path(project), *cred, "push", "-q", self.upstream_url(repo), f"{sha}:refs/heads/{branch}", env=env)

    async def aiohttp_handler(self, request: web.Request) -> web.StreamResponse:
        """Serve `git http-backend` for /git/<project>.git/<tail>, for runner or session tokens."""
        token = _bearer_or_basic(request.headers.get("Authorization", ""))
        project = request.match_info["project"]
        if not self.auth(token, project):
            return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="agent"'})
        if not self.path(project).is_dir():
            return web.Response(status=404)
        env = {"GIT_PROJECT_ROOT": str(self.root), "GIT_HTTP_EXPORT_ALL": "1", "REMOTE_USER": "agent",
               "PATH_INFO": f"/{project}.git/{request.match_info['tail']}", "REQUEST_METHOD": request.method,
               "QUERY_STRING": request.query_string, "CONTENT_TYPE": request.headers.get("Content-Type", ""),
               "PATH": "/usr/bin:/bin"}
        # aiohttp has already inflated a gzipped body (git gzips requests over 1 KB), so http-backend gets it plain
        # and is never told it's compressed
        body = await request.read()
        proc = await asyncio.create_subprocess_exec("git", "http-backend", env=env, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate(body)
        head, _, payload = out.partition(b"\r\n\r\n")
        if not _:
            head, _, payload = out.partition(b"\n\n")
        status, headers = 200, {}
        for line in head.decode(errors="replace").splitlines():
            key, _, value = line.partition(":")
            if key.lower() == "status":
                status = int(value.strip().split()[0])
            elif key:
                headers[key.strip()] = value.strip()
        return web.Response(status=status, body=payload, headers=headers)


def tokens_auth(resolve: Callable[[str], object]) -> Callable[[str | None, str], bool]:
    """Any live runner or session token (resolve returns None for unknown, expired or revoked tokens). One runner
    works on every project, so such a token reaches every repo this install knows."""
    def check(token: str | None, project: str) -> bool:
        info = resolve(token) if token else None
        return info is not None and info.kind in ("runner", "session")
    return check

