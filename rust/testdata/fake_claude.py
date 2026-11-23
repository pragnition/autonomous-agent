#!/usr/bin/env python3
"""A stand-in for Claude Code in tests: plays a scenario file of steps, one JSON object per line.

Every line it reads on stdin is handled the way the real CLI does: a user message with a uuid is echoed back with
isReplay, a control request gets a success response. Steps:
  {"emit": {...}}            print this frame
  {"wait_stdin": n}          wait until n lines have arrived on stdin (all kinds counted)
  {"wait_interrupt": true}   wait for an interrupt control request
  {"sleep_ms": n}            sleep
  {"spawn": ["cmd", ...]}    start a child process (it stays in this process group)
  {"listen": port}           listen on a TCP port until exit
  {"exit": code}             exit now
  {"defer_echo": true}       (first step only) hold echoes back until {"flush_echo": true}
  {"tool": {"name", "args"}} call an agentd tool through the holder, then say its answer as an assistant message
At the end of the scenario it waits for stdin to close, then exits 0.

Started with Claude Code's own arguments after the scenario path, it takes the session id from --session-id= or
--resume= and puts it on every frame. Each start is recorded in <scenario>.starts (its arguments), and start k plays
<scenario>.<k> when that file exists.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time

path, args = sys.argv[1], sys.argv[2:]
sid = next((a.split("=", 1)[1] for a in args if a.startswith(("--session-id=", "--resume="))), None)
starts = path + ".starts"
k = len(open(starts).read().splitlines()) if os.path.exists(starts) else 0
with open(starts, "a") as f:
    f.write(json.dumps(args) + "\n")
if os.path.exists(f"{path}.{k}"):
    path = f"{path}.{k}"
if sid and os.environ.get("CLAUDE_CONFIG_DIR"):  # the session's record, as the real CLI keeps one
    record = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "fake")
    os.makedirs(record, exist_ok=True)
    open(os.path.join(record, f"{sid}.jsonl"), "a").close()
steps = [json.loads(line) for line in open(path) if line.strip()]
lock = threading.Condition()
state = {"lines": 0, "interrupted": False, "closed": False, "defer": bool(steps and steps[0].get("defer_echo"))}
held, deferred = [], []


def say(frame):
    if sid is not None:
        frame = {**frame, "session_id": frame.get("session_id", sid)}
    with lock:
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()


def read_stdin():
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") == "user" and msg.get("uuid"):
            echo = {**msg, "isReplay": True, "session_id": sid or "fake-session"}
            with lock:
                held_back = state["defer"]
                if held_back:
                    deferred.append(echo)
            if not held_back:
                say(echo)
        elif msg.get("type") == "control_request":
            say({"type": "control_response", "response": {"subtype": "success", "request_id": msg.get("request_id"),
                                                          "response": {}}})
            if msg.get("request", {}).get("subtype") == "interrupt":
                with lock:
                    state["interrupted"] = True
        with lock:
            state["lines"] += 1
            lock.notify_all()
    with lock:
        state["closed"] = True
        lock.notify_all()


threading.Thread(target=read_stdin, daemon=True).start()
for step in steps:
    if "emit" in step:
        say(step["emit"])
    elif "wait_stdin" in step:
        with lock:
            lock.wait_for(lambda: state["lines"] >= step["wait_stdin"] or state["closed"])
    elif "wait_interrupt" in step:
        with lock:
            lock.wait_for(lambda: state["interrupted"] or state["closed"])
    elif "sleep_ms" in step:
        time.sleep(step["sleep_ms"] / 1000)
    elif "spawn" in step:
        held.append(subprocess.Popen(step["spawn"]))
    elif "listen" in step:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", step["listen"]))
        s.listen()
        held.append(s)
    elif "exit" in step:
        sys.stdout.flush()
        sys.exit(step["exit"])
    elif "flush_echo" in step:
        with lock:
            state["defer"], echoes = False, list(deferred)
        for echo in echoes:
            say(echo)
    elif "tool" in step:
        sock = os.path.join(os.environ["FAKE_RUN_ROOT"], os.environ["AGENT_CONVERSATION"], "holder.sock")
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(sock)
            s.sendall((json.dumps({"op": "tool", **step["tool"]}) + "\n").encode())
            answer = json.loads(s.makefile().readline())
        say({"type": "assistant", "parent_tool_use_id": None,
             "message": {"role": "assistant", "model": "fake", "content": [{"type": "text", "text": answer["text"]}]}})
with lock:
    lock.wait_for(lambda: state["closed"])
