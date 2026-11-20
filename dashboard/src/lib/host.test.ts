import { describe, expect, it } from "vitest"
import type { HostConsumer, HostPoint } from "@/api"
import { byKindRows, consumerLabel, pairRows, rank } from "@/lib/host"

const point = (ts: number, cpu: Record<string, number>): HostPoint => ({
  ts, cpu: 10, mem_used: 1, swap_used: 0, rx_bps: 5, tx_bps: 6, read_bps: 1, write_bps: 2, containers: 0, ports: 0,
  cpu_by_kind: cpu, mem_by_kind: {},
})

const consumer = (over: Partial<HostConsumer>): HostConsumer => ({
  key: "k", kind: "session", name: "W-1", item: "W-1", conversation: "V-1", cpu_percent: 0, mem_bytes: 0,
  read_bps: 0, write_bps: 0, rx_bps: null, tx_bps: null, processes: 1, session: null, ...over,
})

describe("host", () => {
  it("gives every kind a column, zero when missing", () => {
    const rows = byKindRows([point(1, { session: 4 })], "cpu_by_kind")
    expect(rows[0]).toMatchObject({ t: 1, session: 4, browser: 0, other: 0 })
    expect(pairRows([point(2, {})], "rx_bps", "tx_bps")).toEqual([{ t: 2, rx_bps: 5, tx_bps: 6 }])
  })

  it("ranks consumers by a metric and leaves out the idle ones", () => {
    const rows = rank([consumer({ key: "a", cpu_percent: 1 }), consumer({ key: "b", cpu_percent: 5 }),
      consumer({ key: "c", cpu_percent: 0 })], (c) => c.cpu_percent)
    expect(rows.map((c) => c.key)).toEqual(["b", "a"])
  })

  it("names sessions by their title", () => {
    const session = { id: "W-1", title: "fix the build", state: "running" as const }
    expect(consumerLabel(consumer({ session }))).toBe("fix the build")
    expect(consumerLabel(consumer({ kind: "browser", session }))).toBe("Browser · fix the build")
    expect(consumerLabel(consumer({ kind: "service", name: "bridge" }))).toBe("bridge")
  })
})
