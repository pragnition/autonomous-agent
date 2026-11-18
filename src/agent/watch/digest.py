"""Rendering the hourly digest: turning measured facts into the markdown text improve-jensen reads.

`render` only arranges already-scrubbed strings into markdown — nothing here touches secrets or `Host`; scrubbing
happens in `measure.py` before a `ConvoView` is built. The one thing `render` shrinks on its own is the tool/error
lines pulled from each turn's `TurnFacts`: the cap on how many characters of each line to show steps down
300 -> 120 -> 40 -> 0 until the whole text fits `budget`, while `asked` and `replied` are never touched, since a
person's message and Jensen's reply are the point of the digest, even on a day with nothing else to show for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# The tool-line cap steps down through these, stopping at the first that fits `budget`; 0 means "omit the
# transcript-derived lines for this turn entirely."
_CAPS = (300, 120, 40, 0)

_FMT = "%Y-%m-%dT%H:%M:%SZ"

# `signals` keys, in the order a turn shows them.
_SIGNAL_ORDER = ("context_first", "context_peak", "big_start_small_ask", "no_delegation", "inline_code_chars",
                 "lost_background", "still_running", "turn_failed", "reclone", "tool_errors", "missing_tools")


@dataclass
class ConvoView:
    """One conversation's turns, as the digest shows them. Each turn is a plain dict with `id`, `source`,
    `status`, `outcome`, `minutes`, `asked`, `replied`, `facts` (a `TurnFacts`) and `signals` (a dict)."""

    id: str
    project: str | None
    turns: list[dict] = field(default_factory=list)


def _iso(value: datetime) -> str:
    return value.strftime(_FMT)


def _capped(lines: list[str], cap: int) -> list[str]:
    if cap <= 0:
        return []
    return [line[:cap] for line in lines]


def _signal_text(signals: dict) -> str:
    bits = []
    for key in _SIGNAL_ORDER:
        if key not in signals:
            continue
        value = signals[key]
        bits.append(f"{key}={len(value)}" if isinstance(value, list) else f"{key}={value}")
    return ", ".join(bits)


def _render_turn(turn: dict, cap: int) -> list[str]:
    lines = [
        f"- turn {turn.get('id')} [{turn.get('source')}] status={turn.get('status')} "
        f"outcome={turn.get('outcome')} minutes={turn.get('minutes')}",
        f"  asked: {turn.get('asked', '')}",
        f"  replied: {turn.get('replied', '')}",
        f"  signals: {_signal_text(turn.get('signals') or {})}",
    ]
    facts = turn.get("facts")
    tool_lines = _capped(getattr(facts, "tool_lines", []) or [], cap)
    error_lines = _capped(getattr(facts, "error_lines", []) or [], cap)
    lines.extend(f"  tool: {line}" for line in tool_lines)
    lines.extend(f"  error: {line}" for line in error_lines)
    return lines


def _render_at(window: tuple[datetime, datetime], convos: list[ConvoView], totals: dict, prs: list[dict],
               deployed: list[dict], cap: int) -> str:
    start, end = window
    out = ["# Jensen hourly digest", "", f"Window: {_iso(start)} to {_iso(end)}", "", "## Totals"]
    if totals:
        out.extend(f"- {value}" for value in totals.values())
    else:
        out.append("- none")

    out += ["", "## New PRs"]
    if prs:
        for pr in prs:
            out.append(f"- {pr.get('url')} ({pr.get('repo')}): {pr.get('lines')} lines, "
                        f"ci={pr.get('ci')}, open_to_merge_s={pr.get('open_to_merge_s')}")
    else:
        out.append("- none")

    out += ["", "## Being watched"]
    if deployed:
        for issue in deployed:
            out.append(f"- #{issue.get('number')} {issue.get('title')}")
    else:
        out.append("- none")

    out += ["", "## Conversations"]
    if convos:
        for convo in convos:
            out.append(f"### {convo.id} ({convo.project or 'no project'})")
            for turn in convo.turns:
                out.extend(_render_turn(turn, cap))
            out.append("")
    else:
        out.append("- none")

    return "\n".join(out)


def render(window: tuple[datetime, datetime], convos: list[ConvoView], totals: dict, prs: list[dict],
           deployed: list[dict], budget: int = 400_000) -> str:
    """The digest's markdown text. Tries the tool-line cap at each step in `_CAPS`, returning the first rendering
    that fits `budget`; if none fit (the people's own messages alone are already over budget), returns the last
    (narrowest) one anyway, since `asked`/`replied` are never cut."""
    text = ""
    for cap in _CAPS:
        text = _render_at(window, convos, totals, prs, deployed, cap)
        if len(text) <= budget:
            return text
    return text
