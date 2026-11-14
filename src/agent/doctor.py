"""`doctor`: one pass over everything the install needs, each check reported by name."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from agent.config import ConfigError, load_agent_config, load_packs
from agent.proxy import ProxyUnavailable

REQUIRED_CORE = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "LINEAR_API_KEY", "GITHUB_READ_TOKEN")


def required_core(config) -> tuple[str, ...]:
    required = REQUIRED_CORE
    if config.linear.public_url:  # the Linear agent app
        required += ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET")
    return required


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def check_runner_token(conn, path: Path) -> Check:
    """The file the agent container reads holds a live runner token."""
    from agent.core.runner_tokens import read_runner_token
    from agent.core.sessions import resolve_token

    token = read_runner_token(path)
    try:
        info = resolve_token(conn, token) if token else None
    except sqlite3.OperationalError as e:  # core hasn't created the database's tables yet
        return Check("runner token", False, f"no tokens table in the database yet ({e}); start core first")
    ok = info is not None and info.kind == "runner"
    return Check("runner token", ok, "live" if ok else f"missing or invalid in {path}")


async def run_checks(*, config_dir: Path, secrets: dict[str, str], proxy=None, github=None,
                     linear=None) -> list[Check]:
    checks: list[Check] = []
    try:
        config = load_agent_config(Path(config_dir) / "agent.toml")
        checks.append(Check("config", True, f"{len(config.people)} people"))
    except ConfigError as e:
        return [Check("config", False, "; ".join(e.problems))]
    try:
        packs = load_packs(Path(config_dir), config)
        checks.append(Check("packs", True, f"{len(packs)} pack(s): {', '.join(sorted(packs))}"))
    except ConfigError as e:
        packs = {}
        checks.append(Check("packs", False, "; ".join(e.problems)))

    names = list(required_core(config))
    names += sorted(up.key for up in config.llm_meter.upstreams.values())  # keys the model-call meter adds
    for name in dict.fromkeys(names):
        checks.append(Check(f"secret:{name}", bool(secrets.get(name)), "set" if secrets.get(name) else "missing"))

    if proxy is not None:
        try:
            usage = await proxy.usage()
            verified = [a for a in usage.accounts.values() if a.verified]
            checks.append(Check("proxy", bool(verified), f"{len(verified)} verified account(s), "
                                                        f"{'capacity available' if usage.has_capacity() else 'no capacity right now'}"))
        except ProxyUnavailable as e:
            checks.append(Check("proxy", False, str(e)))
    for name, pack in sorted(packs.items()):
        if github is not None:
            ok = await github.can_read(pack.project.repo)
            checks.append(Check(f"github:{pack.project.repo}", ok, "readable" if ok else "cannot read the repo"))
        if linear is not None and pack.project.linear_status:
            try:
                existing = await linear.team_state_names(pack.project.linear_team)
                missing = sorted(set(pack.project.linear_status.values()) - existing)
                checks.append(Check(f"linear:{name}", not missing,
                                    "states exist" if not missing else f"missing states: {', '.join(missing)}"))
            except Exception as e:
                checks.append(Check(f"linear:{name}", False, str(e)))
    return checks


def _proxy_relay() -> str | None:
    """The account proxy's relay for core, from PROXY_URL (compose.yaml sets it)."""
    return os.environ.get("PROXY_URL")


def _config_or_none(config_dir: Path):
    try:
        return load_agent_config(Path(config_dir) / "agent.toml")
    except ConfigError:
        return None


def main() -> int:
    from agent import paths
    from agent.connectors.github_read import GitHubReader
    from agent.connectors.linear import LinearClient
    from agent.proxy import ProxyClient
    from agent.secrets import load_secrets

    secrets = load_secrets(paths.SECRETS_FILE)
    relay = _proxy_relay()
    checks = asyncio.run(run_checks(
        config_dir=paths.CONFIG_DIR, secrets=secrets, proxy=ProxyClient(relay) if relay else None,
        github=GitHubReader(secrets["GITHUB_READ_TOKEN"]) if secrets.get("GITHUB_READ_TOKEN") else None,
        linear=LinearClient(secrets["LINEAR_API_KEY"]) if secrets.get("LINEAR_API_KEY") else None))
    config = _config_or_none(paths.CONFIG_DIR)
    if config is not None:
        from agent import db

        conn = db.connect(paths.DB_DIR / "agent.db")
        checks.append(check_runner_token(conn, paths.DATA / "runner" / "runner-token"))
    for c in checks:
        print(f"{'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    return 0 if all(c.ok for c in checks) else 1
