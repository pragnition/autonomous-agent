"""deploy/cutover.sh against stub docker, sudo, stat and sleep: the steps of the switch and the rollback, in order."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent

STUB = """#!/usr/bin/env bash
printf '%s\\n' "$(basename "$0") $*" >> "$CUTOVER_LOG"
case "$(basename "$0") $*" in
  "docker exec agent-core python -c"*COUNT*) echo 0 ;;
  "docker ps -q"*) echo c1; echo c2 ;;
  "stat -c %g /var/run/docker.sock") echo 116 ;;
  "stat -c %u"*) echo 0 ;;
esac
exit 0
"""


def run(tmp_path: Path, command: str) -> tuple[list[str], str]:
    live = tmp_path / "live"
    (live / "deploy").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "deploy" / "cutover.sh", live / "deploy" / "cutover.sh")
    for name in ("core.env", "agent.env"):
        (live / "data" / "secrets").mkdir(parents=True, exist_ok=True)
        (live / "data" / "secrets" / name).write_text("K=v\n")
    stubs = tmp_path / "bin"
    stubs.mkdir(exist_ok=True)
    for tool in ("docker", "sudo", "stat", "sleep"):
        (stubs / tool).write_text(STUB)
        (stubs / tool).chmod(0o755)
    log = tmp_path / "log"
    log.write_text("")
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}", "CUTOVER_LOG": str(log), "CUTOVER_IDLE_S": "0"}
    env.pop("AGENT_DATA", None)
    result = subprocess.run(["bash", str(live / "deploy" / "cutover.sh"), command], env=env, capture_output=True,
                            text=True)
    assert result.returncode == 0, result.stderr
    return log.read_text().splitlines(), (live / ".env").read_text()


def positions(calls: list[str], *wanted: str) -> list[int]:
    return [next(i for i, c in enumerate(calls) if c.startswith(w)) for w in wanted]


def test_switch_steps_in_order(tmp_path):
    calls, env = run(tmp_path, "switch")
    order = positions(calls, "docker compose config -q", "docker exec agent-core python -c from agent import db",
                      "docker exec agent-core python -m agent stop", "docker exec agent-core python -c import sqlite3",
                      "docker stop agent-supervisor agent-supervisor-standby", "docker stop agent-core agent-runner",
                      "sudo chown -R 1001:1001", "sudo setfacl -m u:1001:r",
                      "docker compose up -d control services supervisor supervisor-standby",
                      "docker exec agent-control agentd resume", "docker exec agent-control agentd doctor",
                      "docker exec agent-control agentd smoke")
    assert order == sorted(order), calls
    assert "DOCKER_GID=116\n" in env and f"AGENTD_DATA_HOST={tmp_path / 'live' / 'data'}\n" in env
    assert "SUPERVISE_CONTROL=http://control:8080/health\n" in env


def test_rollback_steps_in_order(tmp_path):
    run(tmp_path, "switch")
    calls, env = run(tmp_path, "rollback")
    order = positions(calls, "docker stop agent-supervisor agent-supervisor-standby", "docker exec agent-control agentd stop",
                      "docker stop c1 c2", "docker compose stop control services",
                      "docker compose --profile python up -d core agent", "docker compose up -d supervisor supervisor-standby",
                      "docker exec agent-core python -m agent resume")
    assert order == sorted(order), calls
    assert "SUPERVISE_CONTROL=\n" in env and env.count("SUPERVISE_CONTROL=") == 1  # the supervisors watch core again
    assert "DOCKER_GID=116\n" in env
