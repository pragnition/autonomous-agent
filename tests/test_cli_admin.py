import asyncio
import json

import pytest

from agent import db
from agent.connectors.slack_app import make_action_handler
from agent.core.agent_tools import AgentToolRouter
from agent.core.cli import CliRunner
from agent.core.cli_admin import DeviceCodeLogin, Proposals, parse_device_code, register_login_tools
from agent.core.sessions import WorkQueue
from agent.memory.store import MemoryStore
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack

AZ_TEXT = ("To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code "
           "ABCD1234 to authenticate.")


class FakeProc:
    def __init__(self, out: bytes, code: int = 0, lines=None):
        self.out, self.returncode = out, code
        self.stdout = FakeStream(lines or [])

    async def communicate(self):
        return self.out, None

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


class FakeStream:
    def __init__(self, lines):
        self.lines = list(lines)

    async def readline(self):
        return self.lines.pop(0).encode() if self.lines else b""


def exec_returning(out: str, seen: list):
    async def exec_fn(*argv, **kw):
        seen.append(argv)
        return FakeProc(out.encode())
    return exec_fn


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


@pytest.fixture
def setup(conn, tmp_path):
    cfg = make_config(tmp_path)
    slack, queue = FakeSlack(), WorkQueue(conn)
    seen = []
    runner = CliRunner(cfg.cli["azure"], secret_values=[], exec_fn=exec_returning("deleted rg", seen))
    proposals = Proposals(conn, slack, config=cfg, runner_for=lambda cli: runner, queue=queue)
    router = AgentToolRouter(conn, config=cfg, packs={"sample": make_pack()}, queue=queue,
                             memory=MemoryStore(tmp_path / "memory.db"), slack=slack, notifier=FakeNotifier(),
                             secret_values=[])
    cid = add_conversation(conn, sandbox="debug-sample", channel="C1", thread_ts="9.0")
    ctx = router.context_for(add_item(queue, cid, "debug-sample", actor="bob"))
    return proposals, ctx, slack, queue, seen, router


def proposal_id(conn):
    return conn.execute("SELECT id FROM proposals").fetchone()[0]


async def test_propose_posts_buttons(setup, conn):
    proposals, ctx, slack, _, _, _ = setup
    text = await proposals.propose(ctx, "azure", ["group", "delete", "-n", "g"])
    assert "admin" in text
    actions = slack.posts[-1]["blocks"][-1]["elements"]
    pid = proposal_id(conn)
    assert [a["action_id"] for a in actions] == [f"cli_confirm:{pid}", f"cli_cancel:{pid}"]
    assert slack.posts[-1]["thread_ts"] == "9.0" and "az group delete -n g" in slack.posts[-1]["text"]


async def test_non_admin_click_refused(setup, conn):
    proposals, ctx, _, _, seen, _ = setup
    await proposals.propose(ctx, "azure", ["group", "delete", "-n", "g"])
    assert await proposals.on_action(f"cli_confirm:{proposal_id(conn)}", "U2") == "Only an admin can confirm this."
    assert seen == []


async def test_expired_proposal_refused(setup, conn):
    proposals, ctx, _, _, seen, _ = setup
    await proposals.propose(ctx, "azure", ["group", "delete", "-n", "g"])
    conn.execute("UPDATE proposals SET expires_at = '2000-01-01T00:00:00Z'")
    assert await proposals.on_action(f"cli_confirm:{proposal_id(conn)}", "U1") == "This request expired."
    assert seen == []


