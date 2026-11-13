import { CirclePause, CirclePlay, CircleX, Dot } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"
import { type EventRow, useEvents } from "@/api"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { Badge } from "@/components/ui/badge"
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
  work_started: "Chore started",
  task_created: "Task created",
  status_changed: "Task status changed",
  cli_command: "Ran a CLI command",
  cli_proposed: "Proposed a CLI command",
  cli_confirmed: "CLI command confirmed",
  production_scrub: "Scrubbed production data",
  consolidated: "Memory consolidated",
  consolidation_failed: "Memory consolidation failed",
  llm_caps_changed: "Model-call caps changed",
  llm_cap_reached: "Model-call cap reached",
}

const ICONS: Record<string, typeof Dot> = {
  stop_all: CirclePause, resume_all: CirclePlay, session_cancelled: CircleX, llm_cap_reached: CirclePause,
}

function Data({ data }: { data: unknown }) {
  if (data == null) return <span className="text-muted-foreground">—</span>
  if (typeof data !== "object") return <span className="font-mono text-xs">{String(data)}</span>
  const items = Object.entries(data as Record<string, unknown>)
  return (
    <span className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs">
      {items.map(([k, v]) => {
        const value = Array.isArray(v) ? (v.length ? v.join(", ") : "none") : typeof v === "object" && v ? JSON.stringify(v) : String(v)
        const link = k === "work_item" && typeof v === "string"
        return (
          <span key={k} className="min-w-0">
            <span className="text-muted-foreground">{k.replaceAll("_", " ")}</span>{" "}
            {link ? <Link to={`/sessions/${v}`} className="font-mono hover:underline">{value}</Link>
              : <span className="font-mono break-all">{value.length > 160 ? `${value.slice(0, 159)}…` : value}</span>}
          </span>
        )
      })}
    </span>
  )
}

function Rows({ rows }: { rows: EventRow[] }) {
  return rows.map((e) => {
    const Icon = ICONS[e.type] ?? Dot
    const loud = e.type in ICONS
    const tone = e.type === "resume_all" ? "text-good" : "text-critical"
    return (
      <TableRow key={e.id}>
        <TableCell className="tabular whitespace-nowrap text-muted-foreground">{fmtDateTime(e.ts)}</TableCell>
        <TableCell className="whitespace-nowrap">
          <span className="inline-flex items-center gap-1.5">
            <Icon className={loud ? `size-4 ${tone}` : "size-4 text-muted-foreground"} aria-hidden />
            <span className={loud ? "font-medium" : ""}>{LABELS[e.type] ?? e.type}</span>
          </span>
        </TableCell>
        <TableCell className="whitespace-nowrap">{e.actor ?? <span className="text-muted-foreground">—</span>}</TableCell>
        <TableCell className="font-mono text-xs whitespace-nowrap">{e.task_id ?? ""}</TableCell>
        <TableCell className="max-w-[36rem]"><Data data={e.data} /></TableCell>
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
          <TableCell colSpan={5} className="py-2 text-center">
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
    <Page title="Activity" subtitle="Everything core recorded, newest first: who stopped or resumed the agent, tasks, commands."
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
                  <TableHead>When</TableHead>
                  <TableHead>What</TableHead>
                  <TableHead>Who</TableHead>
                  <TableHead>Task</TableHead>
                  <TableHead>Details</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.length === 0 && (
                  <TableRow><TableCell colSpan={5} className="py-10 text-center text-muted-foreground">Nothing yet.</TableCell></TableRow>
                )}
                <Rows rows={rows} />
                {older.length === 0 && rows.length === 100 && (
                  <TableRow>
                    <TableCell colSpan={5} className="py-2 text-center">
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
      <p className="mt-3 text-xs text-muted-foreground">
        <Badge variant="outline" className="mr-1.5 font-normal">Tip</Badge>
        Stops, resumes and cancels carry the person's name, or their Tailscale login when they aren't in the config.
      </p>
    </Page>
  )
}
