import asyncio

from agent.sandbox.runners import RunnerSet, profile_for, read_tokens


class FakeRunner:
    def __init__(self, name, token, finish=False):
        self.name, self.token, self.finish = name, token, finish
        self.started = asyncio.Event()

    async def run_forever(self):
        self.started.set()
        if not self.finish:
            await asyncio.Event().wait()


def runner_set(finish=False):
    made = []

    def make(name, token):
        made.append(FakeRunner(name, token, finish))
        return made[-1]

    return RunnerSet(make), made


def test_profile_for():
    assert [profile_for(q) for q in ("general", "code-sample", "debug-sample")] == ["general", "code", "debug"]


async def test_starts_one_runner_per_queue():
    runners, made = runner_set()
    await runners.reconcile({"code-sample": "a", "general": "b"})
    assert sorted((r.name, r.token) for r in made) == [("code-sample", "a"), ("general", "b")]
    await asyncio.wait_for(asyncio.gather(*(r.started.wait() for r in made)), 1)
    await runners.stop()


async def test_restarts_a_runner_when_its_token_changes():
    runners, made = runner_set()
    await runners.reconcile({"general": "b"})
    old = runners.tasks["general"]
    await runners.reconcile({"general": "c"})
    await asyncio.sleep(0)
    assert old.cancelled() and [(r.name, r.token) for r in made] == [("general", "b"), ("general", "c")]
    await runners.stop()


async def test_finished_runner_restarts_only_with_a_new_token():
    runners, made = runner_set(finish=True)
    await runners.reconcile({"general": "b"})
    await asyncio.sleep(0.01)
    await runners.reconcile({"general": "b"})
    assert len(made) == 1
    await runners.reconcile({"general": "c"})
    assert [r.token for r in made] == ["b", "c"]
    await runners.stop()


async def test_stops_runners_for_removed_queues():
    runners, made = runner_set()
    await runners.reconcile({"code-sample": "a"})
    task = runners.tasks["code-sample"]
    await runners.reconcile({})
    await asyncio.sleep(0)
    assert task.cancelled() and runners.tasks == {}


def test_read_tokens_missing_or_bad_file(tmp_path):
    path = tmp_path / "runner-tokens.json"
    assert read_tokens(path) == {}
    for bad in ("[]", "x"):
        path.write_text(bad)
        assert read_tokens(path) == {}
    path.write_text('{"general": "t"}')
    assert read_tokens(path) == {"general": "t"}


def test_agent_environment_takes_the_github_noreply_email():
    from agent.sandbox.runners import agent_environment
    env = agent_environment({}, "chilli", git_email="7+chilli-bot@users.noreply.github.com")
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "7+chilli-bot@users.noreply.github.com"
    assert env["GIT_AUTHOR_NAME"] == "chilli"


def test_agent_environment():
    from agent.sandbox.runners import agent_environment
    env = agent_environment({"GH_TOKEN": "ghp_abc", "OPENAI_API_KEY": "k", "PATH": "/bin"}, "chilli")
    assert env["GH_TOKEN"] == "ghp_abc" and env["OPENAI_API_KEY"] == "k" and "PATH" not in env
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "chilli"
    assert env["GIT_AUTHOR_EMAIL"] == "chilli@users.noreply.github.com"
