#!/usr/bin/env python3
"""Spike: a stdio MCP server with one tool, echo(text) -> text (newline-delimited JSON-RPC 2.0)."""
import json
import sys

TOOL = {"name": "echo", "description": "Return the given text unchanged.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}

for line in sys.stdin:
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        continue
    if method == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "echo", "version": "0"}}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": msg["params"]["arguments"]["text"]}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": method}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}), flush=True)
