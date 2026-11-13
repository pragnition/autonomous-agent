import { ArrowRight } from "lucide-react"
import { Link } from "react-router"
import { useAccounts, useHistory, useKpis, useLlm, useOverview } from "@/api"
import { StackedColumns } from "@/components/charts"
import { Page } from "@/components/Page"
import { ErrorNote, QueryState } from "@/components/QueryState"
import { SessionCard } from "@/components/session"
import { StatTile } from "@/components/StatTile"
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { fmtDuration, fmtPct, fmtTokens, fmtUsd, sumTokens } from "@/lib/format"
import { agentPeopleHourly } from "@/lib/history"
import { useNow } from "@/lib/hooks"

const hourLabel = (t: number) =>
  new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false })

function SectionLink({ to, children }: { to: string; children: string }) {
  return (
    <Link to={to} className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
      {children} <ArrowRight className="size-3" aria-hidden />
    </Link>
  )
}

function WeekCard() {
  const kpis = useKpis(7)
  return (
    <Card className="shadow-none">
      <CardHeader>
        <CardTitle className="text-sm font-medium">Last 7 days</CardTitle>
        <CardAction><SectionLink to="/kpis">KPIs</SectionLink></CardAction>
      </CardHeader>
      <CardContent>
        <QueryState query={kpis}>
          {({ kpis: k }) => (
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-sm">
              {[
                ["Ticket → pickup", fmtDuration(k.pickup.median_s)],
                ["Pickup → PR", fmtDuration(k.to_pr.median_s)],
                ["PR → merge", fmtDuration(k.to_merge.median_s)],
                ["Merge rate", fmtPct(k.merge_rate)],
                ["Tasks", String(k.tasks)],
                ["PRs · merged", `${k.prs} · ${k.merged}`],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-xs text-muted-foreground">{label}</dt>
                  <dd className="font-medium">{value}</dd>
                </div>
              ))}
            </dl>
          )}
        </QueryState>
        <p className="mt-4 text-xs text-muted-foreground">Times are medians.</p>
      </CardContent>
    </Card>
  )
}

export function Overview() {
  const now = useNow(5000)
  const overview = useOverview()
  const accounts = useAccounts()
  const history = useHistory(24)
  const llm = useLlm()
  const meter = llm.data?.installed ? llm.data : null
  const pool = accounts.data?.pool
  const agentHour = accounts.data?.accounts.reduce((n, a) => n + sumTokens(a.agent_60m), 0)
  const peopleHour = accounts.data?.accounts.reduce((n, a) => n + sumTokens(a.people_60m), 0)

  return (
    <Page title="Overview" subtitle="What the agent is doing now and how the week is going.">
      <div className={`grid grid-cols-2 gap-3 md:grid-cols-3 ${meter ? "xl:grid-cols-6" : "xl:grid-cols-5"}`}>
        <StatTile label="Running now" value={overview.data?.counts.running ?? "—"}
          sub={overview.data?.paused ? "paused" : "sessions"} />
        <StatTile label="Queued" value={overview.data?.counts.queued ?? "—"} sub="waiting to start" />
        <StatTile label="Finished today" value={overview.data?.counts.done_today ?? "—"}
          sub={`${overview.data?.counts.failed_today ?? 0} failed`} />
        <StatTile label="Account pool, this week" value={fmtPct(pool?.used_7d)}
          sub={pool ? `on-pace target ${fmtPct(pool.target_7d)} · ${pool.state ?? "unknown"}` : "proxy unreachable"} />
        <StatTile label="Tokens, last hour" value={agentHour == null ? "—" : fmtTokens(agentHour + (peopleHour ?? 0))}
          sub={`agent ${fmtTokens(agentHour)} · people ${fmtTokens(peopleHour)}`} />
        {meter && (
          <Link to="/foundry" className="contents">
            <StatTile label="Foundry spend today" value={fmtUsd(meter.spend.today_usd)}
              sub={`${fmtUsd(meter.spend.month_usd)} this month${meter.capped ? " · capped" : ""}`} />
          </Link>
        )}
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-3">
        <section className="space-y-3 lg:col-span-2" aria-label="Sessions now">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium">Now</h2>
            <SectionLink to="/sessions">All sessions</SectionLink>
          </div>
          <QueryState query={overview}>
            {(o) => o.running.length + o.queued.length === 0 ? (
              <Card className="items-center px-4 py-10 text-sm text-muted-foreground shadow-none">
                {o.paused ? "Paused. Nothing is running." : "Idle. Nothing is running or waiting."}
              </Card>
            ) : (
              <div className="space-y-3">
                {o.running.map((r) => <SessionCard key={r.id} row={r} recent={r.recent} now={now} />)}
                {o.queued.map((r) => <SessionCard key={r.id} row={r} now={now} />)}
              </div>
            )}
          </QueryState>
        </section>
        <WeekCard />
      </div>

      <Card className="mt-6 shadow-none">
        <CardHeader>
          <CardTitle className="text-sm font-medium">Tokens per hour, last 24 hours</CardTitle>
          <CardAction><SectionLink to="/accounts">Accounts</SectionLink></CardAction>
        </CardHeader>
        <CardContent>
          <QueryState query={history}>
            {(h) => h.error ? <ErrorNote error={h.error} /> : (
              <StackedColumns data={agentPeopleHourly(h)} xKey="t" xFormat={hourLabel} yFormat={fmtTokens}
                series={[{ key: "agent", label: "Agent", color: "var(--series-1)" },
                         { key: "people", label: "People", color: "var(--series-2)" }]} />
            )}
          </QueryState>
        </CardContent>
      </Card>
    </Page>
  )
}
