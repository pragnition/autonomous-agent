import type { TaskRecord } from "@/api"

// A task on no configured project
export const GENERAL = "general"

export type ProjectOption = { value: string; label: string; count: number }

// The project filter's choices: each project with tasks, most first, then General
export function projectOptions(tasks: TaskRecord[]): ProjectOption[] {
  const counts = new Map<string, number>()
  let general = 0
  for (const t of tasks) {
    if (t.project) counts.set(t.project, (counts.get(t.project) ?? 0) + 1)
    else general += 1
  }
  const projects = [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([value, count]) => ({ value, label: value, count }))
  return general ? [...projects, { value: GENERAL, label: "General", count: general }] : projects
}

// The tasks a filter keeps: "all", a project's name, or GENERAL
export function tasksIn(tasks: TaskRecord[], filter: string): TaskRecord[] {
  if (filter === "all") return tasks
  return tasks.filter((t) => (filter === GENERAL ? !t.project : t.project === filter))
}

// The filter to apply: the chosen one while it still has tasks in the range, else "all" (a project with no tasks
// in a shorter range would leave the list empty under a filter the menu no longer shows)
export function filterOrAll(filter: string, options: ProjectOption[]): string {
  return options.some((o) => o.value === filter) ? filter : "all"
}
