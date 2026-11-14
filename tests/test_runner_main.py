import asyncio

from agent.runner.main import RunnerKeeper


class FakeRunner:
    def __init__(self, token, finish=False):
        self.token, self.finish = token, finish
        self.started = asyncio.Event()

    async def run_forever(self):
        self.started.set()
        if not self.finish:
            await asyncio.Event().wait()


def keeper(finish=False):
    made = []

    def make(token):
        made.append(FakeRunner(token, finish))
        return made[-1]

    return RunnerKeeper(make), made


async def test_starts_the_runner_when_a_token_appears():
    runner, made = keeper()
    await runner.reconcile("")
    assert made == [] and runner.task is None
    await runner.reconcile("a")
    assert [r.token for r in made] == ["a"]
    await asyncio.wait_for(made[0].started.wait(), 1)
    await runner.stop()


async def test_restarts_the_runner_when_its_token_changes():
    runner, made = keeper()
    await runner.reconcile("b")
    first = runner.task
    await runner.reconcile("b")
    assert runner.task is first and len(made) == 1
    await runner.reconcile("c")
    await asyncio.sleep(0)
    assert first.cancelled() and [r.token for r in made] == ["b", "c"]
    await runner.stop()


async def test_finished_runner_restarts_only_with_a_new_token():
    runner, made = keeper(finish=True)
    await runner.reconcile("b")
    await asyncio.sleep(0.01)
    await runner.reconcile("b")
    assert len(made) == 1
    await runner.reconcile("c")
    assert [r.token for r in made] == ["b", "c"]
    await runner.stop()


async def test_stops_the_runner_when_the_token_is_gone():
    runner, made = keeper()
    await runner.reconcile("a")
    task = runner.task
    await runner.reconcile("")
    await asyncio.sleep(0)
    assert task.cancelled() and runner.task is None


def test_agent_environment_takes_the_github_noreply_email():
    from agent.runner.main import agent_environment
    env = agent_environment({}, "jensen", git_email="7+jensen-bot@users.noreply.github.com")
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "7+jensen-bot@users.noreply.github.com"
    assert env["GIT_AUTHOR_NAME"] == "jensen"


def test_agent_environment():
    from agent.runner.main import agent_environment
    env = agent_environment({"GH_TOKEN": "ghp_abc", "OPENAI_API_KEY": "k", "PATH": "/bin"}, "jensen")
    assert env["GH_TOKEN"] == "ghp_abc" and env["OPENAI_API_KEY"] == "k" and "PATH" not in env
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "jensen"
    assert env["GIT_AUTHOR_EMAIL"] == "jensen@users.noreply.github.com"
