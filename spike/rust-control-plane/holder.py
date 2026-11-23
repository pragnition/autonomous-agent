#!/usr/bin/env python3
"""Spike: a throwaway holder, run as a container's main process.

It starts Claude Code when asked, keeps its input open, appends every output line as {"seq": N, "line": ...} to
out.jsonl, and answers newline-delimited JSON requests on holder.sock: start, send, tail, status.
"""
import json
import os
import socketserver
import subprocess
import threading

ROOT = "/var/agent/rcp-spike"
DIR = f"{ROOT}/holder"
OUT, SOCK = f"{DIR}/out.jsonl", f"{DIR}/holder.sock"
ENV = {**os.environ, "ANTHROPIC_BASE_URL": "http://172.30.0.1:28091",
       "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-container-via-host-proxy",
       "ENABLE_CLAUDEAI_MCP_SERVERS": "false", "CLAUDE_CONFIG_DIR": f"{ROOT}/claude"}
CMD = ["claude", "--output-format", "stream-json", "--verbose", "--input-format", "stream-json",
       "--replay-user-messages", "--permission-mode", "bypassPermissions", "--model", "claude-haiku-4-5-20251001"]

lock = threading.Lock()
state = {"proc": None, "seq": sum(1 for _ in open(OUT)) if os.path.exists(OUT) else 0}


def pump(proc: subprocess.Popen) -> None:
    with open(OUT, "a") as out:
        for line in proc.stdout:
            with lock:
                state["seq"] += 1
                out.write(json.dumps({"seq": state["seq"], "line": line.rstrip("\n")}) + "\n")
                out.flush()


def handle(req: dict) -> dict:
    op = req["op"]
    if op == "start":
        proc = subprocess.Popen(CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=ENV, text=True, bufsize=1,
                                cwd=ROOT)
        state["proc"] = proc
        threading.Thread(target=pump, args=(proc,), daemon=True).start()
        for obj in req["lines"]:
            proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
        return {"ok": True, "pid": proc.pid}
    proc = state["proc"]
    if op == "send":
        proc.stdin.write(json.dumps(req["line"]) + "\n")
        proc.stdin.flush()
        return {"ok": True}
    if op == "tail":
        entries = []
        with lock, open(OUT) as f:
            for row in f:
                entry = json.loads(row)
                if entry["seq"] > req["from_seq"]:
                    entries.append(entry)
        return {"ok": True, "entries": entries}
    if op == "status":
        return {"ok": True, "pid": proc.pid if proc else None,
                "alive": bool(proc and proc.poll() is None), "seq": state["seq"]}
    return {"ok": False, "error": f"unknown op {op}"}


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        for line in self.rfile:
            self.wfile.write((json.dumps(handle(json.loads(line))) + "\n").encode())
            self.wfile.flush()


if __name__ == "__main__":
    os.makedirs(DIR, exist_ok=True)
    open(OUT, "a").close()
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    socketserver.ThreadingUnixStreamServer(SOCK, Handler).serve_forever()
