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
        return Usage({"maple": AccountUsage("maple", "allowed", 1, self.verified, 0, 0, 0)}, "on")


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
    for command in ("core", "runner", "supervisor", "doctor", "stop", "resume"):
        assert command in out.stdout
