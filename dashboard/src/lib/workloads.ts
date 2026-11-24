import type { ConversationBox, Proc } from "@/api"
import { fmtDuration } from "@/lib/format"

/** What the page shows on the right: a conversation's container, a service, a stack, or one of the stack's own. */
export type Selection =
  | { kind: "container"; name: string }
  | { kind: "service"; name: string }
  | { kind: "stack"; name: string }
  | { kind: "system"; name: string }

export function parseSelection(sel: string | null): Selection | null {
  if (!sel) return null
  const at = sel.indexOf(":")
  if (at < 1) return null
  const kind = sel.slice(0, at)
  const name = sel.slice(at + 1)
  if (!name || !["container", "service", "stack", "system"].includes(kind)) return null
  return { kind, name } as Selection
}

export const selectionKey = (s: Selection) => `${s.kind}:${s.name}`

/** "in 3h 20m", "now" when it has passed, "—" without a time. */
export function untilText(at: string | null | undefined, now: number = Date.now()): string {
  if (!at) return "—"
  const s = (Date.parse(at) - now) / 1000
  return s <= 0 ? "now" : `in ${fmtDuration(s)}`
}

/** A container's state in a word, and how loud to show it. */
export function boxState(c: ConversationBox): { label: string; tone: "live" | "on" | "off" | "gone" } {
  if (c.state === "missing") return { label: "gone", tone: "gone" }
  if (c.state !== "running") return { label: "stopped", tone: "off" }
  if (c.turn_running) return { label: "turn running", tone: "live" }
  if (c.listeners.length) return { label: `serving ${c.listeners.length === 1 ? `:${c.listeners[0]}` : `${c.listeners.length} ports`}`, tone: "on" }
  return { label: "idle", tone: "on" }
}

// eslint-disable-next-line no-control-regex
const ANSI = /\u001b\[[0-9;?]*[ -/]*[@-~]|\u001b\][^\u0007]*(\u0007|\u001b\\)|\u001b[()][0-9A-B]/g

/** A log line without terminal colours and cursor codes. */
export function stripAnsi(line: string): string {
  return line.replace(ANSI, "")
}

/** Processes in tree order (each under what started it), with their depth. */
export function processTree(procs: Proc[]): { p: Proc; depth: number }[] {
  const pids = new Set(procs.map((p) => p.pid))
  const kids = new Map<number, Proc[]>()
  for (const p of procs) {
    const parent = pids.has(p.ppid) && p.ppid !== p.pid ? p.ppid : 0
    kids.set(parent, [...(kids.get(parent) ?? []), p])
  }
  const out: { p: Proc; depth: number }[] = []
  const seen = new Set<number>()
  const walk = (parent: number, depth: number) => {
    for (const p of (kids.get(parent) ?? []).sort((a, b) => a.pid - b.pid)) {
      if (seen.has(p.pid)) continue
      seen.add(p.pid)
      out.push({ p, depth })
      walk(p.pid, depth + 1)
    }
  }
  walk(0, 0)
  return out
}

/** A process and everything under it, summed: what stopping it frees. */
export function subtreeTotals(procs: Proc[], pid: number): { pss: number; cpu: number; count: number } {
  const under = new Set([pid])
  let grew = true
  while (grew) {
    grew = false
    for (const p of procs) if (under.has(p.ppid) && !under.has(p.pid)) { under.add(p.pid); grew = true }
  }
  const mine = procs.filter((p) => under.has(p.pid))
  return { pss: mine.reduce((a, p) => a + p.pss, 0), cpu: mine.reduce((a, p) => a + p.cpu_percent, 0), count: mine.length }
}

export const KIND_LABEL: Record<Proc["kind"], string> = {
  init: "init", holder: "holder", claude: "Claude Code", server: "server", process: "process",
}
