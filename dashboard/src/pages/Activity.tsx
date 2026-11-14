import { Bot, CirclePause, CirclePlay, CircleX, Dot, Kanban, LayoutDashboard, MessageSquare, Terminal }
  from "lucide-react"
import { useState } from "react"
import { type EventRow, type EventSource, useEvents } from "@/api"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { fmtDateTime } from "@/lib/format"

const ALL = "all"

const LABELS: Record<string, string> = {
  stop_all: "Stopped everything",
  resume_all: "Resumed",
  session_cancelled: "Cancelled a session",
  session_started: "Session started",
  task_created: "Task created",
  project_bound: "Project bound",
  status_changed: "Task status changed",
  llm_caps_changed: "Model-call caps changed",
  llm_cap_reached: "Model-call cap reached",
}

const LOUD: Record<string, { icon: typeof Dot; tone: string }> = {
  stop_all: { icon: CirclePause, tone: "text-critical" },
  session_cancelled: { icon: CircleX, tone: "text-critical" },
  llm_cap_reached: { icon: CirclePause, tone: "text-critical" },
  resume_all: { icon: CirclePlay, tone: "text-good" },
}

const SOURCES: Record<EventSource, { label: string; icon: typeof Dot }> = {
  slack: { label: "Slack", icon: MessageSquare },
  linear: { label: "Linear", icon: Kanban },
  dashboard: { label: "Dashboard", icon: LayoutDashboard },
  "command line": { label: "Command line", icon: Terminal },
  agent: { label: "Agent", icon: Bot },
  other: { label: "Other", icon: Dot },
}

function Source({ row }: { row: EventRow }) {
  const s = SOURCES[row.source] ?? SOURCES.other
  return (
    <span className="inline-flex items-center gap-1.5">
      <s.icon className="size-4 text-muted-foreground" aria-hidden />
      <span>{s.label}</span>
      {row.sender && <span className="text-muted-foreground">· {row.sender}</span>}
    </span>
  )
}

function Rows({ rows }: { rows: EventRow[] }) {
  return rows.map((e) => {
    const loud = LOUD[e.type]
    const Icon = loud?.icon ?? Dot
    return (
      <TableRow key={e.id}>
        <TableCell className="tabular whitespace-nowrap text-muted-foreground">{fmtDateTime(e.ts)}</TableCell>
        <TableCell className="whitespace-nowrap">
          <span className="inline-flex items-center gap-1.5">
            <Icon className={`size-4 ${loud?.tone ?? "text-muted-foreground"}`} aria-hidden />
            <span className={loud ? "font-medium" : ""}>{LABELS[e.type] ?? e.type}</span>
          </span>
        </TableCell>
        <TableCell className="whitespace-nowrap"><Source row={e} /></TableCell>
      </TableRow>
    )
  })
}

function OlderPage({ type, before, last, onMore }: { type?: string; before: number; last: boolean;
  onMore: (id: number) => void }) {
  const page = useEvents(type, before)
  const rows = page.data?.rows ?? []
  return (
    <>
      <Rows rows={rows} />
      {page.data && last && (
        <TableRow>
          <TableCell colSpan={3} className="py-2 text-center">
            {rows.length === 100
              ? <Button variant="ghost" size="sm" onClick={() => onMore(rows[rows.length - 1].id)}>Load older</Button>
              : <span className="text-xs text-muted-foreground">That's everything.</span>}
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

export function Activity() {
  const [type, setType] = useState("")
  const [older, setOlder] = useState<number[]>([])
  const first = useEvents(type || undefined)

  function choose(v: string) {
    setType(v === ALL ? "" : v)
    setOlder([])
  }

  return (
    <Page title="Activity" subtitle="Everything core recorded, newest first, and where it came from."
      actions={
        <Select value={type || ALL} onValueChange={choose}>
          <SelectTrigger size="sm" className="w-56" aria-label="Event type"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>Every event</SelectItem>
            {(first.data?.types ?? []).map((t) => <SelectItem key={t} value={t}>{LABELS[t] ?? t}</SelectItem>)}
          </SelectContent>
        </Select>
      }>
      <Card className="py-0 shadow-none">
        <QueryState query={first} rows={10}>
          {({ rows }) => (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-48">When</TableHead>
                  <TableHead>What</TableHead>
                  <TableHead>Source</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.length === 0 && (
                  <TableRow><TableCell colSpan={3} className="py-10 text-center text-muted-foreground">Nothing yet.</TableCell></TableRow>
                )}
                <Rows rows={rows} />
                {older.length === 0 && rows.length === 100 && (
                  <TableRow>
                    <TableCell colSpan={3} className="py-2 text-center">
                      <Button variant="ghost" size="sm" onClick={() => setOlder([rows[rows.length - 1].id])}>Load older</Button>
                    </TableCell>
                  </TableRow>
                )}
                {older.map((before, i) => (
                  <OlderPage key={before} type={type || undefined} before={before} last={i === older.length - 1}
                    onMore={(id) => setOlder([...older, id])} />
                ))}
              </TableBody>
            </Table>
          )}
        </QueryState>
      </Card>
    </Page>
  )
}