async def test_confirm_runs_and_enqueues_turn(setup, conn):
    proposals, ctx, slack, queue, seen, _ = setup
    await proposals.propose(ctx, "azure", ["group", "delete", "-n", "g"])
    assert await proposals.on_action(f"cli_confirm:{proposal_id(conn)}", "U1") == "Confirmed."
    assert seen == [("az", "group", "delete", "-n", "g")]
    assert "deleted rg" in slack.posts[-1]["text"]
    item = queue.get(conn.execute("SELECT id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert item.conversation_id == ctx.conversation_id and "deleted rg" in item.payload["text"]
    assert item.payload["text"].startswith("Result of the confirmed command `az group delete -n g`")
    assert await proposals.on_action(f"cli_confirm:{proposal_id(conn)}", "U1") == "Already handled."


async def test_confirm_result_goes_to_the_caller(setup, conn):
    proposals, _, _, queue, _, router = setup
    caller = add_conversation(conn, sandbox="code-sample", channel="C1", thread_ts="9.0")
    debug = add_conversation(conn, sandbox="debug-sample", channel="C1", thread_ts="9.0", is_primary=0)
    ctx = router.context_for(add_item(queue, debug, "debug-sample", kind="production", actor="bob",
                                      caller_conversation_id=caller))
    await proposals.propose(ctx, "azure", ["group", "delete", "-n", "g"])
    await proposals.on_action(f"cli_confirm:{proposal_id(conn)}", "U1")
    item = queue.get(conn.execute("SELECT id FROM work_items ORDER BY rowid DESC LIMIT 1").fetchone()[0])
    assert item.conversation_id == caller and item.sandbox == "code-sample"


async def test_cancel_button(setup, conn):
    proposals, ctx, _, _, seen, _ = setup
    await proposals.propose(ctx, "azure", ["group", "delete", "-n", "g"])
    assert await proposals.on_action(f"cli_cancel:{proposal_id(conn)}", "U1") == "Cancelled."
    assert seen == []


def test_parse_device_code():
    assert parse_device_code(AZ_TEXT) == ("https://microsoft.com/devicelogin", "ABCD1234")
    assert parse_device_code("nothing here") is None


async def test_device_code_dm_goes_to_requesting_admin_only(conn, tmp_path):
    cfg = make_config(tmp_path)
    slack = FakeSlack()

    async def exec_fn(*argv, **kw):
        if argv[:2] == ("az", "login"):
            return FakeProc(b"", lines=[AZ_TEXT + "\n"])
        return FakeProc(json.dumps({"user": {"name": "ann@x.com"}, "name": "Dev Sub"}).encode())

    login = DeviceCodeLogin(slack, config=cfg, rules=cfg.cli, exec_fn=exec_fn)
    router = AgentToolRouter(conn, config=cfg, packs={"sample": make_pack()}, queue=WorkQueue(conn),
                             memory=MemoryStore(tmp_path / "memory.db"), slack=slack, notifier=FakeNotifier(),
                             secret_values=[])
    register_login_tools(router, login)
    cid = add_conversation(conn, sandbox="general", channel="D-ann")
    admin_ctx = router.context_for(add_item(router.queue, cid, "general", actor="ann"))
    text = (await router.call(admin_ctx, "az_login", {})).text
    assert "DM" in text
    assert slack.opened == [{"users": "U1"}]
    dm = [p for p in slack.posts if p["channel"] == "D-U1"][0]
    assert "https://microsoft.com/devicelogin" in dm["text"] and "ABCD1234" in dm["text"]
    await asyncio.gather(*login.pending)
    assert "Logged in as ann@x.com (Dev Sub)" in slack.posts[-1]["text"]
    member_ctx = router.context_for(add_item(router.queue, cid, "general", actor="bob"))
    assert (await router.call(member_ctx, "az_login", {})).text == "not available here"


async def test_slack_action_handler_posts_ephemeral():
    seen, posted = [], []

    async def on_action(action_id, user):
        seen.append((action_id, user))
        return "Confirmed."

    class Client:
        async def chat_postEphemeral(self, **kw):
            posted.append(kw)

    async def ack():
        posted.append("acked")

    handler = make_action_handler(on_action)
    await handler(ack=ack, body={"user": {"id": "U1"}, "actions": [{"action_id": "cli_confirm:P-1"}],
                                 "channel": {"id": "C1"}}, client=Client())
    assert seen == [("cli_confirm:P-1", "U1")]
    assert posted[0] == "acked" and posted[1] == {"channel": "C1", "user": "U1", "text": "Confirmed."}


async def test_proposal_from_threadless_debug_session_posts_in_callers_thread(setup, conn):
    proposals, _, slack, queue, _, router = setup
    caller = add_conversation(conn, sandbox="code-sample", channel="C7", thread_ts="7.0")
    debug = add_conversation(conn, sandbox="debug-sample", channel=None, thread_ts=None, is_primary=0)
    ctx = router.context_for(add_item(queue, debug, "debug-sample", kind="production", actor="bob",
                                      caller_conversation_id=caller))
    await proposals.propose(ctx, "azure", ["group", "delete", "-n", "g"])
    assert slack.posts[-1]["channel"] == "C7" and slack.posts[-1]["thread_ts"] == "7.0"


@pytest.mark.parametrize("run_as", [1001, None])
async def test_login_runs_as_agent_uid(conn, tmp_path, run_as):
    cfg = make_config(tmp_path)
    calls = []

    async def exec_fn(*argv, **kw):
        calls.append(kw)
        if argv[:2] == ("az", "login"):
            return FakeProc(b"", lines=[AZ_TEXT + "\n"])
        return FakeProc(json.dumps({"user": {"name": "ann@x.com"}, "name": "Dev Sub"}).encode())

    login = DeviceCodeLogin(FakeSlack(), config=cfg, rules=cfg.cli, exec_fn=exec_fn, run_as=run_as)
    router = AgentToolRouter(conn, config=cfg, packs={"sample": make_pack()}, queue=WorkQueue(conn),
                             memory=MemoryStore(tmp_path / "memory.db"), slack=FakeSlack(), notifier=FakeNotifier(),
                             secret_values=[])
    cid = add_conversation(conn, sandbox="general", channel="D-ann")
    await login.start(router.context_for(add_item(router.queue, cid, "general", actor="ann")), "azure")
    await asyncio.gather(*login.pending)
    assert len(calls) == 2
    for kw in calls:
        if run_as:
            assert kw["user"] == 1001 and kw["env"]["HOME"] == "/tmp"
            assert kw["env"]["USER"] == kw["env"]["LOGNAME"] == "agent"
        else:
            assert "user" not in kw
