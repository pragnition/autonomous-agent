// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router"
import { afterEach, describe, expect, it } from "vitest"
import type { SessionRow } from "@/api"
import { SessionCard } from "./session"

afterEach(cleanup)

function row(over: Partial<SessionRow> = {}): SessionRow {
  return {
    id: "W-1", kind: "turn", state: "done", project: null, conversation_id: null, task_id: null,
    title: "Fix the login", message: "Fix the login", source: "slack", requester: "ann", created_at: "2026-11-13T09:00:00Z",
    started_at: "2026-11-13T09:00:01Z", ended_at: "2026-11-13T09:05:00Z", account: "badger",
    account_name: null, turns: 3, tokens: { in: 10, out: 20, cache_read: 0, cache_write: 0 },
    outcome: "answered", error: null, live: false, links: { slack: null, linear: null, pr: null },
    ...over,
  }
}

describe("SessionCard", () => {
  it("shows the project badge when the session is bound to a project", () => {
    render(<MemoryRouter><SessionCard row={row({ project: "chilliclaw" })} now={Date.now()} /></MemoryRouter>)
    expect(screen.getByText("chilliclaw")).toBeTruthy()
  })

  it("shows no project badge when the session isn't bound to a project", () => {
    const { container } = render(
      <MemoryRouter><SessionCard row={row({ project: null })} now={Date.now()} /></MemoryRouter>)
    expect(screen.queryByText("chilliclaw")).toBeNull()
    expect(container.querySelectorAll(".font-mono.font-normal")).toHaveLength(0)
  })
})
