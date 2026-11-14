import { describe, expect, it } from "vitest"
import type { SessionState } from "@/api"
import { openByDefault } from "./conversation"

const turn = (id: string, state: SessionState = "done") => ({ id, state })

describe("openByDefault", () => {
  it("opens the latest turn when no turn was asked for", () => {
    expect([...openByDefault([turn("W-1"), turn("W-2")], null)]).toEqual(["W-2"])
  })

  it("opens the turn the link named instead of the latest", () => {
    expect([...openByDefault([turn("W-1"), turn("W-2")], "W-1")]).toEqual(["W-1"])
  })

  it("falls back to the latest turn when the named one isn't in the conversation", () => {
    expect([...openByDefault([turn("W-1"), turn("W-2")], "W-9")]).toEqual(["W-2"])
  })

  it("always opens running and queued turns", () => {
    const open = openByDefault([turn("W-1", "running"), turn("W-2", "queued"), turn("W-3")], "W-3")
    expect([...open].sort()).toEqual(["W-1", "W-2", "W-3"])
  })

  it("opens nothing for a conversation without turns", () => {
    expect(openByDefault([], null).size).toBe(0)
  })
})
