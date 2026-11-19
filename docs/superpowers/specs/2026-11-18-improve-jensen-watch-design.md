# improve-jensen: a continuous engine that makes Jensen a better engineer

## Intent

Jensen is meant to be a full AI engineer: it ships enterprise-grade products, it doesn't produce slop, and it can
work for many hours without a person. It isn't there yet. No solid product has come out of it so far.

improve-jensen is the agent that owns this repo. It becomes an engine that keeps checking everything Jensen does:
- what Jensen ships;
- how it works (delegation, context, finding project context, its toolkit);
- how it holds up over long runs;
- how it deals with people.

improve-jensen finds what holds Jensen back and proposes the changes in Slack. Once a person approves one, it ships it
itself, end to end: change, merge, deploy, check, and revert if the check fails. Continuous checking of Jensen's work
is the core.

**Nobody tells improve-jensen what to improve.** It is given the goal and the evidence, and it works out for itself
what Jensen needs, the way a sharp engineer would:
- reading Jensen's transcripts;
- auditing the code Jensen shipped;
- hearing what Jensen and people report;
- reading current research and practice.

Several times a day it decides what matters most and proposes work on that, from one-line fixes to multi-day changes
to Jensen's architecture. It and Jensen talk to each other: Jensen reports what gets in its way, and improve-jensen
tells Jensen what it changed.

improve-jensen runs outside Jensen's containers, on the host's self-hosted runner. A bad deploy can break Jensen, but
it can't break the agent that has to undo it.

## Where things stand (live install, 2026-11-18)

### improve-jensen today

- It is a Claude Code GitHub Action: `.github/workflows/improve-jensen.yml`, with its charter in
  `.github/improve-jensen.md`. It runs on a self-hosted runner on the host and reaches Claude through the proxy's
  `improve-jensen` origin.
- It wakes only on an `improve-jensen` issue or an `@improve-jensen` mention. It opens a PR, and its charter forbids
  merging, so people merge and deploy.
- The runner's user, `gha-improve-jensen`, has no Docker and no `data/`, so improve-jensen can't see Jensen's work or
  the live install. `gh` isn't on the host. `main` has no branch protection.
- Nothing watches Jensen. Core's events log records no failures. The evidence lives in `work_items.result`, the
  transcripts (`/var/agent/transcripts/<item>.jsonl` in agent-runner), Jensen's Claude session files and the repos
  Jensen works in.

### What Jensen ships

Jensen's one product build so far is a demo from a tender spec. It came to about 39k lines in 14 PRs over about 9
hours. The result looks polished, but it isn't enterprise-grade:
- **Security holes no review caught.** They include privilege escalation in the access-control work and sensitive
  records readable by any signed-in user. One hole was known, written up as an "observation", and merged anyway.
- **Parallel build lanes never reconciled.** They contradict each other on core domain rules, such as what
  "approved" means and who may do what. They left duplicated plumbing (five copies of one helper), unused "seam"
  code and clashing migration numbers.
- **Tests that can't fail.** Some return early when no database is present and pass green. Implementers wrote their
  own tests in the same commit. Front-end tests run against a 459-line mock that copies the backend.
- **CI never ran,** and Jensen didn't notice. Main carries failing tests and type errors, and Jensen called new
  failures "flakes" after comparing by hand.
- **PRs of 1–6k lines merged within a minute of opening,** reviewed only by same-model subagents in the same session.
  Commit messages claim fixes that aren't in the code. Departures from the spec and ADRs are listed as "assumptions"
  and then self-merged.
- **94 of 95 commits carry a "Co-Authored-By: Claude" trailer.**

### How Jensen works

These figures come from 436 turns over 11-14 to 11-18:
- **Background work dies when the turn ends.** Subagents and background shells live inside the turn's CLI process.
  - Parallel lanes were lost this way three times in one day.
  - Six headless `claude -p` lanes Jensen started itself failed at once with "Not logged in".
  - To keep turns alive, Jensen fills the main thread with sleep loops: 32 of them, about 290 minutes of timeout
    budget.
- **Context never resets.**
  - One build session grew from 24k to 794k tokens.
  - 41 turns started above 100k tokens, most of them small questions that paid for the whole carried context.
  - The fixed prompt overhead of a fresh session rose from 19.5k to 29k tokens in four days.
- **Little delegation.**
  - 3.9% of turns used any subagent.
  - Of the 13 turns with 40 or more tool calls, 5 used none and 5 used only a reviewer.
  - Delegation runs only through Superpowers. When a person told Jensen not to use Superpowers on one repo, five
    milestones were coded inline in the main context.
- **Code is written through Bash heredocs.** Bash makes up 75% of main-thread tool calls, and Edit was used twice.
  Whole files go into the context.
- **Project context is rediscovered.**
  - Each new conversation re-clones and re-learns the setup: 15 and 47 clone commands for two projects in four days,
    and 73 GB of scratch.
  - Project context skills loaded in only about half the conversations.
  - Project facts are split across a context skill, memory files and the repo's CLAUDE.md.
