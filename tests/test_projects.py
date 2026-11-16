"""Tests for core's long-projects table and its three tools: start_project, finish_project, tell_project."""

import pytest

from agent.core import projects
from agent.core.projects import register_project_tools
from agent_fixtures import add_conversation, add_item
from test_agent_tools import conn, ctx_for, latest_item, router  # noqa: F401 (fixtures and helpers to reuse)


@pytest.fixture(autouse=True)
def _project_tools(router):  # noqa: F811 (router: the fixture imported above)
    register_project_tools(router)


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


async def test_a_change_from_another_conversation_reaches_an_idle_build(router):
    build = ctx_for(router, actor="ann")
    await router.call(build, "start_project", {"name": "roster-demo", "repo": "o/r"})
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
