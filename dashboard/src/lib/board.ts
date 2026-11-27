import type { BoardTask } from "@/api"

const ACTIVE = new Set(["running", "queued", "waiting", "parked", "idle"])

/** Whether the task is still open: going, waiting, parked or idle, not finished. */
export function isActive(t: BoardTask): boolean {
  return ACTIVE.has(t.state)
}

/** The tasks the agent has going now, and the ones that finished in the board's window. */
export function split(tasks: BoardTask[]): { active: BoardTask[]; finished: BoardTask[] } {
  return { active: tasks.filter(isActive), finished: tasks.filter((t) => !isActive(t)) }
}

/** Steps done out of steps listed; null when the agent hasn't listed any. */
export function stepProgress(t: BoardTask): { done: number; total: number } | null {
  if (t.steps.length === 0) return null
  return { done: t.steps.filter((s) => s.status === "completed").length, total: t.steps.length }
}