- **Unused toolkit.** 27 of 43 installed skills and 90 of 109 MCP tools were never used. That includes test-driven
  development, systematic debugging, verification, the design skills and Context7.
- **Long runs.** Velocity holds up, but cost per call grows four-fold and browser checks thin out in later milestones.
  The turn cap (90 minutes), the account limits hit by lanes running in one process, and lanes refused for
  unregistered projects cut builds short.

### How Jensen deals with people

From about 320 messages from people:
- **It goes quiet.**
  - Turns promise to report back and then end.
  - 53 messages fed into running turns got no direct reply.
  - Messages sent mid-turn wait up to 9 minutes behind a sleep loop.
- **It states guesses as facts** and retracts them later (about 10).
- **It doesn't know its own setup** (about 7).
- **It forgets standing instructions across threads.** One instruction was repeated in six threads.
- **It misreads asks or uses the wrong environment** (about 8).
- **Its environment has gaps.**
  - Tools are missing from the runner image.
  - Its GitHub token can't read CI checks (a 403 in 33 transcripts), which let it approve a PR over failing CI.

## Goals

1. **Every hour**, Jensen's new work is read in four areas: what it shipped, how it worked, how its long runs held up,
   and how it dealt with people. Each problem is recorded once, with its evidence and how often it happens.
2. **Every PR Jensen opens or merges, in any repo, gets an independent review within the hour.** The review reads the
   repo's CI results and reviews the diff for defects it can prove.
3. **Product defects go back to Jensen as work.** The gap in Jensen's process that let a defect through becomes an
   improvement to Jensen.
4. **A person approves each change before it ships.** improve-jensen posts the changes it wants as short bullets in
   Slack, and only an approved one is made.
5. **Approved improvements ship without a person,** to the same bar we ask of Jensen:
   - CI passes and a fresh-context review runs before merge;
   - the deploy is checked;
   - the change is reverted automatically if the check fails.
6. **The team can follow what improve-jensen saw, changed and why,** in issues and PRs, without being notified every
   hour.
7. **Every hour with new conversation since the last one, improve-jensen assesses Jensen against the goal** and ranks what is holding it back, with
   evidence, without anyone naming the areas. The top items become its proposals, including multi-day projects. It
   also asks what worked that Jensen should keep, and proposes that too.
8. **Jensen reports what gets in its way,** and improve-jensen replies and tells Jensen what changed.

## Non-goals

- Public benchmarks, large eval suites, replay harnesses or scoring dashboards. Jensen's real work is the test.
- Following a shipped change up to see whether it helped. A person approved it; beyond the deploy's check (§9),
  nothing measures it afterwards.
- improve-jensen fixing product code itself. The products are Jensen's, and their defects are handed to Jensen.
- Rubrics, checklists or build skills of our own. Quality comes from mechanical checks and maintained tools (see the
  off-the-shelf toolkit design). The one exception is a short skill that writes down a way people drove Jensen that
  worked, once a person approves it (§11).
- A scheduled scan of upstream releases. Each fix run checks whether an existing Claude Code feature or maintained
  plugin already solves its problem.
- Pruning Jensen's toolkit by usage. Skills like test-driven development and verification go unused because Jensen's
  process skips them. The fix is that process, not deleting the skills.
- Slack announcements of what improve-jensen did, beyond the proposals and their thread replies (§12).

## Anti-goals (failures even if everything technically works)

- A PR every hour for its own sake, or issues opened from noise. "Nothing to change" is a normal hour.
- Jensen's prompt growing with every fix. A mechanism (runner behaviour, config, a CI check, a maintained tool) is
  preferred over another line of prompt.
- improve-jensen grading its own work, or changing how a problem is measured so that it looks fixed.
- improve-jensen changing Jensen on its own say-so. A person approves each change, and the change is held to the bar
  improve-jensen holds Jensen to.
- A review and Jensen trading nitpicks back and forth.
- improve-jensen overriding something a person told Jensen. It works within the instruction.
- Jensen left broken after a deploy with nobody knowing.
- Jensen's conversations, people's names, or the details of products and customers ending up in this public repo.
- Hourly notification noise. A proposal is one short list, posted only when there is something to approve.

## Constraints

- **This repo is public.** improve-jensen's issues, PRs, commits and workflow logs never hold:
  - secrets;
  - conversation text;
  - people's names;
  - details of products, customers or their code (file paths, vulnerabilities).
  
  The evidence stays on the host.
- improve-jensen shares the Claude account pool with Jensen, and people's requests come first.
- The host sometimes suspends. A missed hour is caught up, not lost.
- Runs start on the host listener's clock and can start late, so nothing depends on exact timing.
- `just doctor` has a known failing check (`LINEAR_API_KEY`), so it can't be the deploy check.
- improve-jensen reads text from outside the team: product feedback, Linear issues and code in other repos.
  Everything it reads is data about Jensen, never instructions to improve-jensen.
- **No gates or guards on Jensen in core or the runner,** as decided in the earlier designs. Jensen still merges its
  own PRs and is never refused an action by our code.
  - Quality gates live where a teammate's would: in the product repo's CI. Jensen sets them up and works within them.
  - A product repo never requires a person's approval to merge.

