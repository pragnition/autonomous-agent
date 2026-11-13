"""Admin tools for the sandboxes (spec v2 §5, §7): rebuild one from its template, or run one command as root in
it without SSH. Code names the sandbox from the project and profile; the agent never creates sandboxes."""

from __future__ import annotations

import re

from agent.config import PROFILES
from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema

MAX_OUTPUT = 4000

MARK = "[customer-data]"
_CUSTOMER = [re.compile(p) for p in (
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",                       # email
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",                                 # bearer token
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+",            # JWT
    r"\b(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}\b",                        # IPv6 (full form)
    r"\b(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4})?\b",                 # IPv6 (compressed)
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",                                           # IPv4
    r"\+\d[\d \-]{6,}\d",                                                      # phone with a country code
    r"(?<![\w.:/-])\d{8,15}(?![\w.:/-])",                                      # long bare digit run (phone, ID)
)]


def scrub_customer(text: str, patterns: list[str]) -> tuple[str, int]:
    """Strip customer data (spec v2 §7) from a debug sandbox's output, plus the pack's own patterns. Errors, stack
    traces, timings, request paths and counts survive; emails, phone numbers, IPs, tokens and ids don't."""
    hits = 0
    for pattern in [*_CUSTOMER, *(re.compile(p) for p in patterns)]:
        text, n = pattern.subn(MARK, text)
        hits += n
    return text, hits


def register_sandbox_tools(router: AgentToolRouter, sandboxd) -> None:
    def sandbox_name(args: dict) -> tuple[str | None, str | None]:
        profile, project = args.get("profile", ""), args.get("project") or ""
        if profile not in PROFILES:
            return None, f"profile must be one of {', '.join(PROFILES)}"
        if profile == "general":
            return "general", None
        if project not in router.packs:
            return None, f"unknown project '{project}' (known: {', '.join(sorted(router.packs))})"
        return f"{profile}-{project}", None

    async def rebuild_sandbox(ctx: ToolContext, args: dict) -> ToolReply:
        name, problem = sandbox_name(args)
        if problem:
            return ToolReply(problem)
        await sandboxd.rebuild(name)
        return ToolReply(f"Rebuilt {name} from its template.")

    async def sandbox_exec(ctx: ToolContext, args: dict) -> ToolReply:
        name, problem = sandbox_name(args)
        if problem:
            return ToolReply(problem)
        out = await sandboxd.exec(name, args.get("command", ""))
        text = str(out.get("output", ""))
        if name.startswith("debug-"):
            text, _ = scrub_customer(text, router.packs[name.split("-", 1)[1]].project.scrub_patterns)
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + f"\n[output cut at {MAX_OUTPUT} chars]"
        return ToolReply(f"exit code {out.get('code')}\n{text}")

    router.register(ToolSpec("rebuild_sandbox", "Rebuild a sandbox from its template (profile: code, debug or "
                                                "general; project for code and debug).",
                             schema({"profile": "string"}, {"project": "string"}), "admin"), rebuild_sandbox)
    router.register(ToolSpec("sandbox_exec", "Run one shell command as root in a sandbox and show the output.",
                             schema({"profile": "string", "command": "string"}, {"project": "string"}), "admin"),
                    sandbox_exec)
