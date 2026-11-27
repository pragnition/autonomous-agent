// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { MemoryRouter, Route, Routes, useLocation } from "react-router"
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"
import type { BoardTask, TaskBoard } from "@/api"
import { TaskBoardView } from "@/pages/Tasks"
import { TooltipProvider } from "@/components/ui/tooltip"
import { TaskCard } from "./board"

afterEach(cleanup)

beforeAll(() => {
  // Radix positions popovers with ResizeObserver, which jsdom lacks
  globalThis.ResizeObserver ??= class { observe() {} unobserve() {} disconnect() {} }
})

const NOW = Date.parse("2026-11-27T10:00:00Z")

function task(over: Partial<BoardTask> = {}): BoardTask {
  return {
    conversation_id: "conv-1", title: "Add CSV export", state: "running", source: "slack", project: null,
    requester: "ann", started_at: "2026-11-27T09:00:00Z", last_active_at: "2026-11-27T09:50:00Z", update: null,
    steps: [], attention: [], slack_url: "https://slack.example/p50.0", parent: null, ...over,
  }
}

/** As the app renders it: under the router and the tooltip provider main.tsx sets up. */
function inApp(ui: React.ReactNode, url = "/tasks") {
  return render(<TooltipProvider><MemoryRouter initialEntries={[url]}>{ui}</MemoryRouter></TooltipProvider>)
}

function card(t: BoardTask, onDismiss = () => {}) {
  return inApp(<TaskCard task={t} now={NOW} onDismiss={onDismiss} />)
}

describe("TaskCard", () => {
  it("opens the Slack thread from its title", () => {
    card(task())
    expect(screen.getByRole("link", { name: /Add CSV export/ }).getAttribute("href")).toBe("https://slack.example/p50.0")
  })

  it("opens the transcript from its title when it has no Slack thread", () => {
    card(task({ slack_url: null }))
    expect(screen.getByRole("link", { name: "Add CSV export" }).getAttribute("href")).toBe("/sessions/conv-1")
  })

  it("counts a waiting task's time up to now, not to its last activity", () => {
    card(task({ state: "waiting", started_at: "2026-11-27T09:00:00Z", last_active_at: "2026-11-27T09:00:00Z" }))
    expect(screen.getByText("1h 0m")).toBeTruthy()
  })

  it("stops a finished task's time at its last activity", () => {
    card(task({ state: "done", started_at: "2026-11-27T09:00:00Z", last_active_at: "2026-11-27T09:30:00Z" }))
    expect(screen.getByText("30m")).toBeTruthy()
  })

  it("shows the share of steps done", () => {
    card(task({ steps: [{ content: "Endpoint", status: "completed" }, { content: "Button", status: "pending" },
      { content: "Docs", status: "completed" }] }))
    const bar = screen.getByRole("progressbar")
    expect([bar.getAttribute("aria-valuenow"), bar.getAttribute("aria-valuemax")]).toEqual(["2", "3"])
  })

  it("shows the question when the blocked step is hovered", async () => {
    card(task({
      state: "waiting",
      steps: [{ content: "Decide the columns", status: "blocked" }],
      attention: [{ id: 7, kind: "question", text: "Include phone numbers?", at: "2026-11-27T09:40:00Z", slack_url: null }],
    }))
    expect(screen.queryByText("Include phone numbers?")).toBeNull()

    fireEvent.pointerEnter(screen.getByRole("button", { name: "Decide the columns" }))

    expect(await screen.findByText("Include phone numbers?")).toBeTruthy()
  })

  it("puts the question on the state icon when no step is blocked", async () => {
    card(task({
      state: "waiting",
      attention: [{ id: 7, kind: "question", text: "Which env?", at: "2026-11-27T09:40:00Z", slack_url: null }],
    }))

    fireEvent.click(screen.getByRole("button", { name: "Waiting on your answer" }))

    expect(await screen.findByText("Which env?")).toBeTruthy()
  })

  it("shows a flagged latest update once, as the flag", () => {
    const at = "2026-11-27T09:40:00Z"
    card(task({ update: { text: "Check hosting", at },
      attention: [{ id: 42, kind: "look", text: "Check hosting", at, slack_url: null }] }))
    expect(screen.getAllByText("Check hosting")).toHaveLength(1)
  })

  it("dismisses a flagged update by its id", () => {
    const onDismiss = vi.fn()
    card(task({ attention: [{ id: 42, kind: "look", text: "Check hosting", at: "2026-11-27T09:40:00Z", slack_url: null }] }),
      onDismiss)

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }))

    expect(onDismiss).toHaveBeenCalledWith(42)
  })
})

function Where() {
  return <span data-testid="where">{useLocation().search}</span>
}

function boardAt(board: TaskBoard, url: string) {
  return inApp(
    <Routes><Route path="/tasks" element={<><TaskBoardView board={board} now={NOW} onDismiss={() => {}} /><Where /></>} /></Routes>,
    url)
}

const BOARD: TaskBoard = {
  since: "2026-11-26T10:00:00Z",
  channels: [
    { key: "C1", label: "roster-dev", kind: "channel", tasks: [task({ conversation_id: "a", title: "Roster task" })] },
    { key: "C2", label: "nyp-tender", kind: "channel", tasks: [task({ conversation_id: "b", title: "Tender task" })] },
  ],
}

describe("TaskBoardView", () => {
  it("opens the channel named in the address", () => {
    boardAt(BOARD, "/tasks?channel=C2")
    const shown = (title: RegExp) => screen.queryByRole("link", { name: title }) !== null
    expect([shown(/Tender task/), shown(/Roster task/)]).toEqual([true, false])
  })

  it("opens the first channel when the address names none it has", () => {
    boardAt(BOARD, "/tasks?channel=gone")
    expect(screen.getByRole("link", { name: /Roster task/ })).toBeTruthy()
  })

  it("keeps the chosen channel in the address", () => {
    boardAt(BOARD, "/tasks")

    fireEvent.mouseDown(screen.getByRole("tab", { name: /nyp-tender/ }))

    expect(screen.getByTestId("where").textContent).toBe("?channel=C2")
  })
})
