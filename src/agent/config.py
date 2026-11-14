"""Load and validate the install's config: agent.toml and one pack per project.

Every problem is collected before raising, so `doctor` can show them all at once.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PERSON_ROLES = ("member", "admin")
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

# The stack's own credentials: never available to a pack, whatever the pack declares
RESERVED_SECRETS = {"GITHUB_WRITE_TOKEN", "GITHUB_READ_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN",
                    "SLACK_ALERT_WEBHOOK", "LINEAR_API_KEY"}


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Person:
    key: str
    slack: str
    github: str
    linear: str
    role: str
    tailscale: str = ""  # the person's Tailscale login, for the dashboard


@dataclass
class AgentSettings:
    uid: int = 1001                # the agent container's user, which owns what core shares with it


@dataclass
class LinearSettings:
    public_url: str = ""           # Tailscale Funnel base URL for Linear's webhook and OAuth callback


@dataclass(frozen=True)
class McpServer:
    type: str                      # "http" (streamable HTTP)
    url: str
    headers: dict[str, str]        # values may use ${VAR}, filled from the session's environment


@dataclass
class ObservabilitySettings:
    digest_channel: str = ""       # where the daily KPI digest goes; empty means no digest
    digest_time: str = "09:00"     # HH:MM in the config's timezone


@dataclass
class Limits:
    max_concurrent_sessions: int = 10
    max_tasks_per_day: int = 20
    task_minutes: int = 90
    max_turns: int = 400


@dataclass
class Models:
    default: str = "claude-opus-5-5"
    hard: str = "claude-fable-5-1"


@dataclass
class CliRules:
    command: str
    env: dict[str, str] = field(default_factory=dict)
    login: list[str] | None = None
    login_check: list[str] | None = None


@dataclass(frozen=True)
class DashboardSettings:
    port: int = 8086
    trusted_peers: tuple[str, ...] = ("172.30.0.1",)   # where tailscale serve's requests arrive from: the host gateway
    agent_origins: tuple[str, ...] = ("agent-core", "agent-sandbox")  # the account proxy's origins that are the agent


@dataclass(frozen=True)
class LlmUpstream:
    base_url: str                  # e.g. https://<resource>.openai.azure.com/openai/v1
    key: str                       # the name of core's secret holding its API key
    header: str = "api-key"        # where the key goes; "Authorization" sends it as a Bearer token


@dataclass(frozen=True)
class LlmPrice:
    input: float                   # USD per million tokens
    cached_input: float
    output: float


@dataclass
class LlmMeterSettings:
    """Model calls made by the apps the agent runs and tests, metered against caps (core/llm_meter.py)."""
    port: int = 8087
    daily_usd: float = 10.0        # starting caps; the dashboard can change them at runtime
    monthly_usd: float = 100.0
    upstreams: dict[str, LlmUpstream] = field(default_factory=dict)
    prices: dict[str, LlmPrice] = field(default_factory=dict)   # by model name or part of one; "default" for the rest


@dataclass
class AgentConfig:
    name: str
    timezone: str
    people: dict[str, Person]
    limits: Limits
    models: Models
    agent: AgentSettings = field(default_factory=AgentSettings)
    cli: dict[str, CliRules] = field(default_factory=dict)
    observability: ObservabilitySettings = field(default_factory=ObservabilitySettings)
    linear: LinearSettings = field(default_factory=LinearSettings)
    mcp: dict[str, McpServer] = field(default_factory=dict)
    github: str = ""  # the agent's GitHub login
    dashboard: DashboardSettings = field(default_factory=DashboardSettings)
    llm_meter: LlmMeterSettings = field(default_factory=LlmMeterSettings)

    def person_by_tailscale(self, login: str | None) -> Person | None:
        if not login:
            return None
        return next((p for p in self.people.values() if p.tailscale and p.tailscale.lower() == login.lower()), None)

    def person_by_slack(self, user_id: str) -> Person | None:
        return next((p for p in self.people.values() if p.slack == user_id), None)

    def person_by_linear(self, email: str | None) -> Person | None:
        if not email:
            return None
        return next((p for p in self.people.values() if p.linear.lower() == email.lower()), None)


@dataclass
class ProjectConfig:
    name: str
    repo: str
    default_branch: str
    owner: str
    slack_channel: str
    linear_team: str
    linear_status: dict[str, str]
    build_setup: list[str]
    build_checks: list[str]
    secrets_worker: list[str]


@dataclass
class Pack:
    project: ProjectConfig


def _read_toml(path: Path, problems: list[str]) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        problems.append(f"{path}: file not found")
    except tomllib.TOMLDecodeError as e:
        problems.append(f"{path}: invalid TOML ({e})")
    return {}


def _str(data: dict, key: str, where: str, problems: list[str], default: str | None = None) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        problems.append(f"{where}: '{key}' is required and must be a non-empty string")
        return ""
    return value


def _str_list(data: dict, key: str, where: str, problems: list[str]) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{where}: '{key}' must be a list of strings")
        return []
    return value


def _int(data: dict, key: str, default: int, where: str, problems: list[str]) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        problems.append(f"{where}: '{key}' must be a non-negative integer")
        return default
    return value


def _cmd(data: dict, key: str, where: str, problems: list[str]) -> list[str] | None:
    return _str_list(data, key, where, problems) if key in data else None


def _cli(name: str, data: dict, where: str, problems: list[str]) -> CliRules:
    cw = f"{where} [cli.{name}]"
    env = data.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        problems.append(f"{cw}: 'env' must be a table of strings")
        env = {}
    return CliRules(command=_str(data, "command", cw, problems), env=dict(env),
                    login=_cmd(data, "login", cw, problems), login_check=_cmd(data, "login_check", cw, problems))


def _mcp(name: str, data: dict, where: str, problems: list[str]) -> McpServer:
    mw = f"{where} [mcp.{name}]"
    kind, url, headers = data.get("type", "http"), data.get("url", ""), data.get("headers", {})
    if kind != "http":
        problems.append(f"{mw}: 'type' must be \"http\" (streamable HTTP), got {kind!r}")
    if not (isinstance(url, str) and url.startswith("https://")):
        problems.append(f"{mw}: 'url' must be an https:// URL")
    if not (isinstance(headers, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items())):
        problems.append(f"{mw}: 'headers' must be a table of strings")
        headers = {}
    return McpServer(kind, url if isinstance(url, str) else "", dict(headers))


def _usd(data: dict, key: str, default: float, where: str, problems: list[str]) -> float:
    value = data.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        problems.append(f"{where}: '{key}' must be a non-negative number (US dollars)")
        return default
    return float(value)


def _llm_meter(data: dict, where: str, problems: list[str]) -> LlmMeterSettings:
    mw = f"{where} [llm_meter]"
    upstreams = {}
    for name, up in (data.get("upstreams") or {}).items():
        uw = f"{mw}.upstreams.{name}"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            problems.append(f"{uw}: the name goes in a URL path; use lowercase letters, digits and '-'")
        if not isinstance(up, dict):
            problems.append(f"{uw}: must be a table")
            continue
        base = up.get("base_url", "")
        if not (isinstance(base, str) and base.startswith("https://")):
            problems.append(f"{uw}: 'base_url' must be an https:// URL")
            base = ""
        header = up.get("header", "api-key")
        if header not in ("api-key", "Authorization"):
            problems.append(f"{uw}: 'header' must be \"api-key\" or \"Authorization\"")
        upstreams[name] = LlmUpstream(base.rstrip("/"), _str(up, "key", uw, problems), header)
    prices = {}
    for model, price in (data.get("prices") or {}).items():
        pw = f"{mw}.prices"
        if not (isinstance(price, list) and len(price) == 3 and
                all(isinstance(p, (int, float)) and not isinstance(p, bool) and p >= 0 for p in price)):
            problems.append(f"{pw}: '{model}' must be [input, cached input, output] in USD per million tokens")
            continue
        prices[model] = LlmPrice(*(float(p) for p in price))
    if upstreams and "default" not in prices:
        problems.append(f"{mw}.prices: add a 'default' price for models not listed, so every call is counted")
    return LlmMeterSettings(port=_int(data, "port", 8087, mw, problems),
                            daily_usd=_usd(data, "daily_usd", 10.0, mw, problems),
                            monthly_usd=_usd(data, "monthly_usd", 100.0, mw, problems),
                            upstreams=upstreams, prices=prices)


def load_agent_config(path: Path) -> AgentConfig:
    problems: list[str] = []
    data = _read_toml(Path(path), problems)
    if problems:
        raise ConfigError(problems)
    where = str(path)

    name = _str(data, "name", where, problems)
    github_login = _str(data, "github", where, problems) if "github" in data else name
    timezone = _str(data, "timezone", where, problems, default="Asia/Singapore")

    people: dict[str, Person] = {}
    for key, p in (data.get("people") or {}).items():
        pw = f"{where} [people.{key}]"
        if not isinstance(p, dict):
            problems.append(f"{pw}: must be a table")
            continue
        role = p.get("role", "member")
        if role not in PERSON_ROLES:
            problems.append(f"{pw}: 'role' must be one of {', '.join(PERSON_ROLES)}")
        login = p.get("github", "")  # optional
        if not isinstance(login, str):
            problems.append(f"{pw}: 'github' must be a string")
            login = ""
        tailscale = p.get("tailscale", "")  # optional
        if not isinstance(tailscale, str):
            problems.append(f"{pw}: 'tailscale' must be a string")
            tailscale = ""
        people[key] = Person(key, _str(p, "slack", pw, problems), login,
                             _str(p, "linear", pw, problems), role, tailscale)
    if not people:
        problems.append(f"{where}: at least one [people.<key>] entry is required")

    lim = data.get("limits") or {}
    lw = f"{where} [limits]"
    limits = Limits(max_concurrent_sessions=_int(lim, "max_concurrent_sessions", 10, lw, problems),
                    max_tasks_per_day=_int(lim, "max_tasks_per_day", 20, lw, problems),
                    task_minutes=_int(lim, "task_minutes", 90, lw, problems),
                    max_turns=_int(lim, "max_turns", 400, lw, problems))

    claude = (data.get("claude") or {}).get("models") or {}
    models = Models()
    for key in ("default", "hard"):
        if key in claude:
            setattr(models, key, _str(claude, key, f"{where} [claude.models]", problems))

    ag = data.get("agent") or {}
    aw = f"{where} [agent]"
    agent_settings = AgentSettings()
    uid = ag.get("uid", 1001)
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        problems.append(f"{aw}: 'uid' must be a positive integer (the agent container's uid)")
    else:
        agent_settings.uid = uid

    ob = data.get("observability") or {}
    ow = f"{where} [observability]"
    observability = ObservabilitySettings(digest_channel=ob.get("digest_channel", ""),
                                          digest_time=ob.get("digest_time", "09:00"))
    if not isinstance(observability.digest_channel, str):
        problems.append(f"{ow}: 'digest_channel' must be a string")
    if not (isinstance(observability.digest_time, str) and _HHMM.match(observability.digest_time)):
        problems.append(f"{ow}: 'digest_time' must be HH:MM (24-hour), got {observability.digest_time!r}")

    cli = {cname: _cli(cname, cdata, where, problems) for cname, cdata in (data.get("cli") or {}).items()}

    lin = data.get("linear") or {}
    linear = LinearSettings(public_url=lin.get("public_url", ""))
    if linear.public_url and not (isinstance(linear.public_url, str) and linear.public_url.startswith("https://")):
        problems.append(f"{where} [linear]: 'public_url' must be an https:// URL")
    linear.public_url = linear.public_url.rstrip("/") if isinstance(linear.public_url, str) else ""
    mcp = {mname: _mcp(mname, mdata, where, problems) for mname, mdata in (data.get("mcp") or {}).items()}

    dash = data.get("dashboard") or {}
    dw = f"{where} [dashboard]"
    dashboard = DashboardSettings(
        port=_int(dash, "port", 8086, dw, problems),
        trusted_peers=tuple(_str_list(dash, "trusted_peers", dw, problems)) if "trusted_peers" in dash
        else DashboardSettings.trusted_peers,
        agent_origins=tuple(_str_list(dash, "agent_origins", dw, problems)) if "agent_origins" in dash
        else DashboardSettings.agent_origins)

    llm_meter = _llm_meter(data.get("llm_meter") or {}, where, problems)

    if problems:
        raise ConfigError(problems)
    return AgentConfig(name, timezone, people, limits, models, agent_settings, cli, observability, linear,
                       mcp, github_login, dashboard, llm_meter)


def load_pack(pack_dir: Path, agent: AgentConfig) -> Pack:
    problems: list[str] = []
    pack_dir = Path(pack_dir)
    data = _read_toml(pack_dir / "project.toml", problems)
    if problems:
        raise ConfigError(problems)
    where = str(pack_dir / "project.toml")
    proj = data.get("project") or {}
    pw = f"{where} [project]"
    build = data.get("build") or {}
    secrets = data.get("secrets") or {}

    project = ProjectConfig(
        name=_str(proj, "name", pw, problems),
        repo=_str(proj, "repo", pw, problems),
        default_branch=_str(proj, "default_branch", pw, problems, default="main"),
        owner=_str(proj, "owner", pw, problems),
        slack_channel=_str(proj, "slack_channel", pw, problems),
        linear_team=_str(proj, "linear_team", pw, problems),
        linear_status={k: v for k, v in ((data.get("linear") or {}).get("status") or {}).items()},
        build_setup=_str_list(build, "setup", f"{where} [build]", problems),
        build_checks=_str_list(build, "checks", f"{where} [build]", problems),
        secrets_worker=_str_list(secrets, "worker", f"{where} [secrets]", problems),
    )
    reserved = RESERVED_SECRETS & set(project.secrets_worker)
    for name in sorted(reserved):
        problems.append(f"{where} [secrets]: {name} is the stack's own credential and cannot be given to a pack")
    if project.owner and project.owner not in agent.people:
        problems.append(f"{pw}: owner '{project.owner}' is not in agent.toml [people]")

    plugin_dir = pack_dir / "plugin"
    if not (plugin_dir / ".claude-plugin" / "plugin.json").is_file():
        problems.append(f"{plugin_dir}: missing .claude-plugin/plugin.json")

    if problems:
        raise ConfigError(problems)
    return Pack(project)


def load_packs(config_dir: Path, agent: AgentConfig) -> dict[str, Pack]:
    problems: list[str] = []
    packs: dict[str, Pack] = {}
    for pack_dir in sorted((Path(config_dir) / "packs").glob("*/")):
        try:
            pack = load_pack(pack_dir, agent)
        except ConfigError as e:
            problems.extend(e.problems)
            continue
        packs[pack.project.name] = pack
    if problems:
        raise ConfigError(problems)
    return packs
