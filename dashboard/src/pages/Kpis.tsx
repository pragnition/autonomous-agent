import { ChevronDown, ChevronRight } from "lucide-react"
import { Fragment, useState } from "react"
import { Link } from "react-router"
import { type TaskRecord, useKpis, useMe } from "@/api"
import { StackedColumns } from "@/components/charts"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { SourceBadge } from "@/components/session"
import { StatTile } from "@/components/StatTile"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { fmtDateTime, fmtDuration, fmtPct } from "@/lib/format"

const RANGES = [7, 30, 90]

const dayLabel = (d: string | number) =>
  new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

function Finished({ value }: { value: boolean | null }) {
  if (value == null) return <span className="text-muted-foreground">—</span>
  return value
    ? <Badge variant="secondary" className="font-normal">finished</Badge>
    : <Badge variant="outline" className="font-normal text-muted-foreground">not finished</Badge>
}

function TaskDetail({ task }: { task: TaskRecord }) {
  const links = [...task.where, ...task.links]
  return (
    <div className="space-y-3 py-2 text-sm">
      {task.outcome && <p><span className="text-muted-foreground">Outcome: </span>{task.outcome}</p>}
      {task.followed_up_by.length > 0 && (
        <p className="text-muted-foreground">Followed up by {task.followed_up_by.join(", ")}</p>
      )}
      {links.length > 0 && (
        <ul className="space-y-0.5">
          {links.map((url) => (
            <li key={url} className="truncate">
              <a href={url} target="_blank" rel="noreferrer" className="text-xs hover:underline">{url}</a>
            </li>
          ))}
        </ul>
      )}
      <ol className="space-y-2 border-l pl-3">
        {task.steps.map((s) => (
          <li key={s.work_item}>
            <div className="text-xs text-muted-foreground">
              {fmtDateTime(s.at)} · {s.by ?? "someone"}
              {s.conversation && <> · <Link to={`/sessions/${s.conversation}`} className="hover:underline">session</Link></>}
            </div>
            <div className="whitespace-pre-wrap">{s.asked}</div>
            {s.outcome && <div className="text-muted-foreground">→ {s.outcome}</div>}
          </li>
        ))}
      </ol>
    </div>
  )
}

function TaskList({ tasks }: { tasks: TaskRecord[] }) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const toggle = (id: string) => setOpen((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Task</TableHead>
          <TableHead>Asked by</TableHead>
          <TableHead>Started</TableHead>
          <TableHead className="text-right">Time</TableHead>
          <TableHead>Status</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {tasks.length === 0 && (
          <TableRow>
            <TableCell colSpan={5} className="py-10 text-center text-muted-foreground">No tasks in this range.</TableCell>
          </TableRow>
        )}
        {tasks.map((t) => (
          <Fragment key={t.id}>
            <TableRow className="cursor-pointer" onClick={() => toggle(t.id)} aria-expanded={open.has(t.id)}>
              <TableCell className="max-w-[32rem]">
                <div className="flex items-center gap-1.5">
                  {open.has(t.id)
                    ? <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                    : <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />}
                  <span className="truncate font-medium">{t.title}</span>
                </div>
                <div className="mt-1 flex items-center gap-1.5 pl-5 text-xs text-muted-foreground">
                  <SourceBadge source={t.source} />
                  <span className="font-mono">{t.id}</span>
                  <span className="tabular">{plural(t.requests, "request", "requests")}</span>
                </div>
              </TableCell>
              <TableCell className="text-muted-foreground">{t.requested_by ?? "—"}</TableCell>
              <TableCell className="text-muted-foreground">{fmtDateTime(t.started_at)}</TableCell>
              <TableCell className="tabular text-right">{fmtDuration(t.work_time_s)}</TableCell>
              <TableCell><Finished value={t.finished} /></TableCell>
            </TableRow>
            {open.has(t.id) && (
              <TableRow className="hover:bg-transparent">
                <TableCell colSpan={5} className="max-w-0 whitespace-normal pl-10"><TaskDetail task={t} /></TableCell>
              </TableRow>
            )}
          </Fragment>
        ))}
      </TableBody>
    </Table>
  )
}

export function Kpis() {
  const [days, setDays] = useState(7)
  const kpis = useKpis(days)
  const agent = useMe().data?.agent || "the agent"

  return (
    <Page title="KPIs" subtitle={`What ${agent} worked on, how long it took, and what shipped.`}
      actions={
        <Select value={String(days)} onValueChange={(v) => setDays(Number(v))}>
          <SelectTrigger size="sm" className="w-32" aria-label="Range"><SelectValue /></SelectTrigger>
          <SelectContent>
            {RANGES.map((d) => <SelectItem key={d} value={String(d)}>Last {d} days</SelectItem>)}
          </SelectContent>
        </Select>
      }>
      <QueryState query={kpis} rows={6}>
        {({ kpis: k, daily, tasks }) => (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile label="Tasks" value={k.tasks}
                sub={`Slack ${k.by_source.slack} · Linear ${k.by_source.linear} · schedules ${k.by_source.schedule}`} />
              <StatTile label="Time per task" value={fmtDuration(k.time.median_s)}
                sub={k.time.n ? `median · p90 ${fmtDuration(k.time.p90_s)}` : "no tasks in this range"} />
              <StatTile label="Finished" value={fmtPct(k.finished_rate)} sub={`${k.finished} of ${k.tasks}`} />
              <StatTile label="Shipped" value={k.merged}
                sub={`${k.merged === 1 ? "PR" : "PRs"} merged · ${plural(k.issues_closed, "Linear issue", "Linear issues")} closed`} />
            </div>

            <Card className="mt-6 shadow-none">
              <CardHeader>
                <CardTitle className="text-sm font-medium">Tasks per day</CardTitle>
                <CardDescription className="text-xs">by where they came from</CardDescription>
              </CardHeader>
              <CardContent>
                <StackedColumns data={daily as unknown as Record<string, number>[]} xKey="date"
                  xFormat={(v) => dayLabel(v)} labelFormat={dayLabel} yFormat={(v) => String(v)}
                  series={[{ key: "slack", label: "Slack", color: "var(--series-1)" },
                           { key: "linear", label: "Linear", color: "var(--series-2)" },
                           { key: "schedule", label: "Schedules", color: "var(--series-3)" }]} />
              </CardContent>
            </Card>

            <Card className="mt-6 py-0 shadow-none">
              <CardHeader className="pt-5">
                <CardTitle className="text-sm font-medium">Tasks</CardTitle>
                <CardDescription className="text-xs">Newest first. Chat isn't counted. Click a task for its steps.</CardDescription>
              </CardHeader>
              <TaskList tasks={tasks} />
            </Card>
          </>
        )}
      </QueryState>
    </Page>
  )
}
