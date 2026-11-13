import { useState } from "react"
import { type Stat, useKpis } from "@/api"
import { StackedColumns } from "@/components/charts"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { StatTile } from "@/components/StatTile"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { fmtDuration, fmtPct, fmtUsd } from "@/lib/format"

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
  const projects = (kpis.data?.cost_by_project ?? []).map((p) => p.project).filter((p) => p !== "(none)")

  return (
    <Page title="KPIs & cost" subtitle="How fast work moves from ticket to merged PR, and what it costs."
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
        {({ kpis: k, daily, cost_by_project }) => {
          const maxCost = Math.max(0, ...cost_by_project.map((p) => p.cost_usd))
          return (
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
                <StatTile label="Cost" value={fmtUsd(k.cost_usd)} sub={`${fmtUsd(k.cost_per_task)} per task`} />
                <StatTile label="Cost per merged PR" value={fmtUsd(k.cost_per_merged)}
                  sub="every run of the tasks behind them" />
              </div>

              <div className="mt-6 grid gap-6 xl:grid-cols-2">
                <Card className="shadow-none">
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
                <Card className="shadow-none">
                  <CardHeader>
                    <CardTitle className="text-sm font-medium">Cost per day</CardTitle>
                    <CardDescription className="text-xs">estimated from each run's token use</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <StackedColumns data={daily as unknown as Record<string, number>[]} xKey="date"
                      xFormat={(v) => dayLabel(v)} labelFormat={dayLabel} yFormat={(v) => fmtUsd(v)}
                      series={[{ key: "cost_usd", label: "Cost", color: "var(--series-1)" }]} />
                  </CardContent>
                </Card>
              </div>

              <Card className="mt-6 shadow-none">
                <CardHeader>
                  <CardTitle className="text-sm font-medium">Cost by project</CardTitle>
                  <CardDescription className="text-xs">all projects in this range; "(none)" is work outside a project</CardDescription>
                </CardHeader>
                <CardContent>
                  {cost_by_project.length === 0 ? (
                    <p className="text-sm text-muted-foreground">No runs in this range.</p>
                  ) : (
                    <ul className="space-y-2.5">
                      {cost_by_project.map((p) => (
                        <li key={p.project} className="grid grid-cols-[8rem_1fr_6rem] items-center gap-3 text-sm">
                          <span className="truncate">{p.project}</span>
                          <div className="h-2 rounded-r-[4px] bg-[var(--series-1)]"
                            style={{ width: `${maxCost ? Math.max(1, (p.cost_usd / maxCost) * 100) : 0}%` }} aria-hidden />
                          <span className="tabular text-right">
                            {fmtUsd(p.cost_usd)} <span className="text-xs text-muted-foreground">· {p.tasks} tasks</span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </CardContent>
              </Card>
            </>
          )
        }}
      </QueryState>
    </Page>
  )
}
