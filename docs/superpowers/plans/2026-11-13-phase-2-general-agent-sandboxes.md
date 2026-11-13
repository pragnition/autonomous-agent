# Phase 2: General Agent on Docker Sandboxes, Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Phase 1's roles and stages with one general agent (Claude Code) that runs in Docker Sandboxes with no credentials and acts on the world only through `core`'s tools.

**Architecture:**
- `core` (container) holds every credential. It turns Slack and Linear messages into **work items**, serves the agent API, a git endpoint and the tools, and never runs Claude.
- A **runner** inside each sandbox (`code-<project>`, `debug-<project>`, `general`) pulls work items, runs Claude Code sessions with full permissions, and forwards tool calls to `core`.
- **`sandboxd`** (a host service) wraps the `sbx` CLI and creates, stops and rebuilds sandboxes with their resources and network rules.

**Tech Stack:** Python 3.12, claude-agent-sdk 0.2.180, aiohttp, httpx, slack-bolt, SQLite, git (`git http-backend`), Azure CLI, Docker Sandboxes (`sbx`, version pinned in Task 1), Docker Compose, Just.

**Spec:** `docs/superpowers/specs/2026-11-13-general-agent-sandboxes-design.md` (v2). It defers to `docs/superpowers/specs/2026-11-12-autonomous-agent-design.md` (v1) for the proxy, memory, persona and metrics. `az` rule format: v1 §6.16 (revision 2).

**Branch:** build on `phase-1`.

## Global Constraints

- No credential ever enters a sandbox. Slack, Linear, GitHub and Azure tokens, and pack integration secrets, live in `core` only. The only things inside a sandbox are the sandbox token (a root-only file) and per-session tokens (in the runner process).
- Neutral names only. Sandboxes are `code-<project>`, `debug-<project>` and `general`; the agent is `config.name`. The system is never named after ChilliClaw (it is one project).
- The agent pushes only to `refs/heads/<name>/*` on `core`'s git endpoint. `core` pushes to GitHub, opens **draft** PRs only, never merges and never force-pushes.
- Debug sandboxes are Locked Down: they may reach only `localhost:18091` (Claude account proxy) and `localhost:18080` (`core`). Coding sandboxes and `general` default to `network = "open"`.
- Ports:
  - `core`'s agent API listens on `8080` in the container and is published as `127.0.0.1:18080`.
  - `sandboxd` listens on `127.0.0.1:18100`; `core` reaches it through the relay at `172.30.0.1:28100`.
  - Sandboxes use the proxy origin on `18091`, reached as `host.docker.internal:18091`.
- Models: `claude-opus-5-5` by default; `claude-fable-5-1` for a conversation once a human message says "use fable" (case-insensitive; it sticks for that conversation); `claude-sonnet-5` for memory chores.
- Limits: `max_concurrent_sessions` 3, `max_tasks_per_day` 20, `task_minutes` 90, `max_turns` 400.
- Sandbox lifecycle: idle stop after 30 minutes; rebuild every 7 days; `host_memory_budget` 0.75.
- Default resources: coding 4 CPUs, 8 GB memory, 30 GB disk; debug 2 CPUs, 2 GB, 10 GB; general 2 CPUs, 2 GB, 10 GB.
- `az` always refuses `account get-access-token` and `rest` (built in, whatever the config says). Output cap: `max_output_chars` 8000. Command timeout: 60 s.
- Persona text goes only into Slack conversation turns, never into production or chore sessions.
- Every tool reply is passed through secret stripping (`agent.memory.scrub.scrub`).
- Tests run in the dev image: `just test`. Use TDD: watch every test fail before you implement.
- Never hand-edit `claude_proxy/`.

## Review Focus

1. **Two Slack messages land in one thread while its session is running.** The second must run as the next turn of the same session after the first finishes: never dropped, never run in parallel. (Task 4, Task 13)
2. **A runner dies mid-session** (sandbox stopped, laptop sleep). The claimed work item's lease expires and it is re-queued once with `attempts=1`, then marked failed after a second loss. It must never stay "claimed" forever. (Task 4)
3. **A tool call arrives with a revoked or expired session token** (after `stop_all`, cancel, or the item finishing). It gets 401, and the runner interrupts the session. (Task 5, Task 17)
4. **A push of `refs/heads/main`, a tag, or a branch deletion to `core`'s git endpoint.** All are rejected. So is `open_pr` with a SHA that isn't the branch tip. (Task 6, Task 8)
5. **`save_to="../../etc/x"`, or a 50 MB `az` output.** The traversal is refused; the output is capped in context and saved whole only inside the session's worktree. (Task 10, Task 17)

---

### Task 1: Spike: prove Docker Sandboxes on the laptop (run with the user)

This task is manual and must be run **on the laptop, with the user present**. Installing `sbx` and logging in to a Docker account are host changes, so ask before each one. The code tasks (2–17, 21–22) use fakes and may proceed while waiting for the laptop. Tasks 18–20, 23 and 24 need this task's findings.

**Files:**
- Create: `docs/superpowers/spikes/2026-11-13-sbx-spike.md` (findings, committed)
- Scratch: `spike/` (throwaway, not committed)

- [ ] **Step 1 (A1):** Run `lsb_release -rs; ls -l /dev/kvm; id -nG`. Expected: `24.04` or later, `/dev/kvm` exists, and the user is in `kvm`. If not, stop and report: Docker Sandboxes can't run on this laptop.
- [ ] **Step 2:** With the user's OK, install `sbx` and record the exact version for pinning. The user runs `sbx login`.
- [ ] **Step 3 (A2):** Build a template from Docker's Claude Code + Docker variant. Add a user `s1` without `sudo`, then `sbx create` from it. Record the template image name and tag, and the create command.
- [ ] **Step 4 (A3):**
  1. Allow `localhost:18091` on the sandbox and set `ANTHROPIC_BASE_URL=http://host.docker.internal:18091` with the proxy's placeholder token.
  2. Run `claude -p "say hi"` as `s1`.
  3. Expected: a reply. The proxy log shows `X-Claude-Code-Session-Id`, and `/proxy/whoami?sid=` names an account.
- [ ] **Step 5 (A4):**
  1. `sbx exec -d` a loop that curls `host.docker.internal:<test port>` every 5 s.
  2. Stop and start the sandbox.
  3. Record whether the process survives. If it doesn't, record the command that relaunches it.
- [ ] **Step 6 (A5, A9):** Find the per-sandbox rule syntax that gives one sandbox open egress and another Locked Down with exactly two allows. Expected:
  - in the locked sandbox, `curl https://example.com` fails and `curl host.docker.internal:18091` works
  - in the open sandbox, `curl https://pypi.org` works
  Record whether this needs global Open plus per-sandbox deny, or global Locked Down plus per-sandbox allow-all.
- [ ] **Step 7 (A6):** `docker compose up` a two-service sample app inside the sandbox. Expected: it serves on `localhost` inside the VM.
- [ ] **Step 8 (A7):** On the host, serve a bare repo with `git http-backend` on `127.0.0.1:18080`, allow it, then `git clone` and `git push` from the sandbox. Expected: both work.
- [ ] **Step 9 (A8):** `sbx secret set-custom` an `api-key` header for a test host. Expected:
  - a request from inside the sandbox arrives carrying the header, observed with a host-side echo server
  - `env` and the filesystem inside the sandbox show only the placeholder
