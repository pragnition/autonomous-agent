# Long projects: Jensen ships a product from a spec

## Intent

The team hands Jensen a spec and Jensen turns it into a working product over several days with nobody driving it. It plans the work, builds it with subagents, tests it in its own Docker and browser, opens and merges its own PRs, posts screenshots every hour, and answers anyone who asks what it has done so far.

The first project is a RosterRolo demo built from a tender specification, in a new repo made for Jensen. Every request names the repo to work in. Jensen works in it on its own and merges its own PRs there, as it may in any repo it works in.

## Phases

This build runs on the current Python core, so the first project can start now. A later phase moves core's queue and state into a control plane. To keep the two compatible:
- The runner keeps one protocol to talk to whatever control plane it pulls from.
- This build writes that protocol down (`docs/runner-protocol.md`), including the new heartbeat fields, so the control plane implements it.
- The runner, the skill and the prompt carry over unchanged. Only the queue and conversation code in core is replaced.

## Where things stand (live install, 2026-11-16)

- **A request stops after about 6 hours.** A turn has 90 minutes (`task_minutes`), then carries on at most 3 times (`continuations`). Each carry-on posts "Still on it…" in the thread.
- **A runner restart loses the turn.** Core learns a turn's session id only when the turn ends, so when the runner dies the re-queued turn has no session to resume. The queue re-queues a turn once, then marks it failed (`max_attempts = 2`).
- **A message sent while a turn runs waits for the turn to end,** which can be up to 90 minutes.
- **Jensen has no planning skills.** Superpowers isn't in its Claude home; only skill-creator is.
- **The prompt says to open PRs** and says nothing about merging them.

## Goals

1. A request runs until the work is done. Carry-ons are silent and default to 100, about 150 hours at 90 minutes each. A scheduled run still stops after 3 carry-ons, so a recurring schedule can't pile up runaway runs.
2. A runner restart costs a minute, not the work. The retried turn resumes the live session and is told it was restarted.
3. A message sent mid-turn reaches the running session within one heartbeat (15 s) plus the step in progress, and Jensen answers it right away.
4. Jensen runs a spec milestone by milestone with Superpowers (writing-plans, then subagent-driven-development), in the repo the request names. It fits the milestones to the repo's own conventions and existing plans.
5. Jensen merges its own PRs once tests and CI pass and its review found nothing blocking.
6. Every hour, a scheduled run posts screenshots and progress in the project thread.
7. Anyone who asks what it has done so far gets an answer, in the thread or anywhere else, built from the plan ledger, PRs and the thread.
8. Anyone can steer the project from any conversation. For example, in a DM: "does the admin page have xyz?", "no", "add it". Every session knows which projects are being built, and it passes the change to the build through core. The running build works it into the plan at its next step.
9. Memory is a folder of one-fact files with a short index, not a CLAUDE.md that grows without limit.
10. The runner protocol is written down for the control-plane phase.

## Non-goals

- Replacing core's queue and state in this build. That's the control-plane phase.
- Gates or a pipeline for projects. The workflow lives in a skill, and Jensen decides the steps. Core's only new tools are the three project tools, which hold state only core has.
- ralph-loop. Its `/ralph-loop` command is marked `hide-from-slash-command-tool`, so the model can't start it, and Jensen's sessions have no person typing. Carry-ons plus the skill's rule (the turn ends when the last milestone ships) do the same job. Revisit if a live run ends early.
- GitHub webhooks for PR comments or CI. Jensen waits on CI itself with `gh pr checks --watch`.
- Deploying the product. Jensen runs it in its own environment and deploys only where it has credentials and the request asks; it says what access it would need otherwise. Production stays off limits.

## Anti-goals

- Bot-style posts from core. No "Still on it" and no run headers; updates come from Jensen, worded like a teammate.
- A message answered twice, or lost. Every message is either read by the running session or runs as its own next turn.

## Approaches considered

- **Adopt an off-the-shelf Rust orchestrator** (harness, loom, c9r-io/orchestrator, software-factory-rs). Not taken. They're young (1–92 stars), they would replace the ingress, dashboard and KPIs along with the queue, and they put a fixed pipeline around the agent.
- **Wait for the control-plane phase.** Not taken. The first project would wait on it, and the runner-side work (mid-turn messages, session resume) is needed either way.
- **Close the gaps in the current core, give Jensen a skill, and write down the runner protocol.** Chosen. Each core change is small, and the parts the control plane will need carry over.

## Design

### Carry-ons

`limits.continuations` goes from 3 to 100 by default. A new `limits.schedule_continuations` (default 3) caps carry-ons for scheduled runs. A carry-on posts nothing, and the message keeps its ⏳ reaction until the request finishes. Running out of carry-ons still posts the existing failure reply.

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

### Projects core knows about, and steering from any conversation

A long project's build runs in one conversation. Core keeps a `projects` table recording each project's name, repo, build conversation, thread link, who started it, and whether it's active or done. The build registers itself with `start_project` and closes with `finish_project`.

