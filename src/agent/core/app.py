"""core: the identity side. It holds every credential, runs only our code and never runs Claude.

It turns Slack and Linear messages into work items, serves the agent API and the git endpoint the agent
container pulls from, and carries the guarantees in its tools.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from aiohttp import web

from agent import db, events
from agent.config import load_agent_config, load_packs
from agent.connectors.linear import LinearClient, LinearPoller
from agent.core.agent_api import make_agent_app
from agent.core.agent_tools import AgentToolRouter
from agent.core.bundles import build_bundle
from agent.core.cli_admin import DeviceCodeLogin, register_login_tools
from agent.core.conversations import Conversations
from agent.core.kpis import compute_kpis, format_kpis, register_kpi_tools
from agent.core.linear_app import INSTALL_DM_KEY, LinearAppAuth, register_linear_tools
from agent.core.linear_sessions import LinearAgentSessions
from agent.core import llm_meter
from agent.core.llm_meter import make_meter_app
from agent.core.public_api import make_public_app
from agent.core.notifier import SlackNotifier
from agent.core.payloads import Payloads
from agent.core.pr_stats import PrStatsPoller
from agent.core.runner_tokens import sync_runner_token
from agent.core.sessions import Gate, WorkItem, WorkQueue
from agent.core.tracker_tools import register_tracker_tools
from agent.dashboard.app import make_dashboard_app
from agent.dashboard.names import NAMES_FILE, AccountNames
from agent.dashboard.transcript import make_scrubber
from agent.persona import load_persona, render_persona
from agent.scrub import scrub
from agent.tasks import Task

log = logging.getLogger("agent.core")


def core_is_root() -> bool:
    return os.geteuid() == 0


PUBLIC_PORT = 8085                   # Linear's webhook and OAuth callback (Tailscale Funnel), published on 127.0.0.1:18085
DIGEST_SETTING = "kpi_digest_date"   # the local date of the last digest, so restarts don't post twice
PERMALINK_RETRY_S = 600  # how long a failed Slack permalink lookup is remembered
PERMALINK_CACHE = 5000   # permalinks kept in memory; the oldest goes first
DASHBOARD_STATIC = Path(os.environ.get("DASHBOARD_STATIC", "/app/web"))  # the built web app in the core image


class Notifiers:
    """Fan a task change out to Slack and Linear; one failing doesn't stop the other."""

    def __init__(self, *parts):
        self.parts = [p for p in parts if p is not None]

    async def task_changed(self, task: Task) -> None:
        for part in self.parts:
            try:
                await part.task_changed(task)
            except Exception:
                log.exception("notifier %s failed for %s", type(part).__name__, task.id)

    async def ask(self, task: Task, question: str) -> None:
        for part in self.parts:
            try:
                await part.ask(task, question)
            except Exception:
                log.exception("notifier %s failed to ask for %s", type(part).__name__, task.id)


class LinearNotifier:
    def __init__(self, poller):
        self.poller = poller

    async def task_changed(self, task: Task) -> None:
        await self.poller.sync(task)

    async def ask(self, task: Task, question: str) -> None:
        if task.linear_issue_id:
            await self.poller.client.comment(task.linear_issue_id, f"I have a question; answer it in the Slack "
                                                                   f"thread for {task.id}: {question}")


