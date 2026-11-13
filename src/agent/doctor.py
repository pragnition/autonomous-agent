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


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


async def run_checks(*, config_dir: Path, secrets: dict[str, str], proxy=None, github=None, linear=None,
                     required: tuple[str, ...] = REQUIRED_CORE) -> list[Check]:
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

    names = list(required) + sorted({r.secret for p in packs.values() for r in p.project.egress_inject.values()})
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
    for c in checks:
        print(f"{'ok  ' if c.ok else 'FAIL'}  {c.name}: {c.detail}")
    return 0 if all(c.ok for c in checks) else 1
