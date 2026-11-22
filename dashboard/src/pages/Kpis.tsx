import { ChevronDown, ChevronRight } from "lucide-react"
import { Fragment, type KeyboardEvent, useState } from "react"
import { Link } from "react-router"
import { type AppUsage, type DailyTokens, type TaskRecord, type Usage, type UsageWindow, type UsageWindowName, useKpis, useMe } from "@/api"
import { StackedColumns } from "@/components/charts"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { SourceBadge } from "@/components/session"
import { StatTile } from "@/components/StatTile"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { fmtDateTime, fmtDuration, fmtPct, fmtTokens, fmtUsd } from "@/lib/format"
import { filterOrAll, GENERAL, projectOptions, tasksIn } from "@/lib/tasks"

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
  const onKey = (id: string) => (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault()
      toggle(id)
    }
  }
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
            <TableRow className="cursor-pointer" onClick={() => toggle(t.id)} onKeyDown={onKey(t.id)} role="button"
              tabIndex={0} aria-expanded={open.has(t.id)}>
              <TableCell className="max-w-[32rem]">
                <div className="flex items-center gap-1.5">
                  {open.has(t.id)
                    ? <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                    : <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />}
                  <span className="truncate font-medium">{t.title}</span>
                </div>
                <div className="mt-1 flex items-center gap-1.5 pl-5 text-xs text-muted-foreground">
                  <Badge variant="outline" className="font-normal">{t.project ?? GENERAL}</Badge>
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

function TasksDialog({ tasks, days, count, sub }: { tasks: TaskRecord[]; days: number; count: number; sub: string }) {
  const [chosen, setProject] = useState("all")
  const options = projectOptions(tasks)
  const project = filterOrAll(chosen, options)
  const shown = tasksIn(tasks, project)
  return (
    <Dialog>
      <DialogTrigger asChild>
        <button type="button" className="rounded-xl text-left transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          aria-label={`Show the ${count} tasks`}>
          <StatTile label="Tasks" value={count} sub={sub} className="h-full bg-transparent" />
        </button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-5xl">
        <DialogHeader>
          <DialogTitle>Tasks, last {days} days</DialogTitle>
          <DialogDescription>
            Newest first. Project tasks are work on a project; general tasks belong to none. Chat isn't counted. Click
            a task for its steps.
          </DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-2">
          <Select value={project} onValueChange={setProject}>
            <SelectTrigger size="sm" className="w-56" aria-label="Project"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All projects ({tasks.length})</SelectItem>
              {options.map((o) => <SelectItem key={o.value} value={o.value}>{o.label} ({o.count})</SelectItem>)}
            </SelectContent>
          </Select>
          <span className="text-xs text-muted-foreground tabular">{plural(shown.length, "task", "tasks")}</span>
        </div>
        <div className="-mx-6 min-h-0 flex-1 overflow-y-auto border-t px-2">
          <TaskList tasks={shown} />
        </div>
      </DialogContent>
    </Dialog>
  )
}

const WINDOWS: { key: UsageWindowName; label: string }[] =
  [{ key: "today", label: "Today" }, { key: "7d", label: "Last 7 days" }, { key: "month", label: "This month" }]

function UsageTable({ rows, name, totals }:
  { rows: Record<string, Usage | AppUsage>; name: string; totals?: Usage }) {
  const entries = Object.entries(rows)
  const billed = !totals   // the apps' calls: what the provider bills; the agent's own runs show no cost
  const cols = billed ? 7 : 6
  const cells = (u: Usage | AppUsage) => (
    <>
      <TableCell className="tabular text-right">{u.runs}</TableCell>
      <TableCell className="tabular text-right">{fmtTokens(u.input_tokens)}</TableCell>
      <TableCell className="tabular text-right">{fmtTokens(u.output_tokens)}</TableCell>
      <TableCell className="tabular text-right">{fmtTokens(u.cache_read_tokens)}</TableCell>
      <TableCell className="tabular text-right">{fmtTokens(u.cache_write_tokens)}</TableCell>
      {billed && <TableCell className="tabular text-right">{fmtUsd("cost_usd" in u ? u.cost_usd : null)}</TableCell>}
    </>
  )
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>{name}</TableHead>
          <TableHead className="text-right">{totals ? "Runs" : "Calls"}</TableHead>
          <TableHead className="text-right">Input</TableHead>
          <TableHead className="text-right">Output</TableHead>
          <TableHead className="text-right">Cache read</TableHead>
          <TableHead className="text-right">Cache write</TableHead>
          {billed && <TableHead className="text-right">Billed</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {entries.length === 0 && (
          <TableRow>
            <TableCell colSpan={cols} className="py-6 text-center text-muted-foreground">Nothing in this window.</TableCell>
          </TableRow>
        )}
        {entries.map(([key, u]) => <TableRow key={key}><TableCell>{key}</TableCell>{cells(u)}</TableRow>)}
        {totals && entries.length > 1 && (
          <TableRow className="font-medium"><TableCell>Total</TableCell>{cells(totals)}</TableRow>
        )}
      </TableBody>
    </Table>
  )
}