class CoreApp:
    def __init__(self, *, config_dir: Path, data_dir: Path, secrets: dict[str, str], slack, proxy,
                 linear_client=None, github_reader=None, bot_user_id: str | None = None,
                 cli_exec=asyncio.create_subprocess_exec):
        self.config_dir, self.data_dir, self.secrets = Path(config_dir), Path(data_dir), secrets
        self.slack = slack
        self.config = load_agent_config(self.config_dir / "agent.toml")
        self.packs = load_packs(self.config_dir, self.config)
        (self.data_dir / "db").mkdir(parents=True, exist_ok=True)
        self.conn = db.connect(self.data_dir / "db" / "agent.db")
        db.create_schema(self.conn)
        self.secret_values = [v for v in secrets.values() if v]
        self.queue = WorkQueue(self.conn)
        self.linear_poller = LinearPoller(self.conn, linear_client, config=self.config, packs=self.packs,
                                          agent_label=self.config.name) if linear_client else None
        self.notifier = Notifiers(SlackNotifier(slack, self.conn),
                                  LinearNotifier(self.linear_poller) if self.linear_poller else None)
        persona = render_persona(load_persona(self.config_dir / "persona.toml"), name=self.config.name)
        self.payloads = Payloads(self.conn, config=self.config, packs=self.packs, persona_text=persona,
                                 config_dir=self.config_dir, secrets=secrets)

        self.router = AgentToolRouter(self.conn, config=self.config, packs=self.packs, queue=self.queue,
                                      slack=slack, notifier=self.notifier, secret_values=self.secret_values,
                                      turn_payload=self._turn_payload)
        if github_reader is not None or linear_client is not None:
            register_tracker_tools(self.router, reader=github_reader, linear=linear_client)
        register_kpi_tools(self.router)
        self.linear_auth, self.public_app, self.linear_sessions = None, None, None
        if self.config.linear.public_url and secrets.get("LINEAR_CLIENT_ID"):
            self.linear_auth = LinearAppAuth(
                self.conn, client_id=secrets["LINEAR_CLIENT_ID"], client_secret=secrets.get("LINEAR_CLIENT_SECRET", ""),
                redirect_uri=f"{self.config.linear.public_url}/linear/oauth/callback",
                token_path=self.data_dir / "runner" / "linear.env",
                owner_uid=self.config.agent.uid if core_is_root() else None)
            register_linear_tools(self.router, self.linear_auth)
            if linear_client is not None:   # installed, core's own Linear calls show as the agent too
                linear_client.token = self.linear_auth.token
            self.linear_sessions = LinearAgentSessions(
                self.conn, queue=self.queue, payloads=self.payloads,
                client=linear_client or LinearClient("", token=self.linear_auth.token))
            self.public_app = make_public_app(webhook_secret=secrets.get("LINEAR_WEBHOOK_SECRET", ""),
                                              on_event=self.on_linear_event, auth=self.linear_auth,
                                              on_installed=self.on_linear_installed)
        self.runner_token_path = self.data_dir / "runner" / "runner-token"
        self.login = DeviceCodeLogin(slack, config=self.config, rules=self.config.cli, exec_fn=cli_exec,
                                     run_as=self.config.agent.uid)
        register_login_tools(self.router, self.login)

        self._permalinks: dict[tuple[str, str], tuple[float, str | None]] = {}
        self._names: dict[str, tuple[float, str | None]] = {}
        self.conversations = Conversations(self.conn, config=self.config, packs=self.packs, queue=self.queue,
                                           slack=slack, payloads=self.payloads, notifier=self.notifier,
                                           bot_user_id=bot_user_id, linear_poller=self.linear_poller,
                                           person_name=self.person_name, permalink=self.permalink,
                                           secret_values=self.secret_values)
        self.pr_stats = PrStatsPoller(self.conn, github_reader, agent_login=self.config.github, packs=self.packs,
                                      on_change=self.notifier.task_changed) if github_reader else None
        self.agent_app = make_agent_app(
            conn=self.conn, queue=self.queue, gate=Gate(self.conn, self.config, proxy), tools=self.router,
            context_for=self.router.context_for,
            bundles=lambda: build_bundle(self.config_dir),
            on_result=self.on_result, on_files=self.on_files, refresh=self._refresh_turn,
            on_start=self.conversations.on_start,
            runs_dir=self.data_dir / "runs",
            session_ttl_s=(self.config.limits.task_minutes + 10) * 60)
        self.account_names = AccountNames(self.config_dir / NAMES_FILE)
        self.dashboard_app = make_dashboard_app(
            conn=self.conn, config=self.config, proxy=proxy, runs_dir=self.data_dir / "runs",
            static_dir=DASHBOARD_STATIC if DASHBOARD_STATIC.is_dir() else None, permalink=self.permalink,
            scrub=make_scrubber(self.secret_values), account_names=self.account_names,
            person_name=self.person_name, **dict(zip(("llm_view", "llm_set_caps"), self.llm_meter())))
        # Model calls from the apps the agent runs and tests, metered against caps; only the stack's network reaches it
        self.meter_app = make_meter_app(self.conn, self.config.llm_meter, secrets=secrets,
                                        timezone=self.config.timezone) if self.config.llm_meter.upstreams else None

    def llm_meter(self):
        """The model-call meter's view and cap setter for the dashboard, when the meter is installed."""
        settings = self.config.llm_meter
        if not settings.upstreams:
            return None, None
        return (lambda: llm_meter.meter_view(self.conn, settings, self.config.timezone),
                lambda actor, daily_usd=None, monthly_usd=None: llm_meter.set_caps(
                    self.conn, actor=actor, daily_usd=daily_usd, monthly_usd=monthly_usd))

    async def person_name(self, key: str) -> str | None:
        """A person's name as Slack has it, for the dashboard and each turn's first line; failures are remembered
        like permalinks'."""
        person = self.config.people.get(key)
        if person is None or not person.slack:
            return None
        now, cached = time.monotonic(), self._names.get(key)
        if cached and (cached[1] is not None or now - cached[0] < PERMALINK_RETRY_S):
            return cached[1]
        try:
            user = (await self.slack.users_info(user=person.slack))["user"]
            name = user.get("real_name") or (user.get("profile") or {}).get("real_name") or None
        except Exception:
            log.info("no Slack name for %s", key, exc_info=True)
            name = None
        self._names[key] = (now, name)
        return name

    async def permalink(self, channel: str, thread_ts: str) -> str | None:
        """A Slack message's link: a thread's for the dashboard, each message's for its turn's first line. A failure
        (the bot left the channel, a rate limit) is remembered for PERMALINK_RETRY_S, so a page that polls doesn't
        ask Slack again every few seconds."""
        key, now = (channel, thread_ts), time.monotonic()
        cached = self._permalinks.get(key)
        if cached and (cached[1] is not None or now - cached[0] < PERMALINK_RETRY_S):
            return cached[1]
        try:
            link = (await self.slack.chat_getPermalink(channel=channel, message_ts=thread_ts))["permalink"]
        except Exception:
            log.info("no Slack permalink for %s/%s", channel, thread_ts, exc_info=True)
            link = None
        self._permalinks.pop(key, None)
        self._permalinks[key] = (now, link)
        while len(self._permalinks) > PERMALINK_CACHE:
            del self._permalinks[next(iter(self._permalinks))]
        return link

    def _turn_payload(self, conversation_id: str, text: str, actor: str | None, unattended: bool) -> dict:
        convo = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return self.payloads.turn(convo, text, actor, unattended=unattended)

    def _refresh_turn(self, conversation_id: str, payload: dict) -> dict:
        convo = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return self.payloads.refresh_turn(convo, payload) if convo is not None else payload

    # results from the runner

    def _record_run(self, item: WorkItem, result: dict) -> None:
        convo = self.conn.execute("SELECT task_id FROM conversations WHERE id = ?",
                                  (item.conversation_id,)).fetchone() if item.conversation_id else None
        now = db.utcnow()
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(id, task_id, kind, status, session_id, account_code, forks, outcome, "
            "turns, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd_est, transcript_path, "
            "error, started_at, ended_at) VALUES (?, ?, ?, 'finished', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.id, convo["task_id"] if convo else None, item.kind,
             result.get("session_id"), result.get("account_code"), int(result.get("forks") or 0), result.get("outcome"),
             int(result.get("turns") or 0), int(result.get("input_tokens") or 0), int(result.get("output_tokens") or 0),
             int(result.get("cache_read_tokens") or 0), int(result.get("cache_write_tokens") or 0),
             float(result.get("cost_usd_est") or 0), str(self.data_dir / "runs" / f"{item.id}.jsonl"),
             result.get("error"), now, now))

    async def on_result(self, item: WorkItem | None, result: dict) -> None:
        if item is None:
            return
        self._record_run(item, result)
        await self.conversations.on_result(item, result)

    async def on_files(self, item_id: str, files: list[tuple[str, bytes]], text: str) -> bool:
        """Post files to the conversation's Slack thread. Every post is logged as evidence; returns whether it
        reached Slack."""
        ctx = self.router.context_for(item_id)
        if not files:
            return False
        text = scrub(text, self.secret_values)
        posted = bool(ctx.channel)
        if posted:
            await self.slack.files_upload_v2(channel=ctx.channel, thread_ts=ctx.thread_ts,
                                             initial_comment=text or None,
                                             file_uploads=[{"file": data, "filename": name} for name, data in files])
        events.append(self.conn, "files_posted", task_id=ctx.task_id, actor=ctx.actor,
                      data={"conversation_id": ctx.conversation_id, "files": [name for name, _ in files],
                            "text": text, "slack": posted})
        return posted

    # the agent container

    def _agent_owner(self) -> int | None:
        return self.config.agent.uid if core_is_root() else None

    def prepare_agent_dirs(self) -> None:
        """The agent container owns what core shares with it (the CLI logins, its runner token)."""
        cli, runner = self.data_dir / "cli", self.data_dir / "runner"
        for path in (cli, runner):
            path.mkdir(parents=True, exist_ok=True)
        owner = self._agent_owner()
        if owner is None:
            return
        os.chown(runner, owner, -1)
        os.chown(cli, owner, -1)
        for parent, dirs, files in os.walk(cli):
            for name in [*dirs, *files]:
                os.chown(Path(parent) / name, owner, -1)

    def share_project_envs(self) -> None:
        """Each project's app env file (data/secrets/projects/<project>.env, what the app needs to run locally)
        goes to the agent as runner/projects/<project>.env, readable only by the agent. Read at start, so restart
        core after changing one."""
        source, dest = self.data_dir / "secrets" / "projects", self.data_dir / "runner" / "projects"
        if not source.is_dir():
            return
        dest.mkdir(parents=True, exist_ok=True)
        owner = self._agent_owner()
        for env in sorted(source.glob("*.env")):
            tmp = dest / f".{env.name}.tmp"
            tmp.write_bytes(env.read_bytes())
            os.chmod(tmp, 0o600)
            if owner is not None:
                os.chown(tmp, owner, -1)
            os.replace(tmp, dest / env.name)
        if owner is not None:
            os.chown(dest, owner, -1)

    def sync_runner_token(self) -> str:
        return sync_runner_token(self.conn, self.runner_token_path, owner_uid=self._agent_owner())

    # loops

    async def maintenance(self) -> None:
        self.sync_runner_token()

    # Linear app

    async def on_linear_event(self, body: dict) -> None:
        log.info("Linear %s/%s event", body.get("type"), body.get("action"))
        if self.linear_sessions is not None:
            await self.linear_sessions.on_event(body)

    async def on_linear_installed(self, app_user_id: str) -> None:
        log.info("the Linear app is installed (app user %s)", app_user_id)
        dm = db.get_setting(self.conn, INSTALL_DM_KEY)
        if dm:
            await self.slack.chat_postMessage(channel=dm, text="I'm installed in Linear: delegate issues to me there.")

    async def refresh_linear_token(self) -> None:
        """Keep the agent's LINEAR_APP_TOKEN fresh even when core itself makes no Linear calls for a while."""
        if self.linear_auth is not None:
            await self.linear_auth.token()

    async def post_digest(self, now: datetime | None = None) -> bool:
        """The daily KPI digest: once per local day, at or after digest_time, to digest_channel."""
        ob = self.config.observability
        if not ob.digest_channel:
            return False
        now = now or datetime.now(UTC)
        local = now.astimezone(ZoneInfo(self.config.timezone))
        today = local.date().isoformat()
        if local.strftime("%H:%M") < ob.digest_time or db.get_setting(self.conn, DIGEST_SETTING) == today:
            return False
        text = format_kpis(compute_kpis(self.conn, since=now - timedelta(days=7), until=now))
        await self.slack.chat_postMessage(channel=ob.digest_channel, text=text)
        db.set_setting(self.conn, DIGEST_SETTING, today)
        return True

    async def _every(self, seconds: float, fn) -> None:
        while True:
            try:
                await fn()
            except Exception:
                log.exception("loop %s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(seconds)

    async def serve(self, slack_app, app_token: str, port: int = 8080) -> None:
        from agent.connectors.slack_app import run_socket_mode

        self.prepare_agent_dirs()
        self.share_project_envs()
        self.sync_runner_token()
        runner = web.AppRunner(self.agent_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        if self.public_app is not None:
            public = web.AppRunner(self.public_app)
            await public.setup()
            await web.TCPSite(public, "0.0.0.0", PUBLIC_PORT).start()
        dashboard = web.AppRunner(self.dashboard_app)
        await dashboard.setup()
        await web.TCPSite(dashboard, "0.0.0.0", self.config.dashboard.port).start()
        if self.meter_app is not None:
            meter = web.AppRunner(self.meter_app)
            await meter.setup()
            await web.TCPSite(meter, "0.0.0.0", self.config.llm_meter.port).start()
        loops = [self._every(60, self.conversations.intake_linear),
                 self._every(3600, self.maintenance),
                 self._every(300, self.post_digest), self._every(600, self.refresh_linear_token)]
        if self.pr_stats:
            loops.append(self._every(300, self.pr_stats.poll_all))
        await asyncio.gather(run_socket_mode(slack_app, app_token), *loops)


def main() -> int:
    from slack_sdk.web.async_client import AsyncWebClient

    from agent import paths
    from agent.connectors.github_read import GitHubReader
    from agent.connectors.slack_app import build_app
    from agent.proxy import ProxyClient
    from agent.secrets import load_secrets

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    secrets = load_secrets(paths.SECRETS_FILE)

    async def run() -> None:
        slack = AsyncWebClient(token=secrets["SLACK_BOT_TOKEN"])
        bot_user_id = (await slack.auth_test())["user_id"]
        app = CoreApp(
            config_dir=paths.CONFIG_DIR, data_dir=paths.DATA, secrets=secrets, slack=slack,
            proxy=ProxyClient(os.environ.get("PROXY_URL", "http://172.30.0.1:28090")),
            linear_client=LinearClient(secrets["LINEAR_API_KEY"]) if secrets.get("LINEAR_API_KEY") else None,
            github_reader=GitHubReader(secrets["GITHUB_READ_TOKEN"]) if secrets.get("GITHUB_READ_TOKEN") else None,
            bot_user_id=bot_user_id)
        await app.serve(build_app(secrets["SLACK_BOT_TOKEN"], app.conversations), secrets["SLACK_APP_TOKEN"])

    asyncio.run(run())
    return 0

