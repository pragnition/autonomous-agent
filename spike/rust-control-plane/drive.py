#!/usr/bin/env python3
"""Spike: drive Claude Code over stream-json without the Python SDK.

Usage: drive.py turn|deny|interrupt|now
Prints one short line per output frame (seconds since start, type, subtype, key fields) and keeps every full line
in /var/agent/rcp-spike/logs/<scenario>.jsonl.
"""
import json
import os
import subprocess
import sys
import threading
import time
import uuid

ROOT = "/var/agent/rcp-spike"
ENV = {**os.environ, "ANTHROPIC_BASE_URL": "http://172.30.0.1:28091",
       "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-container-via-host-proxy",
       "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CONFIG_DIR": f"{ROOT}/claude"}
CMD = ["claude", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
       "--replay-user-messages", "--permission-mode", "bypassPermissions", "--model", "claude-haiku-4-5-20251001",
       "--settings", f"{ROOT}/settings.json", "--mcp-config", f"{ROOT}/mcp.json"]
LOOP = "for i in $(seq 1 60); do echo $i; sleep 1; done"
PROMPTS = {
    "turn": "Run `echo hi` with Bash, then call the echo tool with text x, then reply done.",
    "deny": "Run `echo start && sleep 31 && echo end` with Bash.",
    "interrupt": f"Run this exact command with Bash in the foreground: {LOOP} . Then reply finished.",
    "now": f"Run this exact command with Bash in the foreground (not in the background): {LOOP} . Then reply finished.",
}


def user(text: str, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
            "session_id": "", "uuid": str(uuid.uuid4()), **extra}


def short(frame: dict) -> str:
    t, sub = frame.get("type"), frame.get("subtype", "")
    bits = [t, sub]
    if t == "assistant":
        for b in frame["message"]["content"]:
            if b.get("type") == "tool_use":
                bits.append(f"tool_use {b['name']} {json.dumps(b['input'])[:80]}")
            elif b.get("type") == "text":
                bits.append(f"text {b['text'][:80]!r}")
    elif t == "user":
        content = frame["message"]["content"]
        if isinstance(content, str):
            bits.append(f"echo uuid={frame.get('uuid')} replay={frame.get('isReplay')} {content[:50]!r}")
        else:
            for b in content:
                if b.get("type") == "tool_result":
                    text = b.get("content")
                    text = text if isinstance(text, str) else json.dumps(text)
                    bits.append(f"tool_result error={b.get('is_error')} {text[:90]!r}")
                elif b.get("type") == "text":
                    bits.append(f"text {b['text'][:80]!r}")
    elif t == "result":
        bits.append(f"is_error={frame.get('is_error')} {str(frame.get('result'))[:60]!r}")
    elif t == "system" and sub not in ("init",):
        bits.append(json.dumps({k: v for k, v in frame.items() if k not in ("type", "subtype", "session_id", "uuid")})[:160])
    elif t in ("control_response", "control_request"):
        bits.append(json.dumps(frame)[:160])
    return " ".join(b for b in bits if b)


def main(scenario: str) -> None:
    os.makedirs(f"{ROOT}/logs", exist_ok=True)
    log = open(f"{ROOT}/logs/{scenario}.jsonl", "w")
    proc = subprocess.Popen(CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=ENV, text=True, bufsize=1,
                            cwd=f"{ROOT}")
    t0 = time.monotonic()

    def send(obj: dict) -> None:
        print(f"{time.monotonic() - t0:6.1f} >> {json.dumps(obj)[:140]}", flush=True)
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    send({"type": "control_request", "request_id": "init", "request": {"subtype": "initialize", "hooks": None}})
    send(user(PROMPTS[scenario]))
    results, last = 0, time.monotonic()
    state = {"bash_seen_at": None, "sent": False}

    def later() -> None:
        while not state["sent"]:
            if state["bash_seen_at"] and time.monotonic() - state["bash_seen_at"] > 5:
                state["sent"] = True
                if scenario == "interrupt":
                    send({"type": "control_request", "request_id": "i1", "request": {"subtype": "interrupt"}})
                else:
                    send(user("Reply with the single word ok, right now, before anything else.",
                              priority="now", origin={"kind": "human"}))
            time.sleep(0.2)

    if scenario in ("interrupt", "now"):
        threading.Thread(target=later, daemon=True).start()

    def closer() -> None:
        # The turn scenarios end at their first result; `now` keeps reading until the loop's shell has had time to
        # finish (60 s) plus a margin, then closes stdin so the CLI exits.
        while True:
            time.sleep(1)
            done = results >= 1 and scenario in ("turn", "deny", "interrupt")
            if scenario == "now" and time.monotonic() - t0 > 120:
                done = True
            if done:
                proc.stdin.close()
                return

    threading.Thread(target=closer, daemon=True).start()
    for line in proc.stdout:
        log.write(line)
        log.flush()
        try:
            frame = json.loads(line)
        except ValueError:
            print(f"{time.monotonic() - t0:6.1f} << (not json) {line[:100]!r}", flush=True)
            continue
        if frame.get("type") == "assistant" and any(b.get("type") == "tool_use" and b.get("name") == "Bash"
                                                    for b in frame["message"]["content"]):
            state["bash_seen_at"] = state["bash_seen_at"] or time.monotonic()
        if frame.get("type") == "result":
            results += 1
        if frame.get("type") == "stream_event":
            continue
        print(f"{time.monotonic() - t0:6.1f} << {short(frame)}", flush=True)
    print(f"exit {proc.wait()} after {time.monotonic() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