- [ ] **Step 10:** Record `--cpus`/`--memory`, the Docker volume size setting, how to list sandboxes and their state as JSON, and where policy refusals are logged.
- [ ] **Step 11:** Write the findings doc as a table of A1–A9, PASS/FAIL, evidence, and exact commands, plus the pinned version. **Go/no-go:** A1, A3, A4, A5 and A9 must pass. If one fails, stop and ask the user (the spec's R1 fallback is a plain-container `SandboxProvider`).
- [ ] **Step 12:** Commit.

```bash
git add docs/superpowers/spikes/2026-11-13-sbx-spike.md
git commit -m "docs: Docker Sandboxes spike findings"
```

---

### Task 2: Config v2

**Files:**
- Modify:
  - `src/agent/config.py`
  - `examples/config/agent.toml`
  - `examples/config/packs/sample/project.toml`
  - `examples/pack-templates/chilliclaw/project.toml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `SandboxResources(cpus: int, memory_gb: int, disk_gb: int)`
  - `SandboxSettings(resources: dict[str, SandboxResources], host_memory_budget: float = 0.75, idle_stop_minutes: int = 30, rebuild_every_days: int = 7)`. `resources` keys are `"code"`, `"debug"` and `"general"`.
  - `Limits(max_concurrent_sessions=3, max_tasks_per_day=20, task_minutes=90, max_turns=400)`, which replaces the old `Limits` and `RoleLimits`
  - `Models(default="claude-opus-5-5", hard="claude-fable-5-1", chores="claude-sonnet-5")`
  - `CliRules(command: str, profiles: list[str], env: dict[str, str], login: list[str] | None, login_check: list[str] | None, relogin_patterns: list[str], read: list[str], confirm: list[str], deny: list[str], egress: list[str], max_output_chars: int = 8000)`
  - `InjectRule(header: str, secret: str)`
  - `AgentConfig` gains `sandbox: SandboxSettings` and `cli: dict[str, CliRules]` (from `[cli.<name>]`). `memory_budget` keys become `"session"`, `"production"` and `"ambient"`.
  - `ProjectConfig` gains:
    - `network: str` (`"open"` or `"read"`)
    - `egress_allow: list[str]`
    - `egress_inject: dict[str, InjectRule]`
    - `scrub_patterns: list[str]`
    - `sandbox_setup: dict[str, list[str]]`
    - `sandbox_resources: dict[str, SandboxResources]`
    - `sandbox_template: dict[str, str]` (profile → image; optional)

    It drops `mcp` and `secrets_core`. `linear_status` keys are `working`, `ready_for_review`, `merged` and `failed`.
  - `ROLES`, `WORKER_ROLES` and `Models.for_role` are removed.
  - `AZ_BUILTIN_DENY = ("account get-access-token", "rest")` is always merged into `cli["azure"].deny`.

- [ ] **Step 1: Write failing tests.**

```python
def test_defaults(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, ""))
    assert cfg.limits.max_concurrent_sessions == 3 and cfg.limits.task_minutes == 90
    assert cfg.models.default == "claude-opus-5-5" and cfg.models.hard == "claude-fable-5-1"
    assert cfg.sandbox.resources["code"] == SandboxResources(4, 8, 30)
    assert cfg.sandbox.resources["debug"] == SandboxResources(2, 2, 10)

def test_az_builtin_deny_always_present(tmp_path):
    cfg = load_agent_config(write_agent(tmp_path, '[cli.azure]\ncommand = "az"\nprofiles = ["debug"]\ndeny = []'))
    assert {"account get-access-token", "rest"} <= set(cfg.cli["azure"].deny)

def test_cli_only_in_debug_profile(tmp_path):
    with pytest.raises(ConfigError, match="debug"):
        load_agent_config(write_agent(tmp_path, '[cli.azure]\ncommand = "az"\nprofiles = ["code"]'))

def test_pack_rejects_wildcard_egress(tmp_path):
    with pytest.raises(ConfigError, match="wildcard"):
        load_pack(write_pack(tmp_path, '[egress]\nallow = ["*.github.io"]'), agent_cfg())

def test_pack_network_values(tmp_path):
    assert load_pack(write_pack(tmp_path, ""), agent_cfg()).project.network == "open"
    with pytest.raises(ConfigError):
        load_pack(write_pack(tmp_path, 'network = "closed"'), agent_cfg())

def test_pack_inject_and_setup(tmp_path):
    p = load_pack(write_pack(tmp_path, '[egress.inject."x.example.com"]\nheader = "api-key"\nsecret = "X_KEY"\n'
                                       '[sandbox.code]\nsetup = ["apt-get install -y jq"]'), agent_cfg()).project
    assert p.egress_inject["x.example.com"] == InjectRule("api-key", "X_KEY")
    assert p.sandbox_setup["code"] == ["apt-get install -y jq"]
```

- [ ] **Step 2:** `just test tests/test_config.py`. Expected: FAIL (new names undefined).
- [ ] **Step 3:** Implement in `config.py`.
  - Wildcard check: any host containing `*` or `?` is an error. The message names the host and says "no wildcards".
  - Unknown profile keys under `[sandbox.*]` are errors.
  - Update the three example files to the v2 keys. The ChilliClaw template keeps its `CONFIRM` markers.
- [ ] **Step 4:** `just test tests/test_config.py`. Expected: PASS. Then run `just test`: other modules' failures caused by removed names are expected until their tasks. Record them in the ledger rather than fixing them here.
- [ ] **Step 5:** Commit: `feat(config): v2 config for sandboxes, CLI rules and limits`.

### Task 3: Task statuses and the v2 schema

**Files:**
- Modify: `src/agent/tasks.py`, `src/agent/db.py`
- Test: `tests/test_tasks.py`, `tests/test_db_migrate.py` (new)

**Interfaces:**
- **Produces `Status`:** `queued`, `working`, `needs_input`, `ready_for_review`, `done`, `merged`, `closed`, `failed`, `cancelled`. `TERMINAL = {done, merged, closed, cancelled}`.
- **Legal edges:**
  - `queued→working`
  - `working→{needs_input, ready_for_review, done, failed, queued}`
  - `needs_input→{working, queued}`
  - `ready_for_review→{merged, closed, working}`
  - `failed→queued`
  - any non-terminal → `cancelled`
- **`Task`** drops `criteria` and `fix_cycles`.
- **New tables:**
  - `conversations(id PK, channel, thread_ts, sandbox, project, task_id, session_id, scope, is_primary INT, waiting INT, model, created_at, last_active_at, consolidated_at)`, indexed on `(channel, thread_ts)`
  - `work_items(id PK, conversation_id, sandbox, kind, payload, status, lease_until, attempts, result, created_at, updated_at)`. `kind` is `turn`, `production` or `chore`; `status` is `queued`, `claimed`, `done`, `failed` or `cancelled`.
  - `tokens(hash PK, kind, sandbox, work_item_id, expires_at, revoked INT)`
  - `proposals(id PK, conversation_id, kind, payload, status, created_at, expires_at)`
- **`SCHEMA_VERSION = 2`.** `migrate(conn)` upgrades a v1 database:
  - statuses `building`, `investigating`, `specifying` and `testing` become `working`
  - the v1 `conversations` table is renamed to `conversations_v1`

- [ ] **Step 1: Failing tests.**

```python
def test_status_edges(conn):
    t = make_task(conn)
    transition(conn, t.id, Status.working, actor="x")
    transition(conn, t.id, Status.done, actor="x")
    with pytest.raises(IllegalTransition):
        transition(conn, t.id, Status.working, actor="x")

def test_cancel_from_any_open_state(conn):
    t = make_task(conn); transition(conn, t.id, Status.working, actor="x")
    assert transition(conn, t.id, Status.cancelled, actor="x").status == Status.cancelled

def test_migrate_v1_maps_statuses(tmp_path):
    conn = v1_database_with_task(tmp_path, status="building")
    db.migrate(conn)
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "working"
    assert db.get_setting(conn, "schema_version") == "2"
    assert {"work_items", "tokens", "proposals"} <= table_names(conn)
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `migrate` is idempotent: running it twice is a no-op.
- [ ] **Step 4:** `just test tests/test_tasks.py tests/test_db_migrate.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(tasks): v2 statuses and schema`.

### Task 4: Session tokens, work queue and start gate

**Files:**
- Create: `src/agent/core/sessions.py`
- Test: `tests/test_sessions.py`

**Interfaces:**
- **Tokens:**
  - `mint_token(conn, kind: Literal["sandbox","session"], *, sandbox: str, work_item_id: str | None, ttl_s: int) -> str`. Stores only the SHA-256 of the token.
  - `TokenInfo(kind, sandbox, work_item_id)`
  - `resolve_token(conn, token: str, now: datetime | None = None) -> TokenInfo | None`
  - `revoke_for_work_item(conn, work_item_id) -> None`
