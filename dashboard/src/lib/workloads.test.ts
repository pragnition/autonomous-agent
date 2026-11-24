import { describe, expect, it } from "vitest"
import type { Proc } from "@/api"
import { parseSelection, processTree, stripAnsi, subtreeTotals, untilText } from "@/lib/workloads"

const proc = (pid: number, ppid: number, pss = 100, cpu = 1): Proc => ({
  pid, ppid, pgid: pid, comm: "x", state: "S", cmd: `cmd ${pid}`, cwd: null, threads: 1, started: null, cpu_percent: cpu,
  cpu_seconds: 0, rss: pss, pss, swap: 0, ports: [], log: null, err: null, uid: 1001, kind: "process", in_turn: false,
  kept: false,
})

describe("workloads", () => {
  it("reads a selection from the page's address", () => {
    expect(parseSelection("container:V-1")).toEqual({ kind: "container", name: "V-1" })
    expect(parseSelection("stack:rr-a:b")).toEqual({ kind: "stack", name: "rr-a:b" })
    expect(parseSelection("nonsense:x")).toBeNull()
    expect(parseSelection("container:")).toBeNull()
    expect(parseSelection(null)).toBeNull()
  })

  it("says how long until an expiry", () => {
    const now = Date.parse("2026-11-24T00:00:00Z")
    expect(untilText("2026-11-24T03:20:00Z", now)).toBe("in 3h 20m")
    expect(untilText("2026-11-23T00:00:00Z", now)).toBe("now")
    expect(untilText(null, now)).toBe("—")
  })

  it("lists processes under what started them", () => {
    const tree = processTree([proc(9, 6), proc(1, 0), proc(6, 1), proc(7, 1), proc(12, 9)])
    expect(tree.map(({ p, depth }) => `${p.pid}@${depth}`)).toEqual(["1@0", "6@1", "9@2", "12@3", "7@1"])
  })

  it("sums what a process and its children use", () => {
    const procs = [proc(1, 0), proc(6, 1, 300, 2), proc(9, 6, 200, 3), proc(7, 1)]
    expect(subtreeTotals(procs, 6)).toEqual({ pss: 500, cpu: 5, count: 2 })
  })

  it("drops terminal colours from log lines", () => {
    expect(stripAnsi("\u001b[32m  VITE v8\u001b[0m ready \u001b[1min\u001b[22m 300 ms")).toBe("  VITE v8 ready in 300 ms")
  })
})
