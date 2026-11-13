---
name: azure-debugging
description: Use in a debug session for ChilliClaw, to find its Azure resources, logs and metrics with the az tool.
---

# ChilliClaw on Azure

Fill in the values marked CONFIRM before installing this pack. Pass az arguments as a list, for example `["monitor", "log-analytics", "query", "-w", "<workspace-id>", "--analytics-query", "<KQL>"]`.

**Where it runs**
- Subscription: CONFIRM
- Resource groups: CONFIRM
- App Service / Container Apps names: CONFIRM

**Logs and metrics**
- Log Analytics workspace ID: CONFIRM
- Application Insights app: CONFIRM

**Useful queries** (always limit the time window; use save_to for anything large and search the file)
- Recent exceptions for an operation:
  `exceptions | where timestamp > ago(24h) | where operation_Name has "<CONFIRM>" | summarize count() by type, outerMessage | top 20 by count_`
- Failed requests by route:
  `requests | where timestamp > ago(24h) and success == false | summarize count() by name, resultCode | top 20 by count_`
- One request end to end: `union requests, dependencies, exceptions, traces | where operation_Id == "<id>" | order by timestamp asc`
