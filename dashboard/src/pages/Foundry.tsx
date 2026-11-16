import { AlertTriangle } from "lucide-react"
import { type FormEvent, useState } from "react"
import { type LlmMeter, type LlmPricing, useLlm, useMe, useSetCaps } from "@/api"
import { Meter, StackedColumns } from "@/components/charts"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { fmtAgo, fmtTokens, fmtUsd } from "@/lib/format"

const dayLabel = (d: string | number) =>
  new Date(`${d}T00:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" })

function SpendTile({ label, spent, cap }: { label: string; spent: number; cap: number | null }) {
  return (
    <Card className="gap-2 px-4 py-3.5 shadow-none">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-2xl font-semibold tracking-tight">{fmtUsd(spent)}</div>
      {cap != null && cap > 0 ? (
        <>
          <Meter value={spent / cap} label={`${label} against its cap`} />
          <div className="text-xs text-muted-foreground">of a {fmtUsd(cap)} cap · {fmtUsd(Math.max(0, cap - spent))} left</div>
        </>
      ) : <div className="text-xs text-muted-foreground">no cap</div>}
    </Card>
  )
}

const perMillion = (x: number | undefined) => (x == null ? "list" : fmtUsd(x))

function Pricing({ upstream, pricing }: { upstream: string; pricing: LlmPricing | undefined }) {
  if (pricing?.source !== "azure") {
    return (
      <li>
        <span className="font-mono">{upstream}</span>: agent.toml's list prices
        {pricing?.error && <> (couldn't read Azure's: {pricing.error})</>}
      </li>
    )
  }
  return (
    <li>
      <span className="font-mono">{upstream}</span>: what Azure billed per token on {pricing.resource} over the
      last {pricing.window_days} days, read {fmtAgo(pricing.as_of)}
      {pricing.addon > 0 && <>, with Defender for AI at {fmtUsd(pricing.addon)} per million tokens</>}
      {pricing.error && <> (the last read failed: {pricing.error})</>}
      {Object.entries(pricing.rates).map(([model, r]) => (
        <div key={model} className="pl-4">
          {model}, per million: {perMillion(r.input)} input · {perMillion(r.cached_input)} cached ·{" "}
          {perMillion(r.cache_write)} cache write · {perMillion(r.output)} output
        </div>
      ))}
    </li>
  )
}

function CapsForm({ meter }: { meter: LlmMeter }) {
  const setCaps = useSetCaps()
  const [daily, setDaily] = useState(String(meter.caps.daily_usd ?? ""))
  const [monthly, setMonthly] = useState(String(meter.caps.monthly_usd ?? ""))

  function submit(e: FormEvent) {
    e.preventDefault()
    setCaps.mutate({ daily_usd: Number(daily), monthly_usd: Number(monthly) })
  }

  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-3">
      <label className="space-y-1 text-xs text-muted-foreground">
        <span>Daily cap (USD)</span>
        <Input type="number" min={0} step="0.01" value={daily} onChange={(e) => setDaily(e.target.value)} className="h-8 w-32" />
      </label>
      <label className="space-y-1 text-xs text-muted-foreground">
        <span>Monthly cap (USD)</span>
        <Input type="number" min={0} step="0.01" value={monthly} onChange={(e) => setMonthly(e.target.value)} className="h-8 w-32" />
      </label>
      <Button type="submit" size="sm" disabled={setCaps.isPending || daily === "" || monthly === ""}>Save caps</Button>
      {setCaps.isSuccess && <span className="text-xs text-muted-foreground">Saved.</span>}
      {setCaps.error && <span role="alert" className="text-xs text-critical">{setCaps.error.message}</span>}
    </form>
  )
}

export function Foundry() {
  const llm = useLlm()
  const { data: me } = useMe()

  return (
    <Page title="Foundry spend"
      subtitle="What the apps the agent runs and tests spend on Azure AI Foundry: Foundry's own token count for each call through core's meter, at what Azure bills per token, against a daily and a monthly cap.">
      <QueryState query={llm} rows={4}>
        {(view) => !view.installed ? (
          <Card className="px-6 py-10 text-sm text-muted-foreground shadow-none">
            The model-call meter isn't installed on this core yet. Once it is, every call an app under test makes to
            Azure AI Foundry through core shows up here with what it cost, against the daily and monthly caps.
          </Card>
        ) : (
          <>
            {view.capped && (
              <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-critical/40 px-4 py-3 text-sm">
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-critical" aria-hidden />
                <span><b>Capped.</b> {view.capped} Calls are refused until the cap resets or is raised.</span>
              </div>
            )}
            <div className="grid gap-3 md:grid-cols-2">
              <SpendTile label="Today" spent={view.spend.today_usd} cap={view.caps.daily_usd} />
              <SpendTile label="This month" spent={view.spend.month_usd} cap={view.caps.monthly_usd} />
            </div>
            {me?.can_resume && (
              <Card className="mt-3 shadow-none">
                <CardContent><CapsForm key={`${view.caps.daily_usd}/${view.caps.monthly_usd}`} meter={view} /></CardContent>
              </Card>
            )}

            <Card className="mt-6 shadow-none">
              <CardHeader>
                <CardTitle className="text-sm font-medium">Spend per day, last 30 days</CardTitle>
              </CardHeader>
              <CardContent>
                {view.daily.length === 0 ? <p className="text-sm text-muted-foreground">No calls yet.</p> : (
                  <StackedColumns data={view.daily as unknown as Record<string, number>[]} xKey="day"
                    xFormat={(v) => dayLabel(v)} labelFormat={dayLabel} yFormat={(v) => fmtUsd(v)}
                    series={[{ key: "cost_usd", label: "Spend", color: "var(--series-1)" }]} />
                )}
              </CardContent>
            </Card>

            <Card className="mt-6 py-0 shadow-none">
              <CardHeader className="pt-6">
                <CardTitle className="text-sm font-medium">This month by model</CardTitle>
                <CardDescription className="text-xs">
                  {view.upstreams.length === 0 ? "No endpoints configured." : (
                    <ul className="space-y-1">
                      {view.upstreams.map((u) => <Pricing key={u} upstream={u} pricing={view.pricing?.[u]} />)}
                    </ul>
                  )}
                </CardDescription>
              </CardHeader>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Endpoint</TableHead>
                    <TableHead>Model</TableHead>
                    <TableHead className="text-right">Calls</TableHead>
                    <TableHead className="text-right">Errors</TableHead>
                    <TableHead className="text-right">Input</TableHead>
                    <TableHead className="text-right">Cached</TableHead>
                    <TableHead className="text-right">Cache writes</TableHead>
                    <TableHead className="text-right">Output</TableHead>
                    <TableHead className="text-right">Spend</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {view.by_model.length === 0 && (
                    <TableRow><TableCell colSpan={9} className="py-8 text-center text-muted-foreground">No calls this month.</TableCell></TableRow>
                  )}
                  {view.by_model.map((m) => (
                    <TableRow key={`${m.upstream}/${m.model}`}>
                      <TableCell className="font-mono text-sm">{m.upstream}</TableCell>
                      <TableCell>{m.model}</TableCell>
                      <TableCell className="tabular text-right">{m.calls}</TableCell>
                      <TableCell className="tabular text-right">{m.errors}</TableCell>
                      <TableCell className="tabular text-right">{fmtTokens(m.input_tokens)}</TableCell>
                      <TableCell className="tabular text-right">{fmtTokens(m.cached_tokens)}</TableCell>
                      <TableCell className="tabular text-right">{fmtTokens(m.cache_write_tokens)}</TableCell>
                      <TableCell className="tabular text-right">{fmtTokens(m.output_tokens)}</TableCell>
                      <TableCell className="tabular text-right font-medium">{fmtUsd(m.cost_usd)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Card>
          </>
        )}
      </QueryState>
    </Page>
  )
}
