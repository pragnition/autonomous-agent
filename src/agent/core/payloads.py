"""What a session starts with: the work item payload a runner turns into a Claude Code session.

A short system prompt from config (or the shipped default) and the persona, the model and budgets, the checkout to
prepare and the folder to work in. Sessions with a checkout also get the pack's test-only secrets; nothing else
secret ever goes into a payload.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_SYSTEM = Path(__file__).parent.parent / "defaults" / "system.md"


class Payloads:
    def __init__(self, conn: sqlite3.Connection, *, config, packs: dict, persona_text: str, config_dir: Path,
                 secrets: dict[str, str]):
        self.conn, self.config, self.packs = conn, config, packs
        self.persona_text, self.config_dir, self.secrets = persona_text, Path(config_dir), secrets

    def system_prompt(self) -> str:
        custom = self.config_dir / "agent" / "system.md"
        text = (custom if custom.is_file() else DEFAULT_SYSTEM).read_text()
        return text.replace("{name}", self.config.name).strip()

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
        payload = {"text": text, "actor": actor, "unattended": unattended,
                   "model": convo["model"] or self.config.models.default,
                   "system_prompt": "\n\n".join(p for p in (self.system_prompt(), self.persona_text) if p),
                   "max_turns": self.config.limits.max_turns, "timeout_s": self.config.limits.task_minutes * 60,
                   # ${VAR} stays unexpanded: the runner fills it from the session's environment at session start
                   "mcp": {name: {"type": m.type, "url": m.url, "headers": dict(m.headers)}
                           for name, m in self.config.mcp.items()},
                   "conversation_id": convo["id"]}
        return self.refresh_turn(convo, payload)

    def refresh_turn(self, convo, payload: dict) -> dict:
        """The turn with its conversation's state as it is now: the session to resume, the project, the checkout,
        the working folder and the test keys. Done again when a runner claims the turn, because a turn queued while
        the one before it ran must resume the session that turn ended with and get the checkout its work_on made.
        The text and system prompt stay as they were when the turn was queued."""
        project, checkout = convo["project"], self.checkout(convo)
        return {**payload, "resume_session_id": convo["session_id"], "project": project, "worktree": checkout,
                "workdir": convo["workdir"], "test_secrets": self.test_secrets(project) if checkout is not None else {}}
