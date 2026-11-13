# Autonomous Engineering Agent v2: One General Agent on Docker Sandboxes

- **Date:** 2026-11-13
- **Status:** Approved 2026-11-13. Its isolation design (G2–G4, G9, §4–§5) is the next phase; `2026-11-13-open-agent-design.md` (v3) covers the current phase.
- **Author:** Parthib Das Gupta (with Claude)
- **Supersedes** these parts of the v1 spec (`2026-11-12-autonomous-agent-design.md`): roles and stages (§6.3), the worker (§6.4), the tester pipeline (§6.8), the task lifecycle (§7), the services and credentials (§5.1, §5.2), and the v1 revision-2 additions (CLI tools, sandboxes, `egress`).
- **Unchanged from v1:** the Claude account proxy (§5.4, except which origin sandboxes use), memory (§6.15), persona (§6.13), ambient mode (§6.14), metrics and the PR stats poller (§6.11), the GitHub identity and PR format (§6.10), and the kill switch (D13).

## 1. Summary

The agent is **one general agent**, Claude Code driven through the Agent SDK. It decides how to handle any request: answering a question, investigating a bug, fixing it, building a feature, or something nobody planned for.
- **No task types or pipelines in code.** Know-how lives in **skills**, and behaviour lives in a short system prompt. Both are config.
- **The environment is secured, not the agent.**
  - Claude Code runs with its full native toolset inside a **Docker Sandbox**: a microVM with its own kernel and its own Docker engine, which can reach only the hosts its policy allows.
  - Every credential lives in **`core`**, which runs only our code and never runs Claude.
  - The agent acts on the outside world only through `core`'s tools, and those tools carry the guarantees: never merge, push only the agent's branches, and confirm before major infrastructure changes.

## 2. What changed from v1, and why

| v1 | v2 | Why |
|---|---|---|
| Roles (chat, scout, builder, tester, memory) and fixed stages (investigate → specify → build → test) | One general agent; skills say how to approach kinds of work | Not scalable: every new kind of work needed code |
| Role prompts shipped in the image | A short system prompt plus skills, both in config | Behaviour changes without a release |
| Claude sessions inside `core`, next to the tokens | `core` never runs Claude; sessions run in sandboxes with no credentials | A tricked or compromised session finds nothing to steal |
| A `worker` container, plus a planned `egress` proxy and Sysbox | Docker Sandboxes: microVM, network policy and Docker inside, out of the box | Less security code of our own; a VM boundary instead of a shared kernel |
| Read-only roles without a shell; builder handed a summary of production data | Full Claude Code in every session; one session sees the whole job | Quality: native shell, no lossy hand-offs |
| Worker pushes and opens PRs | `core` pushes and opens PRs through the `open_pr` tool | The GitHub write token never enters a sandbox |

## 3. Decisions

