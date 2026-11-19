"""Rendering the hourly digest: turning measured facts into the markdown text improve-jensen reads.

`render` only arranges already-scrubbed strings into markdown — nothing here touches secrets or `Host`; scrubbing
happens in `measure.py` before a `ConvoView` is built, and again over the whole rendered text before it's
written. The one thing `render` shrinks on its own is the tool/error lines pulled from each turn's `TurnFacts`:
the first attempt leaves them exactly as `turn_facts` built them (up to 300 characters of a tool call's input
*and* up to 300 of its output, up to 500 for an error); later attempts cut each line down further -- to 120,
then 40, then 0 (omitted entirely) -- until the whole text fits `budget`. `asked` and `replied` are never
touched, since a person's message and Jensen's reply are the point of the digest, even on a day with nothing
else to show for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# The tool-line cap steps down through these, stopping at the first that fits `budget`. `None` means "exactly as
# `turn_facts` built the line, no further cut" (it must come first: cutting an already-300/300-built tool line
# down to a flat 300 characters total would lose the output half of a call whose input alone was already near
# that length); 0 means "omit the transcript-derived lines for this turn entirely."
_CAPS: tuple[int | None, ...] = (None, 120, 40, 0)

_FMT = "%Y-%m-%dT%H:%M:%SZ"

# `signals` keys, in the order a turn shows them -- also what `measure.py` uses to pull exactly these fields out
# of a turn's metrics record when building that turn's `signals` dict, so the two don't drift apart.
SIGNAL_KEYS = ("context_first", "context_peak", "big_start_small_ask", "no_delegation", "inline_code_chars",
               "lost_background", "still_running", "turn_failed", "reclone", "tool_errors", "missing_tools")


@dataclass
class ConvoView:
    """One conversation's turns, as the digest shows them. Each turn is a plain dict with `id`, `source`,
    `status`, `outcome`, `minutes`, `asked`, `replied`, `facts` (a `TurnFacts`) and `signals` (a dict), and
    optionally `own` (true for improve-jensen's own traffic, which the totals leave out)."""

    id: str
    project: str | None
    turns: list[dict] = field(default_factory=list)


def _iso(value: datetime) -> str:
    return value.strftime(_FMT)


def _capped(lines: list[str], cap: int | None) -> list[str]:
    if cap is None:
        return list(lines)
    if cap <= 0:
        return []
    return [line[:cap] for line in lines]


def _signal_text(signals: dict) -> str:
    bits = []
    for key in SIGNAL_KEYS:
        if key not in signals:
            continue
        value = signals[key]
        bits.append(f"{key}={len(value)}" if isinstance(value, list) else f"{key}={value}")
    return ", ".join(bits)


def _render_turn(turn: dict, cap: int | None) -> list[str]:
    own = " (improve-jensen's own: not in the totals)" if turn.get("own") else ""
    lines = [
        f"- turn {turn.get('id')} [{turn.get('source')}] status={turn.get('status')} "
        f"outcome={turn.get('outcome')} minutes={turn.get('minutes')}{own}",
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
               notes: list[dict], cap: int | None, alerts: list[str]) -> str:
    start, end = window
    out = ["# Jensen hourly digest", "", f"Window: {_iso(start)} to {_iso(end)}", "", "## Needs attention"]
    if alerts:
        out.extend(f"- {alert}" for alert in alerts)
    else:
        out.append("- none")
    out += ["", "## Totals"]
    if totals:
        out.extend(f"- {value}" for value in totals.values())
    else:
        out.append("- none")

    out += ["", "## New PRs"]
    if prs:
        for pr in prs:
            # `conversation` is the one that made the PR (or the build's project conversation); the watch copies it
            # into plan.json so a review's findings go back into that session.
            out.append(f"- {pr.get('url')} ({pr.get('repo')}): {pr.get('lines')} lines, "
                        f"ci={pr.get('ci')}, open_to_merge_s={pr.get('open_to_merge_s')}, "
                        f"conversation={pr.get('conversation') or 'none'}")
    else:
        out.append("- none")

    out += ["", "## Jensen reports"]
    if notes:
        for note in notes:
            out.append(f"### {note.get('name')} (modified {note.get('modified')})")
            # Quoted, never raw: a note is Jensen's own free text, and an unquoted "## " or "### " line inside
            # it would otherwise render as a real digest heading, impersonating a section that was never there.
            out.extend(f"> {line}" for line in (note.get("text") or "").splitlines())
            out.append("")
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
           budget: int = 400_000, *, notes: list[dict] | None = None, alerts: list[str] | None = None) -> str:
    """The digest's markdown text. `alerts` (core unreachable, a failed deploy, a behaviour-changing deploy that
    landed) go first, under "Needs attention". `notes` (already-scrubbed Jensen reports, each `{name, text,
    modified}`) get their own "## Jensen reports" section, one `###` subsection per note, each line of its text
    quoted with `> ` (so a note's own text can't impersonate a digest heading) and shown in full -- never cut by
    the tool-line budget below, the same as `asked`/`replied`. Tries the tool-line cap at each step in `_CAPS`, returning the first rendering that fits
    `budget`; if none fit (the people's own messages alone are already over budget), returns the last
    (narrowest) one anyway."""
    text = ""
    for cap in _CAPS:
        text = _render_at(window, convos, totals, prs, list(notes or []), cap, list(alerts or []))
        if len(text) <= budget:
            return text
    return text
