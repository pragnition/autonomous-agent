---
name: long-project
description: Use when someone hands you a spec or asks for a whole product, app or service to be built (work that takes hours or days), and whenever anyone, anywhere, asks about or asks for a change to a project you're building.
---

# Building a project from a spec

You build the whole thing yourself, over as many hours or days as it takes, with nobody driving. Your turn carries on by itself when it hits a time or step limit and after a restart, so keep working until the last milestone is merged and verified. Stop early only when you need something only a person can give (a secret, access, a decision about what they want), and say exactly what.

## Start

1. **Superpowers.** Check `claude plugin list` for superpowers. If it's missing, run `claude plugin install superpowers@anthropic-plugin-directory`. Its skills load from your next session; until then, read them from the plugin's folder under `~/.claude/plugins/cache/`.
2. **Repo.** Work in the repo the request names, and clone it into this conversation's folder. A new product with no repo named gets a new private repo in the team's GitHub org (`gh repo create <org>/<name> --private --clone`); say where in the kickoff.
3. **Spec.** The spec may be a message, a file in the thread (PDF, Word) or a link. Read all of it. In a new repo, or one that keeps docs, commit the original and a Markdown copy under `docs/` if they aren't there yet; otherwise keep them in your conversation folder and note the path in the memory note.
4. **Plan.** Read the repo's CLAUDE.md, its roadmap and its existing plans first, so your milestones fit what's already there. Then use superpowers:writing-plans:
   - Split the work into milestones. Each one is something a person can run and see.
   - Give each milestone its own plan, in the folder where the repo keeps its plans. A new repo uses `docs/plans/`; an existing repo that keeps none gets them in your conversation folder, with the path in the memory note.
   - For a new product (a new repo), the first milestone sets the standard every later screen and PR holds to:
     - Scaffold it from the stack's official starter without prompts (`create-next-app --yes`, or FastAPI's full-stack template for a Python backend), and delete the template's workflows that need secrets or deploy targets you don't have.
     - Set its look with frontend-design and commit it: design tokens (colour, type, spacing, radius) in code, the reasoning in a design doc, and a CLAUDE.md that points to both and lists the commands. In a React app, build dialogs, menus, comboboxes and tabs on unstyled accessible primitives (React Aria Components or Radix Primitives) styled with those tokens, not by hand.
     - CI runs the type check, lint, unit and end-to-end tests, @axe-core/playwright on the main screens (WCAG 2.2 AA) and Playwright screenshot comparisons. CI never calls a real model or outside service; stub them. Screenshot tests use fixed time and data, no animations and bundled fonts, and run in Playwright's official Docker image locally and in CI, so the baselines match.
     - Turn on CodeQL default setup and make all of these required checks on the default branch where the repo's GitHub plan allows; where it doesn't, or your `gh` login can't, say so once and carry on.
   - In a repo that already exists, build to its design, CI and conventions: new UI reuses its components and tokens, and you don't add a second design system or change its CI or settings unless asked.
   - Point every UI task in the plans at the design doc and tokens (or the existing components), so each fresh subagent builds to the same design.
   - Settle the questions the skills would ask you yourself, and post what you assumed in the thread.
5. **Register it.** Call `start_project` with a short name and the repo. Every session, in any conversation, then sees the project in its prompt and can pass you changes.
6. **Memory note.** Save a memory file `project-<name>.md` with:
   - this conversation's folder (`pwd`), where your clone lives
   - the plan files and where the ledger lives
   - the progress schedule's name

   Sessions answering questions about the project read it next to core's project list.
7. **Hourly progress.** Add a schedule named `<project>-progress` (schedules skill) with `cron = "0 * * * *"`, using the thread's channel as its channel. Write its prompt for a fresh session:
   - Read the project's memory note.
   - Fetch the repo and check out the newest pushed commit of the milestone in progress (the default branch when none is).
   - Run the app on a free port and screenshot its main screens (playwright-cli).
   - Post the screenshots in the project thread with two or three lines: what merged since the last update, what's being built now, and what's next, taken from the ledger and `git log`. A scheduled run has no thread of its own, so upload with Slack's `files.getUploadURLExternal` and `files.completeUploadExternal` using `$SLACK_BOT_TOKEN`, the channel and the thread's `thread_ts`.
   - Skip the post when nothing changed since the last one.
8. **Kickoff.** Post a short message in the thread: the milestones, and when the first one should land.

## Each milestone

1. Branch from the default branch, or from the previous milestone's branch while its PR waits on a required review, named the way the repo names its branches.
2. Run its plan with superpowers:subagent-driven-development: a fresh implementer subagent and a reviewer for each task, with the ledger in the repo's `.superpowers/sdd/`. Push the branch after every task, so the hourly run and anyone else can see it. Its final whole-branch review is this milestone's superpowers:requesting-code-review.
3. Test it end to end in a real browser, in a `playwright-cli` session named after the branch (`-s=<branch>`): the whole path, including an empty state, an error and a long value, at desktop width and, where the product is meant for phones, phone width, with the console checked. Read every screenshot. Run the full suite.
4. Review the whole branch with fresh eyes: the final review from step 2, the pr-review-toolkit agents the diff calls for when it changes logic, and for UI a fresh subagent comparing the screenshots with the design doc (or the product's existing design) and web-design-guidelines. Fix what's real, then open the PR. Wait for CI with `gh pr checks --watch` and fix what fails. Then merge it with `gh pr merge --squash --delete-branch` (or the repo's own style), and check that the default branch still builds and runs. When a required approval holds the merge, request a review from the person who asked, turn on `gh pr merge --auto` and carry on with the next milestone; never bypass branch protection (`--admin`).
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
