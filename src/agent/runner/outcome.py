"""Turning the SDK's final ResultMessage into a run outcome."""

from __future__ import annotations

from claude_agent_sdk import ResultMessage

FORK_STATUSES = (403, 503)
ACCOUNT_TROUBLE = ("rejected", "disabled")


def map_result(msg: ResultMessage) -> tuple[str, str | None]:
    """(outcome, error) for a finished session."""
    if msg.subtype == "success":
        return "done", None
    if msg.subtype == "error_max_turns":
        return "max_turns", None
    return "error", f"{msg.api_error_status} {msg.errors}"


def usage_tokens(usage: dict | None) -> tuple[int, int, int, int]:
    u = usage or {}
    return (int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
            int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0))
