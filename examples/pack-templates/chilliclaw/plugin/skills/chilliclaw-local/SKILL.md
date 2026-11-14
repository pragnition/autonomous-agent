---
name: chilliclaw-local
description: Use when running ChilliClaw locally to build, test, debug or demo a change - before using the browser-testing skill on ChilliClaw, or when its UI, API or model calls need checking end to end.
---

# Running ChilliClaw locally

ChilliClaw runs in `dev-lite` mode: Postgres, Redis, the gateway, the renderer and office/DSL workers, and the Forge UI on http://localhost:5173. Knowledge search (Milvus) and PII sanitisation are off, so use made-up test data only, and say so when a change needs search to be tested. Audio transcription and image generation are off too (no keys for them).

The stack runs with the team's real integration credentials (Slack, Microsoft, Teams), so it acts as the real dev bot. Send test messages only to yourself or a test channel, never to people or team channels, and never to customers. This skill's directory (shown when it loads) holds `agent-overlay.toml` and `seed-test-user.sql`.

## Start it

Your worktree's setup already wrote `.env` from the team's own ChilliClaw `.env` (`/run/agent/projects/chilliclaw.env`) and ran `bun install`. If your `.env` still has placeholders where that file has values, copy those values in from it. Never put the Foundry key in `.env`: the meter adds it (see Model calls).

1. **Make sure nobody else's ChilliClaw is up.** Ports are fixed, so only one stack runs at a time:
   ```bash
   docker ps --filter publish=5173 --format '{{.Names}}  {{.Label "com.docker.compose.project.working_dir"}}'
   ```
   If it's another worktree's, don't stop it. Wait and check again every few minutes. If its working directory is gone, it's orphaned and you may remove it: `docker compose -p <project> down`.
2. **Build the config overlay**: local-lite, with model calls through the meter.
   ```bash
   mkdir -p .agent-out
   cat deployments/default/config.local-lite.toml <skill-dir>/agent-overlay.toml > .agent-out/chilliclaw-overlay.toml
   ```
3. **Start it**: `just local_lite_config=.agent-out/chilliclaw-overlay.toml dev-lite`. The first run builds images and takes a while; later runs take a minute or two. It returns once every container is healthy.
4. **Seed the test admin** (once per stack; safe to repeat). There's no `just` recipe for this, so this is the one place you call `docker compose` yourself:
   ```bash
   docker compose --env-file .env.docker exec -T postgres sh -c 'psql -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
     < <skill-dir>/seed-test-user.sql
   ```
   It prints the user's id.

## Sign in

The dev impersonation switch signs the browser in as that user, with no SSO:

```bash
playwright-cli -s=$S open http://localhost:5173
playwright-cli -s=$S localstorage-set chilliclaw-dev-user-id <user-id>
playwright-cli -s=$S goto http://localhost:5173
```

Check the snapshot shows the app ("Good afternoon, Agent!"), not the landing page's sign-in buttons. A new user first sees the product tour (click Skip) and, after the first message, a Terms of Use dialog (click Close); clear them before you take screenshots. For API calls without a browser, send the header `x-dev-user-id: <user-id>`.

## Model calls

The overlay sends every chat model call to the dev Foundry deployment `chilliclaw-dev-gpt-5.6-terra` through core's meter at `http://172.30.0.10:8087/llm/chilliclaw-dev` (an address, because ChilliClaw's containers can't resolve the name `core`). core adds the key and counts the cost; `curl -s http://172.30.0.10:8087/llm` shows today's spend and the caps. An error with `cost_cap_reached` means the budget is spent: stop testing model features and say so in the thread. Never put a real key in `.env` or call Foundry directly.

## Logs, tests and stopping

- Gateway logs: `docker compose --env-file .env.docker logs --tail 200 gateway` (`just logs` follows them and never returns).
- Checks before a PR: `just check` (typecheck, lint, format). Run the relevant `bun test <path>` while you work.
- ChilliClaw has no end-to-end suite yet; Forge's `*.browser.test.ts` tests mock `/api`. Add one of those for UI logic, and put the steps you ran in the browser in the PR body.
- Don't read or quote `.env` or `.env.docker` (the repo's CLAUDE.md rule); use the commands above.
- **Always bring it down when you're done, even after a failure**: `just down`.