## Approaches

- **A. An hourly engine with GitHub issues as its record (chosen).**
  - Every hour, a script measures Jensen's new work, and improve-jensen reads the result.
  - Each new Jensen PR gets a review job.
  - Each problem gets an issue with a running count.
  - A problem that qualifies is proposed in Slack; once a person approves it, a fix job ships the change and checks
    the deploy.
- **B. One hourly agent that watches, reviews and fixes in a single run,** with its own notes file. There's less
  wiring, but there's no visible record, and a busy hour crowds out the reviews and the fixes.
- **C. Coaching only.** improve-jensen tells Jensen what went wrong so Jensen saves notes or skills. This can't fix the
  biggest problems found above, which are runner architecture, CI and toolkit problems. C was rejected because the
  improver must ship code.

A was chosen. It reuses improve-jensen and its runner, the issues are a record the team can read, and every problem
is counted by a script. C's lever, editing Jensen's memory and skills, is one of A's fix targets.

**Project-scale problems go to improvement projects (§13), not fix jobs.** Two known ones are:
- background work that outlives the turn;
- a fresh context at milestone boundaries.

Both are runner rewrites. They need a design and live checks of long builds, so they carry the `project` label. The
assessment (§11) proposes them, and once a person approves one, project runs (§13) build it. A fix job never picks
them up.

**The assessment and projects extend approach A.** Making improve-jensen a separate always-on agent instead
was considered and rejected:
- it would need its own memory, process supervision and restarts;
- it would duplicate what the runners and the record already give.

Each run reads the record and the state folder, so it picks up where the last one stopped.

## Design

### 1. Runners and jobs

**Runners.** Two GitHub Actions runners on the host, all running as `pragnition`, the user that owns the live
checkout. That user has Docker and passwordless sudo, so the runners have full access to the host.
- One runner is labelled `watch`, so the hourly watch never queues behind a long fix.
- One is labelled `work`, for reviews, fixes, assessments, approvals, projects and the runs that answer issues; they
  take turns. Most of the host belongs to Jensen, so improve-jensen runs at most one job besides the watch, and both
  runners run below Jensen on CPU and disk (systemd `CPUWeight=20`, `IOWeight=20`, `Nice=10`).
- The runners' work folders stay outside `~/autonomous-agent`.
- `gh` is installed on the host.

**Tokens.** improve-jensen reads Jensen's work on GitHub with Jensen's own token (`GH_TOKEN` in `data/secrets/agent.env`).
- Today that token gets a 403 on CI checks.
- A person widens it to read checks, commit statuses and Actions on the repos Jensen works in. This is a one-time
  switch-on step, and it fixes the same gap for Jensen.
- Until then, CI status is recorded as `unknown`, never as "didn't run", so no finding rests on it.

**One workflow, `.github/workflows/improve-jensen.yml`, with these jobs:**

| Job | Runs on | Started by | Does |
|---|---|---|---|
| `watch` | `watch` | the listener's `improve-jensen-watch` at :17 every hour, `workflow_dispatch mode=watch` | measures the hour; Claude reads it, updates the record, and picks the PRs to review and the issues to propose; a step posts the proposals in Slack (§12) |
| `review` | `work` | `needs: watch`, one matrix entry per new Jensen PR; `workflow_dispatch mode=review` | reviews one PR, hands defects to Jensen, records process gaps |
| `approvals` | `work` | the listener's `improve-jensen-approvals` every 10 minutes and after each decision in Slack, `workflow_dispatch mode=approvals` | no Claude: applies the decisions from the Slack threads, labels issues approved or rejected, and names the next approved fix and project (§12) |
| `fix` | `work` | `needs: approvals` when it named an issue; `workflow_dispatch mode=fix` | ships one approved improvement: change, PR, CI, review, merge, deploy, check, revert |
| `respond` | `work` | today's issue and comment triggers | answers people's and Jensen's `improve-jensen` issues under the fix rules, with no Slack approval |
| `assess` | `work` | the listener's `improve-jensen-assess` at :47 every hour, `workflow_dispatch mode=assess` | the assessment (§11): ranks what holds Jensen back, files or updates the issues, and proposes the top items |
| `project` | `work` | `needs: approvals` when it named one; `workflow_dispatch mode=project` | one approved improvement project (§13), which can carry on over several runs |

- Only people with write access can trigger a run, which is the action's default. That matters because every run has
  the host.
- All runs use the `improve-jensen` proxy origin and `claude-opus-5-5`, and use subagents freely.
- The `fix` job is in one concurrency group, so at most one fix runs, with at most one waiting.
- The approvals job starts a fix only when no other fix job is queued or running. The fix job first checks that its
  issue is still open and approved (`jensen_watch.py ready`), adds the `fixing` label when it starts and always
  removes it when it ends.
- Deploys from any job are serialised by a host lock (§8).
- At most one `project` run at a time, in its own concurrency group. A project and a fix can run side by side, and
  their deploys queue on the host lock.

### 2. Measuring the hour: `scripts/jensen_watch.py`

The watch job's first step runs `jensen_watch.py measure` on the host.

