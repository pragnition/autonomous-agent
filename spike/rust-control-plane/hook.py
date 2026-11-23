#!/usr/bin/env python3
"""Spike: a PreToolUse command hook on Bash. Logs each call; turns away commands containing `sleep 3`."""
import json
import sys

event = json.load(sys.stdin)
command = event.get("tool_input", {}).get("command", "")
with open("/var/agent/rcp-spike/logs/hook.log", "a") as log:
    log.write(json.dumps({"tool": event.get("tool_name"), "command": command}) + "\n")
if "sleep 3" in command:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "waiting-only command"}}))
