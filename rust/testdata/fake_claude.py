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
At the end of the scenario it waits for stdin to close, then exits 0.
"""
import json
import socket
import subprocess
import sys
import threading
import time

steps = [json.loads(line) for line in open(sys.argv[1]) if line.strip()]
lock = threading.Condition()
state = {"lines": 0, "interrupted": False, "closed": False}
held = []


def say(frame):
    with lock:
        sys.stdout.write(json.dumps(frame) + "\n")
        sys.stdout.flush()


def read_stdin():
    for line in sys.stdin:
        msg = json.loads(line)
        if msg.get("type") == "user" and msg.get("uuid"):
            say({**msg, "isReplay": True, "session_id": "fake-session"})
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
with lock:
    lock.wait_for(lambda: state["closed"])