**Every session sees the active projects.** Core writes them into each session's system prompt when the turn starts, the same way it lists the packs: name, repo, thread link, and whether the build is running, queued or idle. So a session in Sam's DM knows "rosterrolo" is a running build before it reads Sam's message.

**Any session passes a change to the build** with `tell_project(name, message)`. Core queues it in the build's conversation:
- It arrives as a message from the person who asked, with that person's origin line (who, where, link), taken from the asking turn. Core knows who asked, so it doesn't depend on what the message text says.
- If the build is running, it reaches the build at its next step (mid-turn messages, above). If the build is idle, it runs now.
- A wrong name is answered with the active projects. A done project goes back to active.

**What's left to the model** is deciding that a message is a change for the build, not a question or separate work. The project list in its prompt gives it what it needs, and the skill tells it to ask once when that's unclear.

Two other designs were considered:
- **Posting into the project thread with a self-mention.** It depends on Slack echoing the bot's own posts and on parsing a mention, and the sender would only be named in the text.
- **Automatic routing,** where core classifies every message against active projects with a quick model call. That puts an LLM call that can fail or misroute in front of every turn, and turns the build into a chat channel.

When a change arrives, from the thread or through `tell_project`, the build follows the skill's change-of-direction steps:
- commit the spec change
- rewrite the milestones that aren't merged yet
- close obsolete PRs
- update its memory note and the progress schedule
- post what changed

### Memory

Today Jensen appends what it learns to `~/.claude/CLAUDE.md`, which loads in full into every session. The memory folder the runner sets up has never been used. The cause is the runner's own system prompt, which replaces Claude Code's, including Claude Code's memory instructions. That doesn't scale:
- everything loads every turn
- nothing removes stale facts
- parallel sessions append to one file

Memory becomes a folder:
- **One file per fact,** with a name, a one-line description and the fact itself, including who said it, when and the link.
- **A `MEMORY.md` index** with one line per fact.
- **The runner adds the rules and the index to every session's system prompt.** The index is cut at 200 lines, with a note to tidy it. A session reads a fact's file when its line matters.
- **CLAUDE.md** no longer receives learned facts.

Jensen moves its current facts into the folder once, and a nightly schedule it sets up merges duplicates, fixes or deletes stale facts, and keeps the index short.

### Runner protocol

`docs/runner-protocol.md` describes:
- What the runner and core exchange: pull, heartbeat, events, tool calls, files, result and bundle.
- The two kinds of token.
- Leases, attempts and restarts.
- One running item per conversation.
- The heartbeat's session, read and messages fields.
- The result's read list.

The control-plane phase implements this contract.

### The `long-project` skill

The project workflow lives in a core skill, written for Jensen:
- **Superpowers.** Check for it and install it if missing (`claude plugin install superpowers@anthropic-plugin-directory`).
- **Repo.** Work in the repo the request names.
- **Spec.** The spec may be a message, a file in the thread (PDF, Word) or a link. Jensen reads all of it and commits the original and a Markdown copy.
- **Plan.** Read the repo's CLAUDE.md, roadmap and existing plans, then write milestone plans with writing-plans. Each milestone is something a person can run and see.
- **Register, remember, schedule.** Call `start_project`, save a `project-<name>.md` memory file (conversation folder, plans, ledger, schedule), and add an hourly `<project>-progress` schedule.
- **Each milestone.** Run it with subagent-driven-development, pushing after every task. Test it in the browser, open the PR, wait for CI and merge it. Then post what landed with screenshots.
- **Status questions** are answered from the ledger, `git log` and `gh pr list`.
- **Changes of direction**, from the thread or passed on, beat the plan (see Steering).
- **From another conversation:** answer from the project's memory note and the code; pass a settled change to the build with `tell_project`.
- **Finish.** Call `finish_project`, delete the schedule, update the note, and report what shipped, how to run it and how it was verified.

The hourly run posts screenshots of the newest pushed commit, plus what merged, what's in progress and what's next. It skips the post when nothing changed since the last one.

### Prompt

Two lines in `system.md`:
- A spec, or anything that will take more than a few hours, uses the long-project skill. The turn carries on by itself.
- Jensen merges its own PRs once tests and CI pass and its review found nothing blocking.

## Constraints

- The live runner uses claude-agent-sdk 0.2.180, which bundles Claude Code 2.1.321.
- The core is Python, and tests run in the dev image with `just test`.
- Jensen's workflow stays in skills. Core posts nothing on Jensen's behalf except real failures.
- A project's app keys reach Jensen only through `data/secrets/projects/<project>.env`.

## Decisions made without the team

- 100 carry-ons for requests, 3 for scheduled runs, and 5 attempts per turn.
- The hourly post is skipped when nothing changed since the last one.
- One PR per milestone, rather than one PR at the end of the project.
- ralph-loop is left out (see Non-goals).

## Left to the builder

- The exact wording of Slack updates, which Jensen writes itself.
- How the dashboard shows a `delivered` item. It's shown as running.