| # | Decision | Choice | Why |
|---|---|---|---|
| G1 | The agent | One general agent (Claude Code via the Agent SDK). No roles, stages or task types in code. | Handles work nobody coded for; skills scale it. |
| G2 | Security model | Secure the environment, not the agent. Full native Claude Code inside the sandbox; every limit is enforced outside it (VM, network policy, `core`'s tools). | Keeps the agent's quality; never relies on the agent or Claude Code to contain itself. |
| G3 | Credentials | `core` is the identity container. It holds every credential, runs only our code and never runs Claude. | Nothing of value in any sandbox. |
| G4 | Sandbox | Docker Sandboxes (`sbx`): per project, a **coding** sandbox (`code-<project>`, open internet) and a **debug** sandbox (`debug-<project>`, locked down), plus `general`. Each is a microVM with Docker inside. Behind a small `SandboxProvider` interface. | VM isolation and safe Docker-inside, without building it ourselves. Swappable while `sbx` is young. |
| G5 | Guarantees | Live in `core`'s tools, not in a pipeline. | The agent decides what to do; the tools decide what's allowed. |
| G6 | Control direction | A runner inside each sandbox pulls work from `core`. `core` never connects into a sandbox. | Simple, and nothing needs inbound access to a VM. |
| G7 | Approval prompts | As few as possible. Only major infrastructure changes (an integration's `confirm` rules) wait for an admin's click. | Autonomy. |
| G8 | Kept from v1 | Agent SDK (D2), packs (D4), GitHub identity (D6), no agent push rights (D7, now enforced by `core`), Slack tiers (D8), kill switch (D13), account proxy (D17), memory (D18) | Still right. |
| G9 | Production data | Lands only in the project's **debug** sandbox, which can reach nothing but Claude and `core`. Coding sessions get production signals through `ask_production` (§7), whose answers code scrubs of customer data. Coding sandboxes get the open internet. | The coding agent works like a developer with full web access, and customer data has no way out. |

## 4. Architecture

```
  Slack · Linear · GitHub · Azure (direct, outbound)
                 ▲
┌────────────────┴──────────── core (container) ───────────────────────┐
│ intake (Slack, Linear, schedules) · conversations · task records      │
│ tools with guarantees (§7) · connectors · az · memory · git endpoint  │
│ every credential · our code only, never Claude                        │
└───────▲──────────────────────────────────┬───────────────────────────┘
        │ agent API + git (pull, per-session token)   │ relay → host
        │ via host.docker.internal:18080              ▼
┌───────┴───── code-<project> sandbox (microVM) ─────────┐   ┌─── host (the laptop) ────────────┐
│ runner (ours) · Claude Code sessions, full toolset     │   │ sandboxd: wraps the sbx CLI      │
│ one worktree + one OS user per session                 │   │ Claude account proxy (§5.4)      │
│ own Docker engine (compose, the app, Playwright)       │──►│ sbx host proxy: enforces each    │
│ no credentials                                         │   │ sandbox's network policy         │
└────────────────────────────────────────────────────────┘   └──────────────────────────────────┘
┌───────────── debug-<project> sandbox (microVM) ────────┐
│ production logs and traces only · repo read-only       │
│ Locked Down: Claude proxy and core, nothing else       │
└────────────────────────────────────────────────────────┘
┌───────────── Docker Sandbox: general (microVM) ────────┐   supervisor (container): watchdog
│ DMs, questions not tied to a project, memory chores    │   relay (container): core → host
└────────────────────────────────────────────────────────┘
```

**Who can reach whom:**

| From | To | How |
|---|---|---|
| `core` | Slack, Linear, GitHub, Azure | Directly (outbound only) |
| `core` | `sandboxd`, proxy usage API | Through the relay (as in v1 §5.4) |
| Sandbox | Claude account proxy | `host.docker.internal:18091`, allowed by the sandbox's policy |
| Sandbox | `core`'s agent API and git endpoint | `host.docker.internal:18080`, allowed by policy; `core` publishes it on `127.0.0.1` only |
| Coding sandbox, `general` | The internet | Open by default (`network = "open"`). A pack can choose `"read"`: GET-only package registries, GitHub and docs sites (§5). |
| Debug sandbox | Anything except the Claude proxy and `core` | Refused |
| Any sandbox | Other sandboxes, other ports on the laptop, the office LAN | Refused |
| `core` | A sandbox | Never. Sandboxes pull. |

**Credentials:**

| Credential | Lives in |
|---|---|
| Slack, Linear, GitHub read and write tokens, Azure login, pack integration secrets | `core` only |
| Claude account tokens | The host proxy only (unchanged) |
| Docker account (for `sbx`) | The host only |
| Keys the app under test needs (e.g. a dev Foundry key) | The `sbx` host proxy only, injected per host (§5); the sandbox sees a placeholder |
| `sandboxd` token | `core` and `sandboxd` |
| Per-sandbox token | The sandbox's runner (root-only file), issued by `core` at creation |
| Per-session token | The runner process only, never the session's OS user (§6) |

## 5. Sandboxes

**Sandbox profiles:**
- **Coding** (`code-<project>`, one per project):
  - serves every session for that project
  - open internet by default
  - never receives production data
- **Debug** (`debug-<project>`, one per project):
  - the only place production logs, traces and other production data land
  - runs `ask_production` sessions (§7) with the Azure and tracing tools, plus a read-only clone of the repo so it can match stack traces to code
  - **Locked Down:** it reaches the Claude proxy and `core`, nothing else
- **`general`:** serves DMs, questions not tied to a project, and memory consolidation. It has no repos and has open internet.
- **Persistent.** A sandbox keeps its repo clone, toolchains, caches, pulled Docker images and Claude Code session files across sessions.
  - Each session's worktree is removed when its task ends.
  - **Idle stop:** `sandboxd` stops a sandbox after `sandbox.idle_stop_minutes` (default 30) and starts it again when work arrives. Stopping keeps its disk.
  - **Rebuild:** "rebuild sandbox" (Admin tier) deletes it and creates a fresh one from its template. `sandbox.rebuild_every_days` (default 7) does this on a schedule, so nothing left behind by a bad package lasts long.

**Resources (config, never Claude).** Without limits, `sbx create` gives each sandbox half the host's memory and all of its CPUs, so `sandboxd` always sets them explicitly.
- **Defaults** per profile live in `agent.toml [sandbox.resources]`, sized for a 32 GB laptop:

  | Profile | CPUs | Memory | Docker disk |
  |---|---|---|---|
  | coding | 4 | 8 GB | 30 GB |
  | debug | 2 | 2 GB | 10 GB |
  | general | 2 | 2 GB | 10 GB |

- **Per-project overrides** go in the pack's `[sandbox]`.
- **Applied at creation:** `sbx create --cpus/--memory`, plus the Docker volume size setting. Changing them means a rebuild.
- **The laptop is protected:**
  - `sandboxd` won't start a sandbox if the running ones plus the new one would exceed `sandbox.host_memory_budget` (default 75% of host RAM). It stops idle sandboxes first.
  - `doctor` warns when the configured numbers can't fit.
- **Claude can't change them.** If a build runs out of memory, the agent reports it, and an admin raises the pack's numbers by config change.

**Template.**
- Built from Docker's `docker/sandbox-templates` Claude Code variant with Docker inside.
- It adds our runner, `git`, and a toolchain manager.
- The pack names the project's toolchain versions and setup commands. They are installed inside the sandbox, through its network policy.

**Custom tools per sandbox.**
1. **Setup commands per profile** (`[sandbox.code] setup`, `[sandbox.debug] setup` in the pack). They run as root inside the VM when the sandbox is created or rebuilt, and persist between sessions.
2. **A custom template image** per project or profile, for heavy or fixed toolchains, so rebuilds don't reinstall.
3. **User-level installs by the agent itself** whenever it needs them. Session users have no `sudo`, so system-wide installs always come from 1 or 2, which are admin config.

Admins get a shell without SSH through the Admin-tier `sandbox_exec(project, profile, command)` tool, which posts the output to the thread. On the laptop, `sbx exec -it <sandbox> bash` also works.

Installing a CLI is fine; **logging it in inside a sandbox is not**, because that puts a credential in the box. Production Azure access stays with `core`'s `az` tool. If a test truly needs its own cloud access, use a separate low-privilege dev identity limited to dev resources, placed knowingly. Prefer the debug sandbox, which can't send it anywhere.

**Sessions inside a sandbox.**
- Each session gets its own worktree, cloned from `core`'s git endpoint, and its own OS user. Session users have no `sudo`.
- **The honest boundary.** Docker access inside the VM is root-equivalent within that VM, so sessions of the same project are kept apart on a best-effort basis. **The VM is the boundary that holds:** projects never share a sandbox, and nothing of value is inside.
- Running each task in its own sandbox is a later option if this ever matters.

**What a sandbox gets from `core`.** Everything arrives over the one allowed connection to `core`. A sandbox holds a working copy of the code, but never the memory store or any credentials.

| What | How |
|---|---|
| Code | `core` mirrors each repo with its read token and serves it as a git endpoint (smart HTTP). The sandbox keeps one local clone as a cache; each session gets a worktree. Pushes are accepted only to `refs/heads/<name>/*`; `open_pr` pushes to GitHub from `core`. |
| Other branches, PRs, CI logs | `core`'s GitHub read tools (§7) |
| Memory | The store stays in `core`. The briefing comes with the work item, built from the conversation's scopes; `recall` / `read_memory` / `remember` are tool calls, scope-checked on every call. Consolidation runs in `general`: `core` sends the material and validates and writes the notes. |
| Slack thread history | Sent with the work item; `read_thread` for more |
| System prompt, skills, pack plugin | The runner pulls them from `core` at session start, versioned and cached |
| Repo `CLAUDE.md`, settings, hooks | Come with the clone |
| Pack "worker" secrets (test accounts) | Sent with the work item; test-only values |
| Transcripts | Kept in the sandbox for resume; streamed to `core` for logs and metrics |

**Network policy.**
- **Coding sandboxes and `general`: open internet** (`network = "open"`, the default per pack).
  - The agent searches, reads docs, issues and library source, and installs packages in real time, like a developer.
  - It runs with no credentials, which makes it safer than a developer laptop.
  - A pack can choose `network = "read"` instead: GET-only package registries, GitHub (`github.com`, `api.github.com`, `raw.githubusercontent.com`, `codeload.github.com`, `objects.githubusercontent.com`), a default list of official docs sites, and its `[egress] allow` hosts. It never gets wildcards over user-hosted sites such as `*.github.io`.
- **Debug sandboxes: Locked Down.** Only the Claude proxy and `core` are reachable, so nothing a debug session reads can leave.
- **All sandboxes:** they reach the Claude proxy and `core` through `host.docker.internal`. Nothing else on the laptop, and nothing on the office LAN.
- **`doctor`:** checks that every debug sandbox refuses `example.com`.

**External services a project's app needs** when the agent runs it to test it (e.g. a dev Azure AI Foundry deployment):
- **Host:** reachable with open internet. With `network = "read"`, it goes in the pack's `[egress] allow`.
- **Inject the key, never ship it.** The key is declared in the pack's `[egress.inject]`, which maps a host to a header and a secret name:
  - `sandboxd` registers it with `sbx secret set-custom`
  - the sandbox's host proxy adds the header to requests for that host only
  - the app inside sees only a placeholder value
- **Dev resources only,** each with its own spending cap.
- **Emulators where they exist** (e.g. Azurite), run in the sandbox's Docker with no network at all.
- **Entra-token auth:** if an app uses Entra tokens instead of keys, the dev resource uses key auth for tests, or `core` issues short-lived tokens scoped to that resource.

```toml
# example from the ChilliClaw pack's project.toml
[egress]
allow = ["registry.npmjs.org", "chilliclaw-dev.openai.azure.com"]

[egress.inject."chilliclaw-dev.openai.azure.com"]
header = "api-key"
secret = "CHILLICLAW_DEV_FOUNDRY_KEY"
```

**Lifecycle is code, not Claude.** `core` routes each conversation to a sandbox by project: the Slack channel's pack, the Linear team, or `general` for DMs and anything else. `core` and `sandboxd` create, start, stop and rebuild sandboxes. Claude only decides what to do inside its session, and can affect lifecycle only through tier-checked tools (`rebuild_sandbox` is Admin tier).

**`sandboxd`** is a small host service, a systemd user service like the Claude proxy. It wraps the `sbx` CLI and offers `core` a token-protected API:
- `ensure(project)`, `rebuild(project)`, `stop(project)`
- `set_policy(project, hosts)`
- `status()`

Nothing inside a sandbox can reach it.

**Host prerequisites** (added to v1 §5.3):
- Ubuntu 24.04 or later, with KVM enabled and the agent's OS user in the `kvm` group
- `sbx` installed at a pinned version and logged in to a Docker account (one-time, host-level, like adding a Claude account)

## 6. The agent

**Model.**
- **Default:** `claude-opus-5-5`.
- **On request:** `claude-fable-5-1` for hard tasks ("use fable").
- **Internal chores:** `claude-sonnet-5` for memory consolidation.

**What a session is.**
- A Slack thread, a Linear issue, or (Phase 3) a schedule firing is one **conversation**, mapped to one Claude Code session.
- Each new message in the thread resumes that session.
- If the sandbox was rebuilt, the session starts fresh with the thread history and a memory briefing.

**What it starts with** (small, and prompt-cached across turns):
1. **A short system prompt from config** (`/data/config/agent/system.md`):
   - who it is and who it works for
   - that it acts on the outside world only through `core`'s tools
   - when to ask a human: only when the request itself is unclear, never for permission
   - verify before saying done
2. **The persona**, for Slack replies only (v1 §6.13).
3. **A memory briefing** (v1 §6.15).
4. **The repo's own `CLAUDE.md`, settings and hooks.**
5. **The skills list:** one line each. Full text loads only when used.

**Full toolset, full autonomy inside the box.**
- **Tools:** Claude Code's native shell, file tools, background tasks, subagents and web search. Web search runs through Claude, so it needs no extra network access.
- **No permission prompts in the sandbox** (`bypassPermissions`). Its limits are the VM, the network policy and `core`'s tools.

**The runner** (our code, inside the sandbox) does five things:
1. pulls work from `core`
2. starts or resumes sessions with the Agent SDK, as the session's OS user
3. streams events back to `core`
4. serves `core`'s tools to the session as **in-process SDK tools**. The tool code runs in the runner's process, so the per-session token never reaches the session's OS user.
5. runs the project's checks for `open_pr` (§7)

It reuses v1's `ClaudeRunner` (fork on account trouble, watchdog, timeouts).

**Quality levers** (in skills and the system prompt, not code):
- **Verification:** tests, the running app in the sandbox's Docker, Playwright screenshots it looks at, logs after a fix.
- **Fresh-eyes review:** a subagent with a clean context reviews the diff against the request before `open_pr`.
- **Parallel exploration:** subagents, e.g. one crawls logs while another reads code. Subagents share the session's sandbox (their own context, not their own machine). For parallel code changes they can each use a git worktree. For an isolated environment (a second app instance, a scratch database) the agent starts containers in the sandbox's own Docker, still behind the sandbox's network rules. Separate sessions come only from `start_task` (§7).
- **Progress file:** kept in the worktree for long tasks.

**How new capabilities are added:** a skill, plus at most a schedule, a generic tool or a sandbox profile. No capability-specific code. For example, an "enhancement studio" is one skill (research features on the web, build them on branches, test them, post screenshots) run on a schedule in a coding sandbox. Rules that always apply go in the system prompt; procedures used sometimes become skills, which cost only their one-line description until used; one-off instructions go in the message or schedule.

**Starting skills:**
- **Shipped in the image** (core plugin): `fix-bug`, `build-feature`, `verify-in-app`, `review-before-pr`, `pr-description`.
  - `fix-bug`: gather every signal (Azure, traces, recent commits, the issue), reproduce with a failing test, fix, verify.
- **In the ChilliClaw pack:** `chilliclaw-debugging` (where its logs and traces are, useful queries, how to run it locally) and `azure-debugging`.

## 7. `core`'s tools: where the guarantees live

Every call carries the session token. `core` resolves it to the conversation, project and **actor**: the Slack user, or the Linear issue's mapped requester, whose message started the current turn. Tiers (v1 §6.6: Read, Act, Admin) come from the actor, never from the model's arguments.

| Area | Tools | Guarantees enforced by `core` |
|---|---|---|
| Conversation | `post_update(text)`, `ask(question)`, `react(emoji)`, `post_files(paths, text)`. The turn's final text is posted as the reply. | Posts only to the conversation's own thread. `ask` sets the task to waiting-on-human. `post_files`: the runner sends the files (screenshots, videos, reports) to `core`, which uploads them to the thread; size and count limits apply. |
| Tracking | `track_task(project, title, kind)`, `finish_task(outcome)` | Record the current session's own work, for metrics, status messages and Linear sync. Linear issues get a task record at intake; one task per Linear issue, ever. |
| New work | `start_task(project, title, kind, brief)` | Creates a task and runs it in a **new session** in that project's sandbox, linked to the current Slack thread, within `max_concurrent_sessions` and the daily limit. Used for hand-over (a DM in `general` → the project's sandbox) and for splitting work ("fix these 5 bugs" → 5 sessions). Claude names the project and the brief. `core` picks and starts the sandbox, with the project's template and network rules; the agent never creates sandboxes itself. |
| Code | git endpoint: clone and fetch (read-only); push only to `refs/heads/<name>/*` | Any other ref is rejected. |
| | `open_pr(title, body)` | 1. The runner runs the pack's `build.checks` on the exact commit, in the session's worktree; any failure blocks the PR. 2. `core` checks that the branch is `<name>/*`. 3. It pushes that commit to GitHub as a **draft** PR, with the v1 PR format and the requester as assignee. Never merges, never force-pushes. |
| GitHub (read) | PRs, reviews, checks and CI logs, issues, a file at a ref | Read token only. |
| Linear | read issue and comments, comment, set status | Status names from the pack's mapping. |
| Production | `ask_production(request)` | Runs the request as a session in the project's **debug** sandbox, which has the Azure and tracing tools.
- It can be called any number of times; it isn't a fixed stage.
- **Scrubbing:** before the answer reaches the caller, code strips customer data: emails, phone numbers, tokens and keys, IP addresses, and the pack's `[scrub] patterns` (e.g. customer IDs). Errors, stack traces, timings, request paths and counts are kept.
- **Ops requests** ("restart the web app") go the same way, under the Azure rules. |
| Azure | `az(args, save_to?)`, `az_login()` (Admin) | **Debug sessions only.** No shell; `deny` rules refuse secret-printing commands, including `account get-access-token` and `rest`; `confirm` rules (create or delete resources, deployments, networking, access, scaling, slot swaps) wait for an admin's click; unattended conversations run only `read` rules; secret stripping. `save_to` writes the full output into the session's worktree and returns a preview, so large logs stay out of context. Device-code login is DMed to the admin, using the admin's own account; expiry is detected. |
| Web | Claude Code's own web search and fetch | Coding sandboxes and `general`: the open internet, or the pack's `read` list. Debug sandboxes: no web. |
| Memory | `recall`, `read_memory`, `remember` | Scopes per v1 §6.15, by conversation and project. |
| Operations | `status`, `cancel`, `stop_all` (Act); `resume_all`, `rebuild_sandbox`, `sandbox_exec` (Admin) | v1 kill switch semantics. `sandbox_exec` runs one command as root in a named sandbox and posts the capped output to the thread. |

All of `az`'s settings live in `[cli.azure]` in `agent.toml`, with the format from the v1 revision 2, §6.16. Adding another CLI or MCP integration is a config entry plus a skill.

## 8. How a request flows

Example, with ChilliClaw as the project: "@<name> the citation export crashes in cclaw, fix it".

1. **`core` receives the mention.** It checks the person is in `[people]` and opens a conversation for the thread.
2. **The runner in `code-chilliclaw` pulls it** and starts a session with the message, the thread and the briefing.
3. **The agent loads `fix-bug` and gets to work:**
   1. It calls `track_task(kind=bug)`; `core` posts the status message.
   2. It calls `ask_production("what fails in citation export, since when, how often")`. A session in `debug-chilliclaw` crawls the Azure logs with `save_to`, greps them, and answers with stack traces and counts, scrubbed of customer data. The agent asks follow-ups as needed.
   3. It checks recent commits and reproduces the crash with a failing test.
   4. It fixes the bug, runs the tests and brings the app up in Docker to check.
   5. It has a subagent review the diff, then calls `open_pr`.
4. **`open_pr` takes over:** the runner runs the checks, then `core` pushes `<name>/<task>-citation-export`, opens the draft PR, assigns the requester, and updates Linear if linked.
5. **The agent replies in the thread** with the PR, what caused the crash and how it verified the fix.

A Linear issue labelled `<name>` follows the same path. The issue's text is the first message, and replies go to a Slack thread in the pack's channel.

## 9. Budgets, failures and the kill switch

**Per-task budget** (config):
- turns
- wall time (default 90 minutes)
- estimated cost

When one runs out, the runner stops the session and `core` posts what was done and where it stopped. Per-message budgets apply to quick questions.

**Capacity.**
- Before starting a session, `core` checks `/proxy/usage` (v1 §5.4) and holds work when no account has capacity.
- Limits: `max_concurrent_sessions` (default 3) and `max_tasks_per_day` (default 20).

**Failures:**

| Failure | Behaviour |
|---|---|
| Runner or sandbox crash | `core` marks the session interrupted. When the runner pulls again, it resumes the session, or starts fresh with the thread history. |
| `core` restart | Runners keep retrying their pull. Sessions waiting on a tool call retry it. |
| Account trouble | The fork-on-account-trouble path from v1 §5.4. |
| Blocked host | The command fails inside the sandbox. `sandboxd`'s policy log, plus the agent's own report, tell the admin, who adds the host by config change. |

**Kill switch.** `stop_all` (any member) pauses intake and interrupts every session. Only an admin can resume (v1 D13).

## 10. Testing and quality

- **Unit and contract tests (`core`):**
  - every tool's guarantee: branch rules, no merge path, `deny` / `confirm` / `read`
  - actor and tier binding from the triggering message
  - session-token scoping
  - the git endpoint refusing other refs
  - `open_pr` refusing failed checks
  - `ask_production` answers passing through the scrubber
  - Azure and tracing tools refused outside debug sessions
- **Runner tests:** with v1's `FakeRunner`.
- **Sandbox probes (`doctor`):**
  - each sandbox reaches the proxy and `core`
  - a coding sandbox reaches a package registry
  - every debug sandbox refuses `example.com`
- **Smoke test:** a real request against the `sample` pack ends in a draft PR.
- **Quality eval (Phase 3):**
  - Replay a set of past bugs and features from each project (ChilliClaw first) in a scratch sandbox, and score them on PR checks and reviewer-style grading.
  - Run it before any change to the system prompt or skills is kept.

## 11. Phases

| Phase | Scope | Exit |
|---|---|---|
| **2 (next): General agent in sandboxes** | **Spike first** (§12, A1–A7) · `sandboxd` + `SandboxProvider` · template image + runner (pull, in-process tools, checks) · `core` agent API, git endpoint, session tokens · tools in §7, including `az` with `save_to` and device-code login, `post_files`, and `ask_production` with the debug sandbox and scrubbing · coding sandboxes with open internet · conversations replace roles and stages · system prompt + starting skills · budgets, kill switch · remove the `worker` container and pipeline code | From Slack, "fix X" in the first project (ChilliClaw): the agent gathers Azure signals through the debug sandbox, reproduces, fixes, verifies, and opens a draft PR. No task-specific code is involved, and `doctor`'s sandbox probes pass. |
| **3: Learns and runs itself** | Schedules (a prompt delivered at a time) · an enhancement-studio skill · agent-written and Slack-editable skills (diff posted with Undo) · config and secrets from Slack · quality eval · following up on PR review comments · dreaming | One week with zero SSH sessions. |
| **4: Others can install it** | v1 Phase 4 | v1 Phase 4 |
| **5: Part of the team** | Ambient mode, persona check | v1 Phase 5 |
| Later | A browser for work code can't do | — |

**What carries over from the Phase 1 build:**
- **Kept:** config and packs, the SQLite store, events, the proxy client, `ClaudeRunner` (moves into the runner), memory, persona, the Slack, Linear and GitHub connectors, notifier, the PR stats poller, supervisor, `doctor`, stop and resume.
- **Replaced:** `roles.py`, the stage logic in `dispatch.py`, the worker service and `build.py` pipeline, the per-role prompts, and `chat.py`'s chat-only routing.

## 12. Assumptions and risks

| # | Item | Check or mitigation |
|---|---|---|
| A1 | The laptop meets `sbx`'s requirements (Ubuntu ≥ 24.04, KVM) | Spike step 1 |
| A2 | A custom template (ours on top of `docker/sandbox-templates`) builds, and session users without `sudo` work | Spike |
| A3 | Claude Code in a sandbox reaches the account proxy through `host.docker.internal`: streaming works, `X-Claude-Code-Session-Id` arrives, and `/proxy/whoami` pins the session | Spike |
| A4 | A long-lived runner started with `sbx exec -d` keeps pulling from `core` and comes back after a sandbox restart | Spike |
| A5 | A debug sandbox, Locked Down plus exact rules, reaches the proxy and `core` and refuses `example.com` | Spike |
| A6 | Docker inside the sandbox runs `docker compose` for a sample app | Spike |
| A7 | git smart HTTP from the sandbox to `core`: clone works, and pushes outside `<name>/*` are refused | Spike |
| A8 | `sbx secret set-custom` can inject a custom header (e.g. `api-key`) for a custom host, and the value never appears inside the sandbox | Spike. Fallback: a dev key with a tight spending cap is sent with the work item. The allowlist still stops it leaving. |
| A9 | Sandboxes can have different network rules: coding sandboxes open, debug sandboxes Locked Down. Either the global preset is Open with deny-all per debug sandbox, or it is Locked Down with allow-all per coding sandbox. | Spike. Fallback: coding sandboxes get a broad read list. |
| A10 | For `network = "read"` projects, the proxy can enforce GET/HEAD-only on HTTPS hosts | Only needed for `read` projects; checked when the first one is set up. |
| R1 | `sbx` is young (v0.38) and changes often | Pin the version; everything goes through `SandboxProvider`, so a plain-container or Kata backend can replace it. |
| R2 | Sessions in one project's sandbox can affect each other through Docker (root-equivalent in the VM) | Accepted: the VM is the boundary, projects never share one, and nothing of value is inside. Per-task sandboxes are a later option. |
| R3 | The agent is tricked into misusing tools it is allowed to use | The guarantees in §7, `confirm` rules, draft PRs only, and human review before merge. |
| R4 | Source code in an open-internet coding sandbox could be leaked by a malicious package or a tricked agent | The same exposure as a developer laptop, minus credentials. Production data never enters coding sandboxes; it stays in debug sandboxes, which can't send anything out, and reaches coding sessions only scrubbed. A project that needs more can use `network = "read"`. |
| R6 | The scrubber misses a customer identifier | The pack's `[scrub] patterns`; the debug session is told to summarise without identifiers; a scrub-hit counter in the logs helps tune it. |
| R5 | Azure uses an admin's own account | `deny` / `confirm` / `read` rules; every command is logged. |