- **`WorkItem(id, conversation_id, sandbox, kind, payload: dict, status, attempts)`**
- **`WorkQueue(conn, lease_s: int = 90, max_attempts: int = 2)`:**
  - `enqueue(conversation_id: str, sandbox: str, kind: str, payload: dict) -> str`
  - `claim(sandbox: str, now: datetime | None = None) -> WorkItem | None`. Oldest first. It skips an item whose conversation already has a `claimed` item. It first re-queues expired leases (`attempts += 1`), and marks them `failed` once `attempts >= max_attempts`.
  - `heartbeat(item_id) -> bool`. False if the item was cancelled.
  - `complete(item_id, result: dict)`
  - `cancel(item_id)`
  - `cancel_all() -> list[str]`
- **`Gate(conn, config, proxy)`:** `async can_start() -> tuple[bool, str]`. Refuses with:
  - `"paused"` when the `paused` setting is on
  - `"waiting for capacity"` when `(await proxy.usage()).has_capacity()` is false
  - `"busy"` when the count of claimed items ≥ `max_concurrent_sessions`

- [ ] **Step 1: Failing tests,** including Review Focus 1 and 2.

```python
def test_one_claimed_item_per_conversation(conn):
    q = WorkQueue(conn); a = q.enqueue("c1", "code-p", "turn", {"text": "1"}); q.enqueue("c1", "code-p", "turn", {"text": "2"})
    assert q.claim("code-p").id == a
    assert q.claim("code-p") is None          # second message waits for the first
    q.complete(a, {"text": "ok"})
    assert q.claim("code-p").payload["text"] == "2"

def test_expired_lease_requeues_then_fails(conn):
    q = WorkQueue(conn, lease_s=1, max_attempts=2); i = q.enqueue("c", "code-p", "turn", {})
    q.claim("code-p", now=T0)
    assert q.claim("code-p", now=T0 + timedelta(seconds=5)).attempts == 1
    assert q.claim("code-p", now=T0 + timedelta(seconds=10)) is None
    assert status_of(conn, i) == "failed"

def test_token_revoked_and_expired(conn):
    t = mint_token(conn, "session", sandbox="code-p", work_item_id="w1", ttl_s=60)
    assert resolve_token(conn, t).work_item_id == "w1"
    revoke_for_work_item(conn, "w1"); assert resolve_token(conn, t) is None
    t2 = mint_token(conn, "session", sandbox="code-p", work_item_id="w2", ttl_s=1)
    assert resolve_token(conn, t2, now=utc_in(5)) is None

async def test_gate(conn, fake_proxy):
    db.set_setting(conn, "paused", "1"); assert (await Gate(conn, cfg(), fake_proxy).can_start()) == (False, "paused")
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Claim inside `BEGIN IMMEDIATE`.
- [ ] **Step 4:** `just test tests/test_sessions.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): work queue, session tokens and start gate`.

### Task 5: The agent API

**Files:**
- Create: `src/agent/core/agent_api.py`
- Test: `tests/test_agent_api.py`

**Interfaces:**
- **Consumes:** `WorkQueue`, `Gate`, `mint_token` and `resolve_token` (Task 4); `AgentToolRouter.call` and `manifest` (Task 7); `build_bundle` (Task 16).
- **Produces:** `make_agent_app(*, conn, queue, gate, tools, bundles, on_result: Callable[[WorkItem, dict], Awaitable[None]], on_files: Callable[[str, list[tuple[str, bytes]], str], Awaitable[None]], pull_wait_s: float = 25) -> web.Application`.

| Route | Token | Body or response |
|---|---|---|
| `POST /agent/pull` | sandbox | `200 {"work_item": {...}, "session_token": str, "tools": [manifest]}` or `204` after `pull_wait_s` |
| `POST /agent/heartbeat` | session | `{"cancel": bool}` |
| `POST /agent/events` | session | `{"event": {...}}` → `{"cancel": bool}` |
| `POST /agent/tool` | session | `{"name", "args"}` → `{"text", "file": {"name","content_b64"} | null}` |
| `POST /agent/files` | session | multipart files plus `text` → `{"ok": true}` |
| `POST /agent/result` | session | `{"result": {...}}`. Completes the item and revokes the token. |
| `GET /agent/bundle?profile=&project=` | sandbox | `application/gzip`, with header `X-Bundle-Version` |
| `GET /agent/activity` | `SANDBOXD_TOKEN` (a `core` secret shared with `sandboxd`) | `{"busy": [sandbox names]}` (Task 19) |
| `GET /health` | none | `ok` |

- **Pulling:**
  - A pull for sandbox X with a sandbox token minted for Y gets 403.
  - Pull only hands out work when `gate.can_start()` is true.
  - A session token expires after `task_minutes` + 10 minutes.

- [ ] **Step 1: Failing tests,** including Review Focus 3.

```python
async def test_revoked_session_token_is_401(api, conn, queue):
    item, tok = await pull_one(api, "code-p")
    await api.post("/agent/result", json={"result": {"text": "done"}}, headers=bearer(tok))
    r = await api.post("/agent/tool", json={"name": "status", "args": {}}, headers=bearer(tok))
    assert r.status == 401

async def test_sandbox_token_bound_to_its_sandbox(api, conn):
    tok = mint_token(conn, "sandbox", sandbox="code-a", work_item_id=None, ttl_s=3600)
    r = await api.post("/agent/pull", json={"sandbox": "code-b"}, headers=bearer(tok))
    assert r.status == 403

async def test_session_token_cannot_pull(api, conn):
    _, tok = await pull_one(api, "code-p")
    assert (await api.post("/agent/pull", json={"sandbox": "code-p"}, headers=bearer(tok))).status == 401

async def test_heartbeat_reports_cancel(api, queue):
    item, tok = await pull_one(api, "code-p"); queue.cancel(item["id"])
    assert (await (await api.post("/agent/heartbeat", json={}, headers=bearer(tok))).json())["cancel"] is True

async def test_pull_respects_gate(api_paused):
    assert (await pull_status(api_paused, "code-p")) == 204
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - Long-poll by checking the queue every 0.5 s until `pull_wait_s`.
  - Cancellation also revokes the item's session token.
  - `/agent/events` appends each event to `/data/runs/<work_item_id>.jsonl`, which `get_run_log` and metrics use.
  - `/agent/result` records turns, tokens, cost and `account_code` in `runs` (Phase 1 columns).
- [ ] **Step 4:** `just test tests/test_agent_api.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): agent API for sandbox runners`.

### Task 6: `core`'s git endpoint

**Files:**
- Create: `src/agent/gitutil.py` (moved from `worker/repo.py`: `git`, `credential_args`, `slugify`, `branch_name`, `GitError`)
- Create: `src/agent/core/gitserve.py`
- Test: `tests/test_gitserve.py`

**Interfaces:**
- **Produces `GitHost(root: Path, *, agent_name: str, read_token: str | None, upstream_url: Callable[[str], str] = lambda repo: f"https://github.com/{repo}.git")`:**
  - `ensure(project: str, repo: str) -> Path`. Creates the bare repo `root/<project>.git` and installs `hooks/pre-receive`.
  - `refresh(project: str) -> None`. Fetches `+refs/heads/*:refs/heads/*` from upstream, except `refs/heads/<agent_name>/*`.
  - `tip(project: str, branch: str) -> str | None`
  - `push_to_github(project: str, repo: str, branch: str, sha: str, write_token: str) -> None`. No `--force`. The token goes through `credential_args`, never into argv.
  - `aiohttp_handler(request) -> web.StreamResponse`. Runs `git http-backend` as CGI, mounted at `/git/{project}.git/{tail:.*}`; auth is a sandbox or session token.
- **The `pre-receive` hook rejects:**
  - refs outside `refs/heads/<agent_name>/`
  - deletes (new SHA all zeros)
  - tags

- [ ] **Step 1: Failing tests,** with a local `file://` upstream fixture and an aiohttp test server (Review Focus 4).

