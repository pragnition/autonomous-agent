# Jensen as a Linear Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** People delegate Linear issues to the agent and see its progress on the issue. The agent works in Linear
(search, comments, updates) as itself, through Linear's MCP server.

**Architecture:**
- `core` gets a second aiohttp listener, exposed through Tailscale Funnel. It has two routes: a signed webhook for
  AgentSessionEvents and the OAuth callback.
- A `LinearAppAuth` keeps the app's rotating tokens and shares the current one with the agent container.
- `LinearAgentSessions` turns session events into the existing conversations and turns, and turn results into
  agent activities.
- `[mcp.*]` config reaches every session through the work-item payload.

**Tech Stack:** Python 3.12, aiohttp, httpx, SQLite, claude-agent-sdk, Docker Compose, pytest (`just test`).

**Spec:** `docs/superpowers/specs/2026-11-13-open-agent-design.md` §8 (Jensen as a Linear agent).

Builds on branch `phase-1` at the commit that adds this plan.

## Global Constraints

- **Public listener:** container port **8085**, published `127.0.0.1:18085:8085`, with only `POST /linear/webhook`
  and `GET /linear/oauth/callback` (everything else 404). The agent API stays unexposed.
- **Webhook check:** header `Linear-Signature` = hex HMAC-SHA256(raw body, `LINEAR_WEBHOOK_SECRET`), and the body's
  `webhookTimestamp` (ms) within **60 s**. On failure: 401 and no processing. Answer 200 before processing.
- **OAuth:**
  - Authorize at `https://linear.app/oauth/authorize` with `response_type=code`,
    `scope=read,write,app:assignable,app:mentionable`, `actor=app`, `state`, `prompt=consent` and
    `redirect_uri = <public_url>/linear/oauth/callback`.
  - The token endpoint is `https://api.linear.app/oauth/token`, form-encoded.
  - `state` is single use and expires after **15 min**.
- **Tokens:** refresh when less than **3600 s** is left, and store the rotated refresh token. Write
  `data/runner/linear.env` as `LINEAR_APP_TOKEN=<token>`, mode 0600, owned by `sandbox.agent_uid` when core is root.
- **Settings keys:** `linear_app_tokens` (JSON: access, refresh, expires_at, app_user_id), `linear_oauth_state`
  (JSON: state, expires_at), `linear_session:<session id>` → conversation id, and `linear_session_of:<conversation id>`
  → session id.
- **Agent activity:** the GraphQL mutation is `agentActivityCreate(input: {agentSessionId, content: {type, body}})`.
  The types are `thought`, `response`, `error` and `elicitation`.
- **Secrets:** `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` and `LINEAR_WEBHOOK_SECRET` go in core.env. They are
  required by doctor only when `[linear] public_url` is set.
- TDD, `just test` green after each task, one commit per task, no pushes.

## Review Focus

1. **A forged, replayed or unsigned webhook must do nothing.** Task 3 `test_webhook_rejects_bad_signature_and_old_timestamp`.
2. **A token expiring while a session runs must not break Linear MCP calls.** Each session reads the current file,
   and core refreshes ahead of expiry. Task 2 `test_token_refreshes_and_rotates`, Task 5 `test_mcp_headers_use_current_linear_token`.
3. **A second `created` event for an issue that already has a task must not make a second task.** Task 4 `test_created_twice_reuses_the_task`.
4. **An issue in a team with no pack gets an `error` activity, not silence.** Task 4 `test_unknown_team_gets_an_error_activity`.
5. **The OAuth callback with a missing, wrong or expired state must not exchange a code.** Task 3 `test_callback_rejects_bad_state`.

---

### Task 1: Config for the public URL and MCP servers

**Files:** modify `src/agent/config.py`, `examples/config/agent.toml`, `src/agent/doctor.py`. Tests: `tests/test_config.py`, `tests/test_doctor.py`.

**Produces:**
- `LinearSettings(public_url: str = "")`, available as `AgentConfig.linear`.
- `McpServer(type: str, url: str, headers: dict[str, str])`, available as `AgentConfig.mcp: dict[str, McpServer]`.
- `doctor.required_core(config)` adds the three Linear app secrets when `public_url` is set.

- [ ] **Tests:**
  - `test_linear_public_url`: it must start with `https://`.
  - `test_mcp_servers`: only `type = "http"` is accepted, `url` must be https, and `headers` must be a string→string table.
  - `test_example_config_has_linear_mcp`: `cfg.mcp["linear"].headers["Authorization"] == "Bearer ${LINEAR_APP_TOKEN}"`.
  - `test_linear_app_secrets_required_with_public_url`.
- [ ] Run `just test` → FAIL. Implement. Run `just test` → PASS.
- [ ] Commit: `feat: config for the Linear public URL and MCP servers`.

### Task 2: Linear app tokens

**Files:** create `src/agent/core/linear_app.py`. Test: `tests/test_linear_app.py`.

**Produces:**

```python
LinearAppAuth(conn, *, client_id, client_secret, redirect_uri, token_path, owner_uid=None, http=None,
              clock=time.time)
```

with these members:
- `new_authorize_url() -> str`, which stores the state.
- `check_state(state) -> bool`, which is single use.
- `async exchange(code) -> str`, which returns the app user id.
- `async token() -> str | None`, which refreshes when needed.
- `installed: bool`
- `app_user_id: str | None`

