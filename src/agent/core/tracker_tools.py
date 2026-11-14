"""GitHub tools for the agent. core holds the tokens; the agent gets text back."""

from __future__ import annotations

from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema

MAX_FILE_CHARS = 20000


def register_tracker_tools(router: AgentToolRouter, *, reader) -> None:
    packs = router.packs

    def project_of(ctx: ToolContext):
        pack = packs.get(ctx.project or "")
        return pack.project if pack else None

    def needs_project(fn):
        async def wrapped(ctx: ToolContext, args: dict) -> ToolReply:
            project = project_of(ctx)
            if project is None:
                return ToolReply("this conversation isn't tied to a project")
            return await fn(ctx, args, project)
        return wrapped

    @needs_project
    async def gh_prs(ctx, args, project) -> ToolReply:
        prs = await reader.list_prs(project.repo, args.get("state") or "open")
        lines = [f"#{p['number']} {p['title']} ({p['head']['ref']} → {p['base']['ref']}) by "
                 f"{(p.get('user') or {}).get('login', '?')}{' [draft]' if p.get('draft') else ''}" for p in prs]
        return ToolReply("\n".join(lines) or "no pull requests")

    @needs_project
    async def gh_checks(ctx, args, project) -> ToolReply:
        runs = await reader.checks(project.repo, args.get("ref", ""))
        lines = [f"{r['name']}: {r.get('status')}/{r.get('conclusion')} {r.get('details_url', '')}".rstrip()
                 for r in runs]
        return ToolReply("\n".join(lines) or "no checks for that ref")

    @needs_project
    async def gh_ci_log(ctx, args, project) -> ToolReply:
        return ToolReply(await reader.job_log(project.repo, args.get("job_id", ""), max_chars=MAX_FILE_CHARS))

    @needs_project
    async def gh_issue(ctx, args, project) -> ToolReply:
        issue = await reader.issue(project.repo, int(args.get("number", 0)))
        lines = [f"#{issue['number']} {issue['title']} [{issue['state']}] by {(issue.get('user') or {}).get('login')}",
                 issue.get("body") or "", "Comments:"]
        lines += [f"{(c.get('user') or {}).get('login', '?')}: {c.get('body', '')}" for c in issue["comments_list"]]
        return ToolReply("\n".join(lines))

    @needs_project
    async def gh_file(ctx, args, project) -> ToolReply:
        text = await reader.file_at(project.repo, args.get("path", ""), args.get("ref") or project.default_branch)
        return ToolReply(text[:MAX_FILE_CHARS])




    s, reg = schema, router.register
    reg(ToolSpec("gh_prs", "List the project's pull requests (state: open, closed or all).",
                 s(optional={"state": "string"}), "read"), gh_prs)
    reg(ToolSpec("gh_checks", "CI check runs for a commit or branch.", s({"ref": "string"}), "read"), gh_checks)
    reg(ToolSpec("gh_ci_log", "The tail of a GitHub Actions job's log.", s({"job_id": "integer"}), "read"), gh_ci_log)
    reg(ToolSpec("gh_issue", "A GitHub issue with its comments.", s({"number": "integer"}), "read"), gh_issue)
    reg(ToolSpec("gh_file", "A file from the GitHub repo at a ref (default: the default branch).",
                 s({"path": "string"}, {"ref": "string"}), "read"), gh_file)
