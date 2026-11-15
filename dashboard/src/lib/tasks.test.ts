import { describe, expect, it } from "vitest"
import type { TaskRecord } from "@/api"
import { GENERAL, projectOptions, tasksIn } from "./tasks"

const task = (id: string, project: string | null): TaskRecord => ({
  id, title: id, source: "slack", project, requested_by: "ann", followed_up_by: [], where: [],
  started_at: "2026-11-15T10:00:00Z", ended_at: null, work_time_s: 60, requests: 1, finished: true,
  outcome: null, links: [], steps: [],
})

const tasks = [task("TK-1", "chilliclaw"), task("TK-2", null), task("TK-3", "beta"), task("TK-4", "chilliclaw")]

describe("task filters", () => {
  it("offers every project by count, then general, with counts", () => {
    expect(projectOptions(tasks)).toEqual([
      { value: "chilliclaw", label: "chilliclaw", count: 2 },
      { value: "beta", label: "beta", count: 1 },
      { value: GENERAL, label: "General", count: 1 },
    ])
  })

  it("filters by a project, by general, or not at all", () => {
    expect(tasksIn(tasks, "chilliclaw").map((t) => t.id)).toEqual(["TK-1", "TK-4"])
    expect(tasksIn(tasks, GENERAL).map((t) => t.id)).toEqual(["TK-2"])
    expect(tasksIn(tasks, "all")).toHaveLength(4)
  })
})

describe("the project filter", () => {
  it("falls back to all when its project has no tasks in the range", async () => {
    const { filterOrAll } = await import("./tasks")
    expect(filterOrAll("beta", projectOptions(tasks))).toBe("beta")
    expect(filterOrAll("gamma", projectOptions(tasks))).toBe("all")
    expect(filterOrAll("all", [])).toBe("all")
  })
})
