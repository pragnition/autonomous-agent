import asyncio
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from agent import gitutil
from agent.core.gitserve import GitHost


def run(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def commit(repo: Path, name: str, branch: str = "main") -> str:
    (repo / name).write_text(name)
    if run(repo, "rev-parse", "-q", "--verify", "HEAD", check=False).returncode == 0:
        run(repo, "checkout", "-q", branch)
    run(repo, "add", name)
    run(repo, "commit", "-q", "-m", name)
    return run(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def upstream(tmp_path, git_env):
    repo = tmp_path / "upstream"
    repo.mkdir()
    run(repo, "init", "-q", "-b", "main")
    commit(repo, "a.txt")
    run(repo, "branch", "agent/old")            # the agent's earlier branch on "GitHub"
    return repo


@pytest.fixture
def githost(tmp_path, upstream):
    host = GitHost(tmp_path / "git", agent_name="agent", read_token=None, upstream_url=lambda repo: str(upstream),
                   auth=lambda token, project: token == "good" and project == "p")
    host.ensure("p", "o/r")
    return host


@pytest.fixture
async def githost_server(githost):
    app = web.Application()
    app.router.add_route("*", "/git/{project}.git/{tail:.*}", githost.aiohttp_handler)
    server = TestServer(app)
    await server.start_server()
    yield f"http://127.0.0.1:{server.port}/git/p.git"
    await server.close()


async def git_async(cwd, *args):
    return await asyncio.to_thread(run, cwd, "-c", "http.extraHeader=Authorization: Bearer good", *args, check=False)


async def test_clone_over_http(githost_server, tmp_path):
    r = await git_async(tmp_path, "clone", "-q", githost_server, "work")
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "work" / "a.txt").read_text() == "a.txt"


async def test_clone_whose_request_git_gzips(githost, upstream, githost_server, tmp_path):
    """Wanting this many commits makes the request body bigger than 1 KB, so git sends it gzipped."""
    for i in range(40):
        run(upstream, "checkout", "-q", "-b", f"feat/{i}", "main")
        commit(upstream, f"f{i}.txt", branch=f"feat/{i}")
    githost.refresh("p")
    r = await git_async(tmp_path, "clone", "-q", githost_server, "work")
    assert r.returncode == 0, r.stderr


async def test_rejects_main_tags_and_deletes(githost_server, tmp_path):
    work = tmp_path / "work"
    assert (await git_async(tmp_path, "clone", "-q", githost_server, "work")).returncode == 0
    commit(work, "b.txt")
    assert (await git_async(work, "push", "-q", "origin", "HEAD:refs/heads/main")).returncode != 0
    assert (await git_async(work, "push", "-q", "origin", "HEAD:refs/tags/release")).returncode != 0
    assert (await git_async(work, "push", "-q", "origin", "HEAD:refs/heads/agent/t1-x")).returncode == 0
    assert (await git_async(work, "push", "-q", "origin", ":refs/heads/agent/t1-x")).returncode != 0


async def test_wrong_token_is_refused(githost_server, tmp_path):
    r = await asyncio.to_thread(run, tmp_path, "-c", "http.extraHeader=Authorization: Bearer bad", "clone", "-q",
                                githost_server, "work", check=False)
    assert r.returncode != 0


def test_refresh_brings_upstream_commits(githost, upstream):
    new = commit(upstream, "c.txt")
    githost.refresh("p")
    assert githost.tip("p", "main") == new


def test_refresh_never_rewinds_agent_branches(githost, upstream):
    local = githost.tip("p", "agent/old")
    run(upstream, "checkout", "-q", "agent/old")
    commit(upstream, "z.txt", branch="agent/old")
    githost.refresh("p")
    assert githost.tip("p", "agent/old") == local
    assert githost.tip("p", "nope") is None


def test_push_to_github_never_puts_token_in_argv(githost, monkeypatch):
    seen = []

    def fake_run(args, **kw):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(gitutil.subprocess, "run", fake_run)
    githost.push_to_github("p", "o/r", "agent/t1-x", "a" * 40, "SECRET")
    argv = [a for call in seen for a in call]
    assert all("SECRET" not in a for a in argv) and "--force" not in argv and "-f" not in argv
    assert f"{'a' * 40}:refs/heads/agent/t1-x" in argv


def test_push_to_github_refuses_other_branches(githost):
    with pytest.raises(gitutil.GitError):
        githost.push_to_github("p", "o/r", "main", "a" * 40, "SECRET")


def test_any_live_agent_token_reaches_every_repo(tmp_path):
    from agent import db
    from agent.core.gitserve import tokens_auth
    from agent.core.sessions import mint_token, resolve_token, revoke_for_work_item

    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    check = tokens_auth(lambda token: resolve_token(conn, token))

    runner = mint_token(conn, "runner", ttl_s=3600)
    assert check(runner, "sample") and check(runner, "other")     # one runner, every repo

    session = mint_token(conn, "session", work_item_id="w1", ttl_s=3600)
    assert check(session, "sample") and check(session, "other")
    revoke_for_work_item(conn, "w1")
    assert not check(session, "sample")

    expired = mint_token(conn, "runner", ttl_s=-1)
    assert not check(expired, "sample")

    assert not check("not-a-token", "sample")
    assert not check(None, "sample")