**Window.** It covers the time from the end of the last committed window (`state.json` in
`~/.local/state/improve-jensen/`) to now. A step after a successful Claude run commits the window, so a failed run
measures the same hour again next time.

**Sources:**
- core's DB, through `docker exec agent-core` (work items, conversations, projects);
- the transcripts of the turns in the window, through `docker exec agent-runner`;
- PRs opened or merged by Jensen's GitHub user, through `gh` with Jensen's token.

**Signals.** A small fixed set, each tied to a problem found on 11-18. They are computed per turn and per PR and
appended to `metrics.jsonl`:

| Signal | What it counts |
|---|---|
| `lost_background` | background tasks reported stopped or failed because the session ended, or by a usage limit; and background tasks still running when the turn's result came |
| `turn_failed` | turns started by a person that timed out, were interrupted, errored, or ended with empty text |
| `midturn_wait_s` | how long a message sent mid-turn waited before the running turn read it |
| `context_first`, `context_peak` | context size at the turn's first and largest API call; `big_start_small_ask`: a first call over 100k on a turn of 5 or fewer tool calls |
| `no_delegation` | turns with 40 or more main-thread tool calls and no subagent |
| `inline_code_chars` | characters of files written from the main thread through Write or Bash heredocs |
| `tool_errors` | tool errors, normalised (paths, numbers and ids stripped) and counted, with `missing_tool` (command not found, no module) broken out |
| `clones` | repo clones in a conversation's first turn on a repo it has cloned before |
| `pr` | for each Jensen PR: repo, lines changed, seconds from open to merge, CI state at merge (`passed`, `failed`, `none`, `unknown`), Co-Authored-By trailers |

New signals are added only in a PR of their own (§7).

**The digest** (`digest-<window end>.md`) is the readable account of the window.
- For each conversation touched:
  - people's messages and Jensen's replies, in full;
  - one line per tool call: the tool's name plus the first 300 characters of its input and output;
  - tool errors, up to 500 characters;
  - the turn's signals.
- What Jensen added in each turn (installs, and the skills, memory, instructions and settings files it wrote), and
  the skills it used in the window.
- The window's totals for each signal.
- The new Jensen PRs.
- It stays under about 100k tokens by shortening tool lines first. People's messages and Jensen's replies are never
  dropped.

`metrics.jsonl` and the digests are kept for 30 days.

**Other subcommands** used by the runs:
- `count <signal> --since <time>` gives the evidence numbers for an issue. Issues quote this output, never a count
  improve-jensen made by hand.
- `note <issue> <ref> <line>` records an occurrence on the host and prints the issue's count.
- `seed <file>` files the starting issues (§5).

**When Claude doesn't start.** `measure` reports `has_work=false`, and Claude never starts, when either:
- the window has no finished turns, no new PRs, no notes from Jensen and nothing that needs attention; or
- core's usage watch is holding scheduled runs because the pool is low.

### 3. The watch run

improve-jensen reads the digest and the open `jensen-watch` issues. It uses a subagent per area (ships, works, long
runs, people) when the window is busy. Then it:
- **Records problems.** For each problem it sees, it notes the occurrence (`note`) on the matching issue and updates
  the count line in the issue's description, or opens a new issue. Editing a description notifies nobody.
- **Follows pending deploys.** When a deploy that was left pending lands, it comments `Deployed` on the issue and
  tells Jensen what changed (§14).
- **Flags a broken deploy.** A failed deploy check that no run reverted gets `needs-person`, an @-mention of the
  admins and a Slack DM. The watch never reverts anything itself.
- **Closes stale issues.** A problem seen once and not again within 7 days is closed.
- **Writes `plan.json`** in the state folder: the PRs to review, and up to five qualifying issues (§6) to propose,
  each with one short line saying what will change in Jensen. Shipped harm and lost work come first, then how Jensen
  works, then problems with people. A step after the run posts them in Slack (§12).

It comments on an issue only when its state changes, and posts nothing in Slack itself.

### 4. The review job (one Jensen PR)

improve-jensen checks out the PR's repo with Jensen's token. It:
- **Reads CI.** It checks whether the repo's CI ran on the PR and its result. A repo with no CI, or CI that never runs,
  is itself a finding. The review doesn't re-run product test suites on the host.
- **Reads the tests.** It looks for tests that can't fail: suites that skip or return early, tests with no assertions,
  and tests that only exercise mocks of the code under test.
- **Reviews the diff with the maintained review agents** (pr-review-toolkit's code reviewer, silent-failure hunter and
  test analyser), plus a security pass on auth, data access and input handling. The review also checks the diff
  against the rest of the codebase, including other recent PRs from parallel lanes, and against the repo's spec, plans
  and ADRs.
- **Judges how the PR was made:** its size, the time from open to merge, and whether its commit messages match the
  code.

**Only provable defects are reported.** Each needs a failing test or command, a contradiction shown at a file and line,
or a check result. Style opinions and "consider" notes are dropped, so the review and Jensen never trade nitpicks.

