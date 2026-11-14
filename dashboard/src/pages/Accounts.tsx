import { useState } from "react"
import { useNavigate } from "react-router"
import { type Account, type History, type Pace, useAccounts, useHistory } from "@/api"
import { StackedColumns } from "@/components/charts"
import { Page } from "@/components/Page"
import { ErrorNote, QueryState } from "@/components/QueryState"
import { StatTile } from "@/components/StatTile"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { UsageBars } from "@/components/UsageBars"
import { fmtPct, fmtTokens, sumTokens } from "@/lib/format"
import { toSeries } from "@/lib/history"
import { useNow } from "@/lib/hooks"

const RANGES = [{ hours: 6, label: "6 h" }, { hours: 24, label: "24 h" }, { hours: 168, label: "7 d" },
  { hours: 720, label: "30 d" }]

function timeLabel(hours: number) {
  return (t: number) => {
    const d = new Date(t * 1000)
    if (hours <= 24) return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })
    if (hours <= 168) return d.toLocaleDateString(undefined, { weekday: "short", hour: "2-digit", hour12: false })
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" })
  }
}

function tooltipLabel(bucket: number) {
  return (t: string | number) => {
    const start = new Date(Number(t) * 1000)
    const end = new Date((Number(t) + bucket) * 1000)
    const fmt = (d: Date) => d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit",
      minute: "2-digit", hour12: false })
    return `${fmt(start)} – ${end.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })}`
  }
}

function PaceBadge({ pace }: { pace: Pace | null }) {
  if (!pace) return <span className="text-muted-foreground">—</span>
  const text = { ahead: "Ahead", on: "On pace", behind: "Behind" }[pace]
  return <Badge variant={pace === "ahead" ? "default" : "outline"} className="font-normal">{text}</Badge>
}

function SplitBar({ account }: { account: Account }) {
  const agent = sumTokens(account.agent_60m)
  const people = sumTokens(account.people_60m)
  const total = agent + people
  return (
    <div className="space-y-1">
      <div className="tabular text-right text-sm font-medium">{fmtTokens(total)}</div>
      {total > 0 && (
        <div className="flex h-1.5 w-full gap-0.5 overflow-hidden rounded-full" aria-hidden>
          {agent > 0 && <div className="h-full rounded-full bg-[var(--series-1)]" style={{ width: `${(agent / total) * 100}%` }} />}
          {people > 0 && <div className="h-full rounded-full bg-[var(--series-2)]" style={{ width: `${(people / total) * 100}%` }} />}
        </div>
      )}
      <div className="tabular text-right text-[11px] text-muted-foreground">
        agent {fmtTokens(agent)} · people {fmtTokens(people)}
      </div>
    </div>
  )
}

function seconds(v: number | null) {
  return v == null ? "—" : `${v.toFixed(2)}s`
}

function HistoryCharts({ h }: { h: History }) {
  if (h.error) return <ErrorNote error={h.error} />
  const x = timeLabel(h.hours)
  const label = tooltipLabel(h.bucket_s)
  const named = toSeries(h, "account")
  const byAccount = { ...named, series: named.series.map((s) => ({ ...s, label: h.names?.[s.key] ?? s.label })) }
  const byOrigin = toSeries(h, "origin")
  const byKind = toSeries(h, "kind")
  const requests = h.buckets.map((b) => ({ t: b.t, requests: b.requests }))
  const errors = h.buckets.map((b) => ({ t: b.t, errors: b.errors }))
  const per = h.bucket_s >= 3600 ? `${h.bucket_s / 3600} h` : `${h.bucket_s / 60} min`
  return (
    <div className="grid gap-6 xl:grid-cols-2">
      {[
        { title: "Tokens by account", desc: `per ${per}`, s: byAccount },
        { title: "Tokens by origin", desc: "the agent's origins and the people using the pool directly", s: byOrigin },
        { title: "Tokens by kind", desc: "input, output and prompt cache, all accounts", s: byKind },
      ].map(({ title, desc, s }) => (
        <Card key={title} className="shadow-none">
          <CardHeader>
            <CardTitle className="text-sm font-medium">{title}</CardTitle>
            <CardDescription className="text-xs">{desc}</CardDescription>
          </CardHeader>
          <CardContent>
            <StackedColumns data={s.data} series={s.series} xKey="t" xFormat={x} yFormat={fmtTokens} labelFormat={label} />
          </CardContent>
        </Card>
      ))}
      <Card className="shadow-none">
        <CardHeader>
          <CardTitle className="text-sm font-medium">Requests and errors</CardTitle>
          <CardDescription className="text-xs">errors are 429s, usage limits, 5xx and connection failures</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <StackedColumns data={requests} xKey="t" xFormat={x} yFormat={(v) => fmtTokens(v)} labelFormat={label}
            height={110} series={[{ key: "requests", label: "Requests", color: "var(--series-1)" }]} />
          <StackedColumns data={errors} xKey="t" xFormat={x} yFormat={(v) => fmtTokens(v)} labelFormat={label}
            height={90} series={[{ key: "errors", label: "Errors", color: "var(--series-8)" }]} />
        </CardContent>
      </Card>
    </div>
  )
}

