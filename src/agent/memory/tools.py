"""The memory tools every session gets: recall, read_memory and remember, within a per-run token allowance."""

from __future__ import annotations

import json
from collections.abc import Iterable

from agent.memory.scrub import scrub
from agent.memory.store import RECALLABLE, MemoryStore
from agent.tokens import estimate_tokens

BUDGET_SPENT = "memory budget for this run is spent"
MAX_RECALL = 5


class MemoryToolbox:
    def __init__(self, store: MemoryStore, scopes: list[str], lookup_budget: int, *, run_id: str | None,
                 actor: str | None, secret_values: Iterable[str] = ()):
        self.store, self.scopes, self.budget = store, list(scopes), lookup_budget
        self.run_id, self.actor = run_id, actor
        self.secret_values = list(secret_values)
        self.tokens_used = 0

    def _spend(self, text: str) -> bool:
        cost = estimate_tokens(text)
        if self.tokens_used + cost > self.budget:
            return False
        self.tokens_used += cost
        return True

    def recall(self, query: str, limit: int = MAX_RECALL) -> list[dict] | str:
        notes = self.store.search(query, self.scopes, min(max(limit, 1), MAX_RECALL), active_only=False)
        hits = [{"id": n.id, "summary": n.summary, "scope": n.scope, "updated_at": n.updated_at} for n in notes]
        return hits if self._spend(json.dumps(hits)) else BUDGET_SPENT

    def read_memory(self, id: str) -> str:
        note = self.store.get(id)
        if note is None or note.scope not in self.scopes or note.status not in RECALLABLE:
            return "not found"
        label = " (from external text)" if note.trust == "external" else ""
        text = f"[{note.id}] {note.summary}{label}\n{note.body}"
        if not self._spend(text):
            return BUDGET_SPENT
        self.store.touch([note.id])
        return text

    def remember(self, text: str, about: str | None = None) -> str:
        self.store.add_inbox(self.run_id, self.actor, about, scrub(text, self.secret_values))
        return "noted"
