"""Turning one turn's runner transcript (stream-json, one record per line) into `TurnFacts`.

The runner writes one JSON object per line: `AssistantMessage` (with tool-use content blocks), `UserMessage` (with
tool-result blocks), `TaskStartedMessage` and `TaskNotificationMessage` for background subagents, `ResultMessage` for
the turn's end, and `SystemMessage` for events such as a PR being opened. The main thread is where
`parent_tool_use_id` is null; everything else is a subagent's own thread. A line that isn't valid JSON, or isn't the
object we expect, is skipped: a transcript still being written, or truncated mid-line, must never crash the
measurement.

`turn_facts` can take a `clean` function (the watch passes a secret scrubber), applied to each decoded tool input,
tool output and error text before anything is cut to length: a secret holding a `"` or `\\` is escaped in the raw
JSON line, where a scrub of the raw line can't match it, and once a line is cut a prefix of it is too short to
match at all.

`added` names what a turn added that outlives it, for the assessment's "what worked that Jensen should keep?": a
Bash command that installs something lasting (a Claude plugin, marketplace or MCP server, a skills pack, a global
npm or bun package, a user pip package, a uv or pipx tool, Playwright's browsers, an apt package, a script piped
from curl or wget into a shell), and a file written under a `skills/` folder, in Jensen's memory, named `CLAUDE.md`
or `AGENTS.md`, or a Claude settings file. A project-local `npm install` or `pip install -r` adds nothing lasting
and isn't named. Each is named once, in the order first seen, from the main thread and subagents alike.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
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

# `gh pr create` prints the new PR's URL alone on its last line. The CLI's own `code_change_published` event doesn't
# fire for a PR opened through Bash, so this is how most PRs name the turn that made them.
_GH_PR_CREATE_RE = re.compile(r"(?:^|[;&|(\n]\s*)gh\s+pr\s+create\b(?![^;&|\n]*\s(?:--help|-h)\b)")
PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
_PR_URL_LINE_RE = re.compile(r"^(https://github\.com/[^/\s]+/[^/\s]+/pull/\d+)\s*$", re.MULTILINE)

# A Bash command installs something lasting when one of its parts (split on `&&`, `||`, `;`, `|` and newlines)
# matches one of these. `npm`/`bun` count only with `-g`/`--global` and `pip` only with `--user` in the same part:
# without them they install into the project, which adds nothing to Jensen.
_INSTALL_RES = tuple(re.compile(pattern) for pattern in (
    r"\bclaude\s+plugins?\s+(?:install|marketplace\s+add)\b",
    r"\bclaude\s+mcp\s+add\b",
    r"\bnpx\s+(?:(?:-y|--yes)\s+)?skills(?:@\S+)?\s+add\b",
    r"\buv\s+tool\s+install\b",
    r"\bpipx\s+install\b",
    r"\bplaywright(?:-cli)?\s+install(?:-browser)?\b",
    r"\bapt(?:-get)?\s+(?:-\S+\s+)*install\b",
))
_GLOBAL_INSTALL_RE = re.compile(r"\b(?:npm\s+(?:i|install)|bun\s+(?:add|install))\b")
_GLOBAL_FLAG_RE = re.compile(r"(?:^|\s)(?:-g|--global)(?=\s|$)")
_PIP_INSTALL_RE = re.compile(r"\bpip3?\s+install\b")
_USER_FLAG_RE = re.compile(r"(?:^|\s)--user(?=\s|$)")
_COMMAND_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
# A script fetched and piped straight into a shell, which needs the pipe the split above would cut.
_PIPE_TO_SHELL_RE = re.compile(r"\b(?:curl|wget)\b[^|\n]*\|\s*(?:sudo\s+)?(?:ba)?sh\b")
_INSTALL_CHARS = 200

# The tools that write a file, and the input key naming it.
_FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
_MEMORY_DIR = "/var/agent/memory/"
_CLAUDE_HOME = "/var/agent/claude/"
_INSTRUCTION_FILES = {"CLAUDE.md", "AGENTS.md"}
_SETTINGS_FILES = {"settings.json", "settings.local.json"}


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
    # PR URLs Jensen announced in a progress post (`mcp__agent__post_update`): how a PR names its turn when the
    # create's own output was cut (`gh pr create ... | tail -1`).
    prs_announced: list[str] = field(default_factory=list)
    tool_lines: list[str] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


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


def _is_install(command: str) -> bool:
    if _PIPE_TO_SHELL_RE.search(command):
        return True
    for part in _COMMAND_SPLIT_RE.split(command):
        if any(pattern.search(part) for pattern in _INSTALL_RES):
            return True
        if _GLOBAL_INSTALL_RE.search(part) and _GLOBAL_FLAG_RE.search(part):
            return True
        if _PIP_INSTALL_RE.search(part) and _USER_FLAG_RE.search(part):
            return True
    return False


def _added_file(path: str) -> str | None:
    """What a file written at `path` adds (`skill`, `memory`, `instructions` or `settings`), or `None`."""
    parts = PurePosixPath(path).parts
    name = parts[-1] if parts else ""
    if path.startswith(_MEMORY_DIR):
        return "memory"
    if "skills" in parts[:-1]:
        return "skill"
    if name in _INSTRUCTION_FILES:
        return "instructions"
    if name in _SETTINGS_FILES and (".claude" in parts[:-1] or path.startswith(_CLAUDE_HOME)):
        return "settings"
    return None


def _added_line(name: str, tool_input: Any, clean: Callable[[str], str]) -> str | None:
    """The `added` line for one tool call, or `None` when it adds nothing lasting. Whitespace is collapsed, so a
    multi-line command is still one line."""
    tool_input = _as_dict(tool_input)
    if name == "Bash":
        command = str(tool_input.get("command", ""))
        if _is_install(command):
            return f"install: {' '.join(clean(command).split())[:_INSTALL_CHARS]}"
    elif name in _FILE_TOOLS:
        path = tool_input.get(_FILE_TOOLS[name])
        kind = _added_file(path) if isinstance(path, str) else None
        if kind:
            return f"{kind}: {' '.join(clean(path).split())}"
    return None


def turn_facts(lines: Iterable[str], *, tool_chars: int = 300, error_chars: int = 500,
               clean: Callable[[str], str] | None = None) -> TurnFacts:
    """Read one turn's transcript lines into the facts the hourly watch counts.

    A bad line (not JSON, truncated, or not an object) is skipped rather than raised, since the transcript may still
    be being written when this runs. `clean`, when given, runs on each tool input, tool output and error text once
    it's decoded and before it's cut to `tool_chars`/`error_chars` (see the module docstring).
    """
    if clean is None:
        def clean(text: str) -> str:
            return text
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

                added = _added_line(name, tool_input, clean)
                if added and added not in facts.added:
                    facts.added.append(added)

                if name == "Bash":
                    clone = _extract_clone(str(_as_dict(tool_input).get("command", "")))
                    if clone:
                        facts.clones.append(clone)
                elif name == "Skill":
                    skill = _as_dict(tool_input).get("skill")
                    if skill:
                        facts.skills.append(skill)
                elif name.endswith("post_update"):
                    for url in PR_URL_RE.findall(str(_as_dict(tool_input).get("text", ""))):
                        if url not in facts.prs_announced:
                            facts.prs_announced.append(url)

        elif kind == "UserMessage":
            content = record.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or "tool_use_id" not in block:
                    continue
                name, tool_input = pending_tools.pop(block["tool_use_id"], ("?", None))
                output_text = clean(_result_text(block.get("content")))
                input_text = clean(_input_text(name, tool_input))
                facts.tool_lines.append(f"{name}: {input_text[:tool_chars]} -> {output_text[:tool_chars]}")

                if name == "Bash" and not block.get("is_error") and \
                        _GH_PR_CREATE_RE.search(str(_as_dict(tool_input).get("command", ""))):
                    for url in _PR_URL_LINE_RE.findall(_result_text(block.get("content"))):
                        if url not in facts.prs_created:
                            facts.prs_created.append(url)

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
            if data.get("action") == "created" and data.get("url") and data["url"] not in facts.prs_created:
                facts.prs_created.append(data["url"])

    facts.still_running = len(started_tasks)
    return facts