```python
def test_rejects_main_tags_and_deletes(githost_server, workdir):
    clone(githost_server, workdir)
    assert push(workdir, "HEAD:refs/heads/main").returncode != 0
    assert push(workdir, "HEAD:refs/tags/v1").returncode != 0
    assert push(workdir, "HEAD:refs/heads/agent/t1-x").returncode == 0
    assert push(workdir, ":refs/heads/agent/t1-x").returncode != 0

def test_refresh_brings_upstream_commits(githost, upstream):
    new = commit_upstream(upstream, "main"); githost.refresh("p")
    assert githost.tip("p", "main") == new

def test_push_to_github_never_puts_token_in_argv(githost, monkeypatch):
    argv = capture_git_argv(monkeypatch); githost.push_to_github("p", "o/r", "agent/t1-x", sha(), "SECRET")
    assert all("SECRET" not in a for a in argv) and "--force" not in argv
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Move the helpers into `gitutil.py`; the worker imports are removed in Task 21.
- [ ] **Step 4:** `just test tests/test_gitserve.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): git endpoint that accepts only agent branches`.

### Task 7: The tool router, plus conversation, tracking and operations tools

**Files:**
- Create: `src/agent/core/agent_tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- **Produces:**
  - `ToolContext(work_item_id, conversation_id, sandbox, profile: Literal["code","debug","general"], project: str | None, actor: str | None, tier: Literal["read","act","admin"], unattended: bool, task_id: str | None, channel: str | None, thread_ts: str | None)`
  - `ToolReply(text: str, file: tuple[str, bytes] | None = None)`
  - `ToolSpec(name, description, input_schema: dict, tier: str, profiles: tuple[str, ...])`
  - `AgentToolRouter(conn, *, config, packs, queue, memory, slack, notifier, secret_values)`:
    - `register(spec: ToolSpec, handler: Callable[[ToolContext, dict], Awaitable[ToolReply]])`
    - `manifest(ctx) -> list[dict]`
    - `async call(ctx, name, args) -> ToolReply`. Refuses a tool that is unknown, of a higher tier, or outside its profile with `ToolReply("not available here")`. Every reply goes through `scrub()`.
  - `context_for(conn, work_item_id) -> ToolContext`
- **Tools registered here:**

  | Tool | Tier | Profiles |
  |---|---|---|
  | `post_update(text)` | act | all |
  | `ask(question)` | act | all |
  | `react(emoji)` | act | all |
  | `track_task(project, title, kind)` | act | code, general |
  | `finish_task(outcome)` | act | code, general |
  | `start_task(project, title, kind, brief)` | act | code, general |
  | `status()` | read | all |
  | `list_tasks(status?, project?)` | read | all |
  | `get_task(task_id)` | read | all |
  | `cancel_task(task_id)` | act | all |
  | `stop_all()` | act | all |
  | `resume_all()` | admin | all |
  | `read_thread(limit=50)` | read | all |
  | `recall` / `read_memory` / `remember` | read / read / act | all; via `MemoryToolbox` with `scopes_for` |

- **What the tools do:**
  - `ask` sets the conversation's `waiting = 1` and the task's status to `needs_input`, and posts the question.
  - `track_task` and `start_task` refuse with "daily task limit reached" when ≥ `max_tasks_per_day` tasks were created today.
  - `start_task` creates a task, a conversation in `code-<project>` (same channel and thread, `is_primary = 0`) and a `turn` work item whose payload is the brief.
- **`stop_all`** sets the `paused` setting and calls `queue.cancel_all()`, which revokes every session token; runners interrupt on their next heartbeat. **`resume_all`** clears `paused`.
- **The tier comes from the actor:** a member gets `act`, an admin gets `admin`, and an `unattended` context with no actor gets `act`.

- [ ] **Step 1: Failing tests.**

```python
async def test_member_cannot_resume_all(router, ctx_member):
    assert (await router.call(ctx_member, "resume_all", {})).text == "not available here"

async def test_manifest_filters_by_profile(router, ctx_debug):
    names = {t["name"] for t in router.manifest(ctx_debug)}
    assert "track_task" not in names and "status" in names

async def test_ask_marks_waiting(router, ctx_with_task, conn):
    await router.call(ctx_with_task, "ask", {"question": "which env?"})
    assert task_status(conn) == "needs_input" and convo_waiting(conn) == 1

async def test_start_task_queues_code_session(router, ctx_general, conn):
    await router.call(ctx_general, "start_task", {"project": "sample", "title": "fix x", "kind": "bug", "brief": "b"})
    item = latest_item(conn); assert item.sandbox == "code-sample" and item.payload["text"] == "b"

async def test_daily_task_limit(router, ctx_general, conn):
    make_tasks_today(conn, 20)
    assert "daily task limit" in (await router.call(ctx_general, "track_task",
                                  {"project": "sample", "title": "t", "kind": "bug"})).text
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Reuse `CoreTools` handlers where they fit; `CoreTools` itself is deleted in Task 21.
- [ ] **Step 4:** `just test tests/test_agent_tools.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): tool router with tiers and profiles`.

### Task 8: The `open_pr` tool

**Files:**
- Create: `src/agent/core/prs.py` (`pr_body` moved from `worker/repo.py`, plus `open_pr`)
- Test: `tests/test_prs.py`

**Interfaces:**
- **Consumes:** `GitHost.tip`, `GitHost.push_to_github` (Task 6); `GitHubWriter` (Phase 1); `AgentToolRouter.register` (Task 7).
- **Produces:** the `open_pr` tool (tier act, profile code), with args `title, body, branch, sha, checks: {"passed": bool, "log_tail": str}`. The runner fills in `branch`, `sha` and `checks` (Task 17). `open_pr` refuses:
  - with "branch must start with <name>/" when the branch has the wrong prefix
  - with "sha is not the tip of <branch>" when `githost.tip(...) != sha`
  - with "checks failed" plus the log tail when `checks.passed` is false

  On success, it:
  1. pushes to GitHub
  2. reuses an open PR for the branch, or opens a draft
  3. sets the assignee to the requester's GitHub login, falling back to the pack owner's
  4. stores `pr_number` and `pr_url` on the task and moves it to `ready_for_review`
  5. sets Linear to `linear_status["ready_for_review"]`
  6. returns the PR URL

- [ ] **Step 1: Failing tests** (Review Focus 4).

```python
async def test_refuses_non_tip_sha(ctx_code, router, githost):
    r = await router.call(ctx_code, "open_pr", pr_args(branch="agent/t1-x", sha="0" * 40, passed=True))
    assert "not the tip" in r.text

async def test_refuses_failed_checks(ctx_code, router, githost_with_branch):
    assert "checks failed" in (await router.call(ctx_code, "open_pr", pr_args(passed=False))).text

async def test_opens_draft_assigned_to_requester(ctx_code, router, fake_writer, githost_with_branch, conn):
    r = await router.call(ctx_code, "open_pr", pr_args(passed=True))
    assert fake_writer.drafts == [("o/r", "agent/t1-x")] and fake_writer.assignee == "alice-example"
    assert task(conn).status == "ready_for_review" and "https://" in r.text

async def test_reuses_open_pr(ctx_code, router, fake_writer_with_open_pr, githost_with_branch):
    await router.call(ctx_code, "open_pr", pr_args(passed=True)); assert fake_writer_with_open_pr.drafts == []
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_prs.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): open_pr tool, the only way code reaches GitHub`.

### Task 9: GitHub and Linear read and update tools

**Files:**
- Modify: `src/agent/connectors/github_read.py`, `src/agent/connectors/linear.py`
- Create: `src/agent/core/tracker_tools.py`
- Test: `tests/test_tracker_tools.py`

**Interfaces:**
- **Produces in `GitHubReader`:**
  - `list_prs(repo, state="open") -> list[dict]`
  - `checks(repo, ref) -> list[dict]`
  - `job_log(repo, job_id, max_chars=20000) -> str` (keeps the tail)
  - `issue(repo, n) -> dict`
  - `file_at(repo, path, ref) -> str`
- **Produces in `LinearClient`:** `issue(issue_id) -> dict`, including its comments.
- **Tools** (all profiles; the project comes from `ctx`):
  - `gh_prs`, `gh_checks`, `gh_ci_log`, `gh_issue`, `gh_file` (read)
  - `linear_issue` (read)
  - `linear_comment` (act)
  - `linear_set_status(status_key)` (act). The key must be in the pack's `linear_status`, otherwise the tool refuses and lists the valid keys.

- [ ] **Step 1: Failing tests** with a fake HTTP transport: each tool returns the formatted JSON, `gh_ci_log` keeps the last 20000 chars, and `linear_set_status("nope")` refuses.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_tracker_tools.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): GitHub and Linear tools`.

### Task 10: CLI rules and runner (`az`)

