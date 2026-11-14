import subprocess
import sys
from pathlib import Path

from agent.secrets import load_secrets

EXAMPLES = Path(__file__).parent.parent / "examples" / "config"


def test_load_secrets(tmp_path):
    f = tmp_path / "core.env"
    f.write_text("# comment\nSLACK_BOT_TOKEN=xoxb-1\n\nQUOTED=\"a b\"\nEMPTY=\nexport LINEAR_API_KEY=lin_x\n")
    assert load_secrets(f) == {"SLACK_BOT_TOKEN": "xoxb-1", "QUOTED": "a b", "EMPTY": "", "LINEAR_API_KEY": "lin_x"}
    assert load_secrets(tmp_path / "missing.env") == {}


from agent.doctor import REQUIRED_CORE, run_checks
from agent.proxy import AccountUsage, ProxyUnavailable, Usage


class Proxy:
    def __init__(self, verified=True, down=False):
        self.verified, self.down = verified, down

    async def usage(self):
        if self.down:
            raise ProxyUnavailable("down")
        return Usage({"maple": AccountUsage("allowed", 1, self.verified)})


class GitHub:
    def __init__(self, ok=True):
        self.ok = ok

    async def can_read(self, repo):
        return self.ok


class Linear:
    def __init__(self, names=("In Progress", "In Review", "Done", "Todo")):
        self.names = set(names)

    async def team_state_names(self, team):
        return self.names


ALL_SECRETS = {name: "x" for name in REQUIRED_CORE}


def by_name(checks):
    return {c.name: c for c in checks}


async def test_doctor_all_green_on_examples_with_fakes():
    checks = await run_checks(config_dir=EXAMPLES, secrets=ALL_SECRETS, proxy=Proxy(), github=GitHub(),
                              linear=Linear())
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    assert {"config", "packs", "proxy", "github:example-org/agent-sample", "linear:sample"} <= set(by_name(checks))


async def test_doctor_reports_each_failure_by_name():
    secrets = {k: v for k, v in ALL_SECRETS.items() if k != "SLACK_BOT_TOKEN"}
    checks = by_name(await run_checks(config_dir=EXAMPLES, secrets=secrets, proxy=Proxy(verified=False),
                                      github=GitHub(ok=False), linear=Linear(names=("In Progress",))))
    assert not checks["secret:SLACK_BOT_TOKEN"].ok and checks["secret:LINEAR_API_KEY"].ok
    assert not checks["proxy"].ok and "0 verified" in checks["proxy"].detail
    assert not checks["github:example-org/agent-sample"].ok
    assert not checks["linear:sample"].ok and "In Review" in checks["linear:sample"].detail


async def test_doctor_bad_config_stops_early(tmp_path):
    (tmp_path / "agent.toml").write_text("timezone = 'x'\n")
    checks = await run_checks(config_dir=tmp_path, secrets={}, proxy=None, github=None, linear=None)
    assert [c.name for c in checks] == ["config"] and not checks[0].ok


async def test_doctor_proxy_down():
    checks = by_name(await run_checks(config_dir=EXAMPLES, secrets=ALL_SECRETS, proxy=Proxy(down=True),
                                      github=None, linear=None))
    assert not checks["proxy"].ok


def test_cli_help_lists_commands():
    out = subprocess.run([sys.executable, "-m", "agent", "--help"], capture_output=True, text=True,
                         env={"PYTHONPATH": str(Path(__file__).parent.parent / "src"), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0
    for command in ("core", "supervisor", "doctor", "stop", "resume"):
        assert command in out.stdout


def test_linear_app_secrets_and_runner_token(tmp_path):
    import shutil

    from agent import db
    from agent.config import load_agent_config
    from agent.core.runner_tokens import sync_runner_token
    from agent.core.sessions import mint_token
    from agent.doctor import check_runner_token, required_core

    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    cfg = load_agent_config(config / "agent.toml")
    assert "SLACK_BOT_TOKEN" in required_core(cfg)

    conn = db.connect(tmp_path / "agent.db")
    db.create_schema(conn)
    path = tmp_path / "runner-token"
    assert not check_runner_token(conn, path).ok
    path.write_text(mint_token(conn, "session", work_item_id="w1", ttl_s=3600))
    assert not check_runner_token(conn, path).ok         # a live token, but not the runner's
    sync_runner_token(conn, path)
    assert check_runner_token(conn, path).ok
    conn.execute("UPDATE tokens SET revoked = 1")
    assert not check_runner_token(conn, path).ok


def test_runner_token_check_fails_before_core_made_the_database(tmp_path):
    """A database core hasn't set up yet has no tokens table: the check reports FAIL instead of raising."""
    from agent import db
    from agent.doctor import check_runner_token

    path = tmp_path / "runner-token"
    path.write_text("some-token\n")
    check = check_runner_token(db.connect(tmp_path / "agent.db"), path)
    assert not check.ok and check.name == "runner token" and "no tokens table" in check.detail


def test_linear_app_secrets_required_with_public_url(tmp_path):
    import shutil

    from agent.config import load_agent_config
    from agent.doctor import required_core

    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    toml = config / "agent.toml"
    linear = ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET")
    assert not set(linear) & set(required_core(load_agent_config(toml)))
    toml.write_text(toml.read_text().replace('public_url = ""', 'public_url = "https://jensen.example.ts.net"'))
    assert set(linear) <= set(required_core(load_agent_config(toml)))


async def test_meter_upstream_keys_are_required_secrets(tmp_path):
    import shutil

    config = tmp_path / "config"
    shutil.copytree(EXAMPLES, config)
    toml = config / "agent.toml"
    toml.write_text(toml.read_text().replace(
        "# [llm_meter.upstreams.<project>-dev]\n# base_url = \"https://<resource>.openai.azure.com/openai/v1\"\n"
        "# key = \"<PROJECT>_DEV_FOUNDRY_KEY\"",
        "[llm_meter.upstreams.sample-dev]\nbase_url = \"https://x.openai.azure.com/openai/v1\"\nkey = \"SAMPLE_DEV_KEY\""))
    checks = by_name(await run_checks(config_dir=config, secrets=ALL_SECRETS))
    assert not checks["secret:SAMPLE_DEV_KEY"].ok


def test_proxy_check_uses_proxy_url(monkeypatch):
    from agent.doctor import _proxy_relay

    monkeypatch.setenv("PROXY_URL", "http://p:1")
    assert _proxy_relay() == "http://p:1"
