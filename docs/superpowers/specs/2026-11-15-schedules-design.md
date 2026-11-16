# Schedules

## Why

The agent only works when something hands it work: a Slack message or a Linear issue. Nothing starts a turn on a
timer, so it can't keep working through a project unattended, run a recurring sweep, or do something "every
morning". Claude Code's own cron and `/loop` don't help: they fire only while one `claude` process is alive, and each
turn is one process that exits when the turn ends.

Schedules are a timer ingress in core. Anyone asks the agent in Slack for something on a schedule; the agent writes
the schedule into a file with its own tools; core fires each due entry as a turn through the normal queue.

The first use is keeping the agent working on the first project around the clock: a sweep every few hours
dogfoods the app and files each breakage as a Linear issue labelled with the agent's name, and the existing Linear
poller turns each issue into its own session that fixes it.

## Goals

- People set up, change, pause and remove schedules by asking the agent in Slack, or by editing the file by hand.
- A due schedule becomes an unattended turn in a new Slack thread, through the same queue as everything else: stop,
  the proxy's capacity, `max_concurrent_sessions`, the dashboard and the KPIs all apply unchanged.
- People can follow up in a scheduled run's thread like any other conversation.
- The agent can check a schedules file itself before relying on it.

## Non-goals

- No new core tool. The agent edits a file.
- No schedules page on the dashboard.
- No catch-up of every slot missed while core was down (at most one firing on return, below).

## Approaches

- **A. `[schedules.<name>]` in `agent.toml`.** An admin edits config; core reads it on restart. Predictable, but
  the agent and teammates can't add schedules.
- **B. A schedules file the agent owns (chosen).** The agent edits it with its own tools when someone asks in
  Slack; people can edit it by hand on the host. Core re-reads it every minute. No core tool in the agent's path,
  and teammates set up schedules in plain language.
- **C. A core tool and a table** (`schedule_create/list/delete`). Validated in code and easy to list on the
  dashboard, but it is a core tool in the agent's path, which the design avoids.

## Design

### The file

One TOML file, `data/schedules/schedules.toml` on the host, bind-mounted read-write at the same path,
`/data/schedules`, in core and the agent container. Core creates the folder at start and gives it and its files to
the agent's uid; it never writes the file.

Each table is one schedule; its key is the name (`[a-z0-9-]+`, at most 40 characters):

```toml
[project-sweep]
cron = "0 */3 * * *"          # five-field cron, in agent.toml's timezone
channel = "#project-dev"      # where each run's thread starts: a channel name or ID, or a person key for their DM
prompt = """
Dogfood <project> on main and file each new breakage as a Linear issue labelled jensen.
Skip anything already open.
"""
by = "sam"                    # who asked; the turn's actor when it is a person key
link = "https://…"            # where they asked (optional)
paused = false                # optional
```

`cron`, `channel`, `prompt` and `by` are required text; `link` is text and `paused` is true or false. Unknown keys
are an error for that entry. `by` isn't checked against `[people]` (the agent container has no `agent.toml`): a person
key becomes the turn's actor, anything else fires with no actor and shows as written.

### Firing

A new core loop runs every 60 s next to the existing ones:

1. Read and parse the file. A missing file means no schedules. If the file doesn't parse, keep using the last
   valid set. Entries with errors are skipped; valid entries still fire.
2. For each valid, unpaused entry, find the latest cron slot at or before now. Core records the last slot it handled
   per name (`settings`: `schedule.<name>.last`). If the latest slot is newer, fire once and record it. Older missed
   slots are skipped, so a core that was down fires each schedule at most once when it comes back.
3. A schedule core sees for the first time (new, renamed, or back after leaving the file or being invalid), or whose
   `cron` changed, records the current slot without firing; it first fires at its next slot. (`schedule.<name>.last`
   holds the slot and the cron.)
4. While the schedule is `paused` or the agent is stopped, due slots are recorded but not fired, so unpausing or
   resuming never fires a backlog.

A firing:

- posts nothing: the run messages people itself when it has something for them, and its final message goes
  nowhere;
- starts a new conversation with no Slack thread; a run that fails says so in the entry's channel (a person key
  resolves to their DM), and replies in that message's thread continue it;
- queues an unattended turn with the entry's prompt, actor `by`, and the first line
  `[schedule <name> · <cron> · set up by <name of by> · <link>]`.

Each firing is its own conversation, so a run still going when the next slot comes doesn't block it; both run in
parallel within `max_concurrent_sessions`.

### Errors

Errors are the file's (not TOML, invalid entries) and firings that couldn't be queued (retried every
minute). Core DMs each admin the list once per distinct text (a hash in
`settings`: `schedules.errors_hash`) and logs it. No errors clears the hash.

### Checking the file

`python -m agent schedules [--file PATH]` parses the file and prints each schedule with its next three firing
times, then any errors; it exits non-zero when there are errors. It runs in the agent container (the agent checks
its edits) and on the host as `just schedules`. It uses `agent.toml`'s timezone where it can read it (core), else
`/run/agent/timezone`, which core writes at start for the agent container, else UTC.

### The agent's side

- One line in `system.md`: people can ask for work on a schedule; use the schedules skill.
- A `schedules` skill in the core plugin: the file's path and format, record who asked and the link, run
  `python -m agent schedules` after every edit and fix what it reports, reply with the next firing times, pause with
  `paused = true` and remove by deleting the table.

### PRs

The agent's PRs look like any teammate's: a new branch per piece of work named the repo's way, a PR to the default
branch with the repo's usual labels, and no marking as agent work; the GitHub account shows who opened it.
`system.md` says so, and the `pr-description` skill no longer opens the PR body with "you're an AI agent".

### Docs

- `docs/design.md`: a Schedules section, the new mounts and loop, and the CLI.
- `name` in `agent.toml` and `docs/design.md` is the agent's name and Linear label; drop "branch prefix", which no
  longer applies.

## Constraints

- Core's state stays in `agent.db`; the file is only the schedule definitions.
- Nothing about a schedule can fail a turn that has already been queued; a firing that can't be queued is logged
  and retried on the next tick (the slot is recorded only after the turn is queued).
- The cron parser is a small pinned dependency (`croniter`) in `pyproject.toml`, so the core and agent images both have it.

## Left to the builder

- Where the loop and parser live in `src/agent/core/`, and test layout.
- The error DM wording.
