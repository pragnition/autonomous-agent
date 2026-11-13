"""What a session starts with (spec v2 §6): the work item payload a runner turns into a Claude Code session.

A short system prompt from config (or the shipped default), the persona for Slack turns only, a capped memory
briefing, the model and budgets, and the worktree to prepare. Coding sessions also get the pack's test-only
secrets; nothing else secret ever goes into a payload.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agent.core.agent_tools import conversation_scopes, profile_of
from agent.gitutil import branch_name
from agent.memory.briefing import build_briefing
from agent.memory.store import MemoryStore

DEFAULT_SYSTEM = Path(__file__).parent.parent / "defaults" / "system.md"
MEMORY_PROMPT = Path(__file__).parent.parent / "prompts" / "memory.md"
PRODUCTION_RULES = (
    "This session answers a question about production from another session, which can't see production itself. "
    "Investigate with your production tools (for Azure, the az tool). Prefer narrow, aggregated queries over a "
    "time window around the report; use save_to for large output and search the saved file. Answer with what "
    "fails, since when, how often, the error messages and stack traces, the suspected cause with file references, "
    "and steps to reproduce. Leave out customer identifiers (emails, names, IDs); they are removed anyway.")


class Payloads:
    def __init__(self, conn: sqlite3.Connection, *, config, packs: dict, memory: MemoryStore, persona_text: str,
                 config_dir: Path, secrets: dict[str, str]):
        self.conn, self.config, self.packs, self.memory = conn, config, packs, memory
        self.persona_text, self.config_dir, self.secrets = persona_text, Path(config_dir), secrets

    def system_prompt(self) -> str:
        custom = self.config_dir / "agent" / "system.md"
        text = (custom if custom.is_file() else DEFAULT_SYSTEM).read_text()
        return text.replace("{name}", self.config.name).strip()

    def _base(self, *, text: str, actor: str | None, unattended: bool, profile: str, model: str,
              system_parts: list[str]) -> dict:
        return {"text": text, "actor": actor, "unattended": unattended, "profile": profile, "model": model,
                "system_prompt": "\n\n".join(p for p in system_parts if p),
                "max_turns": self.config.limits.max_turns, "timeout_s": self.config.limits.task_minutes * 60,
                "resume_session_id": None, "worktree": None, "test_secrets": {}}

    def _briefing(self, scopes: list[str], text: str, kind: str) -> str:
        return build_briefing(self.memory, scopes, text, self.config.memory_budget.briefing.get(kind, 1000)).text

    def turn(self, convo, text: str, actor: str | None, *, unattended: bool = False) -> dict:
        profile, project = profile_of(convo["sandbox"]), convo["project"]
        scopes = conversation_scopes(convo["channel"], project, actor, self.packs)
        payload = self._base(text=text, actor=actor, unattended=unattended, profile=profile,
                             model=convo["model"] or self.config.models.default,
                             system_parts=[self.system_prompt(), self.persona_text,
                                           self._briefing(scopes, text, "session")])
        payload.update(resume_session_id=convo["session_id"], conversation_id=convo["id"], project=project)
        pack = self.packs.get(project or "")
        if profile == "code" and pack is not None:
            p = pack.project
            title = text.strip().splitlines()[0] if text.strip() else "work"
            payload["worktree"] = {"project": p.name, "default_branch": p.default_branch,
                                   "branch_hint": branch_name(self.config.name, convo["task_id"] or convo["id"], title),
                                   "setup": list(p.build_setup), "checks": list(p.build_checks)}
            payload["test_secrets"] = {k: self.secrets[k] for k in p.secrets_worker if k in self.secrets}
        return payload

    def production(self, project: str, request: str, actor: str | None, unattended: bool,
                   caller_conversation_id: str | None = None) -> dict:
        scopes = conversation_scopes(None, project, actor, self.packs)
        payload = self._base(text=request, actor=actor, unattended=unattended, profile="debug",
                             model=self.config.models.default,
                             system_parts=[self.system_prompt(), PRODUCTION_RULES,
                                           self._briefing(scopes, request, "production")])
        pack = self.packs[project].project
        payload.update(project=project, caller_conversation_id=caller_conversation_id,
                       worktree={"project": project, "default_branch": pack.default_branch, "read_only": True})
        return payload

    def chore(self, prompt: str, schema: dict) -> dict:
        payload = self._base(text=prompt, actor=None, unattended=True, profile="general",
                             model=self.config.models.chores, system_parts=[MEMORY_PROMPT.read_text().strip()])
        payload.update(max_turns=10, timeout_s=300, structured=schema)
        return payload
