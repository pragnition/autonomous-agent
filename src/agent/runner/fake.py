"""A scripted Runner for tests: results are handed out per role, in order."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace

from agent.runner.base import RunResult, RunSpec

Entry = RunResult | Callable[[RunSpec], RunResult]


class FakeRunner:
    def __init__(self, script: dict[str, list[Entry]]):
        self.script = {role: list(entries) for role, entries in script.items()}
        self.specs: list[RunSpec] = []
        self.interrupted: set[str] = set()

    async def run(self, spec: RunSpec) -> RunResult:
        self.specs.append(spec)
        entries = self.script.get(spec.role)
        assert entries, f"FakeRunner: no scripted result left for role {spec.role!r}"
        entry = entries.pop(0)
        result = entry(spec) if callable(entry) else entry
        if inspect.isawaitable(result):
            result = await result
        return replace(result, run_id=spec.run_id)

    async def interrupt(self, run_id: str) -> None:
        self.interrupted.add(run_id)
