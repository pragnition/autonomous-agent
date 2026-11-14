"""core's tools as in-process SDK tools. The tool code runs in the runner's process, so the session token stays
out of the session's environment, files and command lines. One tool does local work: post_files reads files from
the session folder."""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import tool

from agent.runner.link import SessionRevoked


def _text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def remote_sdk_tools(manifest: list[dict], link, tok: str, *, session_dir: Path) -> list:
    async def forward(name: str, args: dict) -> str:
        try:
            reply = await link.tool(tok, name, args)
        except SessionRevoked:
            return "This session was stopped."
        return reply.get("text", "")

    async def post_files(args: dict) -> str:
        items = []
        for raw in args.get("paths") or []:
            path = Path(raw) if Path(raw).is_absolute() else session_dir / raw  # relative to the session folder
            if not _inside(path, session_dir):
                return f"refused: {raw} is outside your session folder"
            if not path.is_file():
                return f"no such file: {raw}"
            items.append((path.name, path.read_bytes()))
        try:
            posted = await link.files(tok, items, args.get("text", ""))
        except SessionRevoked:
            return "This session was stopped."
        if posted is False:
            return (f"recorded {len(items)} file(s) as evidence, but this conversation has no Slack thread, so nobody "
                    "saw them. Attach them where the request came from (e.g. the Linear issue) if they matter.")
        return f"posted {len(items)} file(s)"

    local = {"post_files": post_files}
    tools = []
    for entry in manifest:
        name = entry["name"]

        async def handler(args, name=name):
            if name in local:
                return _text(await local[name](args))
            return _text(await forward(name, args))

        tools.append(tool(name, entry.get("description", ""), entry.get("input_schema") or
                          {"type": "object", "properties": {}})(handler))
    return tools
