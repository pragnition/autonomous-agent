import { ChevronRight, Loader2 } from "lucide-react"
import { useState } from "react"
import type { Entry } from "@/api"
import { entryIcon, toolLabel } from "@/components/session"
import { fmtTime, fmtTokens, sumTokens } from "@/lib/format"
import type { TimelineItem } from "@/lib/activity"
import { cn } from "@/lib/utils"

function Time({ ts }: { ts: string | null }) {
  return <time className="tabular shrink-0 text-[11px] text-muted-foreground">{ts ? fmtTime(ts) : ""}</time>
}

function Code({ children, tone }: { children: string; tone?: "add" | "remove" }) {
  return (
    <pre className={cn("max-h-80 overflow-auto rounded-md border bg-muted/50 px-3 py-2 font-mono text-xs whitespace-pre-wrap break-words",
      tone === "add" && "border-good/30 bg-good/5", tone === "remove" && "border-critical/30 bg-critical/5")}>
      {children}
    </pre>
  )
}

function ToolInput({ entry }: { entry: Entry }) {
  const input = entry.input ?? {}
  const str = (k: string) => (typeof input[k] === "string" ? (input[k] as string) : null)
  if (entry.tool === "Bash" && str("command")) return <Code>{`$ ${str("command")}`}</Code>
  if (entry.tool === "Edit" && str("old_string") != null) {
    return (
      <div className="space-y-1.5">
        <div className="font-mono text-xs text-muted-foreground">{str("file_path")}</div>
        <Code tone="remove">{str("old_string")!}</Code>
        <Code tone="add">{str("new_string") ?? ""}</Code>
      </div>
    )
  }
  if (entry.tool === "Write" && str("content") != null) {
    return (
      <div className="space-y-1.5">
        <div className="font-mono text-xs text-muted-foreground">{str("file_path")}</div>
        <Code tone="add">{str("content")!}</Code>
      </div>
    )
  }
  if (Object.keys(input).length === 0) return null
  return <Code>{JSON.stringify(input, null, 2)}</Code>
}

function ToolCall({ item, live }: { item: TimelineItem; live: boolean }) {
  const [open, setOpen] = useState(false)
  const { entry, result } = item
  const Icon = entryIcon(entry)
  const pending = !result && live
  const failed = result?.is_error
  const summary = entry.summary === entry.tool ? "" : entry.summary
  return (
    <li className="group">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
        className="flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent/60">
        <ChevronRight className={cn("size-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
          aria-hidden />
        <Icon className={cn("size-4 shrink-0 text-muted-foreground", failed && "text-critical")} aria-hidden />
        <span className="shrink-0 font-medium">{toolLabel(entry.tool ?? "tool")}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground">{summary}</span>
        {pending && <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" aria-label="running" />}
        {failed && <span className="shrink-0 text-xs font-medium text-critical">error</span>}
        <Time ts={entry.ts} />
      </button>
      {open && (
        <div className="ml-[2.1rem] space-y-2 pb-3 pr-2">
          <ToolInput entry={entry} />
          {result ? (
            <div>
              <div className="mb-1 text-[11px] text-muted-foreground">{failed ? "Error" : "Result"}</div>
              <Code>{result.content || "(empty)"}</Code>
            </div>
          ) : (
            <div className="text-xs text-muted-foreground">{live ? "Waiting for the result…" : "No result recorded."}</div>
          )}
        </div>
      )}
    </li>
  )
}

function Message({ entry }: { entry: Entry }) {
  const user = entry.role === "user"
  return (
    <li className={cn("my-2 rounded-lg px-3 py-2.5", user ? "border bg-muted/40" : "")}>
      <div className="mb-1 flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium text-muted-foreground uppercase tracking-wide">
          {user ? "Message" : "Agent"}
        </span>
        <Time ts={entry.ts} />
      </div>
      <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">{entry.text}</p>
    </li>
  )
}

function Result({ entry }: { entry: Entry }) {
  return (
    <li className={cn("my-3 rounded-lg border px-4 py-3", entry.is_error ? "border-critical/40" : "border-good/40")}>
      <div className="flex flex-wrap items-center justify-between gap-2 text-sm">
        <span className="font-medium">{entry.is_error ? "Finished with an error" : "Finished"}</span>
        <span className="flex flex-wrap gap-x-4 text-xs text-muted-foreground">
          {entry.turns != null && <span>calls <b className="tabular text-foreground">{entry.turns}</b></span>}
          <span>tokens <b className="tabular text-foreground">{fmtTokens(sumTokens(entry.usage))}</b></span>
          <Time ts={entry.ts} />
        </span>
      </div>
      {entry.text && <p className="mt-2 text-sm whitespace-pre-wrap text-muted-foreground">{entry.text}</p>}
    </li>
  )
}

function Note({ entry }: { entry: Entry }) {
  const Icon = entryIcon(entry)
  return (
    <li className="flex items-center gap-2 px-2 py-1 text-xs text-muted-foreground">
      <Icon className="size-3.5 shrink-0" aria-hidden />
      <span className={cn("min-w-0 flex-1 truncate", entry.kind === "raw" && "font-mono")}>
        {entry.kind === "rate_limit" ? `Rate limit ${entry.text}` : entry.text ?? entry.content}
      </span>
      <Time ts={entry.ts} />
    </li>
  )
}

export function ActivityTimeline({ items, live }: { items: TimelineItem[]; live: boolean }) {
  if (items.length === 0) {
    return <p className="px-2 py-6 text-sm text-muted-foreground">{live ? "Waiting for the first step…" : "No activity recorded."}</p>
  }
  return (
    <ol className="space-y-0.5">
      {items.map((item) => {
        const e = item.entry
        if (e.kind === "tool_call") return <ToolCall key={e.id} item={item} live={live} />
        if (e.kind === "text") return <Message key={e.id} entry={e} />
        if (e.kind === "result") return <Result key={e.id} entry={e} />
        return <Note key={e.id} entry={e} />
      })}
    </ol>
  )
}
