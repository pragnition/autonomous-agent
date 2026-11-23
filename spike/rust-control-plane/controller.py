#!/usr/bin/env python3
"""Spike: a throwaway controller that talks to holder.sock and survives its own restarts.

  controller.py start      start a turn running a 90 s Bash loop
  controller.py follow     print output from the last seq saved in controller.state, forever (the container's main
                           process, so `docker restart` kills and restarts it)
  controller.py send-now   send a person's message with priority "now"
  controller.py status     the holder's status (Claude Code's pid)
"""
import json
import socket
import sys
import time
import uuid

DIR = "/var/agent/rcp-spike/holder"
STATE, SEEN = f"{DIR}/controller.state", f"{DIR}/seen.txt"
LOOP = "for i in $(seq 1 90); do echo $i; sleep 1; done"


def call(req: dict) -> dict:
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(f"{DIR}/holder.sock")
        f = s.makefile("rw")
        f.write(json.dumps(req) + "\n")
        f.flush()
        return json.loads(f.readline())


def user(text: str, **extra) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, "parent_tool_use_id": None,
            "session_id": "", "uuid": str(uuid.uuid4()), **extra}


def follow() -> None:
    try:
        last = int(open(STATE).read())
    except (FileNotFoundError, ValueError):
        last = 0
    print(f"follow: resuming after seq {last}", flush=True)
    while True:
        try:
            entries = call({"op": "tail", "from_seq": last})["entries"]
        except (OSError, ValueError):
            entries = []  # the holder is starting up or restarting; try again
        for entry in entries:
            frame = json.loads(entry["line"])
            print(entry["seq"], frame.get("type"), frame.get("subtype", ""), frame.get("uuid", ""), flush=True)
            with open(SEEN, "a") as seen:
                seen.write(f"{entry['seq']}\n")
            last = entry["seq"]
            with open(STATE, "w") as st:
                st.write(str(last))
        time.sleep(0.5)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "start":
        init = {"type": "control_request", "request_id": "init", "request": {"subtype": "initialize", "hooks": None}}
        prompt = user(f"Run this exact command with Bash in the foreground (not in the background): {LOOP} . "
                      "Then reply finished.")
        print(call({"op": "start", "lines": [init, prompt]}))
    elif cmd == "follow":
        follow()
    elif cmd == "send-now":
        msg = user("Reply with the single word ok, right now, before anything else.", priority="now",
                   origin={"kind": "human"})
        print(msg["uuid"], call({"op": "send", "line": msg}))
    elif cmd == "status":
        print(call({"op": "status"}))
