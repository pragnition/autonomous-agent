import type { History, Tokens } from "@/api"
import { MAX_SERIES, OTHER, type Series, seriesColor } from "@/components/charts"
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


export const KINDS: { key: keyof Tokens; label: string }[] = [
  { key: "in", label: "Input" },
  { key: "out", label: "Output" },
  { key: "cache_read", label: "Cache read" },
  { key: "cache_write", label: "Cache write" },
]

/** Chart rows (one per bucket, every bucket present) and their series: by account or origin (past seven, the
 * smallest fold into Other) or by token kind across all accounts. Series keep alphabetical order, so a series keeps
 * its color while the range doesn't change who is in the top seven. */
export function toSeries(h: History, by: "account" | "origin" | "kind"): { data: Record<string, number>[]; series: Series[] } {
  if (by === "kind") {
    const data = h.buckets.map((b) => {
      const row: Record<string, number> = { t: b.t, in: 0, out: 0, cache_read: 0, cache_write: 0 }
      for (const tk of Object.values(b.by_account)) for (const k of KINDS) row[k.key] += tk[k.key]
      return row
    })
    return { data, series: KINDS.map((k, i) => ({ key: k.key, label: k.label, color: seriesColor(i) })) }
  }
  const pick = (b: History["buckets"][number]) => (by === "account" ? b.by_account : b.by_origin)
  const names = by === "account" ? h.accounts : h.origins
  const totals = new Map(names.map((n) => [n, 0]))
  for (const b of h.buckets) for (const [n, tk] of Object.entries(pick(b))) totals.set(n, (totals.get(n) ?? 0) + sumTokens(tk))
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([n]) => n)
  const fold = ranked.length > MAX_SERIES + 1
  const kept = (fold ? ranked.slice(0, MAX_SERIES) : ranked).sort((a, b) => a.localeCompare(b))
  const keptSet = new Set(kept)
  const data = h.buckets.map((b) => {
    const row: Record<string, number> = { t: b.t, ...Object.fromEntries(kept.map((n) => [n, 0])) }
    if (fold) row.other = 0
    for (const [n, tk] of Object.entries(pick(b))) {
      if (keptSet.has(n)) row[n] = sumTokens(tk)
      else if (fold) row.other += sumTokens(tk)
    }
    return row
  })
  const series: Series[] = kept.map((n, i) => ({ key: n, label: n, color: seriesColor(i) }))
  if (fold) series.push({ key: "other", label: "Other", color: OTHER })
  return { data, series }
}
