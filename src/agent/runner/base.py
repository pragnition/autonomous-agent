"""What a run is asked to do (RunSpec), what it reports (RunResult), and the Runner interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Any, Literal, Protocol

RunOutcome = Literal["done", "error", "max_turns", "interrupted", "timeout"]


@dataclass
class RunSpec:
    run_id: str
    prompt: str
    cwd: Path
    model: str
    system_prompt: str
    max_turns: int
    timeout_s: int
    effort: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    plugins: list[Path] = field(default_factory=list)
    sdk_tools: list[Any] = field(default_factory=list)
    tools: list[str] | None = None
    resume_session_id: str | None = None
    os_user: str | None = None
    max_budget_usd: float | None = None
    # "bypass": full permissions inside a sandbox, where every limit is enforced outside the agent (spec v2 G2)
    permission_mode: Literal["default", "bypass"] = "default"
    on_message: Callable[[dict], Awaitable[None]] | None = None


@dataclass
class RunResult:
    run_id: str
    session_id: str | None
    outcome: RunOutcome
    summary: str = ""
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd_est: float = 0.0
    account_code: str | None = None
    forks: int = 0
    error: str | None = None


class Runner(Protocol):
    async def run(self, spec: RunSpec) -> RunResult: ...

    async def interrupt(self, run_id: str) -> None: ...
