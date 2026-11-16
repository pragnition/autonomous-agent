---
name: long-project
description: Use when someone hands you a spec or asks for a whole product, app or service to be built (work that takes hours or days), and whenever anyone, anywhere, asks about or asks for a change to a project you're building.
---

# Building a project from a spec

You build the whole thing yourself, over as many hours or days as it takes, with nobody driving. Your turn carries on by itself when it hits a time or step limit and after a restart, so keep working until the last milestone is merged and verified. Stop early only when you need something only a person can give (a secret, access, a decision about what they want), and say exactly what.

## Start

1. **Superpowers.** Check `claude plugin list` for superpowers. If it's missing, run `claude plugin install superpowers@anthropic-plugin-directory`. Its skills load from your next session; until then, read them from the plugin's folder under `~/.claude/plugins/cache/`.
2. **Repo.** Work in the repo the request names, and clone it into this conversation's folder.
3. **Spec.** The spec may be a message, a file in the thread (PDF, Word) or a link. Read all of it. Commit the original and a Markdown copy under `docs/` if they aren't in the repo yet.
4. **Plan.** Read the repo's CLAUDE.md, its roadmap and its existing plans first, so your milestones fit what's already there. Then use superpowers:writing-plans:
   - Split the work into milestones. Each one is something a person can run and see.
   - Give each milestone its own plan, in the folder where the repo keeps its plans (`docs/plans/` if it has none).
   - Settle the questions the skills would ask you yourself, and post what you assumed in the thread.
5. **Register it.** Call `start_project` with a short name and the repo. Every session, in any conversation, then sees the project in its prompt and can pass you changes.
6. **Memory note.** Save a memory file `project-<name>.md` with:
   - this conversation's folder (`pwd`), where your clone lives
   - the plan files and where the ledger lives
   - the progress schedule's name

   Sessions answering questions about the project read it next to core's project list.
7. **Hourly progress.** Add a schedule named `<project>-progress` (schedules skill) with `cron = "0 * * * *"`, using the thread's channel as its channel. Write its prompt for a fresh session:
   - Read the project's memory note.
   - Fetch the repo and check out the newest pushed commit of the milestone in progress (main when none is).
   - Run the app on a free port and screenshot its main screens (browser-testing).
   - Post the screenshots in the project thread with two or three lines: what merged since the last update, what's being built now, and what's next, taken from the ledger and `git log`. A scheduled run has no thread of its own, so upload with Slack's `files.getUploadURLExternal` and `files.completeUploadExternal` using `$SLACK_BOT_TOKEN`, the channel and the thread's `thread_ts`.
   - Skip the post when nothing changed since the last one.
8. **Kickoff.** Post a short message in the thread: the milestones, and when the first one should land.

## Each milestone

1. Branch from main, named the way the repo names its branches.
2. Run its plan with superpowers:subagent-driven-development: a fresh implementer subagent and a reviewer for each task, with the ledger in the repo's `.superpowers/sdd/`. Push the branch after every task, so the hourly run and anyone else can see it.
3. Test it end to end in a real browser (browser-testing) and run the full suite.
4. Open the PR (review-before-pr, pr-description). Wait for CI with `gh pr checks --watch` and fix what fails. Then merge it with `gh pr merge --squash --delete-branch` (or the repo's own style), and check that main still builds and runs.
5. Post in the thread what landed, with the PR link and the screenshots (`post_files`).

## When someone asks how it's going

Messages that arrive mid-turn reach you between steps. Answer at once with post_update: what's merged (PR links), what's in progress, what's next and anything blocked. Take it from the ledger, `git log` and `gh pr list`, not from what you remember.

## When the team changes direction

A message that changes the spec or the priorities beats the plan, whether it was posted in the thread or passed on from another conversation. At the next step:

1. Let the task in flight finish, or stop it. Don't merge work the change makes wrong.
2. Commit the change to the spec in `docs/`, with who asked and the link.
3. Rewrite the plans for the milestones that aren't merged yet. Close PRs and delete branches the change makes obsolete.
4. Update the memory note, and the progress schedule's prompt if it names what changed.
5. Post in the thread what changes, what's dropped and the new milestones.

## From another conversation

People talk to you about a project anywhere: a DM, another channel, a new thread. Your prompt lists the projects being built and each one's build. If you're not that build:

1. Answer questions yourself. Read the project's memory note, the ledger, `git log`, `gh pr list` and the code; clone the repo into your own folder if you need it. "Does the admin page have xyz?" is answered from the code.
2. When they ask for a change, settle with them what they want. Then call `tell_project` with the project's name and one message: who wants what, what you two established, and anything the build needs to know. The build gets it at its next step, from the person who asked.
3. Tell them it's passed on, with the project thread's link from `tell_project`'s reply.
4. If you can't tell whether they mean a change to the build or separate work, ask them once.

## Finish

When the last milestone is merged and verified:
- Call `finish_project`, delete the progress schedule and update the memory note.
- End with what shipped, how to run it and how you verified it.
- Deploy only where you have the credentials and the spec or request asks for it: staging, never production. If deploying needs access you don't have, say what.
