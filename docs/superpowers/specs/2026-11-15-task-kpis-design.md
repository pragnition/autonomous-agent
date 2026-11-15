# A task record, and KPIs that count Jensen's tasks from any source

## Why

The KPI page only counted Linear issues handed to the agent and the PRs its GitHub account opened. Most of the work
reaches Jensen as Slack DMs, so after a day of 68 requests (installing plugins, browser testing, a codebase study,
messaging teammates) every number read zero except queue wait. The page also showed ten numbers, most of them stages
of a Linear → PR → merge pipeline that most work never goes through.

Two things are wanted:

- **A permanent record of every task Jensen has done**: what it was, who asked, when it started and ended, how it
  went. Jensen itself can read it, so "what did you do last week?" or "who asked for the debloat study?" in Slack gets
  a real answer.
- **A small fixed set of KPIs computed from that record**, meaning the same thing whatever the work came from (a Slack
  DM, a Slack thread, a Linear issue, a schedule), so weeks can be compared with each other and before/after a change.

## Goals

- Every request Jensen answers is labeled **chat**, **new task** or **same task** (continues an earlier task). Each
  task gets a short title, and each request gets a one-line outcome. Core does this with one Haiku call after the
  turn; Jensen does nothing extra.
- Core keeps a **task record** for every task (title, source, who asked, start, end, Jensen's time, finished or not,
  what was done, links). It's never deleted.
- Jensen can read the record through a **`task-history` skill** and a copy of the record in a folder it can read.
- The KPIs are four numbers: **Tasks**, **Time per task**, **Finished**, **Shipped**. They are the same everywhere: the
  dashboard's KPI page and Overview card, `agent kpis`, the agent's `kpis` tool and the daily digest.
- The KPI page lists the tasks, so the count can be checked by eye.

## Non-goals

- No change to Jensen's prompt or tools. The only addition on its side is the skill.
- The labels don't have to be exact. A "thanks, also check X" labeled as chat now and then is fine.
- Chat (greetings, small talk) isn't in the task record.
- No Claude cost anywhere (Foundry spend has its own page).
- No per-project filter.

## Anti-goals

- Labeling and the record must never touch a turn: not its start, its reply, its reactions or its retry. If Haiku or
  the proxy is down, requests stay unlabeled and get labeled later.
- Jensen never gets core's database. It holds Linear and Slack tokens.

## Design

### Labeling a request

When a turn's result arrives (`/agent/result`), core labels that request in the background, after the result has
been handled. It sends Haiku (`claude-haiku-4-5`, through the same Claude proxy the sessions use):

- the request: its text, trimmed. For a Linear turn, the issue key, title and the prompt or comment, not the raw
  webhook JSON;
- up to the 5 earlier labeled requests in the same conversation, each with its label, task id and title;
- the requester's other tasks from the last 24 hours (up to 10, each with its id, title and latest outcome). Every
  top-level Slack DM is its own conversation, and people often follow up with a new DM instead of replying in the
  thread ("so what are the findings??"). This lets that follow-up join the task it belongs to;
- Jensen's reply (the result's text), trimmed;
- whether the turn ended in an error.

Haiku answers with JSON:

