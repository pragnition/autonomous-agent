---
name: task-history
description: Use when someone asks what you did, worked on, finished or are still working on; who asked for something; when or how long something took; or anything else about past work, yours or for a person, a day, a week or a project.
---

# Task history

Core keeps a record of every task you've done, from Slack, Linear and schedules. It writes it to
`/data/tasks/YYYY-MM.jsonl` (read-only for you): one file per month by when the task started, one task per line,
newest first. Chat (greetings, small talk) isn't recorded. A request shows up about 5 minutes after you answer it.

Each line has: `id`, `title`, `source` (slack, linear, schedule), `requested_by` and `followed_up_by` (person keys,
or a Linear name), `where` (the Slack thread or Linear issue), `started_at` and `ended_at` (UTC), `work_time_s`
(your working time, not waiting on people), `requests`, `finished`, `outcome`, `links` (PRs, issues) and `steps`
(each request: `at`, `by`, `asked`, `outcome`).

## Answering

1. Pick the months the question covers (`ls /data/tasks`). The team's timezone is in `/run/agent/timezone`;
   convert times to it when you answer.
2. Filter with `jq`; never print a whole file. For example:

   ```bash
   jq -c 'select(.started_at >= "2026-11-10") | {id, title, requested_by, finished, outcome}' /data/tasks/2026-11.jsonl
   jq -c 'select(.requested_by == "sam" and .finished != true) | {id, title, outcome}' /data/tasks/2026-11.jsonl
   jq -c 'select(.title + " " + ([.steps[].asked] | join(" ")) | test("debloat"; "i")) | {id, title, where}' /data/tasks/*.jsonl
   ```
3. Answer from the record: name the work, who asked, when, whether it's finished, and link the thread, issue or PR.
   If the record has nothing, say so plainly instead of guessing.
