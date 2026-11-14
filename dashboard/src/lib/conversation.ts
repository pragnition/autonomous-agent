import type { SessionRow } from "@/api"

/** The turns a conversation page shows expanded at first: the one its link named (else the latest), and any turn
 * still running or queued. */
export function openByDefault(turns: Pick<SessionRow, "id" | "state">[], focus: string | null): Set<string> {
  const open = new Set(turns.filter((t) => t.state === "running" || t.state === "queued").map((t) => t.id))
  const named = turns.find((t) => t.id === focus) ?? turns.at(-1)
  if (named) open.add(named.id)
  return open
}