| field | meaning |
|---|---|
| `label` | `chat` (greetings, small talk, status pings with no work), `new`, or `same` |
| `task` | for `same`: the id of the earlier task it continues (from this conversation or the requester's recent tasks) |
| `title` | for `new`: 3–8 words naming the work ("Debloat study of the ChilliClaw codebase") |
| `outcome` | for `new`/`same`: one sentence on what Jensen did or delivered in this reply |
| `finished` | for `new`/`same`: whether Jensen delivered what was asked. A PR opened, a deploy to staging or a report posted counts as finished even while it waits for review or merge (merges show under Shipped). Not finished: couldn't do it, stuck, gave up, stopped mid-work, or waiting on input it needs to go on |

Rules in code, without Haiku:

- A continuation turn (the automatic carry-on after a timeout or max-turns stop) is `same` as the turn it continues.
- A session the agent starts with `start_task` (a hand-over) is `same` as the task of the turn that started it, and
  takes that task's source and requester. Core records the parent work item when it queues the hand-over.
- In both cases Haiku is still asked for the outcome and `finished`; only the label is fixed.
- The smoke-test conversation is skipped.
- A `same` that points at no earlier task becomes `new`.
- A reply that isn't valid JSON is retried on the next sweep. After 3 tries the request is labeled `chat` and
  `finished` stays empty, so one bad request can't hold up the sweep forever.

A sweep every 5 minutes labels any finished request that is still unlabeled. A label only depends on earlier
requests in its conversation and the same requester's recent tasks, so the sweep keeps arrival order **per
requester** and labels different requesters in parallel (a few at a time). Within one requester it stops at the first
request it can't label; other requesters carry on. The same sweep is the backfill: on first start it labels every
request already in the database.

Labeling uses the same Claude seats as Jensen's sessions, so it never competes with them: before each call it asks
the proxy for capacity, the same check sessions use, and waits for the next sweep when there's none. The calls only
go out when Jensen has room to spare.

### Source and requester

Each request has a source and a requester. From now on core records both on the work item when it queues the turn:

| source | requester |
|---|---|
| `slack`: a Slack message | the person who sent it (already the turn's actor) |
| `linear`: a Linear session event | the Linear user who handed the issue over or prompted the session, from the webhook; matched to a team person when possible, otherwise their Linear name |
| `schedule`: a schedule firing | the schedule's `by` |

A continuation keeps its turn's source and requester. For requests queued before this ships, the backfill infers
them:

- **source**: a conversation linked to a Linear issue → `linear`, an origin line starting with `schedule` →
  `schedule`, anything else → `slack`;
- **requester**: the turn's actor. For Linear turns it's read from the stored webhook text.

### The task record

A task is its first request (labeled `new`) plus every later request labeled `same` with that task's id. Core
keeps one record per task, updated whenever one of its requests is labeled:

| field | value |
|---|---|
| `id` | a stable task id, in a form that can't be confused with the old `tasks` table's `T-0001` ids |
| `title` | the first request's title |
| `source` | the first request's source |
| `requested_by` | the first request's requester; anyone else who followed up is listed too |
| `where` | every place the task happened: Slack thread links, the Linear issue key and link, the schedule's name |
| `started_at` | when the first request arrived |
| `ended_at` | when Jensen answered the latest request |
| `work_time_s` | the time Jensen spent working: from when a runner picked up each request (its `session_started`) to its answer, with overlapping requests merged so no second counts twice |
| `requests` | how many requests the task took |
| `finished` | the latest request's `finished`, so a task that failed and was fixed on a follow-up counts as finished |
| `outcome` | the latest request's outcome |
| `steps` | every request in order: when, who, what they asked (trimmed), the outcome |
| `links` | PR and Linear issue links found in Jensen's replies |

Records live in core's database and are never deleted.

### How Jensen reads it

Core writes a copy of the record to one file per month, `/data/tasks/YYYY-MM.jsonl` (by the task's start, in the
agent's timezone), one task per line, newest first. When a record changes, only its month's file is rewritten,
written to a temporary file and renamed so a reader never sees half a file. The folder is mounted into the agent
container read-only, the same way `/data/schedules` is shared.

A new core plugin skill, `task-history`, tells Jensen when and how to use it:

- **When:** someone asks what Jensen did, worked on or finished; who asked for something; how long something took;
  what's still unfinished; or anything about past work.
- **How:** pick the months the question covers, then query those files with `jq` (filter by date, person, source,
  finished; search titles and steps). Never read a whole file into the conversation. Answer from the record, link the
  thread or issue, and say so when the record has nothing.
- **What it doesn't show:** chat isn't recorded, and the newest request appears only once its labeling sweep has run
  (up to about 5 minutes).

### The four KPIs

Over a window `[since, until)`, counting tasks whose first request arrived in the window:

1. **Tasks**: how many, split by source.
2. **Time per task**: `work_time_s`. That's Jensen's own working time; time spent queued (paused, out of Claude
   capacity) and the hours a person takes to reply in between don't count. Shown as median, with p90 below.
3. **Finished**: the share of tasks with `finished` true, with "x of y" below.
4. **Shipped**: PRs merged in the window (from `agent_prs`), with Linear issues completed in the window (from
   `linear_issues`) below. PRs merged is the headline number: adding the two would count a PR and the issue it closes
   twice. Both tables and their pollers stay as they are.

Everything else comes off the page: ticket → pickup, pickup → PR, PR → merge, both ticket → done numbers, queue wait,
merge rate, the issue count and unlinked PRs.

### Where it shows

Text form, used by `agent kpis`, the `kpis` tool and the daily digest:

```
Jensen, last 7 days
Tasks: 12 (Slack 9 · Linear 3 · schedules 0)
Time per task: median 6m · p90 41m
Finished: 83% (10 of 12)
Shipped: 0 PRs merged · 0 Linear issues closed
```

Dashboard KPI page, with the 7/30/90-day range selector kept:

```
What Jensen worked on, how long it took, and what shipped.

[ Tasks 12        ] [ Time per task 6m ] [ Finished 83%   ] [ Shipped 0       ]
[ Slack 9 · Lin 3 ] [ p90 41m          ] [ 10 of 12       ] [ PRs merged      ]
[                 ] [                  ] [                ] [ 0 issues closed ]

Tasks per day                          (columns, one series per source)

Tasks
Debloat study of the ChilliClaw codebase   Slack   sam   Nov 15 03:34   12m   finished
Browser-test chilliclaw-dev end to end     Slack   sam   Nov 14 17:04   31m   not finished
...
```

Each task row opens the task: its steps, outcome and links, plus a link to the session page of each request. The
Overview card shows the same four tiles.

## Approaches considered

How requests are labeled:

- **Haiku labels each request after the turn (chosen).** Jensen does nothing extra. The last KPIs read zero because
  they depended on the agent recording things, and this doesn't. Grouping follow-ups into one task, the titles and
  the outcomes come with it. Costs one small Haiku call per request on the pooled Claude seats, not Foundry.
- **Jensen tags its own reply** (a hidden `[task: …]` line core strips before posting). No extra call and Jensen has
  the most context, but a forgotten tag silently drops a task, and a drifting format leaks into Slack.
- **A rule of thumb on tool steps** (3 or more steps is a task). Free and already recorded. On the first day's data it
  missed "introduce yourself to a teammate, show me the draft first" (1 step) and counted "what's going on?" (5 steps) as
  a task, and every follow-up became its own task.

How Jensen reads the record:

- **A read-only copy in a shared folder, plus a skill (chosen).** Same pattern as schedules. Jensen uses its own
  tools (`jq`) and nothing new sits in its path. The skill's description is what makes Jensen reach for it when
  someone asks.
- **A core `tasks` tool, like the existing `kpis` tool.** It works, but it's one more core tool where a file and a
  skill do the job.
- **Mount core's database into the agent container.** No: it holds Linear and Slack tokens, and a second writer-capable
  mount of a live SQLite file risks locking it.

## Calls made in this design

- **Time per task is Jensen's working time, not wall-clock.** In Slack the gaps are mostly people being away, and
  queue time is mostly capacity or a pause, neither of which says anything about the agent. Start and end times are
  still in the record for anyone who wants wall-clock.
- **A follow-up can join a task from another conversation**, limited to the same requester's tasks from the last 24
  hours, because every top-level DM starts its own conversation.
- **Finished means Jensen delivered**, so a PR waiting for review counts. Otherwise most code tasks would stay
  unfinished forever, since a merge rarely comes back as another request.
- **Hand-overs fold into the task that started them**, so one request split across sessions counts once.
- **Shipped's headline is PRs merged**, with Linear issues closed below it.
- **Finished comes from Haiku reading Jensen's reply**, not from "no errors". The first day had 4 errors, all
  infrastructure failures inside one chat that later succeeded; an error count measures the plumbing, not whether the
  work got done.
- **Tasks are counted by when they started**, Shipped by when the merge or close happened. A PR merged this week for a
  task from last week still counts as shipped this week.
- **Backfill**: the history is already in the database (every request and reply), so it gets labeled and lands in the
  record.
- **JSON lines, one file per month, not a SQLite copy**, for the agent's copy: one task per line is easy to grep and
  `jq`, a change rewrites only one month, and a question about last week never touches last year.
- **Labeling is ordered per requester and waits for spare capacity**, so one stuck request delays only that person's
  labels, and labeling never takes capacity from Jensen's own work.

## Left to the builder

- The prompt wording and the trim lengths.
- How core calls Haiku. It should go through the proxy's Messages endpoint the way the runner's sessions reach it.
  First check that the proxy forwards a plain Messages call for a Haiku model; if it doesn't, stop and report before
  choosing another route.
- Table and column names, and the task id format.
- How the Linear requester is read from the webhook, and the matching to a team person.
- The daily chart's exact series and colours, and the task detail view's layout.

## Addition: project tasks and general tasks, and a deep dive

### Why

A task list that mixes product work with everything else doesn't say how much went into the products. Tasks are now
**project tasks** (work on one of the configured projects: its code, deployments, tests, issues, users or roadmap) or
**general tasks** (work that belongs to no project: installing tools for the agent, messaging teammates, research not
tied to a product). Chat still isn't a task. Clicking the task count opens the tasks behind it, filterable by project.

### Design

- **Haiku names the project** in the same call: one of the configured projects (each listed with its repo, Slack
  channel and Linear team), or null for general. Code passes it signals it can see for itself: the conversation's
  bound project, a Linear key of the project's team or the project's repo in the request or reply, the project's name
  in the request (not the reply: the agent names its main project in most replies). An answer that isn't a configured
  project falls back to the first signal, else general. A null stays general, even in a project's channel: messaging
  a teammate from there is still general work.
- **A task's project** is the first project among its requests, so a follow-up that reveals the product fills it in.
- **Relabeling on change.** Each label carries the labeler's version. When the prompt or the rules change, the
  version goes up and the sweep relabels the history in arrival order, without anyone clearing tables. A relabeled
  `new` request keeps its task id. A request whose relabel keeps failing keeps its previous label.
- **KPIs.** The Tasks number splits by project: `Tasks: 18 (chilliclaw 12 · general 6)`. The other three are
  unchanged.
- **Dashboard.** The Tasks tile opens a dialog with the window's tasks (title, project, source, who, when, time,
  status, expandable steps) and a project filter: all, each project, general. The task list moves into that dialog.
- **The agent's copy** has a `project` field (null for general), and the skill shows how to filter by it.