**Files:**
- Create: `src/agent/core/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- **Produces:**
  - `leading_words(args: list[str]) -> list[str]`: the words before the first argument that starts with `-`
  - `matches(args, pattern: str) -> bool`: a word glob, where `*` matches any number of words
  - `classify(rules: CliRules, args, *, unattended: bool) -> Literal["deny","confirm","run"]`. In order:
    1. `deny` → deny
    2. if unattended: `read` → run, otherwise deny
    3. `confirm` → confirm
    4. otherwise run
  - `CliRunner(rules: CliRules, *, secret_values: list[str], exec_fn=asyncio.create_subprocess_exec)`
  - `async run(args, *, save_to: str | None = None) -> ToolReply`:
    - runs `[rules.command, *args]` with no shell, `env = os.environ | rules.env`, and a 60 s timeout
    - scrubs the output, then caps it at `max_output_chars` with the note `"[output cut at N chars; narrow it, e.g. --query]"`
    - with `save_to`, returns `file=(save_to, full_scrubbed_bytes)` and the preview as text
  - `valid_save_name(name) -> bool`: `^[A-Za-z0-9._-]{1,100}$`, and not `.` or `..`
  - `RELOGIN = "The Azure login has expired."`: returned when the output matches `relogin_patterns`
- **Tools** (profile debug):
  - `az(args, save_to?)`, tier act. `confirm` → the Task 11 proposal; `deny` → "refused: <reason>".

- [ ] **Step 1: Failing tests** (Review Focus 5).

```python
@pytest.mark.parametrize("args,unattended,expected", [
    (["account", "get-access-token"], False, "deny"),
    (["rest", "--url", "https://x"], False, "deny"),
    (["group", "delete", "-n", "g"], False, "confirm"),
    (["monitor", "log-analytics", "query", "-w", "w"], False, "run"),
    (["webapp", "restart", "-n", "a"], False, "run"),
    (["webapp", "restart", "-n", "a"], True, "deny"),
    (["monitor", "log-analytics", "query"], True, "run"),
])
def test_classify(az_rules, args, unattended, expected):
    assert classify(az_rules, args, unattended=unattended) == expected

async def test_huge_output_capped_and_saved(az_runner_with_output):
    r = await az_runner_with_output("x" * 50_000_000).run(["monitor", "log-analytics", "query"], save_to="logs.json")
    assert len(r.text) <= 8200 and len(r.file[1]) == 50_000_000

def test_save_name_rejects_traversal():
    assert not valid_save_name("../../etc/x") and not valid_save_name("..") and valid_save_name("logs.json")

async def test_secret_scrubbed_and_relogin(az_runner_with_output):
    assert "SECRETVALUE" not in (await az_runner_with_output("token SECRETVALUE").run(["account", "show"])).text
    assert (await az_runner_with_output("Please run 'az login'").run(["account", "show"])).text.startswith(RELOGIN)
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Install `azure-cli` in the core image in Task 21; the tests use `exec_fn` fakes.
- [ ] **Step 4:** `just test tests/test_cli.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): az with deny, confirm and read rules`.

### Task 11: Confirm proposals and device-code login

**Files:**
- Create: `src/agent/core/cli_admin.py`
- Modify: `src/agent/connectors/slack_app.py` (an action handler), `deploy/slack-app-manifest.yaml` (`interactivity: true`, scope `im:write`)
- Test: `tests/test_cli_admin.py`

**Interfaces:**
- **Produces `Proposals(conn, slack, runner_for: Callable[[str], CliRunner], queue)`:**
  - `async propose(ctx, cli: str, args: list[str]) -> str`. Stores a proposal (24 h expiry) and posts the command with `Confirm` and `Cancel` buttons, action IDs `cli_confirm:<id>` and `cli_cancel:<id>`. Returns "Waiting for an admin to confirm."
  - `async on_action(action_id: str, slack_user: str) -> str`:
    1. only an admin may act; anyone else gets "Only an admin can confirm this."
    2. an expired proposal gets "This request expired."
    3. otherwise it runs the command, posts the capped output to the thread, and enqueues a `turn` item for the conversation with text `"Result of the confirmed command `az …`:\n<output>"`
- **Produces `DeviceCodeLogin`:**
  - `parse(output: str) -> tuple[str, str] | None`: the URL `https://\S+` and the code (the token after "enter the code")
  - `async start(ctx, cli: str) -> str` (admin tier, tool `az_login`). Starts the login command, DMs the URL and code to `ctx.actor` only (through `conversations.open`), then in the background waits for exit, runs `login_check` and posts "Logged in as <user> (<subscription>)" to the thread.

- [ ] **Step 1: Failing tests:**
  - a non-admin click is refused
  - an expired proposal is refused
  - a confirm runs the command and enqueues a turn
  - `parse` handles the real `az` text, e.g. `"To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code ABCD1234 to authenticate."`, returning `("https://microsoft.com/devicelogin", "ABCD1234")`
  - the DM goes to the requesting admin's Slack ID only
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_cli_admin.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): confirm buttons and device-code login for CLIs`.

### Task 12: `ask_production` and the customer-data scrubber

**Files:**
- Create: `src/agent/core/production.py`
- Test: `tests/test_production.py`

**Interfaces:**
- **Produces:**
  - `scrub_customer(text: str, patterns: list[str]) -> tuple[str, int]`. It replaces these with `[customer-data]` and returns the hit count:
    - emails
    - phone numbers (a `+` or a digit run of 8 or more, allowing spaces and dashes)
    - IPv4 and IPv6 addresses
    - JWT-like strings (`eyJ…`)
    - `Bearer <token>`
    - each pack pattern

    It keeps ISO timestamps, file paths with line numbers, HTTP paths and numeric counts.
  - The tool `ask_production(request)` (tier act, profiles code and general; needs `ctx.project`) does the following:
    1. enqueues a `production` item for `debug-<project>`, with payload `{request, actor, unattended}`
    2. waits for it to complete (poll every 1 s; timeout 15 min → "The production check timed out.")
    3. returns `scrub_customer(scrub(result_text))`
    4. logs an `events` row `production_scrub` with the hit count

- [ ] **Step 1: Failing tests.**

```python
def test_scrub_keeps_debugging_detail():
    text = "2026-11-13T10:00:00Z GET /api/citations/42 500 user=jane@x.com ip=10.1.2.3 at export.py:88 (17 times)"
    out, hits = scrub_customer(text, [])
    assert "jane@x.com" not in out and "10.1.2.3" not in out and hits == 2
    assert "2026-11-13T10:00:00Z" in out and "/api/citations/42" in out and "export.py:88" in out and "17 times" in out

def test_pack_pattern(): assert scrub_customer("cust C-12345 failed", [r"C-\d{5}"])[0] == "cust [customer-data] failed"

async def test_not_available_in_debug(router, ctx_debug):
    assert (await router.call(ctx_debug, "ask_production", {"request": "x"})).text == "not available here"

