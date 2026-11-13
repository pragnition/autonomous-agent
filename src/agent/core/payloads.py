"""What a session starts with (spec v2 §6): the work item payload a runner turns into a Claude Code session.

A short system prompt from config (or the shipped default), the persona for Slack turns only, a capped memory
briefing, the model and budgets, the checkout to prepare and the folder to work in. Sessions with a checkout also
get the pack's test-only secrets; nothing else secret ever goes into a payload.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent.core.agent_tools import conversation_scopes
from agent.memory.briefing import build_briefing
from agent.memory.store import MemoryStore

DEFAULT_SYSTEM = Path(__file__).parent.parent / "defaults" / "system-open.md"
MEMORY_PROMPT = Path(__file__).parent.parent / "prompts" / "memory.md"


class Payloads:
    def __init__(self, conn: sqlite3.Connection, *, config, packs: dict, memory: MemoryStore, persona_text: str,
                 config_dir: Path, secrets: dict[str, str]):
        self.conn, self.config, self.packs, self.memory = conn, config, packs, memory
        self.persona_text, self.config_dir, self.secrets = persona_text, Path(config_dir), secrets

    def system_prompt(self) -> str:
        custom = self.config_dir / "agent" / "system.md"
        text = (custom if custom.is_file() else DEFAULT_SYSTEM).read_text()
        return text.replace("{name}", self.config.name).strip()

    def _base(self, *, text: str, actor: str | None, unattended: bool, model: str,
              system_parts: list[str]) -> dict:
        return {"text": text, "actor": actor, "unattended": unattended, "model": model,
                "system_prompt": "\n\n".join(p for p in system_parts if p),
                "max_turns": self.config.limits.max_turns, "timeout_s": self.config.limits.task_minutes * 60,
                "resume_session_id": None, "worktree": None, "test_secrets": {},
                # ${VAR} stays unexpanded: the runner fills it from the session's environment at session start
                "mcp": {name: {"type": m.type, "url": m.url, "headers": dict(m.headers)}
                        for name, m in self.config.mcp.items()}}

    def _briefing(self, scopes: list[str], text: str, kind: str) -> str:
        return build_briefing(self.memory, scopes, text, self.config.memory_budget.briefing.get(kind, 1000)).text

    def checkout(self, convo) -> dict | None:
        """The checkout a conversation works on, once it has a branch: the same branch on every turn. A conversation
        bound without one (track_task) gets no checkout, so an investigation never pays for a setup run."""
        pack = self.packs.get(convo["project"] or "")
        if not convo["branch"] or pack is None:
            return None
        p = pack.project
        return {"project": p.name, "default_branch": p.default_branch, "branch": convo["branch"],
                "setup": list(p.build_setup), "checks": list(p.build_checks)}

    def test_secrets(self, project: str) -> dict[str, str]:
        """The project's test-only keys (its pack's [secrets] worker list), for a session with its checkout."""
        pack = self.packs.get(project)
        names = pack.project.secrets_worker if pack is not None else []
        return {k: self.secrets[k] for k in names if k in self.secrets}

    def turn(self, convo, text: str, actor: str | None, *, unattended: bool = False) -> dict:
        project = convo["project"]
        scopes = conversation_scopes(convo["channel"], project, actor, self.packs)
        payload = self._base(text=text, actor=actor, unattended=unattended,
                             model=convo["model"] or self.config.models.default,
                             system_parts=[self.system_prompt(), self.persona_text,
                                           self._briefing(scopes, text, "session")])
        checkout = self.checkout(convo)
        payload.update(resume_session_id=convo["session_id"], conversation_id=convo["id"], project=project,
                       worktree=checkout, workdir=convo["workdir"])
        if checkout is not None:
            payload["test_secrets"] = self.test_secrets(project)
        return payload

    def chore(self, prompt: str, schema: dict) -> dict:
        payload = self._base(text=prompt, actor=None, unattended=True,
                             model=self.config.models.chores, system_parts=[MEMORY_PROMPT.read_text().strip()])
        payload.update(max_turns=10, timeout_s=300, structured=schema)
        return payload
