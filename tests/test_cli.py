from pathlib import Path

import pytest

from agent import db
from agent.config import load_agent_config
from agent.core.agent_tools import AgentToolRouter
from agent.core.cli import RELOGIN, CliRunner, classify, leading_words, matches, register_cli_tools, valid_save_name
from agent.core.sessions import WorkQueue
from agent.memory.store import MemoryStore
from agent_fixtures import FakeNotifier, FakeSlack, add_conversation, add_item, make_config, make_pack

EXAMPLE = Path(__file__).parent.parent / "examples" / "config" / "agent.toml"


@pytest.fixture
def az_rules():
    return load_agent_config(EXAMPLE).cli["azure"]


class FakeProc:
    def __init__(self, out: bytes, code: int = 0):
        self.out, self.returncode = out, code

    async def communicate(self):
        return self.out, None

    def kill(self):
        pass


def fake_exec(output: str, code: int = 0, seen: list | None = None):
    async def exec_fn(*argv, **kw):
        if seen is not None:
            seen.append((argv, kw))
        return FakeProc(output.encode(), code)
    return exec_fn


@pytest.fixture
def az_runner_with_output(az_rules):
    def make(output, code=0):
        return CliRunner(az_rules, secret_values=["SECRETVALUE"], exec_fn=fake_exec(output, code))
    return make


def test_leading_words_and_matching():
    assert leading_words(["monitor", "log-analytics", "query", "-w", "x", "--analytics-query", "y"]) == \
        ["monitor", "log-analytics", "query"]
    assert matches(["group", "list"], "* list") and matches(["webapp", "config", "appsettings", "list"], "* list")
    assert matches(["monitor", "metrics", "list"], "monitor *") and not matches(["webapp", "restart"], "* list")
    assert matches(["ad", "sp", "credential", "list"], "* credential* list")


@pytest.mark.parametrize("args,unattended,expected", [
    (["account", "get-access-token"], False, "deny"),
    (["rest", "--url", "https://x"], False, "deny"),
    (["keyvault", "secret", "show", "--name", "k"], False, "deny"),
    (["webapp", "config", "appsettings", "list", "-n", "a"], False, "deny"),
    (["group", "delete", "-n", "g"], False, "confirm"),
    (["webapp", "deployment", "slot", "swap"], False, "confirm"),
    (["monitor", "log-analytics", "query", "-w", "w"], False, "run"),
    (["webapp", "restart", "-n", "a"], False, "run"),
    (["webapp", "restart", "-n", "a"], True, "deny"),
    (["monitor", "log-analytics", "query"], True, "run"),
    (["group", "delete"], True, "deny"),
])
def test_classify(az_rules, args, unattended, expected):
    assert classify(az_rules, args, unattended=unattended) == expected


async def test_runs_without_shell_with_env(az_rules):
    seen = []
    await CliRunner(az_rules, secret_values=[], exec_fn=fake_exec("ok", seen=seen)).run(["account", "show"])
    argv, kw = seen[0]
    assert argv == ("az", "account", "show") and kw["env"]["AZURE_CONFIG_DIR"] == "/data/cli/azure"
    assert "shell" not in kw


async def test_huge_output_capped_and_saved(az_runner_with_output):
    r = await az_runner_with_output("x" * 50_000_000).run(["monitor", "log-analytics", "query"], save_to="logs.json")
    assert len(r.text) <= 8200 and len(r.file[1]) == 50_000_000 and r.file[0] == "logs.json"
    assert "narrow it" in r.text


async def test_cap_without_save(az_runner_with_output):
    r = await az_runner_with_output("y" * 20000).run(["group", "list"])
    assert len(r.text) <= 8200 and r.file is None


def test_save_name_rejects_traversal():
    assert not valid_save_name("../../etc/x") and not valid_save_name("..") and not valid_save_name("a/b")
    assert valid_save_name("logs.json") and not valid_save_name("")


async def test_save_to_traversal_refused(az_runner_with_output):
    r = await az_runner_with_output("data").run(["group", "list"], save_to="../../etc/x")
    assert r.text.startswith("refused") and r.file is None


async def test_secret_scrubbed_and_relogin(az_runner_with_output):
    assert "SECRETVALUE" not in (await az_runner_with_output("token SECRETVALUE").run(["account", "show"])).text
    assert (await az_runner_with_output("Please run 'az login' to setup account.").run(["account", "show"])
            ).text.startswith(RELOGIN)


async def test_nonzero_exit_reported(az_runner_with_output):
    assert "exit code 3" in (await az_runner_with_output("boom", code=3).run(["group", "list"])).text


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.migrate(c)
    return c


@pytest.fixture
def wired(conn, tmp_path):
    cfg = make_config(tmp_path)
    router = AgentToolRouter(conn, config=cfg, packs={"sample": make_pack()}, queue=WorkQueue(conn),
                             memory=MemoryStore(tmp_path / "memory.db"), slack=FakeSlack(), notifier=FakeNotifier(),
                             secret_values=[])
    proposed = []

    async def propose(ctx, cli, args):
        proposed.append((cli, args))
        return "Waiting for an admin to confirm."

    runner = CliRunner(cfg.cli["azure"], secret_values=[], exec_fn=fake_exec('[{"name": "rg-1"}]'))
    register_cli_tools(router, runners={"azure": runner}, propose=propose)
    return router, proposed


def ctx_in(router, sandbox="debug-sample", unattended=False):
    cid = add_conversation(router.conn, sandbox=sandbox)
    return router.context_for(add_item(router.queue, cid, sandbox, actor=None if unattended else "bob",
                                       unattended=unattended))


async def test_tool_runs_and_logs(wired):
    router, _ = wired
    ctx = ctx_in(router)
    assert "rg-1" in (await router.call(ctx, "az", {"args": ["webapp", "list"]})).text
    row = router.conn.execute("SELECT data FROM events WHERE type = 'cli_command'").fetchone()
    assert '"verdict": "run"' in row[0] and "webapp" in row[0]


async def test_tool_refuses_and_proposes(wired):
    router, proposed = wired
    ctx = ctx_in(router)
    assert (await router.call(ctx, "az", {"args": ["account", "get-access-token"]})).text.startswith("refused")
    assert "admin" in (await router.call(ctx, "az", {"args": ["group", "delete", "-n", "g"]})).text
    assert proposed == [("azure", ["group", "delete", "-n", "g"])]


async def test_unattended_non_read_refused(wired):
    router, proposed = wired
    ctx = ctx_in(router, unattended=True)
    assert "read" in (await router.call(ctx, "az", {"args": ["webapp", "restart", "-n", "a"]})).text
    assert proposed == []


async def test_az_only_in_debug(wired):
    router, _ = wired
    assert (await router.call(ctx_in(router, sandbox="code-sample"), "az", {"args": ["group", "list"]})).text == \
        "not available here"


async def test_args_must_be_a_list_of_strings(wired):
    router, _ = wired
    assert "list of strings" in (await router.call(ctx_in(router), "az", {"args": "group list"})).text


@pytest.mark.parametrize("args", [["-o", "json", "account", "get-access-token"], ["--debug", "rest", "--url", "x"],
                                  ["--only-show-errors", "group", "delete", "-n", "g"]])
def test_global_option_first_is_denied(az_rules, args):
    assert classify(az_rules, args, unattended=False) == "deny"


async def test_tool_refuses_global_option_first(wired):
    router, proposed = wired
    r = await router.call(ctx_in(router), "az", {"args": ["-o", "json", "account", "get-access-token"]})
    assert r.text.startswith("refused") and "after the command" in r.text and proposed == []