async def test_answer_is_scrubbed(router, ctx_code, complete_next_item_with):
    complete_next_item_with("crash for bob@y.com")
    assert "bob@y.com" not in (await router.call(ctx_code, "ask_production", {"request": "why"})).text
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_production.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): ask_production with customer-data scrubbing`.

### Task 13: Conversations (Slack and Linear intake), replacing `ChatService` and `Dispatcher`

**Files:**
- Create: `src/agent/core/conversations.py`
- Modify: `src/agent/core/notifier.py` (words and reactions for the v2 statuses: `working` 🔨, `needs_input` ❓, `ready_for_review` ✅, `done` ✅, `failed` ❌), `src/agent/connectors/linear.py` (`sync` uses the keys `working`, `ready_for_review`, `merged` and `failed`)
- Test: `tests/test_conversations.py`

**Interfaces:**
- **Consumes:** `WorkQueue` (Task 4), `build_turn_payload` (Task 14), `SlackNotifier` (Phase 1, status words updated to v2 statuses), `LinearClient`.
- **Produces `Conversations(conn, *, config, packs, queue, slack, payloads, notifier, bot_user_id)`:**
  - `async handle_slack(event: dict) -> None`:
    1. **Ignore** bots, subtypes and its own messages, and dedupe by `channel:ts` (Phase 1 behaviour).
    2. **Unknown person:** one ephemeral reply.
    3. **Routing, in order:**
       - a waiting conversation in this thread gets the reply (and `waiting` is set back to 0)
       - otherwise the thread's primary conversation
       - otherwise a new conversation: a DM goes to `general`; a channel that is some pack's `slack_channel` goes to `code-<project>`; any other configured channel goes to `general`
    4. **It acts on** mentions, DMs, and replies in threads it already has.
    5. It enqueues a `turn` item, payload `payloads.turn(convo, text, actor)`. "use fable" sets `conversations.model` to `models.hard`.
  - `async on_result(work_item: WorkItem, result: dict) -> None`:
    - **Turns:** posts `result["text"]` to the thread (or `FAILED_REPLY` for outcomes `error`, `timeout` or `max_turns`), stores `session_id`, and updates `last_active_at`.
    - **Production items:** nothing to post.
  - `async intake_linear() -> list[str]`. Uses `LinearPoller` (Phase 1) to create tasks. For each new task:
    1. posts a thread root in the pack's channel
    2. creates the conversation in `code-<project>`
    3. enqueues an unattended `turn`, with payload text `"Linear issue <identifier>: <title>\n\n<description>"`

- [ ] **Step 1: Failing tests** (Review Focus 1, end to end).

```python
async def test_second_message_waits_for_first(convos, queue):
    await convos.handle_slack(mention("t1", "do a")); await convos.handle_slack(reply("t1", "also b"))
    first = queue.claim("code-sample"); assert queue.claim("code-sample") is None
    queue.complete(first.id, {"text": "ok"}); assert queue.claim("code-sample").payload["text"].endswith("also b")

async def test_dm_goes_to_general(convos, queue):
    await convos.handle_slack(dm("hi")); assert queue.claim("general") is not None

async def test_reply_goes_to_waiting_split_session(convos, conn, queue):
    child = waiting_child_conversation(conn, thread="t1")
    await convos.handle_slack(reply("t1", "use staging")); assert last_item(conn).conversation_id == child

async def test_unknown_person_gets_ephemeral(convos, slack):
    await convos.handle_slack(mention("t1", "hi", user="U999")); assert slack.ephemerals == 1

async def test_use_fable_sticks(convos, conn):
    await convos.handle_slack(mention("t1", "use fable and fix x"))
    assert convo(conn, "t1").model == "claude-fable-5-1"

async def test_linear_issue_is_unattended(convos, fake_linear, queue):
    await convos.intake_linear(); assert queue.claim("code-sample").payload["unattended"] is True
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_conversations.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): conversations route Slack and Linear to sandboxes`.

### Task 14: Work payloads (what a session starts with)

**Files:**
- Create: `src/agent/core/payloads.py`
- Test: `tests/test_payloads.py`

**Interfaces:**
- **Produces `Payloads(conn, *, config, packs, memory, persona_text, router)`:**
  - `turn(convo, text: str, actor: str | None, *, unattended: bool = False) -> dict`
  - `production(project, request, actor, unattended) -> dict`
  - `chore(prompt: str, schema: dict) -> dict`

  Each returns a dict with these keys:
  - `text`
  - `system_prompt`: `config/agent/system.md`, then the persona (turns only), then the memory briefing
  - `model`: the conversation's `model`, or `models.default`; `models.chores` for chores
  - `max_turns` and `timeout_s` (`task_minutes * 60`)
  - `resume_session_id`
  - `worktree`: `{project, default_branch, branch_hint: "<name>/<task-or-convo-id>-<slug>", setup: build_setup, checks: build_checks}`. Only for `code`; `debug` gets `{project, default_branch, read_only: true}`; `general` gets none.
  - `test_secrets`: the pack's `secrets_worker` values. Only for `code`.
  - `unattended`, `actor`, `profile`
  - `structured`: the schema, for chores only. The runner adds a `submit_result` tool for it.
- **Memory budgets:** `"session"` for turns, `"production"` for production items, from `config.memory_budget`. Scopes come from `scopes_for(SessionContext(...))` for the conversation.

- [ ] **Step 1: Failing tests:**
  - persona text appears in turns and not in production or chores
  - the briefing stays within the budget
  - `test_secrets` appear only for the `code` profile
  - a debug worktree is `read_only`
  - "use fable" conversations get `claude-fable-5-1`
  - chores get `claude-sonnet-5` plus a `structured` schema
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Ship `src/agent/defaults/system.md` here. Its content: who the agent is and who it works for; act on the world only through tools; ask a human only when the request is unclear, never for permission; verify before saying done; reply briefly in Slack.
- [ ] **Step 4:** `just test tests/test_payloads.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): session payloads with system prompt, briefing and budgets`.

### Task 15: Memory consolidation through the `general` sandbox

**Files:**
- Modify: `src/agent/core/consolidation.py`
- Test: `tests/test_consolidation.py`

**Interfaces:**
- **Consumes:** `WorkQueue`, `Payloads.chore`, `apply_consolidation` (Phase 1).
- **Produces:** `ConsolidationService(conn, memory, queue, payloads, *, secret_values, mirror_for)`:
  - `sweep()` and `sweep_threads(now)` enqueue `chore` items for `general` instead of calling a runner
  - `on_result(item, result)` applies `result["structured"]` with `apply_consolidation`, and marks the task or thread consolidated

  The core-side runner path is removed.

- [ ] **Step 1:** Adapt the existing tests to the queue:
  - a finished task enqueues one chore
  - a result applies notes
  - a chore error leaves the task unconsolidated, to be picked up next sweep (closes Phase 1's deferred minor)
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_consolidation.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(memory): consolidation runs as a chore in the general sandbox`.

### Task 16: Skills and bundles

**Files:**
- Create in `src/agent/plugins/core/skills/`: `fix-bug/SKILL.md`, `build-feature/SKILL.md`, `verify-in-app/SKILL.md` and `review-before-pr/SKILL.md`. Keep `pr-description` and delete `report-outcome`.
- Create: `examples/pack-templates/chilliclaw/plugin/skills/chilliclaw-debugging/SKILL.md`, `azure-debugging/SKILL.md` (with `CONFIRM` placeholders: subscription, resource groups, Log Analytics and App Insights workspaces, useful KQL, how to run locally)
- Create: `src/agent/core/bundles.py`
- Test: `tests/test_bundles.py`

**Interfaces:**
- **Produces `build_bundle(config_dir: Path, profile: str, project: str | None) -> tuple[str, bytes]`.** It returns the version (SHA-256 of the contents) and a tar.gz holding:
  - `system.md`
  - `plugins/core/`
  - `plugins/pack/`, only when `profile` is `code` or `debug` and a project is set
- **Each skill's frontmatter `description`** is one line. The bodies follow the spec's §6:
  - **`fix-bug`:** gather signals with `ask_production` (failure, since when, how often, stack traces) and recent commits. Reproduce with a failing test. Fix. Run the tests and the app. Have a subagent review the diff against the request. Call `open_pr`. Reply with the cause and how it was verified.
  - **`build-feature`:** plan briefly, implement in small commits, test, then follow the `verify-in-app` and `review-before-pr` steps.
  - **`verify-in-app`:** bring the app up with `docker compose`, check it with Playwright, look at the screenshots, and `post_files` them.
  - **`review-before-pr`:** a fresh-context subagent compares the diff with the request and the repo's conventions.

