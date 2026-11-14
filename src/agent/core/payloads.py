"""What a session starts with: the work item payload a runner turns into a Claude Code session.

A short system prompt from config (or the shipped default) with the projects listed, the persona, the model and
budgets. Every session also gets the packs' test-only secrets; nothing else secret ever goes into a payload.
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
        return text.replace("{name}", self.config.name).replace("{projects}", self.projects()).strip()

    def projects(self) -> str:
        """One line per pack, for the system prompt: where its code is, so the agent can clone it itself."""
        lines = []
        for name, pack in sorted(self.packs.items()):
            p = pack.project
            where = f"default branch {p.default_branch}" + (f", Slack {p.slack_channel}" if p.slack_channel else "")
            lines.append(f"- {name}: https://github.com/{p.repo} ({where})")
        return "\n".join(lines) or "- none configured"

    def test_secrets(self) -> dict[str, str]:
        """Every pack's test-only keys (its [secrets] worker list), for every session."""
        names = [k for pack in self.packs.values() for k in pack.project.secrets_worker]
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
        """The turn with its conversation's state as it is now: the session to resume, the project and the test keys.
        Done again when a runner claims the turn, because a turn queued while the one before it ran must resume the
        session that turn ended with. The text and system prompt stay as they were when the turn was queued."""
        return {**payload, "resume_session_id": convo["session_id"], "project": convo["project"],
                "test_secrets": self.test_secrets()}
