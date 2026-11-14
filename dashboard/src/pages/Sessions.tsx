import { ChevronLeft, ChevronRight, Search, X } from "lucide-react"
import { useEffect, useState } from "react"
import { Link, useSearchParams } from "react-router"
import { useConversations } from "@/api"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { SOURCE_LABELS, SourceBadge, StateBadge } from "@/components/session"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { fmtAgo, fmtDuration, fmtTokens, sumTokens } from "@/lib/format"
import { useNow } from "@/lib/hooks"

const PAGE = 50
const ALL = "all"
const STATES = { queued: "Queued", running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled" }

function Filter({ label, value, options, onChange }: {
  label: string
  value: string
  options: Record<string, string>
  onChange: (v: string) => void
}) {
  return (
    <Select value={value || ALL} onValueChange={(v) => onChange(v === ALL ? "" : v)}>
      <SelectTrigger size="sm" className="w-36" aria-label={label}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>Any {label.toLowerCase()}</SelectItem>
        {Object.entries(options).map(([k, v]) => <SelectItem key={k} value={k}>{v}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

export function Sessions() {
  const now = useNow(5000)
  const [params, setParams] = useSearchParams()
  const get = (k: string) => params.get(k) ?? ""
  const offset = Number(get("offset")) || 0
  const [q, setQ] = useState(get("q"))

  function set(changes: Record<string, string>) {
    const next = new URLSearchParams(params)
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v)
      else next.delete(k)
    }
    if (!("offset" in changes)) next.delete("offset")
    setParams(next, { replace: true })
  }

  useEffect(() => {
    const id = setTimeout(() => { if (q !== get("q")) set({ q }) }, 300)
    return () => clearTimeout(id)
  })  // debounced search; runs after every render, cancels on the next keystroke

  const sessions = useConversations({ state: get("state"), source: get("source"), project: get("project"),
    account: get("account"), q: get("q"), limit: PAGE, offset })
  const filtered = ["state", "source", "project", "account", "q"].some((k) => params.has(k))

  return (
    <Page title="Sessions" subtitle="Every conversation with the agent, latest message first. Each message continues the same session.">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search titles and messages"
            className="h-8 w-64 pl-8" aria-label="Search" />
        </div>
        <Filter label="State" value={get("state")} options={STATES} onChange={(v) => set({ state: v })} />
        <Filter label="Source" value={get("source")} options={SOURCE_LABELS} onChange={(v) => set({ source: v })} />
        <Input value={get("project")} onChange={(e) => set({ project: e.target.value })} placeholder="Project"
          className="h-8 w-32" aria-label="Project" />
        <Input value={get("account")} onChange={(e) => set({ account: e.target.value })} placeholder="Account"
          className="h-8 w-32" aria-label="Account" />
        {filtered && (
          <Button variant="ghost" size="sm" onClick={() => { setQ(""); setParams({}, { replace: true }) }}>
            <X className="size-3.5" aria-hidden /> Clear
          </Button>
        )}
      </div>

      <Card className="py-0 shadow-none">
        <QueryState query={sessions} rows={8}>
          {({ rows, total }) => (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-28">State</TableHead>
                    <TableHead>Session</TableHead>
                    <TableHead>Asked by</TableHead>
                    <TableHead>Last message</TableHead>
                    <TableHead className="text-right">Work time</TableHead>
                    <TableHead>Account</TableHead>
                    <TableHead className="text-right">Tokens</TableHead>
                    <TableHead>Outcome</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.length === 0 && (
                    <TableRow>
                      <TableCell colSpan={8} className="py-10 text-center text-muted-foreground">
                        {filtered ? "No sessions match these filters." : "No sessions yet."}
                      </TableCell>
                    </TableRow>
                  )}
                  {rows.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell><StateBadge state={r.state} /></TableCell>
                      <TableCell className="max-w-[28rem]">
                        <Link to={`/sessions/${r.id}`} className="block truncate font-medium hover:underline">{r.title}</Link>
                        <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
                          <SourceBadge source={r.source} />
                          {r.project && <span className="font-mono">{r.project}</span>}
                          <span className="tabular">{r.messages === 1 ? "1 message" : `${r.messages} messages`}</span>
                        </div>
                      </TableCell>
                      <TableCell className="text-muted-foreground">{r.requester ?? "—"}</TableCell>
                      <TableCell className="text-muted-foreground">{fmtAgo(r.last_active_at, now)}</TableCell>
                      <TableCell className="tabular text-right">{fmtDuration(r.busy_s)}</TableCell>
                      <TableCell>
                        {r.account
                          ? <button type="button" className="hover:underline" onClick={() => set({ account: r.account! })}>{r.account_name ?? r.account}</button>
                          : <span className="text-muted-foreground">—</span>}
                      </TableCell>
                      <TableCell className="tabular text-right">{r.tokens ? fmtTokens(sumTokens(r.tokens)) : "—"}</TableCell>
                      <TableCell className="max-w-40 truncate text-muted-foreground">{r.outcome ?? "—"}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              <div className="flex items-center justify-between border-t px-4 py-2.5 text-xs text-muted-foreground">
                <span className="tabular">
                  {total === 0 ? "0" : `${offset + 1}–${Math.min(offset + PAGE, total)}`} of {total}
                </span>
                <div className="flex gap-1">
                  <Button variant="ghost" size="sm" disabled={offset === 0}
                    onClick={() => set({ offset: String(Math.max(0, offset - PAGE)) })}>
                    <ChevronLeft className="size-4" aria-hidden /> Newer
                  </Button>
                  <Button variant="ghost" size="sm" disabled={offset + PAGE >= total}
                    onClick={() => set({ offset: String(offset + PAGE) })}>
                    Older <ChevronRight className="size-4" aria-hidden />
                  </Button>
                </div>
              </div>
            </>
          )}
        </QueryState>
      </Card>
    </Page>
  )
}
