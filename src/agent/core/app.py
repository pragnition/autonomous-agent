"""core (spec v2 §4): the identity side. It holds every credential, runs only our code and never runs Claude.

It turns Slack and Linear messages into work items, serves the agent API and the git endpoint that sandbox
runners pull from, carries the guarantees in its tools, and asks sandboxd for sandboxes when work is waiting.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from aiohttp import web

from agent import db
from agent.config import load_agent_config, load_packs
from agent.connectors.linear import LinearPoller
from agent.core.agent_api import make_agent_app
from agent.core.agent_tools import AgentToolRouter, profile_of
from agent.core.bundles import build_bundle
from agent.core.cli import CliRunner, register_cli_tools
from agent.core.cli_admin import DeviceCodeLogin, Proposals, register_login_tools
from agent.core.consolidation import ConsolidationService
from agent.core.conversations import Conversations
from agent.core.gitserve import GitHost, tokens_auth
from agent.core.notifier import SlackNotifier
from agent.core.payloads import Payloads
from agent.core.pr_stats import PrStatsPoller
from agent.core.production import register_production_tools
from agent.core.prs import register_pr_tools
from agent.core.sandbox_tools import register_sandbox_tools
from agent.core.sessions import Gate, WorkItem, WorkQueue, mint_token, resolve_token
from agent.core.tracker_tools import register_tracker_tools
from agent.memory.store import MemoryStore
from agent.persona import load_persona, render_persona
from agent.tasks import Task

log = logging.getLogger("agent.core")
SANDBOX_TOKEN_TTL_S = 60 * 86400
ENSURE_GRACE_S = 120    # a sandbox takes a while to boot; don't ask twice
RUNNER_SILENT_S = 90    # a runner long-polls every 25 s, so this much silence means it is gone


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
    def __init__(self, *, config_dir: Path, data_dir: Path, secrets: dict[str, str], slack, proxy, sandboxd=None,
                 linear_client=None, github_reader=None, github_writer=None, bot_user_id: str | None = None,
                 cli_exec=asyncio.create_subprocess_exec):
        self.config_dir, self.data_dir, self.secrets = Path(config_dir), Path(data_dir), secrets
        self.slack, self.sandboxd = slack, sandboxd
        self.config = load_agent_config(self.config_dir / "agent.toml")
        self.packs = load_packs(self.config_dir, self.config)
        (self.data_dir / "db").mkdir(parents=True, exist_ok=True)
        self.conn = db.connect(self.data_dir / "db" / "agent.db")
        db.migrate(self.conn)
        self.memory = MemoryStore(self.data_dir / "db" / "memory.db")
        self.secret_values = [v for v in secrets.values() if v]
        self.queue = WorkQueue(self.conn)
        self.linear_poller = LinearPoller(self.conn, linear_client, config=self.config, packs=self.packs,
                                          agent_label=self.config.name) if linear_client else None
        self.notifier = Notifiers(SlackNotifier(slack, self.conn),
                                  LinearNotifier(self.linear_poller) if self.linear_poller else None)
        persona = render_persona(load_persona(self.config_dir / "persona.toml"), name=self.config.name)
        self.payloads = Payloads(self.conn, config=self.config, packs=self.packs, memory=self.memory,
                                 persona_text=persona, config_dir=self.config_dir, secrets=secrets)
        self.githost = GitHost(self.data_dir / "git", agent_name=self.config.name,
                               read_token=secrets.get("GITHUB_READ_TOKEN"),
                               auth=tokens_auth(lambda token: resolve_token(self.conn, token)))

        self.router = AgentToolRouter(self.conn, config=self.config, packs=self.packs, queue=self.queue,
                                      memory=self.memory, slack=slack, notifier=self.notifier,
                                      secret_values=self.secret_values, turn_payload=self._turn_payload)
        if github_writer is not None:
            register_pr_tools(self.router, githost=self.githost, writer=github_writer,
                              write_token=secrets.get("GITHUB_WRITE_TOKEN", ""))
        if github_reader is not None or linear_client is not None:
            register_tracker_tools(self.router, reader=github_reader, linear=linear_client)
        self.cli_runners = {name: CliRunner(rules, secret_values=self.secret_values, exec_fn=cli_exec)
                            for name, rules in self.config.cli.items()}
        self.proposals = Proposals(self.conn, slack, config=self.config, runner_for=self.cli_runners.__getitem__,
                                   queue=self.queue, turn_payload=self._turn_payload)
        register_cli_tools(self.router, runners=self.cli_runners, propose=self.proposals.propose)
        self.login = DeviceCodeLogin(slack, config=self.config, rules=self.config.cli, exec_fn=cli_exec)
        register_login_tools(self.router, self.login)
        register_production_tools(self.router, production_payload=self.payloads.production)
        if sandboxd is not None:
            register_sandbox_tools(self.router, sandboxd)

        self.conversations = Conversations(self.conn, config=self.config, packs=self.packs, queue=self.queue,
                                           slack=slack, payloads=self.payloads, notifier=self.notifier,
                                           bot_user_id=bot_user_id, linear_poller=self.linear_poller)
        self.consolidation = ConsolidationService(self.conn, self.memory, self.queue, self.payloads,
                                                  secret_values=self.secret_values, mirror_for=self.mirror)
        self.pr_stats = PrStatsPoller(self.conn, github_reader, agent_login=self.config.name, packs=self.packs,
                                      on_change=self.notifier.task_changed) if github_reader else None
        self.pulls: dict[str, float] = {}      # sandbox -> when its runner last asked for work
        self._started = time.time()            # runners get RUNNER_SILENT_S after a core restart to reconnect
        self._ensured: dict[str, float] = {}   # sandbox -> when we last asked sandboxd to start it
        self.agent_app = make_agent_app(
            conn=self.conn, queue=self.queue, gate=Gate(self.conn, self.config, proxy), tools=self.router,
            context_for=self.router.context_for,
            bundles=lambda profile, project: build_bundle(self.config_dir, profile, project),
            on_result=self.on_result, on_files=self.on_files, sandboxd_token=secrets.get("SANDBOXD_TOKEN", ""),
            runs_dir=self.data_dir / "runs", pulls=self.pulls,
            session_ttl_s=(self.config.limits.task_minutes + 10) * 60)
        self.agent_app.router.add_route("*", "/git/{project}.git/{tail:.*}", self.githost.aiohttp_handler)

    def _turn_payload(self, conversation_id: str, text: str, actor: str | None, unattended: bool) -> dict:
        convo = self.conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        return self.payloads.turn(convo, text, actor, unattended=unattended)

    def mirror(self, project: str | None) -> Path | None:
        path = self.data_dir / "mirrors" / project if project else None
        return path if path and path.exists() else None

    # results from sandboxes

    def _record_run(self, item: WorkItem, result: dict) -> None:
        convo = self.conn.execute("SELECT task_id FROM conversations WHERE id = ?",
                                  (item.conversation_id,)).fetchone() if item.conversation_id else None
        now = db.utcnow()
        self.conn.execute(
            "INSERT OR REPLACE INTO runs(id, task_id, role, stage, status, session_id, account_code, forks, outcome, "
            "turns, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd_est, transcript_path, "
            "error, started_at, ended_at) VALUES (?, ?, ?, ?, 'finished', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item.id, convo["task_id"] if convo else None, profile_of(item.sandbox), item.kind,
             result.get("session_id"), result.get("account_code"), int(result.get("forks") or 0), result.get("outcome"),
             int(result.get("turns") or 0), int(result.get("input_tokens") or 0), int(result.get("output_tokens") or 0),
             int(result.get("cache_read_tokens") or 0), int(result.get("cache_write_tokens") or 0),
             float(result.get("cost_usd_est") or 0), str(self.data_dir / "runs" / f"{item.id}.jsonl"),
             result.get("error"), now, now))

    async def on_result(self, item: WorkItem | None, result: dict) -> None:
        if item is None:
            return
        self._record_run(item, result)
        if item.kind == "turn":
            await self.conversations.on_result(item, result)
        elif item.kind == "chore":
            await self.consolidation.on_result(item, result)

    async def on_files(self, item_id: str, files: list[tuple[str, bytes]], text: str) -> None:
        ctx = self.router.context_for(item_id)
        if not ctx.channel or not files:
            return
        await self.slack.files_upload_v2(channel=ctx.channel, thread_ts=ctx.thread_ts, initial_comment=text or None,
                                         file_uploads=[{"file": data, "filename": name} for name, data in files])

    # sandboxes

    async def ensure_sandboxes(self) -> None:
        """Ask sandboxd, with a fresh sandbox token, for every sandbox that has work waiting and either isn't
        running or has a runner that stopped asking for work. Not again while one is still booting."""
        if self.sandboxd is None:
            return
        wanted = [r["sandbox"] for r in self.conn.execute(
            "SELECT DISTINCT sandbox FROM work_items WHERE status = 'queued' ORDER BY sandbox")]
        if not wanted:
            return
        states, now = await self.sandboxd.status(), time.time()
        for name in wanted:
            if now - self._ensured.get(name, 0) < ENSURE_GRACE_S:
                continue
            running = (states.get(name) or {}).get("state") == "running"
            if running and now - self.pulls.get(name, self._started) < RUNNER_SILENT_S:
                continue
            project = None if name == "general" else name.split("-", 1)[1]
            if project is not None and project not in self.packs:
                continue
            token = mint_token(self.conn, "sandbox", sandbox=name, work_item_id=None, ttl_s=SANDBOX_TOKEN_TTL_S)
            inject = ({host: self.secrets.get(rule.secret, "")
                       for host, rule in self.packs[project].project.egress_inject.items()} if project else {})
            self._ensured[name] = now
            await self.sandboxd.ensure(name, profile_of(name), project, token, inject)

    # loops

    async def refresh_git(self) -> None:
        for name, pack in self.packs.items():
            await asyncio.to_thread(self.githost.ensure, name, pack.project.repo)
            await asyncio.to_thread(self.githost.refresh, name)
            await asyncio.to_thread(self.githost.working_copy, name, pack.project.default_branch)

    async def sweep_memory(self) -> None:
        self.consolidation.sweep()
        self.consolidation.sweep_threads(datetime.now(UTC))

    async def maintenance(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self.memory.demote_unused(now)
        self.conn.execute("UPDATE proposals SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
                          (now.strftime("%Y-%m-%dT%H:%M:%SZ"),))

    async def _every(self, seconds: float, fn) -> None:
        while True:
            try:
                await fn()
            except Exception:
                log.exception("loop %s failed", getattr(fn, "__name__", fn))
            await asyncio.sleep(seconds)

    async def serve(self, slack_app, app_token: str, port: int = 8080) -> None:
        from agent.connectors.slack_app import run_socket_mode

        runner = web.AppRunner(self.agent_app)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", port).start()
        loops = [self._every(300, self.refresh_git), self._every(60, self.conversations.intake_linear),
                 self._every(300, self.sweep_memory), self._every(3600, self.maintenance),
                 self._every(15, self.ensure_sandboxes)]
        if self.pr_stats:
            loops.append(self._every(300, self.pr_stats.poll_all))
        await asyncio.gather(run_socket_mode(slack_app, app_token), *loops)


def main() -> int:
    from slack_sdk.web.async_client import AsyncWebClient

    from agent import paths
    from agent.clients import SandboxdClient
    from agent.connectors.github_read import GitHubReader
    from agent.connectors.github_write import GitHubWriter
    from agent.connectors.linear import LinearClient
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
            sandboxd=SandboxdClient(os.environ.get("SANDBOXD_URL", "http://172.30.0.1:28100"),
                                    secrets.get("SANDBOXD_TOKEN", "")),
            linear_client=LinearClient(secrets["LINEAR_API_KEY"]) if secrets.get("LINEAR_API_KEY") else None,
            github_reader=GitHubReader(secrets["GITHUB_READ_TOKEN"]) if secrets.get("GITHUB_READ_TOKEN") else None,
            github_writer=GitHubWriter(secrets["GITHUB_WRITE_TOKEN"]) if secrets.get("GITHUB_WRITE_TOKEN") else None,
            bot_user_id=bot_user_id)
        await app.serve(build_app(secrets["SLACK_BOT_TOKEN"], app.conversations, on_action=app.proposals.on_action),
                        secrets["SLACK_APP_TOKEN"])

    asyncio.run(run())
    return 0