function TokenUsage({ usage }: { usage: Record<UsageWindowName, UsageWindow> }) {
  const [chosen, setChosen] = useState<UsageWindowName>("today")
  const w = usage[chosen]
  return (
    <Card className="mt-6 shadow-none">
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div className="space-y-1.5">
          <CardTitle className="text-sm font-medium">Tokens by project</CardTitle>
          <CardDescription className="text-xs">
            Every run, counted to the project of the task its request belongs to (else its conversation's);
            general is work on none.
          </CardDescription>
        </div>
        <Select value={chosen} onValueChange={(v) => setChosen(v as UsageWindowName)}>
          <SelectTrigger size="sm" className="w-36" aria-label="Usage window"><SelectValue /></SelectTrigger>
          <SelectContent>
            {WINDOWS.map((o) => <SelectItem key={o.key} value={o.key}>{o.label}</SelectItem>)}
          </SelectContent>
        </Select>
      </CardHeader>
      <CardContent className="space-y-6">
        <UsageTable rows={w.by_project} name="Project" totals={w.total} />
        {Object.keys(w.apps_by_upstream).length > 0 && (
          <div>
            <div className="mb-1 text-xs text-muted-foreground">The apps' own model calls, through the meter</div>
            <UsageTable rows={w.apps_by_upstream} name="Upstream" />
          </div>
        )}
      </CardContent>
    </Card>
  )
}

type TokenMetric = "output_tokens" | "input_tokens" | "cache_read_tokens" | "cache_write_tokens"
const METRICS: { key: TokenMetric; label: string }[] = [
  { key: "output_tokens", label: "Output" }, { key: "input_tokens", label: "Input" },
  { key: "cache_read_tokens", label: "Cache read" }, { key: "cache_write_tokens", label: "Cache write" }]

function DailyTokenChart({ daily }: { daily: DailyTokens }) {
  const [metric, setMetric] = useState<TokenMetric>("output_tokens")
  const named = daily.projects.filter((p) => p !== GENERAL)
  // eight series colors; the projects past them, and general, share the neutral one
  const series = daily.projects.map((p) => {
    const i = named.indexOf(p)
    return { key: p, label: p, color: i >= 0 && i < 8 ? `var(--series-${i + 1})` : "var(--series-other)" }
  })
  const data = daily.days.map((d) => ({
    date: d.date as unknown as number,
    ...Object.fromEntries(daily.projects.map((p) => [p, d.by_project[p]?.[metric] ?? 0])),
  }))
  return (
    <Card className="mt-6 shadow-none">
      <CardHeader className="flex flex-row items-start justify-between gap-2">
        <div className="space-y-1.5">
          <CardTitle className="text-sm font-medium">Tokens per day by project</CardTitle>
          <CardDescription className="text-xs">the last 7 days, counted as in Tokens by project</CardDescription>
        </div>
        <Select value={metric} onValueChange={(v) => setMetric(v as TokenMetric)}>
          <SelectTrigger size="sm" className="w-36" aria-label="Token kind"><SelectValue /></SelectTrigger>
          <SelectContent>
            {METRICS.map((o) => <SelectItem key={o.key} value={o.key}>{o.label}</SelectItem>)}
          </SelectContent>
        </Select>
      </CardHeader>
      <CardContent>
        {series.length === 0
          ? <div className="py-10 text-center text-sm text-muted-foreground">No runs in the last 7 days.</div>
          : <StackedColumns data={data} xKey="date" xFormat={(v) => dayLabel(v)} labelFormat={dayLabel}
              yFormat={(v) => fmtTokens(v)} series={series} />}
      </CardContent>
    </Card>
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
        {({ kpis: k, daily, tasks, usage, daily_tokens }) => (
          <>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <TasksDialog tasks={tasks} days={days} count={k.tasks}
                sub={Object.entries(k.by_project).map(([name, n]) => `${name} ${n}`).join(" · ") || "none yet"} />
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

            <TokenUsage usage={usage} />
            <DailyTokenChart daily={daily_tokens} />
          </>
        )}
      </QueryState>
    </Page>
  )
}