Then:
- **Defects go to Jensen as work,** through `scripts/ask_jensen.py`, which queues a turn in core the way
  `scripts/smoke.py` does.
  - The turn goes into the conversation that made the PR, found through the PR's `code_change_published` event in the
    transcripts, or into the long project's conversation for a build.
  - Jensen gets the findings in the session that already knows the work. No second session starts in the same repo,
    and a running build gets them as a mid-turn message.
  - The message says the findings come from improve-jensen's review, lists them with file and line, and asks Jensen to
    fix them in that repo. It also asks Jensen to say only briefly in the thread what it fixed, with no details of
    security holes.
  - Core is unchanged, and improve-jensen itself posts nothing.
- **The gap in Jensen's process behind each defect goes into the record,** in general terms. For example: "merges with
  CI not running", "parallel lanes merged without an integration pass", "tests written by the implementer in the same
  commit".

### 5. The record: GitHub issues

- **One issue per problem.** Each is labelled `jensen-watch` and with its area (`ships`, `works`, `long-runs`,
  `people`). The title states the problem in plain words. Something that worked and that Jensen should keep is
  labelled `jensen-watch` and `learned` instead (§11).
- **The description** says:
  - what goes wrong, in general terms;
  - why it matters;
  - the evidence: the signal and its numbers from `count`, or the recurrence count, plus the value that would mean
    it's fixed;
  - where the fix probably belongs;
  - a count line that the watch keeps current.
- **State labels:**
  - `proposed`: posted in Slack, waiting for a person (§12).
  - `approved` or `rejected`: a person's answer in Slack.
  - `fixing`: a fix job has the issue.
  - `needs-person`: only a person can make the change.
  - `stuck`: an approved change that didn't ship; a person looks at it.
  - `project`: built as an improvement project (§13), never by a fix job.
- **One pinned issue, labelled `jensen-assessment`, holds the current ranked assessment** (§11) in general terms.
  Each assessment rewrites its description rather than commenting.
- **Comments** go out only on state changes: proposed, approved, deployed, failed, stuck.
- **Seeding.** `deploy/improve-jensen-seed.json` holds the problems listed under "Where things stand", in general
  terms. `jensen_watch.py seed` files them at switch-on if they aren't open already.

### 6. When a problem qualifies for a fix

Any one of these is enough:
- It was seen twice: in two conversations, or in two PRs.
- A person corrected Jensen about it or complained.
- It did visible harm: shipped a security hole, lost work, posted in the wrong place, approved over failing CI, or
  crashed a turn.

A note Jensen writes about the problem (§14) counts as one occurrence. A note about visible harm qualifies on its
own, like a person's complaint.

Issues labelled `stuck` or `needs-person` never qualify, and one already `proposed`, `approved` or `rejected` isn't
proposed again. `project` issues go to the assessment (§11) and run as improvement projects (§13).

### 7. The fix job

The fix job takes the next approved issue. Its `Proposed in Slack:` comment says what was approved: the job makes
that change, nothing wider. It reads the issue and its evidence on the host, finds the cause, and makes the change
that removes it. **First,** it checks whether an existing Claude Code feature, setting or maintained plugin already
solves the problem.

**Where the change goes**
- **The repo:** core, runner, the system prompt, core plugin skills, the Dockerfile, compose and the dashboard.
  - It works on a branch, adds or updates tests, and opens a PR that says `Closes #N`, so the issue closes on the
    merge.
  - A change to how turns run comes with a short design note in the PR.
- **Host config in `data/`** (`agent.toml`, schedules, project registrations): a dated backup next to the file, then a
  restart of whatever reads it. Secrets are never printed or written.
- **Jensen's Claude home** (memory, skills, CLAUDE.md, plugins, settings such as the commit trailer):
  - a dated backup first;
  - it follows Jensen's own memory format;
  - it installs only maintained plugins and skills, after checking their hooks and MCP servers are safe in the
    headless runner (as the toolkit design requires).
- **Jensen's products,** when a process change also needs doing there, such as turning on CI in a product repo: the
  fix job asks Jensen through `ask_jensen.py`, and a later watch checks that it happened.
- **Things only a person can do,** such as a credential, a token scope or a repo setting Jensen's account can't
  change: the issue gets `needs-person` and an @-mention of the repo's admins, with the exact ask.

**Rules**
- One improvement per PR.
- Prompt and memory stay small. `src/agent/defaults/system.md` stays under 12 KB (10.4 KB now) and Jensen's MEMORY.md
  under 150 lines. An edit that would cross either limit merges or removes text elsewhere.
- An improvement never changes `scripts/jensen_watch.py` or how its own evidence is measured. Changes to measurement go
  in a PR of their own, with an issue of their own.
- Runs started by people's and Jensen's issues (`respond`) are a request in their own right and need no approval in
  Slack. They follow the same rules, merge and deploy too, and their PRs still say who asked.

### 8. Merge, deploy, check, revert

**Merge, to the bar we hold Jensen to:**
- **CI must pass.** This repo's CI (`.github/workflows/ci.yml`: pytest, and the dashboard's tests and build) runs on
  the PR. `main` requires it, through branch protection set at switch-on. The fix job waits for it with
  `gh pr checks --watch`.
