"""Tests for core's long-projects table and its three tools: start_project, finish_project, tell_project."""

import asyncio

import pytest

from agent.core import projects
from agent.core.projects import register_project_tools
from agent_fixtures import FakeSlack, add_conversation, add_item
from test_agent_tools import conn, ctx_for, latest_item, router  # noqa: F401 (fixtures and helpers to reuse)


@pytest.fixture(autouse=True)
def _project_tools(router):  # noqa: F811 (router: the fixture imported above)
    register_project_tools(router)


class GatedSlack(FakeSlack):
    """A chat_getPermalink that waits on a gate, so a test can pause two start_project calls at the same point
    (after the permalink lookup, before either has checked or written its row) and release them together."""

    def __init__(self):
        super().__init__()
        self.waiting = 0
        self.gate = asyncio.Event()

    async def chat_getPermalink(self, **kw):
        self.waiting += 1
        await self.gate.wait()
        return {"permalink": "https://slack/t"}


async def test_start_project_registers_this_conversation_as_the_build(router):
    build = ctx_for(router, actor="ann")
    reply = await router.call(build, "start_project", {"name": "roster-demo", "repo": "pragnition/roster-demo"})
    assert "registered" in reply.text
    [p] = projects.active(router.conn)
    assert (p["name"], p["repo"], p["conversation_id"], p["started_by"]) == (
        "roster-demo", "pragnition/roster-demo", build.conversation_id, "ann")


async def test_a_name_another_build_holds_is_refused(router):
    await router.call(ctx_for(router), "start_project", {"name": "roster-demo", "repo": "o/r"})
    reply = await router.call(ctx_for(router), "start_project", {"name": "roster-demo", "repo": "o/r"})
    assert "already being built" in reply.text


async def test_two_conversations_racing_to_register_the_same_new_name_has_exactly_one_winner(router):
    """Both calls reach the (awaited) permalink lookup before either checks or writes the row, so this exercises
    the real interleaving a TOCTOU race would need: the check and the write must still run as one atomic step."""
    slack = GatedSlack()
    router.slack = slack
    a, b = ctx_for(router, actor="ann"), ctx_for(router, actor="bob")
    task_a = asyncio.create_task(router.call(a, "start_project", {"name": "roster-demo", "repo": "o/r"}))
    task_b = asyncio.create_task(router.call(b, "start_project", {"name": "roster-demo", "repo": "o/r"}))
    while slack.waiting < 2:   # both parked on the gate, neither has checked or written yet
        await asyncio.sleep(0)
    slack.gate.set()
    reply_a, reply_b = await asyncio.gather(task_a, task_b)

    texts = [reply_a.text, reply_b.text]
    assert sum("registered" in t for t in texts) == 1
    assert sum("already being built" in t for t in texts) == 1
    [p] = projects.active(router.conn)
    winner = a if "registered" in reply_a.text else b
    assert p["conversation_id"] == winner.conversation_id


async def test_a_change_from_another_conversation_reaches_an_idle_build(router):
    build = ctx_for(router, actor="ann")
    await router.call(build, "start_project", {"name": "roster-demo", "repo": "o/r"})
    router.queue.claim()                          # the build's own turn, finished: nothing left waiting for it
    router.queue.complete(build.work_item_id, {"outcome": "done", "text": "ok"})
    side_id = add_item(router.queue, add_conversation(router.conn, channel="D-bob"), actor="bob",
                       origin="[from Bob (bob) · DM · https://slack/x]", source="slack", requester="bob")
    side = router.context_for(side_id)
    reply = await router.call(side, "tell_project", {"name": "roster-demo", "message": "add xyz to the admin page"})
    item = latest_item(router)
    assert item.conversation_id == build.conversation_id and item.payload["text"] == "add xyz to the admin page"
    assert item.payload["origin"] == "[from Bob (bob) · DM · https://slack/x]"
    assert (item.payload["requester"], item.payload["follows"]) == ("bob", side_id)
    assert "message_ts" not in item.payload
    assert "starts on this now" in reply.text


async def test_a_change_for_a_queued_build_says_so(router):
    """start_project's own turn is still queued, unclaimed — nothing has run yet. tell_project must say that
    the build is about to start, not report it as idle: a turn is already waiting for it."""
    build = ctx_for(router, actor="ann")
    await router.call(build, "start_project", {"name": "roster-demo", "repo": "o/r"})
    reply = await router.call(ctx_for(router, actor="bob"), "tell_project",
                              {"name": "roster-demo", "message": "add xyz"})
    assert "about to start its next turn" in reply.text


async def test_a_change_for_a_running_build_is_delivered_at_its_next_step(router):
    build = ctx_for(router, actor="ann")
    await router.call(build, "start_project", {"name": "roster-demo", "repo": "o/r"})
    running = router.queue.claim()            # the build's own turn
    reply = await router.call(ctx_for(router, actor="bob"), "tell_project",
                              {"name": "roster-demo", "message": "add xyz"})
    assert "next step" in reply.text
    assert [i.payload["text"] for i in router.queue.deliver(build.conversation_id, running.id)] == ["add xyz"]


async def test_an_unknown_project_lists_the_active_ones(router):
    await router.call(ctx_for(router, actor="ann"), "start_project", {"name": "roster-demo", "repo": "o/r"})
    reply = await router.call(ctx_for(router, actor="bob"), "tell_project", {"name": "x", "message": "hi"})
    assert reply.text == "no project named x; active: roster-demo"


async def test_the_build_cannot_tell_itself(router):
    build = ctx_for(router, actor="ann")
    await router.call(build, "start_project", {"name": "roster-demo", "repo": "o/r"})
    same_build = router.context_for(add_item(router.queue, build.conversation_id, actor="ann"))
    reply = await router.call(same_build, "tell_project", {"name": "roster-demo", "message": "hi"})
    assert "this is roster-demo's build" in reply.text


async def test_only_the_build_finishes_its_project(router):
    build = ctx_for(router, actor="ann")
    await router.call(build, "start_project", {"name": "roster-demo", "repo": "o/r"})
    refused = await router.call(ctx_for(router, actor="bob"), "finish_project", {"name": "roster-demo"})
    assert "only roster-demo's build can finish it" in refused.text
    assert [p["name"] for p in projects.active(router.conn)] == ["roster-demo"]
    done = await router.call(build, "finish_project", {"name": "roster-demo"})
    assert "roster-demo marked done" in done.text
    assert projects.active(router.conn) == []
