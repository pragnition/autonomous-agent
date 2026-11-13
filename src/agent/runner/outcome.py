"""Turning the SDK's final ResultMessage, plus what the agent reported via submit_result, into a run outcome."""

from __future__ import annotations

from claude_agent_sdk import ResultMessage

REPORTED = ("done", "needs_input", "blocked")
FORK_STATUSES = (403, 503)
ACCOUNT_TROUBLE = ("rejected", "disabled")


def map_result(msg: ResultMessage, sink: dict) -> tuple[str, str | None]:
    """(outcome, error) for a finished session."""
    if msg.subtype == "success":
        status = sink.get("status")
        return (status if status in REPORTED else "done"), None
    if msg.subtype == "error_max_turns":
        return "max_turns", None
    if msg.subtype == "error_max_budget_usd":
        return "max_budget", None
    return "error", f"{msg.api_error_status} {msg.errors}"


def usage_tokens(usage: dict | None) -> tuple[int, int, int, int]:
    u = usage or {}
    return (int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
            int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0))
