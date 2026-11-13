import { useState } from "react"
import { type Stat, useKpis } from "@/api"
import { StackedColumns } from "@/components/charts"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { StatTile } from "@/components/StatTile"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { fmtDuration, fmtPct } from "@/lib/format"

const RANGES = [7, 30, 90]
const ALL = "all"

const dayLabel = (d: string | number) =>
  new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })

function TimeTile({ label, stat }: { label: string; stat: Stat }) {
  return (
    <StatTile label={label} value={fmtDuration(stat.median_s)}
      sub={stat.n ? `median · p90 ${fmtDuration(stat.p90_s)} · n=${stat.n}` : "no data in this range"} />
  )
}

export function Kpis() {
  const [days, setDays] = useState(7)
  const [project, setProject] = useState("")
  const kpis = useKpis(days, project || undefined)
  const projects = kpis.data?.projects ?? []

  return (
    <Page title="KPIs" subtitle="How fast work moves from ticket to merged PR."
      actions={
        <>
          <Select value={project || ALL} onValueChange={(v) => setProject(v === ALL ? "" : v)}>
            <SelectTrigger size="sm" className="w-40" aria-label="Project"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>All projects</SelectItem>
              {[...new Set([...projects, ...(project ? [project] : [])])].map((p) => (
                <SelectItem key={p} value={p}>{p}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Select value={String(days)} onValueChange={(v) => setDays(Number(v))}>
            <SelectTrigger size="sm" className="w-32" aria-label="Range"><SelectValue /></SelectTrigger>
            <SelectContent>
              {RANGES.map((d) => <SelectItem key={d} value={String(d)}>Last {d} days</SelectItem>)}
            </SelectContent>
          </Select>
        </>
      }>
      <QueryState query={kpis} rows={6}>
        {({ kpis: k, daily }) => (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <TimeTile label="Ticket → pickup" stat={k.pickup} />
              <TimeTile label="Pickup → PR" stat={k.to_pr} />
              <TimeTile label="PR → merge" stat={k.to_merge} />
              <TimeTile label="Queue wait" stat={k.queue_wait} />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile label="Merge rate" value={fmtPct(k.merge_rate)}
                sub={`${k.merged} merged of ${k.prs} PRs · ${k.closed} closed unmerged`} />
              <StatTile label="Tasks" value={k.tasks}
                sub={`${k.failed} failed · ${k.cancelled} cancelled · ${k.reverted} reverted`} />
            </div>

            <Card className="mt-6 shadow-none">
              <CardHeader>
                <CardTitle className="text-sm font-medium">Throughput per day</CardTitle>
                <CardDescription className="text-xs">tasks created, PRs opened and PRs merged</CardDescription>
              </CardHeader>
              <CardContent>
                <StackedColumns data={daily as unknown as Record<string, number>[]} xKey="date" stacked={false}
                  xFormat={(v) => dayLabel(v)} labelFormat={dayLabel} yFormat={(v) => String(v)}
                  series={[{ key: "tasks", label: "Tasks", color: "var(--series-1)" },
                           { key: "prs", label: "PRs opened", color: "var(--series-2)" },
                           { key: "merged", label: "Merged", color: "var(--series-3)" }]} />
              </CardContent>
            </Card>
          </>
        )}
      </QueryState>
    </Page>
  )
}
