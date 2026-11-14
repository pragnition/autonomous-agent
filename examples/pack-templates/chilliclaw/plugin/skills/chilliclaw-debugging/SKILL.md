---
name: chilliclaw-debugging
description: Use when investigating or fixing anything in ChilliClaw (cclaw) - an error, a wrong answer from its agent, a failed upload or document, a broken Forge page, a Slack or Teams integration issue.
---

# Debugging ChilliClaw

Start from the general debugging skill; this one says where ChilliClaw keeps its signals.

**The code.** A Bun and Turbo monorepo. `packages/gateway` is the server and API (`src/server.ts`). `packages/forge` is the web UI (React and Vite). `packages/agent` is the model loop. `packages/core` holds the config schema (`src/config.ts`). `packages/data` holds the database layer and SQL migrations (`migrations/`). Deployment config is in `deployments/<name>/config/*.toml`; dev runs the `ncs-dev-scaled` deployment. Read the repo's CLAUDE.md and AGENTS.md first.

**Deployed signals.** The azure-debugging skill has the subscriptions, apps, workspaces and working KQL. Start there for anything reported from dev, UAT or a user.
- An error a user saw: search `AppExceptions` and the gateway's console around the time they gave, then follow the `OperationId` end to end.
- A wrong or slow answer from the agent: check `AppDependencies` for the model call (duration, status) and `AppGenAIContent`. Prompts are managed in Langfuse (`tools/langfuse/README.md`).
- Something deployed recently: compare the Container App's current revision and image tag with `git log`, and check the PRs merged since the last good revision (`gh pr list --state merged`).

**Reproducing locally.** The chilliclaw-local skill runs it with a seeded admin and the browser signed in. Reproduce UI bugs there, and screenshot the broken state before you fix it. Two things can't be reproduced locally: knowledge search (Milvus is off) and PII sanitisation. For those, work from the deployed signals and the tests.

**Known traps.**
- Config is validated at boot. A bad overlay or a missing env var stops the gateway, so read its logs first when the stack won't come up.
- Migrations run inside the gateway at boot. A failing migration shows up as an unhealthy gateway, not a separate error.
- `FOUNDRY_PROJECT_ENDPOINT` must be set in deployed environments. Startup errors mentioning it are configuration problems, not code bugs.
