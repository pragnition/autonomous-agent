import { useEffect, useRef, useState } from "react"
import type { Entry } from "@/api"

export type TimelineItem = { entry: Entry; result?: Entry }

/** A timeline item per entry, with each tool result folded into the call it answers. */
export function pairEntries(entries: Entry[]): TimelineItem[] {
  const items: TimelineItem[] = []
  const calls = new Map<string, TimelineItem>()
  for (const entry of entries) {
    if (entry.kind === "tool_result" && entry.tool_use_id && calls.has(entry.tool_use_id)) {
      calls.get(entry.tool_use_id)!.result = entry
      continue
    }
    const item: TimelineItem = { entry }
    if (entry.kind === "tool_call" && entry.tool_use_id) calls.set(entry.tool_use_id, item)
    items.push(item)
  }
  return items
}

export type Cursor = { offset: number; line: number }

const RETRY_MS = 1500

/** Entries appended after `cursor`, streamed from core. `active` only decides whether to start: once started, the
 * stream runs until core sends `end` (it does, after the last lines are written), so a finished session's tail is
 * never cut off. A dropped connection reopens from the newest cursor, and an entry already shown is never added
 * twice. */
export function useSessionStream(id: string, cursor: Cursor | undefined, active: boolean) {
  const [entries, setEntries] = useState<Entry[]>([])
  const [ended, setEnded] = useState<string | null>(null)
  const [broken, setBroken] = useState(false)
  const [startedFor, setStartedFor] = useState<string | null>(null)
  const latest = useRef<Cursor | null>(null)
  const seen = useRef(new Set<string>())
  const offset = cursor?.offset
  const line = cursor?.line

  useEffect(() => {
    if (active && offset != null && startedFor !== id) setStartedFor(id)
  }, [active, offset, id, startedFor])

  const go = startedFor === id && offset != null && line != null
  useEffect(() => {
    if (!go) return
    setEntries([])
    setEnded(null)
    setBroken(false)
    seen.current = new Set()
    latest.current = { offset: offset!, line: line! }
    let source: EventSource | null = null
    let retry: ReturnType<typeof setTimeout> | undefined
    let done = false

    const open = () => {
      const { offset: o, line: l } = latest.current!
      source = new EventSource(`/api/sessions/${id}/stream?offset=${o}&line=${l}`)
      source.addEventListener("entries", (e) => {
        const data = JSON.parse((e as MessageEvent).data) as { entries: Entry[]; offset: number; line: number }
        latest.current = { offset: data.offset, line: data.line }
        const fresh = data.entries.filter((x) => !seen.current.has(x.id))
        fresh.forEach((x) => seen.current.add(x.id))
        setBroken(false)
        if (fresh.length) setEntries((prev) => [...prev, ...fresh])
      })
      source.addEventListener("end", (e) => {
        done = true
        setEnded((JSON.parse((e as MessageEvent).data) as { state: string }).state)
        source?.close()
      })
      source.addEventListener("error", () => {
        if (done) return
        source?.close()
        setBroken(true)
        retry = setTimeout(open, RETRY_MS)
      })
    }
    open()
    return () => {
      done = true
      clearTimeout(retry)
      source?.close()
    }
    // the cursor only seeds the first connection; later ones use the newest cursor from the stream
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, go])

  return { entries, ended, broken }
}
