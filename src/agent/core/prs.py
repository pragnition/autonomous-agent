"""open_pr (spec v2 §7): the only way code reaches GitHub.

The runner runs the project's checks on the exact commit and pushes the branch to core's git endpoint; core then
checks the branch name, that the commit is the branch's tip, and that the checks passed, before pushing it to
GitHub as a draft PR assigned to the requester. There is no merge or force-push path.
"""

from __future__ import annotations

import asyncio

from agent import tasks
from agent.core.agent_tools import AgentToolRouter, ToolContext, ToolReply, ToolSpec, schema
from agent.tasks import Status


def pr_body(*, agent_name: str, requester_github: str | None, origin_ref: str, summary: str,
            checks: list[str]) -> str:
    who = f"Requested by @{requester_github} in" if requester_github else "Requested in"
    lines = [f"🤖 {agent_name} (AI agent) · {who} {origin_ref}", ""]
    if summary:
        lines += [summary.strip(), ""]
    if checks:
        lines += ["**Checks run before this PR was opened (all passed)**", *[f"- `{c}`" for c in checks]]
    return "\n".join(lines).rstrip() + "\n"


def register_pr_tools(router: AgentToolRouter, *, githost, writer, write_token: str) -> None:
    config, packs = router.config, router.packs

    def github_login(person_key: str | None) -> str | None:
        person = config.people.get(person_key) if person_key else None
        return person.github if person else None

    async def open_pr(ctx: ToolContext, args: dict) -> ToolReply:
        if ctx.project not in packs:
            return ToolReply("this conversation isn't tied to a project, so there's nowhere to open a PR")
        project = packs[ctx.project].project
        branch, sha = args.get("branch", ""), args.get("sha", "")
        checks = args.get("checks") or {}
        if not branch.startswith(f"{config.name}/"):
            return ToolReply(f"refused: the branch must start with {config.name}/ (got {branch!r})")
        if not sha or await asyncio.to_thread(githost.tip, ctx.project, branch) != sha:
            return ToolReply(f"refused: {sha or 'that commit'} is not the tip of {branch} on core's git endpoint; "
                             "push the branch first")
        if not checks.get("passed"):
            return ToolReply("refused: checks failed. Fix them and try again.\n" + str(checks.get("log_tail", ""))[-3000:])
        task = router.ensure_task(ctx, args.get("title", ""))
        await asyncio.to_thread(githost.push_to_github, ctx.project, project.repo, branch, sha, write_token)
        number = await writer.find_open_pr(project.repo, branch)
        if number:
            url = f"https://github.com/{project.repo}/pull/{number}"
        else:
            origin = f"Linear {task.linear_issue_id}" if task.linear_issue_id else "Slack"
            body = pr_body(agent_name=config.name, requester_github=github_login(task.requester),
                           origin_ref=origin, summary=args.get("body", ""), checks=project.build_checks)
            number, url = await writer.open_draft_pr(project.repo, head=branch, base=project.default_branch,
                                                     title=args.get("title", "") or task.title, body=body)
            assignee = github_login(task.requester) or github_login(project.owner)
            if assignee:
                await writer.set_assignee(project.repo, number, assignee)
        task = tasks.set_fields(router.conn, task.id, branch=branch, pr_number=number, pr_url=url)
        if task.status == Status.needs_input:
            task = tasks.transition(router.conn, task.id, Status.working, actor=ctx.actor or "agent")
        if task.status == Status.working:
            task = tasks.transition(router.conn, task.id, Status.ready_for_review, actor=ctx.actor or "agent",
                                    data={"pr": number})
        await router.notifier.task_changed(task)
        return ToolReply(f"Draft PR #{number} is open: {url}")

    router.register(ToolSpec(
        "open_pr", "Open (or update) a draft pull request for your work. The project's checks run first on your "
                   "latest commit and must pass; your branch is pushed for you.",
        schema({"title": "string", "body": "string"}), "act", ("code",)), open_pr)