export function Accounts() {
  const now = useNow(10_000)
  const navigate = useNavigate()
  const [hours, setHours] = useState(24)
  const accounts = useAccounts()
  const history = useHistory(hours)

  return (
    <Page title="Accounts" subtitle="The pooled Claude accounts: how full each window is, who is using them, and how it has gone.">
      <QueryState query={accounts} rows={6}>
        {(a) => !a.available || !a.pool ? <ErrorNote error={a.error ?? "The account proxy didn't answer."} /> : (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <StatTile label="Pool, this week" value={fmtPct(a.pool.used_7d)}
                sub={`on-pace target ${fmtPct(a.pool.target_7d)} · ${a.pool.state ?? "unknown"}`} />
              <StatTile label="Accounts" value={a.accounts.length}
                sub={`${a.pool.counts.ahead} ahead · ${a.pool.counts.on} on pace · ${a.pool.counts.behind} behind`} />
              <StatTile label="Tokens today" value={fmtTokens(sumTokens(a.pool.burn.today))}
                sub={`${fmtTokens(a.pool.burn.today.out)} output`} />
              <StatTile label="Tokens this week" value={fmtTokens(sumTokens(a.pool.burn.week))}
                sub={`${fmtTokens(a.pool.burn.week.out)} output`} />
            </div>

            <Card className="mt-6 py-0 shadow-none">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Account</TableHead>
                    <TableHead className="min-w-56">Windows</TableHead>
                    <TableHead>Pace</TableHead>
                    <TableHead className="text-right">Sessions</TableHead>
                    <TableHead className="min-w-40 text-right">Tokens, last hour</TableHead>
                    <TableHead className="text-right">429 · limit · 5xx · conn</TableHead>
                    <TableHead className="text-right">TTFT p50 · p90</TableHead>
                    <TableHead className="text-right">tok/s</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {a.accounts.map((acc) => {
                    const e = acc.errors_60m
                    return (
                      <TableRow key={acc.code} className="cursor-pointer"
                        onClick={() => navigate(`/sessions?account=${encodeURIComponent(acc.code)}`)}>
                        <TableCell>
                          <div className="font-medium">{acc.name ?? acc.code}</div>
                          <div className="text-xs text-muted-foreground" title={acc.email ?? undefined}>
                            {acc.name && <span className="font-mono">{acc.code} · </span>}
                            {acc.rank != null ? `rank ${acc.rank}` : acc.verified ? "closed" : "not used yet"}
                            {acc.status && acc.status !== "allowed" && ` · ${acc.status}`}
                          </div>
                        </TableCell>
                        <TableCell><UsageBars account={acc} now={now} /></TableCell>
                        <TableCell><PaceBadge pace={acc.pace} /></TableCell>
                        <TableCell className="tabular text-right">
                          {acc.active}{acc.cap_5h != null && <span className="text-muted-foreground"> / {Math.floor(acc.cap_5h)}</span>}
                        </TableCell>
                        <TableCell><SplitBar account={acc} /></TableCell>
                        <TableCell className="tabular text-right text-muted-foreground">
                          <span className={e["429"] + e.limit + e["5xx"] + e.upstream_error ? "text-foreground" : ""}>
                            {e["429"]} · {e.limit} · {e["5xx"]} · {e.upstream_error}
                          </span>
                        </TableCell>
                        <TableCell className="tabular text-right">
                          {seconds(acc.latency_60m.ttft_p50)} · {seconds(acc.latency_60m.ttft_p90)}
                        </TableCell>
                        <TableCell className="tabular text-right">{acc.latency_60m.tok_s?.toFixed(0) ?? "—"}</TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
              <p className="border-t px-4 py-2.5 text-xs text-muted-foreground">
                Sessions shows how many run on the account now, out of what its 5-hour window has room for. The tick
                on each 7-day bar is where the account should be to use its week by the reset. Click an account to see
                its sessions.
              </p>
            </Card>

            <Card className="mt-6 py-0 shadow-none">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Origin</TableHead>
                    <TableHead>Who</TableHead>
                    <TableHead className="text-right">Sessions now</TableHead>
                    <TableHead className="text-right">Requests, last hour</TableHead>
                    <TableHead className="text-right">Tokens, last hour</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {a.origins.map((o) => (
                    <TableRow key={o.name}>
                      <TableCell className="font-mono text-sm">{o.name}</TableCell>
                      <TableCell>
                        <Badge variant={o.is_agent ? "secondary" : "outline"} className="font-normal">
                          {o.is_agent ? "Agent" : "People"}
                        </Badge>
                      </TableCell>
                      <TableCell className="tabular text-right">{o.active}</TableCell>
                      <TableCell className="tabular text-right">{o.requests_60m}</TableCell>
                      <TableCell className="tabular text-right">{fmtTokens(sumTokens(o.tokens_60m))}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Card>
          </>
        )}
      </QueryState>

      <section className="mt-8" aria-label="History">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-medium">History</h2>
          <Tabs value={String(hours)} onValueChange={(v) => setHours(Number(v))}>
            <TabsList>
              {RANGES.map((r) => <TabsTrigger key={r.hours} value={String(r.hours)}>{r.label}</TabsTrigger>)}
            </TabsList>
          </Tabs>
        </div>
        <QueryState query={history} rows={4}>{(h) => <HistoryCharts h={h} />}</QueryState>
      </section>
    </Page>
  )
}
