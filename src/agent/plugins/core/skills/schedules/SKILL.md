---
name: schedules
description: Use when someone asks for work on a schedule ("every morning at 9", "every night", "every 3 hours"), or to change, pause, list or stop a schedule.
---

# Schedules

Core runs every schedule in `/data/schedules/schedules.toml`. Each run is a new session in a new Slack thread, with the schedule's prompt as its message. You set schedules up by editing that file.

## The file

One table per schedule; the table's name is the schedule's name (lowercase letters, digits and dashes):

```toml
[pr-stats]
cron = "0 9 * * *"                 # five-field cron, in the agent's timezone (the check prints it)
channel = "sam"                    # a person key for their DM, or a channel ("#project-dev" or its ID)
prompt = """
Send PR stats for <owner>/<repo>: opened, merged and still open since yesterday, and review times.
"""
by = "sam"                         # the asker's person key, from the first line of their message
link = "https://…"                 # the link from that first line
paused = false                     # optional
```

- **channel**: "send me" or "DM me" means the asker's person key. Otherwise the channel they named, or the channel they asked in. The bot must be in the channel; join a public one with Slack's `conversations.join`.
- **prompt**: write it for a fresh session that has none of this conversation. It knows earlier runs only from Linear, GitHub and your memory, so say "skip anything already filed or open" when that matters, and name the repo, the project and where the output goes.
- If the file isn't writable, use `sudo`.

## Every edit

1. Edit the file: add a table, change one, set `paused = true` to pause, delete the table to stop it.
2. Run `python -m agent schedules`. It prints each schedule with its next three run times, and any errors. Fix every error it reports.
3. Reply with what you set up and the next run times from the check.

To list schedules, run the check and reply with its output. A schedule that is new or whose `cron` changed first runs at its next time, not straight away.
