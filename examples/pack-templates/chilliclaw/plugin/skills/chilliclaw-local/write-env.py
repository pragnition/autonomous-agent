"""Write a ChilliClaw clone's .env: the team's own ChilliClaw .env (core hands it to the agent) on top of .env.example,
so keys newer than the team's file still exist. Placeholders get local-only values, and blank internal tokens are left
out so ChilliClaw's env generator makes them (a blank one would win, because its justfile loads .env). Model calls go
through the agent's meter, which adds the Foundry key, so the key here is a stand-in. Run it from the clone's root."""

import os
import secrets

TEAM_ENV = "/run/agent/projects/chilliclaw.env"
GENERATED = {"DOCUMENT_RENDERER_TOKEN", "OFFICE_EDIT_WORKER_TOKEN", "PII_SANITISATION_TOKEN", "DSL_EXECUTOR_TOKEN",
             "MILVUS_MINIO_ACCESS_KEY", "MILVUS_MINIO_SECRET_KEY", "KNOWLEDGE_INDEX_REPORTS_AZURITE_KEY"}


def pairs(path):
    for line in open(path).read().splitlines():
        key, sep, value = line.partition("=")
        if sep and key and not key.startswith("#"):
            yield key.strip(), value


team = dict(pairs(TEAM_ENV)) if os.path.exists(TEAM_ENV) else {}
merged = {key: team.get(key, value) for key, value in pairs(".env.example")}  # newer keys exist, team values win
merged.update(team)
out = []
for key, value in merged.items():
    if key in GENERATED and not value:
        continue
    if value.startswith("REPLACE_ME"):
        value = secrets.token_hex(32) if key == "CREDENTIAL_ENCRYPTION_KEY" else "local-placeholder"
    out.append(f"{key}={value}")
open(".env", "w").write("\n".join(out + ["AGENT_LLM_KEY=metered"]) + "\n")