- **A fresh-context review.** A subagent that didn't write the change reviews the diff with pr-review-toolkit. Findings
  it can prove are fixed before merge.
- **Then the job merges.**

**Deploy:** `scripts/deploy.py`, run as `just deploy`.
- It holds a host lock (`flock`), so two jobs never deploy at once.
- It fetches `origin/main` into `~/autonomous-agent` and works out from the changed paths which services to rebuild:
  - `src/agent/runner/` goes to `agent`;
  - the supervisor goes to both supervisors;
  - the rest of `src/agent/`, the dashboard, prompts and plugin skills go to `core`;
  - shared modules, the Dockerfile and compose go to every service they touch;
  - docs and tests go to nothing.
- **Timing.** core, prompt and skill changes deploy straight away. A change that rebuilds the `agent` service waits
  until no turn is running (no claimed work item).
  - If none comes up within 30 minutes, the deploy does nothing and reports `pending`.
  - Every later watch run retries a pending deploy until it goes through.
  - Nothing is pulled until the deploy goes ahead.
- It fast-forwards, rebuilds those services, and records the deploy in `deploys.jsonl`.

**Check after every deploy:**
- the rebuilt containers are running and their restart counts don't move for 2 minutes;
- core's `/health` and `/health/runner` answer 200;
- `scripts/smoke.py` prints `smoke ok`.

**Revert when a check fails:**
- The fix job reverts the merge commit on `main`, pushes and runs `just deploy` again. Then it reopens the issue and
  comments; the workflow marks it `stuck` and says in its Slack thread that the change didn't ship.
- Host-config and Claude-home changes are reverted by restoring their backups.
- If the redeploy's check also fails, the admins in `agent.toml` get a Slack DM through Jensen's bot token. Besides the
  proposals and their thread replies (§12), it is the only Slack message improve-jensen sends.

**Before switch-on:** one live check that a running turn survives a core rebuild. The turn keeps running, or is
re-queued, resumes its session and finishes. If it doesn't survive, core changes also wait for no running turn.

### 9. Did it work

The deploy's check (§8) is the last check on a change. One that passes it is done: its issue closed on the merge, and
nothing measures it afterwards. One that fails it is reverted, and its issue is reopened and marked `stuck` for a
person.

### 10. Cost and the account pool

