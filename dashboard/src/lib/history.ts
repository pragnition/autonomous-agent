import type { History } from "@/api"
import { sumTokens } from "@/lib/format"

const HOUR = 3600

/** Tokens per hour, the agent's origins against everyone else's, from buckets of any size up to an hour. */
export function agentPeopleHourly(h: History): { t: number; agent: number; people: number }[] {
  const hours = new Map<number, { t: number; agent: number; people: number }>()
  for (const b of h.buckets) {
    const t = Math.floor(b.t / HOUR) * HOUR
    const row = hours.get(t) ?? { t, agent: 0, people: 0 }
    row.agent += sumTokens(b.agent)
    row.people += sumTokens(b.people)
    hours.set(t, row)
  }
  return [...hours.values()].sort((a, b) => a.t - b.t)
}
