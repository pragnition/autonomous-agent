"""Turning one turn's runner transcript (stream-json, one record per line) into `TurnFacts`.

The runner writes one JSON object per line: `AssistantMessage` (with tool-use content blocks), `UserMessage` (with
tool-result blocks), `TaskStartedMessage` and `TaskNotificationMessage` for background subagents, `ResultMessage` for
the turn's end, and `SystemMessage` for events such as a PR being opened. The main thread is where
`parent_tool_use_id` is null; everything else is a subagent's own thread. A line that isn't valid JSON, or isn't the
object we expect, is skipped: a transcript still being written, or truncated mid-line, must never crash the
measurement.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Tools that hand work to a subagent.
_SUBAGENT_TOOLS = {"Task", "Agent"}

# A lost background agent's notification names the session boundary that killed it.
_LOST_SESSION_PHRASE = "didn't finish before the previous session ended"

_PATH_RE = re.compile(r"/\S+")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX_RUN_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b")
_DIGIT_RUN_RE = re.compile(r"\d+")
_WHITESPACE_RE = re.compile(r"\s+")

_COMMAND_NOT_FOUND_RE = re.compile(r"(\S+):\s*command not found", re.IGNORECASE)
_MODULE_NOT_FOUND_RE = re.compile(r"no module named ['\"]([^'\"]+)['\"]", re.IGNORECASE)

# A heredoc marker (`<<EOF`, `<<-'EOF'`, ...) together with a redirect or `tee` means the command is writing a file.
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?\w+['\"]?")

_GH_CLONE_RE = re.compile(r"gh repo clone\s+(\S+)")
_GIT_CLONE_RE = re.compile(r"git clone\s+(\S+)")


def normalise_error(text: str) -> str:
    """Fold an error message down to a stable shape so that repeats of the same error count as one."""
    folded = (text or "").lower()
    folded = _PATH_RE.sub("<path>", folded)
    folded = _UUID_RE.sub("<id>", folded)
    folded = _HEX_RUN_RE.sub("<id>", folded)
    folded = _DIGIT_RUN_RE.sub("<n>", folded)
    folded = _WHITESPACE_RE.sub(" ", folded).strip()
    return folded[:160]


@dataclass
class TurnFacts:
    """What one turn's transcript shows, for the hourly watch to count."""

    context_first: int = 0
    context_peak: int = 0
    tool_calls: int = 0
    subagent_calls: int = 0
    inline_code_chars: int = 0
    tool_errors: list[str] = field(default_factory=list)
    missing_tools: list[str] = field(default_factory=list)
    lost_background: int = 0
    still_running: int = 0
    clones: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    prs_created: list[str] = field(default_factory=list)
    tool_lines: list[str] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _result_text(content: Any) -> str:
    """The text of a tool result, which is either a plain string or a list of blocks with `text`."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(block["text"]) for block in content if isinstance(block, dict) and "text" in block)
    return "" if content is None else str(content)


def _input_text(name: str, tool_input: Any) -> str:
    """A short textual stand-in for a tool call's input, for the digest's tool line."""
    tool_input = _as_dict(tool_input)
    if name == "Bash":
        return str(tool_input.get("command", ""))
    if "content" in tool_input:
        return str(tool_input["content"])
    if "file_path" in tool_input:
        return str(tool_input["file_path"])
    return json.dumps(tool_input, default=str, sort_keys=True)


def _is_heredoc_write(command: str) -> bool:
    match = _HEREDOC_RE.search(command)
    if not match:
        return False
    before = command[:match.start()]
    return ">" in before or "tee" in before


def _repo_from_clone_url(arg: str) -> str:
    repo = arg.rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    parts = [p for p in repo.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else repo


def _extract_clone(command: str) -> str | None:
    gh_match = _GH_CLONE_RE.search(command)
    if gh_match:
        return gh_match.group(1)
    git_match = _GIT_CLONE_RE.search(command)
    if git_match:
        return _repo_from_clone_url(git_match.group(1))
    return None


def turn_facts(lines: Iterable[str], *, tool_chars: int = 300, error_chars: int = 500) -> TurnFacts:
    """Read one turn's transcript lines into the facts the hourly watch counts.

    A bad line (not JSON, truncated, or not an object) is skipped rather than raised, since the transcript may still
    be being written when this runs.
    """
    facts = TurnFacts()
    pending_tools: dict[str, tuple[str, Any]] = {}
    started_tasks: dict[str, str] = {}
    context_seen = False

    for raw in lines:
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue

        kind = record.get("type")
        is_main = record.get("parent_tool_use_id") is None

        if kind == "AssistantMessage":
            if is_main:
                usage = _as_dict(record.get("usage"))
                context = (usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) +
                           usage.get("cache_creation_input_tokens", 0))
                if not context_seen:
                    facts.context_first = context
                    context_seen = True
                facts.context_peak = max(facts.context_peak, context)

            for block in record.get("content") or []:
                if not isinstance(block, dict) or "id" not in block or "name" not in block:
                    continue
                name = block["name"]
                tool_input = block.get("input")
                pending_tools[block["id"]] = (name, tool_input)

                if is_main:
                    facts.tool_calls += 1
                    if name in _SUBAGENT_TOOLS:
                        facts.subagent_calls += 1
                    if name == "Write":
                        facts.inline_code_chars += len(str(_as_dict(tool_input).get("content", "")))
                    elif name == "Bash":
                        command = str(_as_dict(tool_input).get("command", ""))
                        if _is_heredoc_write(command):
                            facts.inline_code_chars += len(command)

                if name == "Bash":
                    clone = _extract_clone(str(_as_dict(tool_input).get("command", "")))
                    if clone:
                        facts.clones.append(clone)
                elif name == "Skill":
                    skill = _as_dict(tool_input).get("skill")
                    if skill:
                        facts.skills.append(skill)

        elif kind == "UserMessage":
            content = record.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or "tool_use_id" not in block:
                    continue
                name, tool_input = pending_tools.pop(block["tool_use_id"], ("?", None))
                output_text = _result_text(block.get("content"))
                input_text = _input_text(name, tool_input)
                facts.tool_lines.append(f"{name}: {input_text[:tool_chars]} -> {output_text[:tool_chars]}")

                if block.get("is_error"):
                    facts.error_lines.append(output_text[:error_chars])
                    facts.tool_errors.append(normalise_error(output_text))

                    not_found = _COMMAND_NOT_FOUND_RE.search(output_text)
                    if not_found:
                        facts.missing_tools.append(not_found.group(1))
                    no_module = _MODULE_NOT_FOUND_RE.search(output_text)
                    if no_module:
                        facts.missing_tools.append(no_module.group(1))

        elif kind == "TaskStartedMessage":
            data = _as_dict(record.get("data"))
            task_id = data.get("task_id")
            if task_id:
                started_tasks[task_id] = data.get("task_type", "")

        elif kind == "TaskNotificationMessage":
            data = _as_dict(record.get("data"))
            task_id = data.get("task_id")
            if task_id in started_tasks:
                status = data.get("status") or ""
                summary = data.get("summary") or ""
                if _LOST_SESSION_PHRASE in summary or (status == "failed" and "429" in summary):
                    facts.lost_background += 1
                del started_tasks[task_id]

        elif kind == "SystemMessage" and record.get("subtype") == "code_change_published":
            data = _as_dict(record.get("data"))
            if data.get("action") == "created" and data.get("url"):
                facts.prs_created.append(data["url"])

    facts.still_running = len(started_tasks)
    return facts
