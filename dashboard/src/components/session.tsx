import { Ban, Bot, Braces, CheckCircle2, CircleDashed, CircleDot, FilePen, FileText, Gauge, GitFork, Globe,
  Kanban, ListChecks, Loader2, MessageSquare, Plug, Search, Terminal, Wrench, XCircle } from "lucide-react"
import type { ComponentType } from "react"
import { Link } from "react-router"
import type { Entry, SessionRow, SessionState, Source } from "@/api"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { fmtDuration, fmtTokens, secondsBetween, sumTokens } from "@/lib/format"
import { cn } from "@/lib/utils"

type Icon = ComponentType<{ className?: string }>

const STATES: Record<SessionState, { label: string; icon: Icon; className: string }> = {
  running: { label: "Running", icon: Loader2, className: "text-good [&_svg]:animate-spin" },
  queued: { label: "Queued", icon: CircleDashed, className: "text-muted-foreground" },
  done: { label: "Done", icon: CheckCircle2, className: "text-good" },
  failed: { label: "Failed", icon: XCircle, className: "text-critical" },
  cancelled: { label: "Cancelled", icon: Ban, className: "text-muted-foreground" },
}

export function StateBadge({ state }: { state: SessionState }) {
  const s = STATES[state] ?? STATES.queued
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs font-medium", s.className)}>
      <s.icon className="size-3.5" aria-hidden />
      <span className="text-foreground">{s.label}</span>
    </span>
  )
}

const SOURCES: Record<Source, { label: string; icon: Icon }> = {
  slack: { label: "Slack", icon: MessageSquare },
  linear: { label: "Linear", icon: Kanban },
  handover: { label: "Hand-over", icon: GitFork },
  other: { label: "Other", icon: CircleDot },
}

export const SOURCE_LABELS = Object.fromEntries(Object.entries(SOURCES).map(([k, v]) => [k, v.label]))

export function SourceBadge({ source }: { source: Source }) {
  const s = SOURCES[source] ?? SOURCES.other
  return (
    <Badge variant="secondary" className="gap-1 font-normal">
      <s.icon className="size-3" aria-hidden /> {s.label}
    </Badge>
  )
}

/** The project a session is bound to, or nothing when it isn't bound to one. */
export function ProjectBadge({ project }: { project: string | null }) {
  if (!project) return null
  return <Badge variant="outline" className="font-mono font-normal">{project}</Badge>
}

export function toolLabel(name: string): string {
  return name.startsWith("mcp__") ? name.slice(5).replace("__", " · ") : name
}

function toolIcon(name = ""): Icon {
  if (name === "Bash") return Terminal
  if (["Edit", "Write", "MultiEdit", "NotebookEdit"].includes(name)) return FilePen
  if (name === "Read") return FileText
  if (["Grep", "Glob"].includes(name)) return Search
  if (["WebFetch", "WebSearch"].includes(name)) return Globe
  if (["Task", "Agent"].includes(name)) return Bot
  if (name === "TodoWrite") return ListChecks
  if (name.startsWith("mcp__")) return Plug
  return Wrench
}

export function entryIcon(e: Entry): Icon {
  switch (e.kind) {
    case "tool_call": return toolIcon(e.tool)
    case "text": return MessageSquare
    case "result": return CheckCircle2
    case "rate_limit": return Gauge
    case "tool_result": return e.is_error ? XCircle : CheckCircle2
    default: return Braces
  }
}

/** One line of activity: what a tool call did, or the first line of a message. */
export function EntryLine({ entry }: { entry: Entry }) {
  const Icon = entryIcon(entry)
  const text = entry.kind === "tool_call" ? (entry.summary === entry.tool ? "" : entry.summary)
    : (entry.text ?? entry.content ?? "")
  return (
    <div className="flex min-w-0 items-center gap-2 text-xs">
      <Icon className={cn("size-3.5 shrink-0 text-muted-foreground", entry.is_error && "text-critical")} aria-hidden />
      {entry.kind === "tool_call" && <span className="shrink-0 font-medium">{toolLabel(entry.tool ?? "")}</span>}
      <span className={cn("truncate text-muted-foreground", entry.kind === "tool_call" && "font-mono")}>
        {text?.split("\n")[0]}
      </span>
    </div>
  )
}

export function elapsed(row: SessionRow, now: number): number | null {
  if (row.state === "queued") return secondsBetween(row.created_at, now)
  const start = row.started_at ?? row.created_at
  const end = row.ended_at ? Date.parse(row.ended_at) : now
  return (end - Date.parse(start)) / 1000
}

export function SessionCard({ row, recent, now }: { row: SessionRow; recent?: Entry[]; now: number }) {
  const secs = elapsed(row, now)
  return (
    <Card className="gap-3 px-4 py-3.5 shadow-none transition-colors hover:border-foreground/20">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Link to={`/sessions/${row.id}`} className="line-clamp-2 font-medium leading-snug hover:underline">
            {row.title}
          </Link>
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
            <StateBadge state={row.state} />
            <SourceBadge source={row.source} />
            <ProjectBadge project={row.project} />
            {row.requester && <span>for {row.requester}</span>}
          </div>
        </div>
        <div className="shrink-0 text-right text-xs">
          <div className="tabular font-medium">{fmtDuration(secs)}</div>
          <div className="text-muted-foreground">{row.state === "queued" ? "waiting" : "elapsed"}</div>
        </div>
      </div>
      {row.state !== "queued" && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>account <span className="font-medium text-foreground">{row.account_name ?? row.account ?? "—"}</span></span>
          <span>tokens <span className="tabular font-medium text-foreground">{fmtTokens(sumTokens(row.tokens))}</span></span>
          {row.turns != null && <span>calls <span className="tabular font-medium text-foreground">{row.turns}</span></span>}
        </div>
      )}
      {recent && recent.length > 0 && (
        <div className="space-y-1 border-t pt-2.5">
          {recent.map((e) => <EntryLine key={e.id} entry={e} />)}
        </div>
      )}
    </Card>
  )
}
