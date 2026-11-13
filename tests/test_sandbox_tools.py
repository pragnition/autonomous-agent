import pytest

from agent import db
from agent.core.agent_tools import AgentToolRouter
from agent.core.sandbox_tools import register_sandbox_tools, scrub_customer
from agent.core.sessions import WorkQueue
from agent.memory.store import MemoryStore
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack


def test_scrub_keeps_debugging_detail():
    text = "2026-11-13T10:00:00Z GET /api/citations/42 500 user=jane@x.com ip=10.1.2.3 at export.py:88 (17 times)"
    out, hits = scrub_customer(text, [])
    assert "jane@x.com" not in out and "10.1.2.3" not in out and hits == 2
    assert "2026-11-13T10:00:00Z" in out and "/api/citations/42" in out and "export.py:88" in out and "17 times" in out


def test_scrub_tokens_phones_and_ipv6():
    text = ("auth Bearer abc123def456ghi tok eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_part phone +65 9123 4567 "
            "v6 2001:db8:85a3:0:0:8a2e:370:7334 date 2026-11-13 took 1234 ms")
    out, hits = scrub_customer(text, [])
    for secret in ("abc123def456ghi", "eyJhbGciOiJIUzI1NiJ9", "9123 4567", "2001:db8"):
        assert secret not in out
    assert "2026-11-13" in out and "1234 ms" in out and hits == 4


def test_pack_pattern():
    assert scrub_customer("cust C-12345 failed", [r"C-\d{5}"])[0] == "cust [customer-data] failed"


class FakeSandboxd:
    def __init__(self, output="ok"):
        self.output, self.rebuilt, self.execs = output, [], []

    async def rebuild(self, name):
        self.rebuilt.append(name)

    async def exec(self, name, command):
        self.execs.append((name, command))
        return {"code": 0, "output": self.output}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


def build(conn, tmp_path, sandboxd):
    router = AgentToolRouter(conn, config=make_config(tmp_path), packs={"sample": make_pack()}, queue=WorkQueue(conn),
                             memory=MemoryStore(tmp_path / "memory.db"), slack=FakeSlack(), notifier=FakeNotifier(),
                             secret_values=["SUPERSECRETVALUE"])
    register_sandbox_tools(router, sandboxd)
    return router


def ctx(router, actor):
    cid = add_conversation(router.conn, sandbox="general", project=None, channel="D-1")
    return router.context_for(add_item(router.queue, cid, "general", actor=actor))


async def test_member_is_refused(conn, tmp_path):
    sbx = FakeSandboxd()
    router = build(conn, tmp_path, sbx)
    r = await router.call(ctx(router, "bob"), "rebuild_sandbox", {"project": "sample", "profile": "debug"})
    assert r.text == "not available here" and sbx.rebuilt == []


async def test_admin_rebuild_names_the_sandbox(conn, tmp_path):
    sbx = FakeSandboxd()
    router = build(conn, tmp_path, sbx)
    admin = ctx(router, "ann")
    await router.call(admin, "rebuild_sandbox", {"project": "sample", "profile": "debug"})
    await router.call(admin, "rebuild_sandbox", {"project": "", "profile": "general"})
    assert sbx.rebuilt == ["debug-sample", "general"]


async def test_exec_output_scrubbed_and_capped(conn, tmp_path):
    sbx = FakeSandboxd(output="token SUPERSECRETVALUE " + "x" * 9000)
    router = build(conn, tmp_path, sbx)
    r = await router.call(ctx(router, "ann"), "sandbox_exec", {"project": "sample", "profile": "code",
                                                                "command": "df -h"})
    assert sbx.execs == [("code-sample", "df -h")]
    assert "SUPERSECRETVALUE" not in r.text and len(r.text) <= 4100


async def test_unknown_project_or_profile_refused(conn, tmp_path):
    sbx = FakeSandboxd()
    router = build(conn, tmp_path, sbx)
    admin = ctx(router, "ann")
    assert "unknown project" in (await router.call(admin, "sandbox_exec", {"project": "nope", "profile": "code",
                                                                           "command": "ls"})).text
    assert "profile" in (await router.call(admin, "rebuild_sandbox", {"project": "sample", "profile": "gpu"})).text
    assert sbx.execs == [] and sbx.rebuilt == []


async def test_exec_in_debug_sandbox_strips_customer_data(conn, tmp_path):
    sbx = FakeSandboxd(output="error for jane@example.com at 10.1.2.3")
    router = build(conn, tmp_path, sbx)
    r = await router.call(ctx(router, "ann"), "sandbox_exec", {"project": "sample", "profile": "debug",
                                                                "command": "cat /var/log/x"})
    assert "jane@example.com" not in r.text and "10.1.2.3" not in r.text
