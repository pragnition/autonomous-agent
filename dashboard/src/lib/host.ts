import type { ConsumerKind, HostConsumer, HostPoint } from "@/api"
import { OTHER, type Series } from "@/components/charts"

// Each kind of consumer keeps its color everywhere on the page, in the fixed categorical order.
export const KINDS: { key: ConsumerKind; label: string; color: string }[] = [
  { key: "session", label: "Sessions", color: "var(--series-1)" },
  { key: "browser", label: "Browsers", color: "var(--series-2)" },
  { key: "service", label: "Services", color: "var(--series-3)" },
  { key: "container", label: "Containers", color: "var(--series-4)" },
  { key: "runner", label: "Runner", color: "var(--series-5)" },
  { key: "host", label: "Outside the agent", color: "var(--series-6)" },
  { key: "other", label: "Other", color: OTHER },
]

export const KIND_LABEL: Record<ConsumerKind, string> = {
  session: "Session", browser: "Browser", service: "Service", container: "Container", runner: "Runner",
  other: "Other", host: "Outside the agent container",
}

export const kindSeries: Series[] = KINDS.map((k) => ({ key: k.key, label: k.label, color: k.color }))

/** The history as chart rows: one per sample, with a column per kind (`by` picks CPU or memory). */
export function byKindRows(points: HostPoint[], by: "cpu_by_kind" | "mem_by_kind"): Record<string, number>[] {
  return points.map((p) => {
    const row: Record<string, number> = { t: p.ts }
    for (const k of KINDS) row[k.key] = p[by]?.[k.key] ?? 0
    return row
  })
}

export function pairRows(points: HostPoint[], a: keyof HostPoint, b: keyof HostPoint): Record<string, number>[] {
  return points.map((p) => ({ t: p.ts, [a]: Number(p[a]) || 0, [b]: Number(p[b]) || 0 }))
}

/** The consumers that use any of `metric`, largest first. */
export function rank(consumers: HostConsumer[], metric: (c: HostConsumer) => number | null): HostConsumer[] {
  return consumers.filter((c) => (metric(c) ?? 0) > 0).sort((a, b) => (metric(b) ?? 0) - (metric(a) ?? 0))
}

/** What a consumer is called on the page: a session's title where it has one. */
export function consumerLabel(c: { kind: ConsumerKind; name: string; session: { title: string } | null }): string {
  if (c.kind === "session") return c.session?.title ?? c.name
  if (c.kind === "browser") return c.session ? `Browser · ${c.session.title}` : "Browser"
  if (c.kind === "runner") return "Runner"
  if (c.kind === "other") return "Other processes"
  if (c.kind === "host") return KIND_LABEL.host
  return c.name
}

export function clock(t: number): string {
  return new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })
}

export function clockSeconds(t: string | number): string {
  return new Date(Number(t) * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit",
    second: "2-digit", hour12: false })
}
