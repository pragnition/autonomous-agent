"""GitHub and Linear tools for the agent. core holds the tokens; the agent gets text back."""

from __future__ import annotations

from agent import tasks
from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema

MAX_FILE_CHARS = 20000


def register_tracker_tools(router: AgentToolRouter, *, reader, linear) -> None:
    packs = router.packs

    def project_of(ctx: ToolContext):
        pack = packs.get(ctx.project or "")
        return pack.project if pack else None

    def linear_issue_id(ctx: ToolContext, args: dict) -> str | None:
        if args.get("issue_id"):
            return args["issue_id"]
        task = tasks.get_task(router.conn, ctx.task_id) if ctx.task_id else None
        return task.linear_issue_id if task else None

    def needs_project(fn):
        async def wrapped(ctx: ToolContext, args: dict) -> ToolReply:
            project = project_of(ctx)
            if project is None:
                return ToolReply("this conversation isn't tied to a project")
            return await fn(ctx, args, project)
        return wrapped

    def needs_linear(fn):
        async def wrapped(ctx: ToolContext, args: dict, project) -> ToolReply:
            if linear is None:
                return ToolReply("Linear isn't configured for this install")
            issue_id = linear_issue_id(ctx, args)
            if not issue_id:
                return ToolReply("no Linear issue: pass issue_id, or work on a task that came from Linear")
            return await fn(ctx, args, project, issue_id)
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

    @needs_project
    @needs_linear
    async def linear_issue(ctx, args, project, issue_id) -> ToolReply:
        issue = await linear.issue(issue_id)
        lines = [f"{issue['identifier']} {issue['title']} [{issue['state']}] {issue['url'] or ''}".rstrip(),
                 issue["description"], "Comments:"]
        lines += [f"{c['author']}: {c['body']}" for c in issue["comments"]]
        return ToolReply("\n".join(lines))

    @needs_project
    @needs_linear
    async def linear_comment(ctx, args, project, issue_id) -> ToolReply:
        await linear.comment(issue_id, args.get("body", ""))
        return ToolReply("commented")

    @needs_project
    @needs_linear
    async def linear_set_status(ctx, args, project, issue_id) -> ToolReply:
        key = args.get("status_key", "")
        if key not in project.linear_status:
            return ToolReply(f"unknown status key '{key}'; use one of {', '.join(sorted(project.linear_status))}")
        await linear.set_state(issue_id, project.linear_team, project.linear_status[key])
        return ToolReply(f"Linear status set to {project.linear_status[key]}")

    s, reg = schema, router.register
    reg(ToolSpec("gh_prs", "List the project's pull requests (state: open, closed or all).",
                 s(optional={"state": "string"}), "read"), gh_prs)
    reg(ToolSpec("gh_checks", "CI check runs for a commit or branch.", s({"ref": "string"}), "read"), gh_checks)
    reg(ToolSpec("gh_ci_log", "The tail of a GitHub Actions job's log.", s({"job_id": "integer"}), "read"), gh_ci_log)
    reg(ToolSpec("gh_issue", "A GitHub issue with its comments.", s({"number": "integer"}), "read"), gh_issue)
    reg(ToolSpec("gh_file", "A file from the GitHub repo at a ref (default: the default branch).",
                 s({"path": "string"}, {"ref": "string"}), "read"), gh_file)
    reg(ToolSpec("linear_issue", "A Linear issue with its comments (default: this task's issue).",
                 s(optional={"issue_id": "string"}), "read"), linear_issue)
    reg(ToolSpec("linear_comment", "Comment on a Linear issue (default: this task's issue).",
                 s({"body": "string"}, {"issue_id": "string"}), "act"), linear_comment)
    reg(ToolSpec("linear_set_status", "Set a Linear issue's status by key: working, ready_for_review, merged or "
                                      "failed (default: this task's issue).",
                 s({"status_key": "string"}, {"issue_id": "string"}), "act"), linear_set_status)