- An hour with nothing new makes no Claude call.
- While core's usage watch holds scheduled runs, no watch, review or assess job starts Claude, so nothing new is
  proposed, and the approvals job starts no fix or project (it still records people's answers in Slack).
- An assessment with no new conversation since the last one (no turn of Jensen's and no note from Jensen) makes no
  Claude call.
- The approvals job makes no Claude call. Fixes and projects run only once a person approves them.
- The proxy's usage page shows improve-jensen's share separately, because its origin isn't in `agent_origins`.

### 11. The assessment: what Jensen needs

The `assess` job runs every hour when there's new conversation since the last one. It is where improve-jensen works out what to improve, without anyone naming
the areas.

**It reads:**
- the digests and metrics since the last assessment;
- the open and recently closed `jensen-watch` issues and their counts;
- Jensen's own reports and improve-jensen's replies to them (§14);
- the review findings on Jensen's PRs since the last assessment;
- its previous assessment;
- current research and practice. It may read lab engineering blogs, papers, and Claude Code and plugin release notes,
  but only to understand or solve a problem it already has evidence for.

**It judges everything against the goal:** Jensen ships enterprise-grade products, with little human help, over long
runs. Rank order:
1. harm in what Jensen ships;
2. what stops long runs finishing;
3. what makes people step in;
4. everything else.

**Every item needs evidence from Jensen's own work:** a count, a review finding, a Jensen report or a person's
correction. Research can suggest a fix or a better way, but it never puts an item on the list by itself.
The list stays at 10 items or fewer.

**It writes:**
- **`assessment.md` in the state folder** (private). For each item:
  - the problem, and its evidence with references;
  - why it matters for the goal;
  - what would probably help, and whether that's a fix or a project.

  Earlier versions are kept with their dates.
- **The pinned `jensen-assessment` issue:** the same ranked list in general terms (the public-repo rule applies).
- **New `jensen-watch` issues** for items it has no issue for, labelled `project` when they're project-scale.
- **Its proposals** in `assess-plan.json`: up to three of the top-ranked items, fix-scale or project, that aren't
  already proposed, approved, rejected, stuck or being fixed, each with one short line saying what will change in
  Jensen. A step after the run posts them in Slack (§12). A proposed item needs no other qualification: the
  assessment's evidence is enough. Up to two learned items (below) go in the same list.

**What worked.** Next to what holds Jensen back, the assessment asks "what worked that Jensen should keep?". It reads
the digests' "What Jensen added" sections and conversations, the skills and `CLAUDE.md`/`AGENTS.md` files in Jensen's
checkouts, its Claude home and its memory, and looks for:
- ways people drove Jensen that worked (the outcome was accepted, with little back-and-forth) and that repeat, in two
  sessions or from two people;
- skills, plugins or tools installed or written for one project or one person that would help Jensen everywhere;
- instructions people repeat across threads or projects.

Each becomes a `learned` issue in general terms. In Slack, a learned item is marked as such and credits whose
session it came from; the credit never reaches GitHub. A third-party skill or plugin is held to the same checks as
any install (maintained, safe in a headless runner). Once approved, a fix run ships it: a skill or plugin is
installed for every project in Jensen's Claude home, and a way of driving or a repeated instruction becomes a short
skill in the core plugin, or one line in Jensen's prompt. Manual approval keeps the toolkit from bloating. The
hourly watch proposes problems only.

It skips the hour when nothing is new since the last assessment and no project is active. It posts nothing in Slack
itself.

### 12. Approval in Slack

improve-jensen changes Jensen only once a person approves the change.

**The proposal.** After the watch or the assessment, a workflow step (`jensen_watch.py propose --from watch` or
`--from assess`) posts one message through Jensen's Slack bot to `#autonomous-agent` (the repo variable
`IMPROVE_JENSEN_SLACK_CHANNEL` can name another channel):

```
*improve-jensen wants to change Jensen*
1. <what will change in Jensen> (#31)
2. <what will change in Jensen> (#40)
3. Learned: <what Jensen will keep> (from <name>) (#52)
Reply here: `approve` ships them all, `approve 1 3` ships some, `reject 2` drops one.
```

Each line says in general terms what will change, not what goes wrong. Each issue gets the `proposed` label and a
`Proposed in Slack: <change>` comment. Issues that are closed or already in the flow are left out, and nothing is
posted when none are left.

**Approving.** A person replies in the message's thread in plain language: approve all or some, drop one, add a
condition, or ask about an item. The host listener (`jensen_watch.py listen`, a systemd service on the host, every
15 seconds) puts 👀 on the reply at once and has Claude (through the Claude Code CLI and the proxy's
`improve-jensen` origin) read it against the items, their issues and the thread so far. Claude decides only what the
person clearly asked for: `approve` or `go ahead with all` means every item still open, a question gets an answer
with no decision, and a condition on an item ("but keep it short") becomes that item's note, in general terms. The
listener answers in the thread with what it took and what happens next, stores the decisions in the proposal's record
and starts the approvals job at once. If Claude can't be reached, the thread hears so, and the plain words still
work: `approve` (or `yes`, `ok`, `lgtm`, `ship it`) approves every item, `approve 1 3`, `approve 1-3` some, and
`reject 2` (or `no`, `skip`, `drop`) drops one; a plain reply counts only when it is exactly the word and item
numbers. A reaction on the message itself answers for every item: `:white_check_mark:` or `:ballot_box_with_check:`
approves, `:x:` or `:-1:` rejects; a `:+1:` approves nothing, since it often means "seen". A decision that names an
item decides it either way; one that names none (a bare `approve` or a reaction) only decides items still waiting, so
it never turns a dropped item into an approved one. An approved item is final.

A proposal nobody answers within 14 days is released: its undecided items lose `proposed`, with a comment, and its
thread is no longer read, so a later watch can propose them again. Each issue belongs to its newest proposal, so an
old one never touches a newer proposal of the same issue. An issue labelled `proposed` that no live proposal on the
host names (its record lost, unreadable or already released) is released the same way.

**Who can approve:** the people in `agent.toml`, by the `slack` id of each `[people.*]` entry. Anyone else's reply or
reaction is ignored, and if the list can't be read, nothing is approved.

**The approvals job** runs every 10 minutes, and at once after the listener stores a decision, and makes no Claude
call (`jensen_watch.py approvals`). It applies the listener's stored decisions first, moving each issue from
`proposed` to `approved` or `rejected` with a comment (and an approval's note as `Note from Slack: <note>`), quietly,
since the listener already answered. Then it reads the plain replies the listener hasn't handled and the reactions,
applies them the same way, and replies once in the thread with what those took (`Approved 1, 3: queued to ship.
Dropped 2.`). Then it fires the next change: the lowest-numbered approved issue goes to the `fix` job, and the
lowest-numbered approved `project` issue to the `project` job, each only when no job of that kind is queued or running in any unfinished run of the workflow,
however long ago it started. While core's usage watch holds the pool, it still records the answers but fires
nothing. The `fix` or `project` run it starts first checks that the issue is still open and approved, with no
`stuck` or `needs-person` (`jensen_watch.py ready`), and does nothing otherwise; a person's own dispatch with a
target skips that check.

**After the run,** the last step of the `fix` and `project` jobs (`jensen_watch.py fired`) answers in the proposal's
thread:
- the issue is closed: `Shipped <n>: <change>.`;
- a fix whose issue is still open didn't ship: the issue gets `stuck` in place of `approved`, and the thread hears
  `Couldn't ship <n>: <change>. It needs a person (#N).`;
- a project whose last two runs failed is marked `stuck` in place of `approved`, with a comment, so it doesn't run
  again and again; a run that succeeds starts the count afresh.
- a project that needs a person (`stuck` or `needs-person`) says so once (`<n> needs a person (#N).`). An
  unfinished project says nothing, and the next approvals run starts its next run.

A run started by an `improve-jensen` issue or an `@improve-jensen` mention (`respond`) is a person's own request and
needs no approval.

### 13. Improvement projects

A project is a `project` issue the assessment proposed and a person approved in Slack (§12). The `project` job runs
improve-jensen with `.github/improve-jensen-project.md`.

**The flow:**
1. A short design note, posted as a comment on the issue before building. It's visible to people, but nothing waits
   for them.
2. A plan and a progress file on the branch `improve-jensen/project-<n>`. The file names the steps, what's done, the
   decisions and what's next, in general terms.
3. Building with subagents, tests first, in small reviewed steps.
4. A PR, CI, a fresh-context review, merge, `just deploy`, and the deploy check (§8). Stages ship back to back.
5. The last stage, or a decision to stop, closes the issue.

**Carrying on.** A `project` run lasts up to 6 hours. When it ends unfinished, the progress file on the branch carries
it, and the next approvals run starts the next one. After 5 days the assessment must decide whether to continue, split
or drop the project, and it says which on the issue.

Only one project runs at a time. Projects follow every fix rule in §7 and §8.

### 14. Jensen and improve-jensen talk to each other

**Jensen reports.**
- When something in its own setup gets in its way, Jensen writes a short note to `/var/agent/feedback/` in its
  container: `YYYY-MM-DD-<slug>.md`. Examples: a missing tool or access, a limit it hit, background work it lost, an
  instruction or skill that didn't fit, something it had to work around.
- The note says what it was doing, what got in the way and what would have helped.
- One line in Jensen's prompt (`src/agent/defaults/system.md`) asks for this. Jensen carries on working and doesn't wait
  for an answer.

**improve-jensen reads them.** `measure` collects notes written or changed in the window into the digest's "Jensen
reports" section. They are evidence like any other (§6), and the assessment reads them all.

**improve-jensen answers.**
- It appends a `Status:` line to the note: the issue it went into, the fix that shipped, or why nothing will change.
- After each deploy that changes how Jensen behaves, it adds a dated line in plain words to
  `/var/agent/feedback/CHANGES.md`, newest first, keeping the last 30. Jensen's prompt line says to read that file when
  something about its setup seems different from what it remembers.
- Review findings keep going to Jensen through `ask_jensen.py` (§4).

The notes stay on the host. Public issues describe them in general terms only.

## Switch-on

The build ships nothing to the live install. When Jensen is idle and a person is around:
1. Widen Jensen's GitHub token: checks, statuses and Actions, read.
2. Install `gh` on the host, and re-register the runner as two runners running as `pragnition`: one labelled `watch` and
   one labelled `work`.
3. Merge the PR. Protect `main` so that CI is required.
4. Run the live core-rebuild check (§8).
5. `just deploy` once by hand, then `jensen_watch.py seed`.
6. Check that Jensen's Slack bot is in `#autonomous-agent` (it joins a public channel by itself).
7. Start the first watch with `workflow_dispatch mode=watch` and read its digest, plan and proposals.
8. Start the first assessment with `workflow_dispatch mode=assess` and read `assessment.md`.

9. Install and start the host listener (`deploy/improve-jensen-listen.service`): the workflow's clock, since
   GitHub's cron never fires it, and the ear in the proposals' threads. It exits when the live checkout moves, and
   systemd starts it on the new code.

`deploy/improve-jensen.md` holds the exact commands.

## Files that change

- `.github/workflows/improve-jensen.yml`: the `watch`, `review`, `fix` and `respond` jobs.
- `.github/workflows/ci.yml` (new): pytest and the dashboard's tests and build, on GitHub-hosted runners.
- `.github/improve-jensen.md`: the fix and respond charter. "Don't merge your own PRs" and "Don't touch `data/`" are
  replaced by the rules above.
- `.github/improve-jensen-watch.md` and `.github/improve-jensen-review.md` (new).
- New scripts, with tests: `scripts/jensen_watch.py`, `scripts/ask_jensen.py` and `scripts/deploy.py`, plus a
  `just deploy` recipe.
- `deploy/improve-jensen-seed.json` (new).
- `deploy/improve-jensen.md`: the runners, `gh`, tokens, state folder and switch-on steps.
- `docs/design.md`: an improve-jensen section.
- `.github/improve-jensen-assess.md` and `.github/improve-jensen-project.md` (new): the assessment and project
  instructions.
- The `assess`, `approvals` and `project` jobs in the workflow.
- `src/agent/watch/feedback.py`, `src/agent/watch/slack.py` and `src/agent/watch/approvals.py` (new), with CLI
  subcommands and tests.
- `src/agent/defaults/system.md`: one line asking Jensen to report what gets in its way, and to read `CHANGES.md`.

If the Claude GitHub App's token can't change files under `.github/workflows/`, changes to workflow files come from a
person.

## Left to the builder

- The cron minute, label colours, digest layout and the exact wording of the instructions files.
- How each signal is computed from the stream-json. Starting points: the 11-18 analysis scripts. Transcripts carry no
  timestamps, so durations come from the DB.
- How tool errors are normalised.
