import asyncio

import agent.services as services_main_mod
from agent.services import git_identity, keep_services


def test_git_identity_takes_the_github_noreply_email():
    env = git_identity("jensen", git_email="7+jensen-bot@users.noreply.github.com")
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "7+jensen-bot@users.noreply.github.com"
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "jensen"
    assert git_identity("jensen")["GIT_AUTHOR_EMAIL"] == "jensen@users.noreply.github.com"


def test_services_command_prepares_home_then_keeps_the_services(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(services_main_mod, "prepare_home", lambda root: calls.append(("prepare", root)))
    monkeypatch.setattr(services_main_mod.asyncio, "run", lambda coro: (calls.append(("run",)), coro.close()))
    monkeypatch.setattr(services_main_mod.os, "environ", {})

    assert services_main_mod.services_main(["--root", str(tmp_path), "--agent-name", "jensen"]) == 0

    assert calls == [("prepare", tmp_path), ("run",)]
    assert services_main_mod.os.environ["GIT_AUTHOR_NAME"] == "jensen"  # services run with the agent's git identity


async def test_services_stop_before_the_process_exits():
    stopped = asyncio.Event()

    async def services():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    stopping = asyncio.Event()
    kept = asyncio.create_task(keep_services(services(), stopping))
    await asyncio.sleep(0)
    assert not stopped.is_set()
    stopping.set()  # SIGTERM
    await kept
    assert stopped.is_set()


def test_python_m_agent_services(monkeypatch):
    import agent.__main__ as entry

    seen = []
    monkeypatch.setattr(services_main_mod, "services_main", lambda argv: seen.append(argv) or 0)
    assert entry.main(["services", "--root", "/x"]) == 0
    assert seen == [["--root", "/x"]]
