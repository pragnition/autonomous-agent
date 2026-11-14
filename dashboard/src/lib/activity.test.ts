import { describe, expect, it } from "vitest"
import type { Entry } from "@/api"
import { pairEntries } from "./activity"

const call = (id: string, tool_use_id: string): Entry =>
  ({ id, ts: null, kind: "tool_call", tool: "Bash", tool_use_id, summary: "ls", input: { command: "ls" } })
const res = (id: string, tool_use_id: string): Entry =>
  ({ id, ts: null, kind: "tool_result", tool_use_id, is_error: false, content: "ok" })
const text = (id: string): Entry => ({ id, ts: null, kind: "text", role: "assistant", text: "hi" })

describe("pairEntries", () => {
  it("pairs a result with its call", () => {
    const items = pairEntries([call("1:0", "t1"), res("2:0", "t1")])
    expect(items).toHaveLength(1)
    expect(items[0].entry.id).toBe("1:0")
    expect(items[0].result?.id).toBe("2:0")
  })

  it("keeps an orphan result as its own item", () => {
    const items = pairEntries([res("1:0", "nope"), text("2:0")])
    expect(items.map((i) => i.entry.id)).toEqual(["1:0", "2:0"])
    expect(items[0].result).toBeUndefined()
  })

  it("keeps order, with parallel calls answered later", () => {
    const items = pairEntries([call("1:0", "a"), call("1:1", "b"), text("2:0"), res("3:0", "b"), res("3:1", "a")])
    expect(items.map((i) => i.entry.id)).toEqual(["1:0", "1:1", "2:0"])
    expect(items[0].result?.id).toBe("3:1")
    expect(items[1].result?.id).toBe("3:0")
  })

  it("does not mutate across calls", () => {
    const entries = [call("1:0", "t1")]
    expect(pairEntries(entries)[0].result).toBeUndefined()
    expect(pairEntries([...entries, res("2:0", "t1")])[0].result?.id).toBe("2:0")
  })
})
