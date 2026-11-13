"""Which tool calls a role may make, decided in code (the SDK's can_use_tool callback).

Paths are resolved (symlinks and `..` included) and must sit inside an allowed root. Bash denials are defence in
depth: agents hold no credentials, but they still never get to push, call gh, or read secrets or process env.
"""

from __future__ import annotations

import re
from pathlib import Path

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from agent.runner.base import RunSpec

READ_ONLY_ROLES = {"chat", "scout", "memory"}
READ_TOOLS = {"Read": "file_path", "Grep": "path", "Glob": "path"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "NotebookEdit": "notebook_path"}
BASH_DENY = [re.compile(p) for p in (
    r"\bgit\s+push\b",
    r"\bgit\s+remote\b",
    r"(^|[\s;&|(])gh\s",
    r"/data/secrets",
    r"/proc/[^/\s]+/environ",
    r"\bprintenv\b|\benv\b\s*$",
)]


def _inside(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        root = Path(root).resolve()
        if path == root or root in path.parents:
            return True
    return False


def _resolve(raw: str | None, cwd: Path) -> Path:
    p = Path(raw) if raw else Path(cwd)
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve()


def decide(role: str, tool: str, args: dict, *, cwd: Path, read_paths: list[Path],
           write_paths: list[Path]) -> tuple[bool, str]:
    if tool.startswith("mcp__"):
        return True, ""
    if tool in READ_TOOLS:
        path = _resolve(args.get(READ_TOOLS[tool]), cwd)
        if _inside(path, [*read_paths, *write_paths]):
            return True, ""
        return False, f"{tool} outside the allowed directories: {path}"
    if tool in WRITE_TOOLS:
        if role in READ_ONLY_ROLES:
            return False, f"{tool} is not allowed for the {role} role"
        path = _resolve(args.get(WRITE_TOOLS[tool]), cwd)
        if _inside(path, write_paths):
            return True, ""
        return False, f"{tool} outside the writable directories: {path}"
    if tool == "Bash":
        if role in READ_ONLY_ROLES:
            return False, f"Bash is not allowed for the {role} role"
        command = str(args.get("command", ""))
        for pattern in BASH_DENY:
            if pattern.search(command):
                return False, "this command is not allowed (pushing, gh, secrets and process env are off limits)"
        return True, ""
    return False, f"{tool}: tool not allowed for role {role}"


def make_can_use_tool(spec: RunSpec):
    async def can_use_tool(tool_name, tool_input, context):
        ok, reason = decide(spec.role, tool_name, tool_input or {}, cwd=spec.cwd,
                            read_paths=spec.read_paths, write_paths=spec.write_paths)
        return PermissionResultAllow() if ok else PermissionResultDeny(message=reason)

    return can_use_tool
