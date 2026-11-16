# Long projects: Jensen ships a product from a spec

## Intent

The team hands Jensen a spec and Jensen turns it into a shipped product over several days with nobody driving it. It plans the work, builds it with subagents, tests it in its own Docker and browser, opens and merges its own PRs, posts screenshots every hour, and answers anyone who asks what it has done so far.

## Where things stand (live install, 2026-11-16)

- **A request stops after about 6 hours.** A turn has 90 minutes (`task_minutes`), then carries on at most 3 times (`continuations`). Each carry-on posts "Still on it…" in the thread.
- **A runner restart loses the turn.** Core learns a turn's session id only when the turn ends, so when the runner dies the re-queued turn has no session to resume. The queue re-queues a turn once, then marks it failed (`max_attempts = 2`).
- **A message sent while a turn runs waits for the turn to end,** which can be up to 90 minutes.
- **Jensen has no planning skills.** Superpowers isn't in its Claude home; its one install attempt failed before the shared home existed.
- **The prompt says to open PRs** and says nothing about merging them.

## Goals

1. A request runs until the work is done. Carry-ons are silent and default to 100, about 150 hours at 90 minutes each.
2. A runner restart costs a minute, not the work. The retried turn resumes the live session and is told it was restarted.
3. A message sent mid-turn reaches the running session within one heartbeat (15 s) plus the step in progress, and Jensen answers it right away.
4. Jensen runs a spec milestone by milestone with Superpowers (writing-plans, then subagent-driven-development). It merges each milestone's PR once tests and CI pass.
5. Every hour a scheduled run posts screenshots and progress in the project thread.
6. Anyone who asks what it has done so far gets an answer, in the thread or anywhere else, built from the plan ledger, merged PRs and the thread.

## Non-goals

- An external agent manager or durable-execution engine. Core already queues, leases, retries and resumes.
- Core tools, gates or a pipeline for projects. The workflow lives in a skill, and Jensen decides the steps.
- ralph-loop. Its `/ralph-loop` command is marked `hide-from-slash-command-tool`, so the model can't start it, and Jensen's sessions have no person typing. Carry-ons plus the skill's rule (the turn ends when the last milestone ships) do the same job. Revisit if a live run ends early.
- GitHub webhooks for PR comments or CI. Jensen waits on CI itself with `gh pr checks --watch`.
- Production deploys. The prompt's staging-only rule stands.

## Anti-goals

- Bot-style posts from core. No "Still on it" and no run headers; updates come from Jensen, worded like a teammate.
- A message answered twice, or lost. Every message is either read by the running session or runs as its own next turn.

## Approaches considered

- **Adopt a Rust orchestrator** (harness, loom, c9r-io/orchestrator, software-factory-rs). Rejected. They have 1–92 stars, they would replace a working queue, ingress, dashboard and KPIs, and they put a fixed pipeline around the agent.
- **Durable execution** (Restate, Temporal). Rejected. Jensen's steps are decided inside the Claude session. The state worth keeping is the session id (core stores it) and the ledger in the repo (Superpowers writes it); a workflow engine has nothing else to checkpoint.
- **Close the gaps in the current core and give Jensen a skill.** Chosen. Each gap is a small change to code that already does the job.

## Design

### Carry-ons

`limits.continuations` goes from 3 to 100 by default. A carry-on posts nothing, and the message keeps its ⏳ reaction until the request finishes. Running out of carry-ons still posts the existing failure reply.

### Restarts

The runner's heartbeat, every 15 s, carries the live session id, and core saves it on the conversation. When the runner dies, the queue re-queues the turn once its lease runs out. The retried turn resumes that saved session, and the runner starts its prompt with a restart note. `WorkQueue.max_attempts` goes from 2 to 5, so a few deploys during one turn don't fail it.

### Mid-turn messages

A local test (Claude Code 2.1.325, stream-json input, `--replay-user-messages`) settled how the CLI behaves:
- A user message written to stdin during a turn is read at the next tool boundary, in the same turn.
- A message written during the final answer runs as a second turn in the same process.
- Either way, when the model reads the message the CLI echoes it back with the uuid we gave it.

How messages flow:
- **Core.** A message for a conversation whose turn is running is queued as usual. On each heartbeat the runner says whether it can take messages. If it can, core hands over that conversation's queued turns, marks them `delivered` (with `delivered_into` set to the running item) and reacts ⏳.
- **Runner.** It sends each message into the live session with its own uuid. The text is the origin line, then a mid-turn note, then the message. The runner keeps reading past a result until every message it sent has been echoed. Read ids go back to core on the next heartbeat and in the result. Core marks them done and reacts ✅.
- **Unread messages.** Anything delivered but not read when the turn ends (a timeout, crash, cancel or account fork) goes back to `queued` and runs as the next turn, before any carry-on.
- **The note** tells the session to answer now with post_update (a Linear thought in a Linear session), because its final reply only posts when the whole turn ends.
- **Latency** is at most one heartbeat plus the step in progress. A foreground subagent counts as one step.

### The `long-project` skill

The project workflow lives in a core skill, written for Jensen:
- **Superpowers.** Check for it and install it if missing (`claude plugin install superpowers@anthropic-plugin-directory`).
- **Repo and plan.** Create or clone the repo and commit the spec. Write milestone plans with writing-plans; each milestone is something a person can run and see.
- **Memory and schedule.** Save a project memory note (thread, repo, conversation folder, plans, ledger, schedule), and add an hourly `<project>-progress` schedule.
- **Each milestone.** Run it with subagent-driven-development, pushing after every task. Test it in the browser, open the PR, wait for CI, merge, and post what landed with screenshots.
- **Status questions** are answered from the ledger, `git log` and `gh pr list`.
- **Finish.** Delete the schedule, update the note, and report what shipped, where it runs and how it was verified.

The hourly run posts screenshots of the newest pushed commit, plus what merged, what's in progress and what's next. It skips the post when nothing changed since the last one.

### Prompt

Two lines in `system.md`:
- A spec, or anything that will take more than a few hours, uses the long-project skill. The turn carries on by itself.
- In a repo it's building from a spec, Jensen merges its own PRs once tests and CI pass and its review found nothing blocking. In other repos it follows the repo's review rules.

## Constraints

- The live runner uses claude-agent-sdk 0.2.180, which bundles Claude Code 2.1.321.
- The core is Python, and tests run in the dev image with `just test`.
- Jensen's workflow stays in skills. Core posts nothing on Jensen's behalf except real failures.

## Decisions made without the team

- 100 carry-ons by default, and 5 attempts per turn.
- The hourly post is skipped when nothing changed since the last one.
- Milestones merge one PR each, rather than one PR at the end of the project.
- ralph-loop is left out (see Non-goals).

## Left to the builder

- The exact wording of Slack updates, which Jensen writes itself.
- How the dashboard shows a `delivered` item. It's shown as running.
