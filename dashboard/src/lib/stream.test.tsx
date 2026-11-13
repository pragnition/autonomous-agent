// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { Entry } from "@/api"
import { useSessionStream } from "./activity"

class FakeEventSource {
  static all: FakeEventSource[] = []
  listeners: Record<string, ((e: MessageEvent | Event) => void)[]> = {}
  closed = false

  constructor(public url: string) {
    FakeEventSource.all.push(this)
  }

  addEventListener(type: string, fn: (e: MessageEvent | Event) => void) {
    ;(this.listeners[type] ??= []).push(fn)
  }

  close() {
    this.closed = true
  }

  emit(type: string, data?: unknown) {
    const event = data === undefined ? new Event(type) : new MessageEvent(type, { data: JSON.stringify(data) })
    for (const fn of this.listeners[type] ?? []) fn(event)
  }
}

const text = (id: string): Entry => ({ id, ts: null, kind: "text", role: "assistant", text: id })
const latest = () => FakeEventSource.all[FakeEventSource.all.length - 1]

beforeEach(() => {
  FakeEventSource.all = []
  vi.stubGlobal("EventSource", FakeEventSource)
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("useSessionStream", () => {
  it("keeps following after the session stops being active, until core says end", () => {
    const { result, rerender } = renderHook(({ active }) => useSessionStream("W-1", { offset: 10, line: 2 }, active),
      { initialProps: { active: true } })
    const source = latest()
    expect(source.url).toBe("/api/sessions/W-1/stream?offset=10&line=2")
    rerender({ active: false })
    expect(source.closed).toBe(false)
    act(() => source.emit("entries", { entries: [text("2:0")], offset: 40, line: 3 }))
    act(() => source.emit("end", { state: "done" }))
    expect(result.current.entries.map((e) => e.id)).toEqual(["2:0"])
    expect(result.current.ended).toBe("done")
    expect(source.closed).toBe(true)
  })

  it("reconnects from the latest cursor after an error, without repeating entries", () => {
    const { result } = renderHook(() => useSessionStream("W-1", { offset: 0, line: 0 }, true))
    const first = latest()
    act(() => first.emit("entries", { entries: [text("0:0"), text("1:0")], offset: 50, line: 2 }))
    act(() => first.emit("error"))
    expect(first.closed).toBe(true)
    act(() => { vi.advanceTimersByTime(2000) })
    const second = latest()
    expect(second).not.toBe(first)
    expect(second.url).toBe("/api/sessions/W-1/stream?offset=50&line=2")
    act(() => second.emit("entries", { entries: [text("1:0"), text("2:0")], offset: 80, line: 3 }))
    expect(result.current.entries.map((e) => e.id)).toEqual(["0:0", "1:0", "2:0"])
  })

  it("does not start for a session that has finished", () => {
    renderHook(() => useSessionStream("W-1", { offset: 0, line: 0 }, false))
    expect(FakeEventSource.all).toHaveLength(0)
  })
})
