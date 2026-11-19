"""The host side of Jensen's notes: letting improve-jensen read `/var/agent/feedback/` inside `agent-runner`,
answer a note, and record what changed for Jensen to read back.

Every call pipes `runner_feedback.py`'s own text into `agent-runner`'s `python3` on stdin
(`Host.runner_python`), the same way `core_python` pipes `core_collect.py` into `agent-core` -- `agent-runner`'s
`python3` is 3.12 and can import only the standard library, so the actual file reading and writing lives there,
never here. A note's text or improve-jensen's own reply crosses that `docker exec` boundary base64-encoded
(`--status-b64`, `--text-b64`): either one can hold a newline, a quote or a shell metacharacter that would
otherwise break the argument list `docker exec` hands to `python3 -`.

Nothing here scrubs anything: a note's text can carry a secret or a product name as easily as a transcript can,
and the caller (`measure.py`) already builds a scrubber from `Host.secret_values()` for everything else that
lands in the digest -- reusing that one scrubber on note text, rather than building a second one here.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

from agent.watch.host import Host
from agent.watch.state import FMT

_MODULE = "runner_feedback.py"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime(FMT)


def collect(host: Host, since: datetime, until: datetime) -> list[dict]:
    """Notes from `/var/agent/feedback/` inside `agent-runner`, modified in `(since, until]`, excluding
    `CHANGES.md`. Each is `{name, text, modified}`. A missing folder gives `[]`."""
    raw = host.runner_python(_MODULE, ["collect", "--since", _iso(since), "--until", _iso(until)])
    data = json.loads(raw)
    return [note for note in data if isinstance(note, dict)] if isinstance(data, list) else []


def reply(host: Host, name: str, status: str, now: datetime) -> None:
    """Append a `Status (<ISO now>): <status>` line to the note `name` inside `agent-runner`. Raises `ValueError`
    for a name containing `/` or `..`, or one not ending in `.md` -- checked here first, so a bad name never
    costs a `docker exec` round trip."""
    if "/" in name or ".." in name or not name.endswith(".md"):
        raise ValueError(f"bad note name: {name!r}")
    status_b64 = base64.b64encode(status.encode()).decode()
    host.runner_python(_MODULE, ["reply", "--name", name, "--status-b64", status_b64, "--now", _iso(now)])


def add_change(host: Host, text: str, now: datetime) -> None:
    """Prepend a changelog entry for `text` to `CHANGES.md` inside `agent-runner`, keeping the newest 30."""
    text_b64 = base64.b64encode(text.encode()).decode()
    host.runner_python(_MODULE, ["change", "--text-b64", text_b64, "--now", _iso(now)])
