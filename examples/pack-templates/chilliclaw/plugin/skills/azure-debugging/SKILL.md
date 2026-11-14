---
name: azure-debugging
description: Use when querying ChilliClaw's Azure resources, logs, traces or metrics with az - which subscription, resource group, Container App and Log Analytics workspace each environment uses, and KQL that works on them.
---

# ChilliClaw on Azure

Your `az` login reaches these. **Dev is your staging environment.** Read UAT, never change it. Production (subscription `ChiliClaw-Prod`) is off limits.

| | Dev (staging) | UAT (read only) |
|---|---|---|
| Subscription | `ChiliClaw-Dev` (eb1600b3-91ca-457a-96ce-95bc1433cf1c) | `ChiliClaw-UAT` (0c0f7011-80ba-43c5-a4a0-b0b69ed7cf85) |
| Resource group | `RG_VNET_DEV` | `RG_VNET_UAT` |
| Container Apps | `chilliclaw-dev-gateway-web`, `-gateway-worker`, `-document-renderer`, `-office-worker`, `-self-improvement`, `-teams-relay` | `chilliclaw-uat-gateway-web`, `-gateway-worker`, `-document-renderer`, `-office-worker` |
| Log Analytics (workspace id for `-w`) | `chilliclaw-dev-logs` (07f4a76f-277c-443d-a1a5-a880e6f2b02c) | `chilliclaw-uat-logs` (32d966e6-c2b9-4988-adac-62bf463804b6) |
| Application Insights | `chilliclaw-dev-appinsights` (same workspace) | `chilliclaw-uat-appinsights` |
| Postgres | `chilliclaw-dev-pg` | `chilliclaw-uat-pg` |
| Foundry | `chilliclaw-dev-models-eastus2` (deployment `chilliclaw-dev-gpt-5.6-terra`), `chilliclaw-dev-classifier-models-eastus2` | `chilliclaw-uat-models-eastus2` |
| Web | https://chilliclaw-dev.ncs.com.sg | |

A separate deployment lives in subscription `NCSgpt-Sandbox`, resource group `rg-chilliclaw` (`chilliclaw-gateway-web`, workspace `chilliclaw-aca-law`). Look there only when a report names it.

Always pass `--subscription`. Keep the time window narrow, and save large output to a file and grep it.

## Where things are logged

- `ContainerAppConsoleLogs_CL`: each app's stdout. `ContainerAppName_s` is the app, `Log_s` the line, `RevisionName_s` the revision.
- `ContainerAppSystemLogs_CL`: restarts, crashes, failed probes, scaling.
- `AppRequests`, `AppExceptions`, `AppDependencies`: the gateway's OpenTelemetry. Every role shows as `unknown_service:bun`, so tell them apart by `Name` and `OperationId`, not `AppRoleName`.
- `AppGenAIContent`: model calls. `AGWAccessLogs`: the Application Gateway in front of the web app.

## Queries that work

```kusto
// What fails, by type, today
AppExceptions | where TimeGenerated > ago(24h)
| summarize n=count(), last=max(TimeGenerated) by ExceptionType, OuterMessage | top 20 by n

// Failed requests by route
AppRequests | where TimeGenerated > ago(24h) and Success == false
| summarize n=count() by Name, ResultCode | top 20 by n

// One request end to end
union AppRequests, AppDependencies, AppExceptions | where OperationId == "<id>" | order by TimeGenerated asc

// An app's console around an incident
ContainerAppConsoleLogs_CL | where ContainerAppName_s == "chilliclaw-dev-gateway-web"
| where TimeGenerated between (datetime(<from>) .. datetime(<to>)) | project TimeGenerated, Log_s | order by TimeGenerated asc

// Restarts and crashes
ContainerAppSystemLogs_CL | where TimeGenerated > ago(24h) and Log_s has_any ("crash", "restart", "probe", "Error")
| project TimeGenerated, ContainerAppName_s, Log_s
```

Run them with:

```bash
az monitor log-analytics query --subscription ChiliClaw-Dev -w 07f4a76f-277c-443d-a1a5-a880e6f2b02c --analytics-query "<KQL>" -o json
```

For a live tail, run `az containerapp logs show --subscription ChiliClaw-Dev -g RG_VNET_DEV -n chilliclaw-dev-gateway-web --tail 100`. To see what's deployed, run `az containerapp revision list ... -o table` and compare the image tag with `git log`.
