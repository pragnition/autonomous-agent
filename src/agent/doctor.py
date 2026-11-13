"""`doctor`: one pass over everything the install needs, each check reported by name (spec §6.5)."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

from agent.config import ConfigError, load_agent_config, load_packs
from agent.proxy import ProxyUnavailable

REQUIRED_CORE = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "LINEAR_API_KEY", "GITHUB_READ_TOKEN", "GITHUB_WRITE_TOKEN",
                 "SANDBOXD_TOKEN")


def required_core(config) -> tuple[str, ...]:
    """SANDBOXD_TOKEN only matters when sandboxd runs the sandboxes."""
    required = REQUIRED_CORE if config.sandbox.isolation == "sandboxes" else \
        tuple(n for n in REQUIRED_CORE if n != "SANDBOXD_TOKEN")
    if config.linear.public_url:  # the Linear agent app (spec v3 §8)
        required += ("LINEAR_CLIENT_ID", "LINEAR_CLIENT_SECRET", "LINEAR_WEBHOOK_SECRET")
    return required


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def check_runner_tokens(conn, path: Path, names: list[str]) -> Check:
    """Open mode: every queue has a live token in the file the agent container reads."""
    from agent.core.sessions import resolve_token
    from agent.sandbox.runners import read_tokens

    tokens = read_tokens(path)
    bad = [n for n in names if not (tokens.get(n) and (info := resolve_token(conn, tokens[n])) is not None
                                    and info.sandbox == n)]
    return Check("runner tokens", not bad, f"missing or invalid: {', '.join(bad)}" if bad
                 else f"{len(names)} queue(s): {', '.join(names)}")


async def run_checks(*, config_dir: Path, secrets: dict[str, str], proxy=None, github=None, linear=None,
                     required: tuple[str, ...] | None = None) -> list[Check]:
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

    names = list(required if required is not None else required_core(config)) + sorted({r.secret for p in packs.values() for r in p.project.egress_inject.values()})
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
        checks.append(Check(f"checks:{name}", bool(pack.project.build_checks),
                            f"{len(pack.project.build_checks)} check command(s)" if pack.project.build_checks
                            else "no [build] checks: PRs would go out unverified"))
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
    relay = os.environ.get("ANTHROPIC_BASE_URL")
    checks = asyncio.run(run_checks(
        config_dir=paths.CONFIG_DIR, secrets=secrets, proxy=ProxyClient(relay) if relay else None,
        github=GitHubReader(secrets["GITHUB_READ_TOKEN"]) if secrets.get("GITHUB_READ_TOKEN") else None,
        linear=LinearClient(secrets["LINEAR_API_KEY"]) if secrets.get("LINEAR_API_KEY") else None))
    config = _config_or_none(paths.CONFIG_DIR)
    if config is not None and config.sandbox.isolation == "open":
        from agent import db
        from agent.core.runner_tokens import queue_names

        conn = db.connect(paths.DB_DIR / "agent.db")
        checks.append(check_runner_tokens(conn, paths.DATA / "runner" / "runner-tokens.json",
                                          queue_names(load_packs(paths.CONFIG_DIR, config))))
    for c in checks:
        print(f"{'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    return 0 if all(c.ok for c in checks) else 1
