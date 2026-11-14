import asyncio
import json

import pytest

from agent import db
from agent.core.agent_tools import AgentToolRouter
from agent.core.cli_admin import DeviceCodeLogin, parse_device_code, register_login_tools
from agent.core.sessions import WorkQueue
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


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


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
                             slack=slack, notifier=FakeNotifier(),
                             secret_values=[])
    register_login_tools(router, login)
    cid = add_conversation(conn, channel="D-ann")
    admin_ctx = router.context_for(add_item(router.queue, cid, actor="ann"))
    text = (await router.call(admin_ctx, "az_login", {})).text
    assert "DM" in text
    assert slack.opened == [{"users": "U1"}]
    dm = [p for p in slack.posts if p["channel"] == "D-U1"][0]
    assert "https://microsoft.com/devicelogin" in dm["text"] and "ABCD1234" in dm["text"]
    await asyncio.gather(*login.pending)
    assert "Logged in as ann@x.com (Dev Sub)" in slack.posts[-1]["text"]
    member_ctx = router.context_for(add_item(router.queue, cid, actor="bob"))
    assert (await router.call(member_ctx, "az_login", {})).text == "not available here"


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
                             slack=FakeSlack(), notifier=FakeNotifier(),
                             secret_values=[])
    cid = add_conversation(conn, channel="D-ann")
    await login.start(router.context_for(add_item(router.queue, cid, actor="ann")), "azure")
    await asyncio.gather(*login.pending)
    assert len(calls) == 2
    for kw in calls:
        if run_as:
            assert kw["user"] == 1001 and kw["env"]["HOME"] == "/tmp"
            assert kw["env"]["USER"] == kw["env"]["LOGNAME"] == "agent"
        else:
            assert "user" not in kw
