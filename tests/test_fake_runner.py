from pathlib import Path

import pytest

from agent.runner.base import RunResult, RunSpec
from agent.runner.fake import FakeRunner


def spec(run_id="R1", role="builder", prompt="do it"):
    return RunSpec(run_id=run_id, role=role, prompt=prompt, cwd=Path("/tmp"), model="m",
                   system_prompt="s", max_turns=5, timeout_s=60)


async def test_fake_runner_returns_scripted_results_in_order():
    r = FakeRunner({"builder": [RunResult("x", "s1", "needs_input", question="which?"),
                                RunResult("x", "s1", "done", summary="ok")]})
    first = await r.run(spec("R1"))
    second = await r.run(spec("R2"))
    assert (first.run_id, first.outcome, first.question) == ("R1", "needs_input", "which?")
    assert (second.run_id, second.outcome) == ("R2", "done")
    with pytest.raises(AssertionError):
        await r.run(spec("R3"))


async def test_fake_runner_records_specs():
    r = FakeRunner({"chat": [RunResult("x", "s", "done")]})
    await r.run(spec(role="chat", prompt="hello"))
    assert [s.prompt for s in r.specs] == ["hello"]


async def test_fake_runner_callable_entries_see_spec():
    r = FakeRunner({"builder": [lambda s: RunResult("x", "s", "done", summary=s.prompt.upper())]})
    assert (await r.run(spec(prompt="abc"))).summary == "ABC"


async def test_fake_runner_interrupt_records():
    r = FakeRunner({})
    await r.interrupt("R9")
    assert r.interrupted == {"R9"}


def test_spec_mutable_defaults_are_independent():
    a, b = spec(), spec()
    a.env["X"] = "1"
    assert b.env == {}


async def test_fake_runner_async_callable_entries():
    async def entry(spec):
        return RunResult("x", "s", "done", summary="async " + spec.prompt)

    r = FakeRunner({"chat": [entry]})
    assert (await r.run(spec(role="chat", prompt="hi"))).summary == "async hi"