- [ ] **Step 1: Failing tests:**
  - the `general` bundle has no `plugins/pack`
  - the `code` bundle has both plugins
  - the version is stable for unchanged inputs and changes when `system.md` changes
  - every skill has `name` and `description` frontmatter
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_bundles.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat: starting skills and config bundles for sandboxes`.

### Task 17: The sandbox runner

**Files:**
- Create in `src/agent/sandbox/`: `link.py`, `worktrees.py`, `remote_tools.py`, `runner.py`
- Modify: `src/agent/runner/base.py` (`RunSpec.permission_mode`), `src/agent/runner/claude.py`, `src/agent/__main__.py` (subcommand `runner`)
- Test: `tests/test_sandbox_runner.py`, `tests/test_claude_runner.py`

**Interfaces:**
- **Consumes:** every Task 5 route; `ClaudeRunner` (Phase 1).
- **Produces:**
  - `CoreLink(base_url: str, sandbox_token: str, http=None)`, with:
    - `pull(sandbox) -> dict | None`
    - `heartbeat(tok) -> bool` (cancel?)
    - `event(tok, ev) -> bool`
    - `tool(tok, name, args) -> dict`
    - `files(tok, items: list[tuple[str, bytes]], text) -> None`
    - `result(tok, result) -> None`
    - `bundle(profile, project) -> tuple[str, bytes]`

    Any 401 raises `SessionRevoked`.
  - `Worktrees(root: Path, git_base: str, token_for_git: Callable[[], str])`:
    - `ensure_clone(project) -> Path`, cloned from `git_base/git/<project>.git`
    - `prepare(conversation_id, project, branch, base) -> Path`: fetch, then `git worktree add -B branch <path> origin/base`
    - `prepare_read_only(project, base) -> Path`
    - `remove(conversation_id)`
  - `SessionUsers(names=("s1",…,"s8"))`: `acquire(conversation_id) -> str` (sticky per conversation) and `release(conversation_id)`
  - `remote_sdk_tools(manifest, link, tok, *, worktree: Path | None, user: str, checks: list[str]) -> list`. The special cases:
    - `open_pr`: runs `checks` in the worktree as `user` (timeout 1800 s each), commits pending changes, `git push origin HEAD:refs/heads/<branch>`, then forwards `branch`, `sha` and `checks` to `core`
    - `post_files(paths, text)`: every path must resolve inside `worktree`, otherwise it refuses; reads the files and calls `link.files`
    - any reply carrying `file`: writes it to `worktree/.agent-out/<name>` (the name is re-validated with `valid_save_name`) and returns the text plus `"saved to .agent-out/<name>"`
  - `SandboxRunner(link, worktrees, users, claude: ClaudeRunner, sandbox: str, profile: str, bundles_dir: Path)`:
    - `run_forever()`
    - `async handle(item: dict, tok: str, manifest: list) -> None`:
      1. syncs the bundle by version
      2. prepares the worktree (runs `setup` as `user` on first creation)
      3. builds `RunSpec(permission_mode="bypass", os_user=user, model, max_turns, timeout_s, system_prompt, plugins=[bundle/plugins/*], env={"CLAUDE_CONFIG_DIR": f"/var/agent/sessions/{conversation_id}", **test_secrets}, resume_session_id)`, chowning the session dir to `user`
      4. forwards each SDK message to `link.event`, heartbeats every 15 s, and calls `claude.interrupt` on cancel or `SessionRevoked`
      5. posts `link.result`
    - `run_forever` retries a failed pull with backoff (1 s up to 30 s), so a `core` restart is harmless.
- **`RunSpec.permission_mode: Literal["default","bypass"] = "default"`.** When it's `"bypass"`, `ClaudeRunner.build_options` uses `permission_mode="bypassPermissions"`, `can_use_tool=None` and `setting_sources=["project"]`, and sets `ANTHROPIC_BASE_URL` from the runner's base URL (`http://host.docker.internal:18091` in sandboxes).

- [ ] **Step 1: Failing tests** (Review Focus 3 and 5, runner side).

```python
async def test_cancel_via_heartbeat_interrupts(runner, fake_link_cancelling, fake_claude):
    await runner.handle(item(), "tok", []); assert fake_claude.interrupted == [item()["id"]]

async def test_revoked_token_interrupts(runner, fake_link_401, fake_claude):
    await runner.handle(item(), "tok", []); assert fake_claude.interrupted

async def test_post_files_outside_worktree_refused(tools_for_worktree):
    assert "outside" in (await call(tools_for_worktree, "post_files", {"paths": ["/etc/passwd"], "text": ""}))

async def test_open_pr_blocks_on_failed_checks(tools_with_failing_checks, fake_link):
    out = await call(tools_with_failing_checks, "open_pr", {"title": "t", "body": "b"})
    assert fake_link.tool_calls[-1][1]["checks"]["passed"] is False

async def test_saved_file_lands_in_agent_out(tools_for_worktree, fake_link_returning_file, worktree):
    await call(tools_for_worktree, "az", {"args": ["x"], "save_to": "logs.json"})
    assert (worktree / ".agent-out" / "logs.json").exists()

def test_bypass_options(claude_runner):
    opts = claude_runner.build_options(spec(permission_mode="bypass"), session_id="s")
    assert opts.permission_mode == "bypassPermissions" and opts.can_use_tool is None
```

- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `python -m agent runner --sandbox <name> --profile <p> --core http://host.docker.internal:18080 --token-file /etc/agent/sandbox.token`.
- [ ] **Step 4:** `just test tests/test_sandbox_runner.py tests/test_claude_runner.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(sandbox): runner that pulls work and runs Claude Code with full permissions`.

### Task 18: `SandboxProvider` and the `sbx` backend (needs Task 1)

**Files:**
- Create: `src/agent/sandbox/provider.py`
- Test: `tests/test_provider.py`

**Interfaces:**
- **Produces:**
  - `SandboxSpec(name, profile, template, cpus, memory_gb, disk_gb, network: Literal["open","locked"], allow: list[str], setup: list[str], inject: list[tuple[str, str, str]])`. `inject` entries are (host, header, value).
  - `ExecResult(code: int, output: str)`
  - Protocol `SandboxProvider`:
    - `async create(spec)`
    - `async start(name)`
    - `async stop(name)`
    - `async remove(name)`
    - `async exec(name, cmd: list[str], *, root: bool, timeout_s: int, detach: bool = False) -> ExecResult`
    - `async state() -> dict[str, str]` (name → `running` / `stopped`)
    - `async apply_policy(spec)`
    - `async set_secrets(spec)`
  - `SbxProvider(sbx: str = "sbx", run=asyncio.create_subprocess_exec)` builds argv from the **exact commands recorded in the Task 1 findings** (create with `--cpus` and `--memory`, policy allow and deny per sandbox, `secret set-custom`, `exec`, `exec -d`, state as JSON).
  - `FakeProvider` records calls and simulates state.

- [ ] **Step 1: Failing tests:**
  - the argv for each operation matches the findings doc
  - a `locked` spec produces exactly the allow rules `localhost:18091` and `localhost:18080`
  - an `open` spec produces the open rule recorded in the findings (A9)
  - `inject` values never appear in logged argv (passed through stdin or an env var, as the findings say)
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_provider.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(sandbox): SandboxProvider with sbx backend`.

### Task 19: `sandboxd` (needs Task 1)

**Files:**
- Create: `src/agent/sandbox/sandboxd.py`, `deploy/sandboxd.service`
- Modify: `justfile` (recipe `sandboxd-install`), `src/agent/__main__.py` (subcommand `sandboxd`)
- Test: `tests/test_sandboxd.py`

**Interfaces:**
- **Consumes:** `SandboxProvider` (Task 18), `SandboxSettings` (Task 2), `GET /agent/activity` (Task 5).
- **Produces:** `make_sandboxd_app(*, provider, config_dir, token, host_ram_gb: float, activity: Callable[[], Awaitable[set[str]]]) -> web.Application`, bound to `127.0.0.1:18100`.

| Route | Body | Does |
|---|---|---|
| `POST /ensure` | `{"name", "profile", "project", "sandbox_token", "inject": {host: value}}` | create or start; policy; secrets; setup on create; write `sandbox_token` to `/etc/agent/sandbox.token` (root-only); launch the runner (`exec detach root`) |
| `POST /stop` | `{"name"}` | stop |
| `POST /rebuild` | `{"name"}` | remove, then ensure |
| `POST /exec` | `{"name", "command"}` | `exec root` with a 300 s timeout; output capped at 4000 chars |
| `GET /status` | — | state plus resources |

- **Specs are built from config:**
  - resources come from `sandbox.resources[profile]` overridden by the pack's `sandbox_resources`
  - `network`: `locked` for `debug`; otherwise the pack's `network` (`read` → `locked` plus the read hosts and `egress_allow`)
  - `allow` always includes `localhost:18091` and `localhost:18080`
- **Memory budget:** `ensure` refuses with 409 `"not enough memory"` when the running sandboxes' memory plus the new one exceeds `host_memory_budget * host_ram_gb`, after first stopping idle sandboxes (not in `activity()`).
- **Loops:**
  - idle stop after `idle_stop_minutes` without activity
  - rebuild after `rebuild_every_days`, only when not busy

- [ ] **Step 1: Failing tests,** with `FakeProvider`:
  - a debug sandbox gets the locked policy
  - `ensure` over budget stops an idle sandbox first, then succeeds; with all busy it returns 409
  - the idle loop never stops a busy sandbox
  - `rebuild` re-runs setup
  - requests without the token get 401
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. `deploy/sandboxd.service` is a systemd user unit running `python -m agent sandboxd`, mirroring `claude-fallback-proxy.service` (enable lingering in the install recipe).
- [ ] **Step 4:** `just test tests/test_sandboxd.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(sandbox): sandboxd host service`.

### Task 20: Sandbox template image (needs Task 1)

**Files:**
- Create: `sandbox/Dockerfile`, `sandbox/entrypoint.sh`
- Modify: `justfile` (recipe `sandbox-template`)
- Test: `tests/test_deploy_files.py`

**Interfaces:**
- **Produces the template image `agent-sandbox:<version>`.** It is built `FROM` the template recorded in Task 1, pinned by tag and digest, and includes:
  - Python 3.12 and this package
  - `git`
  - users `s1`–`s8` with no `sudo`, plus `/var/agent/sessions`
  - the default user's `sudo` removed, when the base grants it
- **Produces `just sandbox-template`**, which builds and loads it with the command from the findings.

- [ ] **Step 1: Failing static tests:**
  - the Dockerfile `FROM` line has a digest
  - no `s[1-8]` user is in a `sudo` or `wheel` group
  - `entrypoint.sh` starts nothing (the runner is launched by `sandboxd`)
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. On the laptop, run `just sandbox-template`, then `sbx create` from it, and confirm that `python -m agent runner --help` works inside.
- [ ] **Step 4:** `just test tests/test_deploy_files.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(sandbox): template image`.

### Task 21: `core` wiring and compose; delete the v1 pipeline

**Files:**
- Modify:
  - `src/agent/core/app.py`
  - `src/agent/clients.py` (add `SandboxdClient`, remove `WorkerClient`)
  - `compose.yaml`
  - `Dockerfile` (core: `git`, `azure-cli` in `/opt/az`; remove the `worker` target)
  - `deploy/proxy-origins.example.toml` (origin `agent-sandbox` on 18091)
  - `src/agent/supervisor.py` (targets: `core` and `sandboxd` health)
- Delete:
  - `src/agent/worker/`
  - `src/agent/roles.py`
  - `src/agent/core/dispatch.py`
  - `src/agent/core/chat.py`
  - `src/agent/core/prompts.py`
  - `src/agent/core/tools.py`
  - `src/agent/prompts/chat.md`, `src/agent/prompts/builder.md`
  - `src/agent/runner/policy.py`
  - their tests
- Test: `tests/test_wiring.py`

**Interfaces:**
- **Produces `CoreApp(config_dir, data_dir, secrets, slack, proxy, sandboxd, linear_client, github_reader, github_writer, bot_user_id)`.** It builds:
  - `GitHost` and `WorkQueue`
  - `AgentToolRouter`, with the Task 7–12 tools registered
  - `Payloads`, `Conversations` and `ConsolidationService`
  - the agent API app

  It serves the agent API, the git endpoint and `/health` on `0.0.0.0:8080`.
- **Its loops:**
  - refresh git hosts (300 s)
  - Linear intake (60 s)
  - PR stats (300 s)
  - consolidation sweep (300 s)
  - maintenance (3600 s): demote memories, expire proposals
  - ensure each pack's `code-` and `debug-` sandbox and `general` through `sandboxd` when work is queued for them. `core` mints the sandbox token (Task 4) and sends it, along with the pack's `[egress.inject]` values from `core`'s secrets, in the `ensure` body.
- **`on_files`** uploads to the thread with `files_upload_v2`: at most 10 files, 25 MB each.
- **On `on_result`,** it dispatches by kind:
  - `turn` → `Conversations.on_result`
  - `production` → the waiting tool
  - `chore` → `ConsolidationService.on_result`
- **compose:**
  - no `worker` service
  - `core` gets `ports: ["127.0.0.1:18080:8080"]` and `SANDBOXD_URL=http://172.30.0.1:28100`
  - the relay adds `28100 → 127.0.0.1:18100`
  - the `cli` login state volume is `${AGENT_DATA}/cli:/data/cli` (core only)

- [ ] **Step 1: Failing tests:**
  - `test_wiring` builds `CoreApp` with fakes and serves `/health`
  - `docker compose config` has no `worker` service and publishes core only on `127.0.0.1`
  - `grep -r "agent.worker\|agent.roles" src` is empty
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the wiring and delete the modules. Rewrite the tests that imported the deleted modules, or delete them when their behaviour moved (record each one in the ledger).
- [ ] **Step 4:** `just test`. Expected: all pass. Run `docker compose config -q`. Expected: exit 0.
- [ ] **Step 5:** Commit: `refactor: core serves sandboxes; remove worker, roles and stage pipeline`.

### Task 22: Admin sandbox tools

**Files:**
- Create: `src/agent/core/sandbox_tools.py`
- Test: `tests/test_sandbox_tools.py`

**Interfaces:**
- **Consumes:** `SandboxdClient.rebuild(name)` and `SandboxdClient.exec(name, command)` (Task 21).
- **Produces the tools:**
  - `rebuild_sandbox(project, profile)` (admin)
  - `sandbox_exec(project, profile, command)` (admin; output scrubbed and capped at 4000 chars)

  The name is derived as `code-<project>`, `debug-<project>` or `general`. An unknown project is refused.

- [ ] **Step 1: Failing tests:**
  - a member is refused
  - an admin rebuild calls `sandboxd` with `debug-sample`
  - `exec` output is scrubbed and capped
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_sandbox_tools.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(core): rebuild_sandbox and sandbox_exec admin tools`.

### Task 23: `doctor` v2 (needs Task 1)

**Files:**
- Modify: `src/agent/doctor.py`
- Test: `tests/test_roles_doctor.py`, renamed to `tests/test_doctor.py`

**Interfaces:**
- **Produces `run_checks(...)`,** adding these checks:
  - `sandboxd` (reachable; `sbx` version equals the pinned one)
  - `sandbox:code-<p>` (a registry is reachable, or a read-list host for `network = "read"`)
  - `sandbox:debug-<p>`:
    - `curl -sS --max-time 5 https://example.com` **fails**
    - `curl host.docker.internal:18091/proxy/usage` succeeds
  - `cli:azure` (`login_check` exit 0)
  - `config:egress` (no wildcards; already enforced by config load, reported here)

  Probes run through `sandboxd /exec`.

- [ ] **Step 1: Failing tests,** with fakes:
  - a debug sandbox that reaches `example.com` fails the check, with the message "debug sandbox can reach the internet"
  - an `sbx` version mismatch fails
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `just test tests/test_doctor.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(doctor): sandbox and CLI checks`.

### Task 24: End-to-end on the laptop (run with the user)

**Files:**
- Modify: `scripts/smoke.py`, `justfile` (`smoke`)
- Create: `docs/superpowers/spikes/2026-11-13-phase-2-e2e.md` (results)

- [ ] **Step 1:** On the laptop:
  1. `just proxy-install`
  2. `just sandboxd-install`
  3. `just sandbox-template`
  4. `just up`
  5. `just doctor`

  Expected: every check passes, including the debug sandbox's refusal of `example.com`.
- [ ] **Step 2:** `just smoke`: it injects a Slack-shaped event ("fix the failing test in the sample repo") into `Conversations` and waits up to 30 min. Expected: a draft PR on the sandbox repo from `<name>/…`, and the reply text recorded.
- [ ] **Step 3: Exit criterion, with ChilliClaw.** It needs the user's filled-in pack and an Azure login:
  1. Post "fix <a real bug> in cclaw" in Slack.
  2. Expected:
     - `ask_production` runs in `debug-chilliclaw`
     - the answer reaching the coding session is scrubbed
     - a failing test reproduces the bug
     - a draft PR is opened
     - the reply explains the cause and how it was verified
  3. Record it in the results doc.
- [ ] **Step 4:** Commit: `docs: phase 2 end-to-end results`.
