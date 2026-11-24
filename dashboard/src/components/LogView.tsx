import { ArrowDownToLine, Pause, Play, Trash2, WrapText } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { query } from "@/api"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { stripAnsi } from "@/lib/workloads"

const KEEP = 5000

/** Where a log comes from, as agentd's /api/workloads/logs takes it. */
export type LogSource = {
  source: "container" | "process" | "service"
  kind: "container" | "services" | "app" | "system"
  target: string
  pid?: number
  service?: string
}

type State = "connecting" | "live" | "ended" | "error"

/** A log followed live: the last 300 lines, then each new one as it is written. Pause holds the view (lines keep
 * arriving); the filter only hides lines. */
export function LogView({ src, className }: { src: LogSource; className?: string }) {
  const [lines, setLines] = useState<string[]>([])
  const [state, setState] = useState<State>("connecting")
  const [note, setNote] = useState<string | null>(null)
  const [paused, setPaused] = useState(false)
  const [follow, setFollow] = useState(true)
  const [wrap, setWrap] = useState(true)
  const [filter, setFilter] = useState("")
  const [generation, setGeneration] = useState(0)
  const box = useRef<HTMLDivElement>(null)
  const frozen = useRef<string[] | null>(null)
  const url = `/api/workloads/logs${query({ ...src })}`

  useEffect(() => {
    setLines([])
    setState("connecting")
    setNote(null)
    const es = new EventSource(url)
    let ended = false
    es.addEventListener("open", () => setState("live"))
    es.addEventListener("lines", (e) => {
      const add = (JSON.parse((e as MessageEvent).data).lines as string[]).map(stripAnsi)
      setLines((prev) => {
        const next = prev.concat(add)
        return next.length > KEEP ? next.slice(next.length - KEEP) : next
      })
      setState("live")
    })
    es.addEventListener("end", (e) => {
      ended = true
      const error = JSON.parse((e as MessageEvent).data).error as string | null
      setNote(error)
      setState(error ? "error" : "ended")
      es.close()
    })
    es.addEventListener("error", () => {
      if (ended) return
      // a refused request (no such process, no log file) comes back as an HTTP error: say why once
      fetch(url).then(async (r) => {
        if (!r.ok) {
          const body = await r.json().catch(() => ({}))
          setNote(body.error ?? `HTTP ${r.status}`)
          setState("error")
          es.close()
        }
      }).catch(() => {})
    })
    return () => es.close()
  }, [url, generation])

  useEffect(() => {
    if (paused && !frozen.current) frozen.current = lines
    if (!paused) frozen.current = null
  }, [paused, lines])

  const shown = useMemo(() => {
    const base = paused && frozen.current ? frozen.current : lines
    if (!filter) return base
    const f = filter.toLowerCase()
    return base.filter((l) => l.toLowerCase().includes(f))
  }, [lines, paused, filter])

  useEffect(() => {
    if (follow && !paused && box.current) box.current.scrollTop = box.current.scrollHeight
  }, [shown, follow, paused])

  return (
    <div className={cn("flex min-h-0 flex-col rounded-lg border", className)}>
      <div className="flex flex-wrap items-center gap-2 border-b px-2 py-1.5">
        <span className={cn("inline-flex items-center gap-1.5 text-xs",
          state === "live" ? "text-[var(--status-good)]" : state === "error" ? "text-[var(--status-critical)]" : "text-muted-foreground")}>
          <span className={cn("size-1.5 rounded-full bg-current", state === "live" && !paused && "animate-pulse")} />
          {state === "live" ? (paused ? "paused" : "live") : state}
        </span>
        <span className="text-xs text-muted-foreground tabular">{lines.length} lines</span>
        <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter lines"
          className="h-7 w-40 text-xs" aria-label="Filter lines" />
        <div className="ml-auto flex items-center gap-1">
          <Button size="icon-xs" variant={paused ? "secondary" : "ghost"} onClick={() => setPaused(!paused)}
            title={paused ? "Resume the view" : "Pause the view"} aria-label={paused ? "Resume" : "Pause"}>
            {paused ? <Play /> : <Pause />}
          </Button>
          <Button size="icon-xs" variant={follow ? "secondary" : "ghost"} onClick={() => setFollow(!follow)}
            title="Keep to the newest line" aria-label="Follow"><ArrowDownToLine /></Button>
          <Button size="icon-xs" variant={wrap ? "secondary" : "ghost"} onClick={() => setWrap(!wrap)}
            title="Wrap long lines" aria-label="Wrap"><WrapText /></Button>
          <Button size="icon-xs" variant="ghost" onClick={() => setLines([])} title="Clear the view" aria-label="Clear">
            <Trash2 />
          </Button>
          {(state === "ended" || state === "error") && (
            <Button size="xs" variant="outline" onClick={() => setGeneration((g) => g + 1)}>Reconnect</Button>
          )}
        </div>
      </div>
      {note && <div className="border-b bg-muted/40 px-3 py-1.5 text-xs text-muted-foreground">{note}</div>}
      <div ref={box} className="min-h-0 flex-1 overflow-auto bg-[color-mix(in_oklab,var(--foreground)_3%,var(--background))] p-2 font-mono text-[11.5px] leading-[1.45]"
        onScroll={(e) => {
          const el = e.currentTarget
          const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 24
          if (atEnd !== follow) setFollow(atEnd)
        }}>
        {shown.length === 0 ? (
          <div className="px-1 py-2 text-muted-foreground">{state === "connecting" ? "Connecting…" : "Nothing yet."}</div>
        ) : shown.map((l, i) => (
          <div key={i} className={cn("px-1 hover:bg-accent/50", wrap ? "whitespace-pre-wrap break-all" : "whitespace-pre")}>
            {l || " "}
          </div>
        ))}
      </div>
    </div>
  )
}
