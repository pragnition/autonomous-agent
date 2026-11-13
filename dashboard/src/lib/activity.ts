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

/** Entries appended after `offset`, streamed from core while the session runs; stops when core sends `end`. */
export function useSessionStream(id: string, offset: number | undefined, live: boolean) {
  const [entries, setEntries] = useState<Entry[]>([])
  const [ended, setEnded] = useState<string | null>(null)
  const started = useRef<string | null>(null)

  useEffect(() => {
    if (offset == null || !live) return
    const key = `${id}@${offset}`
    if (started.current === key) return
    started.current = key
    setEntries([])
    setEnded(null)
    const source = new EventSource(`/api/sessions/${id}/stream?offset=${offset}`)
    source.addEventListener("entries", (e) => {
      const data = JSON.parse((e as MessageEvent).data) as { entries: Entry[] }
      setEntries((prev) => [...prev, ...data.entries])
    })
    source.addEventListener("end", (e) => {
      setEnded((JSON.parse((e as MessageEvent).data) as { state: string }).state)
      source.close()
    })
    return () => {
      source.close()
      started.current = null
    }
  }, [id, offset, live])

  return { entries, ended }
}