- [ ] **Tests** (use an `httpx.MockTransport`):
  - `test_authorize_url_params`: it has every Global Constraints parameter, and the state is stored.
  - `test_state_is_single_use_and_expires`.
  - `test_exchange_stores_tokens_and_app_user`: it posts a form with `grant_type=authorization_code`. A GraphQL
    `viewer { id }` with the new token sets `app_user_id`. `linear.env` is written with mode 0600.
  - `test_token_refreshes_and_rotates`: with under 3600 s left it posts `grant_type=refresh_token`. The new refresh
    token is stored and `linear.env` is updated. With more time left there is no HTTP call.
  - `test_not_installed_returns_none`.
- [ ] Run `just test` → FAIL. Implement. Run `just test` → PASS.
- [ ] Commit: `feat: the Linear app's tokens, refreshed and shared with the agent`.

### Task 3: Public listener and install tool

**Files:** create `src/agent/core/public_api.py`. Modify `src/agent/core/app.py` (start the listener in `serve()`
on 8085 and register `linear_install`), `compose.yaml` (`127.0.0.1:18085:8085`). Tests: `tests/test_public_api.py`,
`tests/test_deploy_files.py`.

**Produces:**
- `make_public_app(*, webhook_secret: str, on_event: Callable[[dict], Awaitable[None]], auth: LinearAppAuth, on_installed: Callable[[str], Awaitable[None]]) -> web.Application`
- The admin tool `linear_install`, which DMs the authorize URL to the asker.

- [ ] **Tests:**
  - `test_webhook_rejects_bad_signature_and_old_timestamp`: a wrong signature, a missing signature, and a timestamp
    90 s old each give 401, and `on_event` is never called.
  - `test_webhook_answers_then_processes`: a valid request gives 200, and `on_event` gets the parsed body.
  - `test_callback_rejects_bad_state`: no exchange happens.
  - `test_callback_installs`: it gives 200 HTML containing "installed", and `on_installed` is called.
  - `test_other_paths_404`.
  - `test_linear_install_is_admin_only_and_dms`.
  - Deploy: `test_core_publishes_public_port`.
- [ ] Run `just test` → FAIL. Implement. Run `just test` → PASS.
- [ ] Commit: `feat: a public listener for Linear webhooks and the app install`.

### Task 4: Agent sessions ↔ conversations

**Files:** create `src/agent/core/linear_sessions.py`. Modify `src/agent/connectors/linear.py`
(`LinearClient(api_key, ..., token=None)`: an async token provider used as `Bearer`, else the key;
`agent_activity(session_id, type, body)`), `src/agent/core/conversations.py` (on_result calls a hook),
`src/agent/core/agent_tools.py` (`ask` calls the hook) and `src/agent/core/app.py` (wiring). Test:
`tests/test_linear_sessions.py`.

**Produces:**
- `LinearAgentSessions(conn, *, config, packs, queue, slack, payloads, client)`
- `async on_event(body) -> None`
- `async on_result(conversation_id, outcome, text) -> None`
- `async on_ask(conversation_id, question) -> None`

- [ ] **Tests:**
  - `test_created_posts_thought_then_queues_a_turn`: the thought activity is the first call. One task (source
    `linear`), one conversation in `code-<project>`, a Slack thread in the pack channel, and a queued turn whose
    text contains the `promptContext`.
  - `test_created_twice_reuses_the_task`.
  - `test_unknown_team_gets_an_error_activity`.
  - `test_prompted_queues_a_follow_up_in_the_same_conversation`.
  - `test_result_becomes_a_response_activity`. A failed turn becomes an `error` activity.
  - `test_ask_posts_an_elicitation`.
  - `test_client_uses_app_token_when_installed`.
- [ ] Run `just test` → FAIL. Implement. Run `just test` → PASS.
- [ ] Commit: `feat: Linear agent sessions drive conversations, results go back as activities`.

### Task 5: MCP servers in sessions

**Files:** modify `src/agent/core/payloads.py` (payload `mcp` = the config's servers with `${VAR}` kept),
`src/agent/sandbox/runner.py` (read `/run/agent/linear.env` at each session start, expand `${VAR}` from extra_env
plus that file, and set `RunSpec.mcp_servers`) and `src/agent/sandbox/runners.py` (pass the linear.env path). Tests:
`tests/test_payloads.py`, `tests/test_sandbox_runner.py`.

- [ ] **Tests:**
  - `test_payload_carries_mcp_servers`.
  - `test_mcp_headers_use_current_linear_token`: the file changes between two sessions, and each spec gets that
    session's token.
  - `test_unset_variable_drops_the_server`: with no token yet, the Linear server is left out and a warning is logged.
- [ ] Run `just test` → FAIL. Implement. Run `just test` → PASS.
- [ ] Commit: `feat: MCP servers in every session, with the Linear token read fresh`.

### Task 6: Bring-up (with the user)

- [ ] **The user:**
  - Creates the Linear app (name `Jensen`, the webhook URL with "Agent session events" on, and the callback URL).
  - Puts the client id, client secret and webhook signing secret in core.env.
  - Runs `sudo tailscale funnel --bg 18085`.
- [ ] Redeploy (`git pull`, `just up`). Then ask Jensen in a DM to `linear_install`, and the admin clicks Authorize.
  Expected: "installed", and `data/runner/linear.env` exists.
- [ ] Delegate a test issue to Jensen. Expected: a "On it" thought within 10 s, a Slack thread, then a `response`
  activity (or a draft PR) when the turn ends.
