"""Load and validate the install's config: agent.toml and one pack per project.

Every problem is collected before raising, so `doctor` can show them all at once.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PERSON_ROLES = ("member", "admin")
PROFILES = ("code", "debug", "general")
ISOLATION = ("open", "sandboxes")
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
NETWORKS = ("open", "read")

DEFAULT_RESOURCES = {"code": (4, 8, 30), "debug": (2, 2, 10), "general": (2, 2, 10)}
DEFAULT_BRIEFING = {"session": 2000, "production": 1000, "ambient": 600}
DEFAULT_LOOKUPS = {"session": 6000, "production": 3000, "ambient": 0}
# Always refused for az, whatever the config says: they print or forward the login's token (spec v2 §7)
AZ_BUILTIN_DENY = ("account get-access-token", "rest")

# The stack's own credentials: never available to a pack, whatever the pack declares
RESERVED_SECRETS = {"GITHUB_WRITE_TOKEN", "GITHUB_READ_TOKEN", "INTERNAL_TOKEN", "SLACK_BOT_TOKEN",
                    "SLACK_APP_TOKEN", "SLACK_ALERT_WEBHOOK", "LINEAR_API_KEY", "SANDBOXD_TOKEN"}


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


@dataclass(frozen=True)
class SandboxResources:
    cpus: int
    memory_gb: int
    disk_gb: int


@dataclass
class SandboxSettings:
    resources: dict[str, SandboxResources] = field(
        default_factory=lambda: {k: SandboxResources(*v) for k, v in DEFAULT_RESOURCES.items()})
    host_memory_budget: float = 0.75
    idle_stop_minutes: int = 30
    rebuild_every_days: int = 7
    isolation: str = "open"        # "open": one agent container (spec v3); "sandboxes": Docker Sandboxes (spec v2)
    agent_uid: int = 1001          # the agent container's user, which owns what core shares with it


@dataclass
class LinearSettings:
    public_url: str = ""           # Tailscale Funnel base URL for Linear's webhook and OAuth callback (spec v3 §8)


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
    max_concurrent_sessions: int = 3
    max_tasks_per_day: int = 20
    task_minutes: int = 90
    max_turns: int = 400


@dataclass
class Models:
    default: str = "claude-opus-5-5"
    hard: str = "claude-fable-5-1"
    chores: str = "claude-sonnet-5"


@dataclass
class MemoryBudget:
    briefing: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BRIEFING))
    lookups: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_LOOKUPS))


@dataclass
class CliRules:
    command: str
    profiles: list[str]
    env: dict[str, str] = field(default_factory=dict)
    login: list[str] | None = None
    login_check: list[str] | None = None
    relogin_patterns: list[str] = field(default_factory=list)
    read: list[str] = field(default_factory=list)
    confirm: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    egress: list[str] = field(default_factory=list)
    max_output_chars: int = 8000


@dataclass(frozen=True)
class DashboardSettings:
    port: int = 8086
    trusted_peers: tuple[str, ...] = ("172.30.0.1",)   # where tailscale serve's requests arrive from: the host gateway
    agent_origins: tuple[str, ...] = ("agent-core", "agent-sandbox")  # the account proxy's origins that are the agent


@dataclass
class AgentConfig:
    name: str
    timezone: str
    people: dict[str, Person]
    slack_channels: list[str]
    limits: Limits
    models: Models
    memory_budget: MemoryBudget
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    cli: dict[str, CliRules] = field(default_factory=dict)
    observability: ObservabilitySettings = field(default_factory=ObservabilitySettings)
    linear: LinearSettings = field(default_factory=LinearSettings)
    mcp: dict[str, McpServer] = field(default_factory=dict)
    github: str = ""  # the agent's GitHub login
    dashboard: DashboardSettings = field(default_factory=DashboardSettings)

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


@dataclass(frozen=True)
class InjectRule:
    header: str
    secret: str


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
    network: str = "open"
    egress_allow: list[str] = field(default_factory=list)
    egress_inject: dict[str, InjectRule] = field(default_factory=dict)
    scrub_patterns: list[str] = field(default_factory=list)
    sandbox_setup: dict[str, list[str]] = field(default_factory=dict)
    sandbox_resources: dict[str, SandboxResources] = field(default_factory=dict)
    sandbox_template: dict[str, str] = field(default_factory=dict)


@dataclass
class Pack:
    project: ProjectConfig
    dir: Path
    plugin_dir: Path


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


def _hosts(data: dict, key: str, where: str, problems: list[str]) -> list[str]:
    hosts = _str_list(data, key, where, problems)
    for host in hosts:
        if "*" in host or "?" in host:
            problems.append(f"{where}: '{host}' is a wildcard; no wildcards, list exact hosts")
    return hosts


def _resources(data: dict, default: SandboxResources, where: str, problems: list[str]) -> SandboxResources:
    return SandboxResources(_int(data, "cpus", default.cpus, where, problems),
                            _int(data, "memory_gb", default.memory_gb, where, problems),
                            _int(data, "disk_gb", default.disk_gb, where, problems))


def _cmd(data: dict, key: str, where: str, problems: list[str]) -> list[str] | None:
    return _str_list(data, key, where, problems) if key in data else None


def _cli(name: str, data: dict, where: str, problems: list[str]) -> CliRules:
    cw = f"{where} [cli.{name}]"
    profiles = _str_list(data, "profiles", cw, problems) or ["debug"]
    if profiles != ["debug"]:
        problems.append(f"{cw}: CLI tools run only for the debug profile (profiles = [\"debug\"]), got {profiles}")
    env = data.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        problems.append(f"{cw}: 'env' must be a table of strings")
        env = {}
    deny = _str_list(data, "deny", cw, problems)
    if _str(data, "command", cw, problems) == "az":
        deny = [*deny, *(d for d in AZ_BUILTIN_DENY if d not in deny)]
    return CliRules(command=data.get("command", ""), profiles=profiles, env=dict(env),
                    login=_cmd(data, "login", cw, problems), login_check=_cmd(data, "login_check", cw, problems),
                    relogin_patterns=_str_list(data, "relogin_patterns", cw, problems),
                    read=_str_list(data, "read", cw, problems), confirm=_str_list(data, "confirm", cw, problems),
                    deny=deny, egress=_hosts(data, "egress", cw, problems),
                    max_output_chars=_int(data, "max_output_chars", 8000, cw, problems))


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

    slack = data.get("slack") or {}
    channels = _str_list(slack, "channels", f"{where} [slack]", problems)

    lim = data.get("limits") or {}
    lw = f"{where} [limits]"
    limits = Limits(max_concurrent_sessions=_int(lim, "max_concurrent_sessions", 3, lw, problems),
                    max_tasks_per_day=_int(lim, "max_tasks_per_day", 20, lw, problems),
                    task_minutes=_int(lim, "task_minutes", 90, lw, problems),
                    max_turns=_int(lim, "max_turns", 400, lw, problems))

    claude = (data.get("claude") or {}).get("models") or {}
    models = Models()
    for key in ("default", "hard", "chores"):
        if key in claude:
            setattr(models, key, _str(claude, key, f"{where} [claude.models]", problems))

    mem = (data.get("memory") or {}).get("budget") or {}
    budget = MemoryBudget()
    for key, target in (("briefing", budget.briefing), ("lookups", budget.lookups)):
        for kind, value in (mem.get(key) or {}).items():
            target[kind] = _int({kind: value}, kind, target.get(kind, 0), f"{where} [memory.budget.{key}]", problems)

    sb = data.get("sandbox") or {}
    sw = f"{where} [sandbox]"
    sandbox = SandboxSettings()
    for profile, res in (sb.get("resources") or {}).items():
        if profile not in PROFILES:
            problems.append(f"{sw}: unknown profile '{profile}' (use {', '.join(PROFILES)})")
            continue
        sandbox.resources[profile] = _resources(res, sandbox.resources[profile], f"{sw}.resources.{profile}", problems)
    budget_frac = sb.get("host_memory_budget", 0.75)
    if not isinstance(budget_frac, (int, float)) or not 0 < budget_frac <= 1:
        problems.append(f"{sw}: 'host_memory_budget' must be a fraction between 0 and 1")
    else:
        sandbox.host_memory_budget = float(budget_frac)
    sandbox.idle_stop_minutes = _int(sb, "idle_stop_minutes", 30, sw, problems)
    sandbox.rebuild_every_days = _int(sb, "rebuild_every_days", 7, sw, problems)
    sandbox.isolation = sb.get("isolation", "open")
    if sandbox.isolation not in ISOLATION:
        problems.append(f"{sw}: 'isolation' must be one of {', '.join(ISOLATION)}, got {sandbox.isolation!r}")
    uid = sb.get("agent_uid", 1001)
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        problems.append(f"{sw}: 'agent_uid' must be a positive integer (the agent container's uid)")
    else:
        sandbox.agent_uid = uid

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

    if problems:
        raise ConfigError(problems)
    return AgentConfig(name, timezone, people, channels, limits, models, budget, sandbox, cli, observability,
                       linear, mcp, github_login, dashboard)


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
    egress = data.get("egress") or {}

    network = proj.get("network", "open")
    if network not in NETWORKS:
        problems.append(f"{pw}: 'network' must be one of {', '.join(NETWORKS)} (got {network!r})")
    inject: dict[str, InjectRule] = {}
    for host, rule in (egress.get("inject") or {}).items():
        iw = f"{where} [egress.inject.\"{host}\"]"
        if "*" in host or "?" in host:
            problems.append(f"{iw}: '{host}' is a wildcard; no wildcards, list exact hosts")
        inject[host] = InjectRule(_str(rule, "header", iw, problems), _str(rule, "secret", iw, problems))
    patterns = _str_list(data.get("scrub") or {}, "patterns", f"{where} [scrub]", problems)
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as e:
            problems.append(f"{where} [scrub]: pattern {pattern!r} does not compile ({e})")
    setup: dict[str, list[str]] = {}
    resources: dict[str, SandboxResources] = {}
    templates: dict[str, str] = {}
    for profile, sdata in (data.get("sandbox") or {}).items():
        sw = f"{where} [sandbox.{profile}]"
        if profile not in PROFILES:
            problems.append(f"{sw}: unknown profile '{profile}' (use {', '.join(PROFILES)})")
            continue
        if "setup" in sdata:
            setup[profile] = _str_list(sdata, "setup", sw, problems)
        if any(k in sdata for k in ("cpus", "memory_gb", "disk_gb")):
            resources[profile] = _resources(sdata, agent.sandbox.resources[profile], sw, problems)
        if "template" in sdata:
            templates[profile] = _str(sdata, "template", sw, problems)

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
        network=network if network in NETWORKS else "open",
        egress_allow=_hosts(egress, "allow", f"{where} [egress]", problems),
        egress_inject=inject,
        scrub_patterns=patterns,
        sandbox_setup=setup,
        sandbox_resources=resources,
        sandbox_template=templates,
    )
    reserved = RESERVED_SECRETS & {*project.secrets_worker, *(r.secret for r in inject.values())}
    for name in sorted(reserved):
        problems.append(f"{where} [secrets]: {name} is the stack's own credential and cannot be given to a pack")
    if project.owner and project.owner not in agent.people:
        problems.append(f"{pw}: owner '{project.owner}' is not in agent.toml [people]")

    plugin_dir = pack_dir / "plugin"
    if not (plugin_dir / ".claude-plugin" / "plugin.json").is_file():
        problems.append(f"{plugin_dir}: missing .claude-plugin/plugin.json")

    if problems:
        raise ConfigError(problems)
    return Pack(project, pack_dir, plugin_dir)


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
