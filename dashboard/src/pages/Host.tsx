import { useState, type ReactNode } from "react"
import { Link } from "react-router"
import { type HostConsumer, type HostConsumers, type HostSessionLink, type HostTotals, useHost,
  useHostConsumers } from "@/api"
import { Meter, type Series, TimeSeries } from "@/components/charts"
import { Page } from "@/components/Page"
import { ErrorNote, QueryState } from "@/components/QueryState"
import { StatTile } from "@/components/StatTile"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { fmtBytes, fmtDuration, fmtRate } from "@/lib/format"
import { byKindRows, clock, clockSeconds, consumerLabel, KIND_LABEL, KINDS, kindSeries, pairRows,
  rank } from "@/lib/host"

const TABS = [
  { key: "cpu", label: "CPU" }, { key: "memory", label: "Memory" }, { key: "network", label: "Network" },
  { key: "disk_io", label: "Disk I/O" }, { key: "disk_space", label: "Disk space" },
  { key: "docker", label: "Docker" }, { key: "ports", label: "Ports" },
] as const
type Tab = (typeof TABS)[number]["key"]

const pct = (v: number) => `${v.toFixed(v < 10 ? 1 : 0)}%`
const KIND_COLOR: Record<string, string> = Object.fromEntries(KINDS.map((k) => [k.key, k.color]))

function SessionLink({ session, children }: { session: HostSessionLink; children: ReactNode }) {
  if (!session) return <>{children}</>
  return <Link to={`/sessions/${encodeURIComponent(session.id)}`} className="hover:underline">{children}</Link>
}

function KindMark({ kind }: { kind: string }) {
  return <span className="inline-block size-2.5 shrink-0 rounded-[3px]"
    style={{ background: KIND_COLOR[kind] ?? "var(--muted-foreground)" }} aria-hidden />
}

function ConsumerCell({ c }: { c: HostConsumer }) {
  return (
    <div className="flex min-w-0 items-center gap-2">
      <KindMark kind={c.kind} />
      <div className="min-w-0">
        <div className="truncate font-medium" title={consumerLabel(c)}>
          <SessionLink session={c.session}>{consumerLabel(c)}</SessionLink>
        </div>
        <div className="text-xs text-muted-foreground">
          {c.kind === "host" ? "core, the agent's Docker daemon and the rest of the machine" : KIND_LABEL[c.kind]}
          {c.session && c.kind !== "session" && c.kind !== "browser" && <> · <SessionLink session={c.session}>
            {c.session.title}</SessionLink></>}
          {c.processes != null && c.processes > 0 && ` · ${c.processes} process${c.processes === 1 ? "" : "es"}`}
        </div>
      </div>
    </div>
  )
}

/** A consumer's share of the largest one, so the table reads as a sorted bar chart. */
function ShareBar({ value, max, kind }: { value: number; max: number; kind: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" aria-hidden>
      <div className="h-full rounded-full" style={{ width: `${max > 0 ? (value / max) * 100 : 0}%`,
        background: KIND_COLOR[kind] ?? "var(--series-other)" }} />
    </div>
  )
}

function ConsumerTable({ rows, metric, format, extra, empty }: {
  rows: HostConsumer[]
  metric: (c: HostConsumer) => number | null
  format: (v: number) => string
  extra?: { head: string; cell: (c: HostConsumer) => ReactNode }
  empty: string
}) {
  const ranked = rank(rows, metric)
  const max = Math.max(0, ...ranked.map((c) => metric(c) ?? 0))
  if (!ranked.length) return <p className="px-4 py-6 text-sm text-muted-foreground">{empty}</p>
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Consumer</TableHead>
          <TableHead className="w-1/3 min-w-32" />
          <TableHead className="text-right">Now</TableHead>
          {extra && <TableHead className="text-right">{extra.head}</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {ranked.map((c) => (
          <TableRow key={c.key}>
            <TableCell className="max-w-96"><ConsumerCell c={c} /></TableCell>
            <TableCell><ShareBar value={metric(c) ?? 0} max={max} kind={c.kind} /></TableCell>
            <TableCell className="tabular text-right font-medium">{format(metric(c) ?? 0)}</TableCell>
            {extra && <TableCell className="tabular text-right text-muted-foreground">{extra.cell(c)}</TableCell>}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function ChartCard({ title, desc, children }: { title: string; desc?: ReactNode; children: ReactNode }) {
  return (
    <Card className="shadow-none">
      <CardHeader>
        <CardTitle className="text-sm font-medium">{title}</CardTitle>
        {desc && <CardDescription className="text-xs">{desc}</CardDescription>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

function TableCard({ title, desc, children }: { title: string; desc?: ReactNode; children: ReactNode }) {
  return (
    <Card className="gap-0 py-0 shadow-none">
      <div className="border-b px-4 py-3">
        <div className="text-sm font-medium">{title}</div>
        {desc && <div className="text-xs text-muted-foreground">{desc}</div>}
      </div>
      {children}
    </Card>
  )
}

const pair = (a: string, aLabel: string, b: string, bLabel: string): Series[] => [
  { key: a, label: aLabel, color: "var(--series-1)" }, { key: b, label: bLabel, color: "var(--series-2)" }]

function Tiles({ h }: { h: HostTotals }) {
  const vol = h.disk_space?.volumes[0]
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-7">
      <StatTile label="CPU" value={h.cpu ? pct(h.cpu.percent) : "—"}
        sub={h.cpu ? `load ${h.cpu.load.map((l) => l.toFixed(1)).join(" · ")} · ${h.cpu.cores} cores` : undefined} />
      <StatTile label="Memory" value={h.memory ? fmtBytes(h.memory.used) : "—"}
        sub={h.memory ? `of ${fmtBytes(h.memory.total)} · swap ${fmtBytes(h.memory.swap_used)}` : undefined} />
      <StatTile label="Network in" value={fmtRate(h.network?.rx_bps)} sub={`out ${fmtRate(h.network?.tx_bps)}`} />
      <StatTile label="Disk read" value={fmtRate(h.disk_io?.read_bps)} sub={`write ${fmtRate(h.disk_io?.write_bps)}`} />
      <StatTile label="Disk used" value={vol ? fmtBytes(vol.used) : "—"}
        sub={vol ? `of ${fmtBytes(vol.total)} · ${fmtBytes(vol.free)} free` : "measuring…"} />
      <StatTile label="Containers" value={h.containers} sub="running in the agent's Docker" />
      <StatTile label="Listening ports" value={h.ports} sub="TCP, in the agent's network" />
    </div>
  )
}

function CategoryView({ tab, h, c }: { tab: Tab; h: HostTotals; c: HostConsumers | undefined }) {
  const consumers = c?.consumers ?? []
  const points = h.history
  const span = points.length > 1 ? fmtDuration(points[points.length - 1].ts - points[0].ts) : null
  const desc = span ? `the last ${span}, every 5 s` : "history starts with the next samples"
  switch (tab) {
    case "cpu":
      return (
        <div className="grid gap-6 xl:grid-cols-[2fr_3fr]">
          <div className="space-y-6">
            <ChartCard title="CPU by consumer" desc={`share of the whole host, ${desc}`}>
              <TimeSeries data={byKindRows(points, "cpu_by_kind")} series={kindSeries} xKey="t" xFormat={clock}
                yFormat={pct} labelFormat={clockSeconds} yMax={100} />
            </ChartCard>
            {h.cpu && (
              <ChartCard title="Per core">
                <div className="grid grid-cols-[2.5rem_1fr_3rem] items-center gap-x-2 gap-y-1.5 text-xs">
                  {h.cpu.per_core.map((v, i) => (
                    <div key={i} className="contents">
                      <span className="text-muted-foreground">cpu{i}</span>
                      <Meter value={v / 100} label={`core ${i} busy`} />
                      <span className="tabular text-right">{pct(v)}</span>
                    </div>
                  ))}
                </div>
              </ChartCard>
            )}
          </div>
          <TableCard title="Who uses the CPU" desc="share of the whole host; what the agent container can't see is outside it">
            <ConsumerTable rows={consumers} metric={(x) => x.cpu_percent} format={pct} empty="Nothing is busy."
              extra={{ head: "Memory", cell: (x) => fmtBytes(x.mem_bytes) }} />
          </TableCard>
        </div>
      )
    case "memory":
      return (
        <div className="grid gap-6 xl:grid-cols-[2fr_3fr]">
          <ChartCard title="Memory by consumer" desc={`PSS, refreshed each minute per process (RSS where the kernel has no PSS); ${desc}`}>
            <TimeSeries data={byKindRows(points, "mem_by_kind")} series={kindSeries} xKey="t" xFormat={clock}
              yFormat={fmtBytes} labelFormat={clockSeconds} />
          </ChartCard>
          <TableCard title="Who uses the memory" desc={h.memory ? `${fmtBytes(h.memory.used)} used of ${fmtBytes(h.memory.total)}` : undefined}>
            <ConsumerTable rows={consumers} metric={(x) => x.mem_bytes} format={fmtBytes} empty="No data yet."
              extra={{ head: "CPU", cell: (x) => pct(x.cpu_percent) }} />
          </TableCard>
        </div>
      )
    case "network":
      return (
        <div className="grid gap-6 xl:grid-cols-[2fr_3fr]">
          <div className="space-y-6">
            <ChartCard title="Network" desc={`the host's own interfaces, not bridges or loopback; ${desc}`}>
              <TimeSeries data={pairRows(points, "rx_bps", "tx_bps")} series={pair("rx_bps", "In", "tx_bps", "Out")}
                xKey="t" xFormat={clock} yFormat={fmtRate} labelFormat={clockSeconds} stacked={false} />
            </ChartCard>
            <TableCard title="By interface">
              <Table>
                <TableHeader><TableRow>
                  <TableHead>Interface</TableHead><TableHead className="text-right">In</TableHead>
                  <TableHead className="text-right">Out</TableHead>
                </TableRow></TableHeader>
                <TableBody>
                  {(c?.interfaces ?? []).filter((i) => i.rx_bps + i.tx_bps > 0).map((i) => (
                    <TableRow key={i.name}>
                      <TableCell className="font-mono text-xs">{i.name}</TableCell>
                      <TableCell className="tabular text-right">{fmtRate(i.rx_bps)}</TableCell>
                      <TableCell className="tabular text-right">{fmtRate(i.tx_bps)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableCard>
          </div>
          <TableCard title="Who uses the network" desc="per container; processes share the agent's network, so they're counted per interface">
            <ConsumerTable rows={consumers} metric={(x) => (x.rx_bps ?? 0) + (x.tx_bps ?? 0)} format={fmtRate}
              empty="No container is sending or receiving."
              extra={{ head: "In · out", cell: (x) => `${fmtRate(x.rx_bps)} · ${fmtRate(x.tx_bps)}` }} />
          </TableCard>
        </div>
      )
    case "disk_io":
      return (
        <div className="grid gap-6 xl:grid-cols-[2fr_3fr]">
          <ChartCard title="Disk I/O" desc={`the host's disks; ${desc}`}>
            <TimeSeries data={pairRows(points, "read_bps", "write_bps")}
              series={pair("read_bps", "Read", "write_bps", "Write")} xKey="t" xFormat={clock} yFormat={fmtRate}
              labelFormat={clockSeconds} stacked={false} />
          </ChartCard>
          <TableCard title="Who reads and writes">
            <ConsumerTable rows={consumers} metric={(x) => (x.read_bps ?? 0) + (x.write_bps ?? 0)} format={fmtRate}
              empty="Nothing is reading or writing."
              extra={{ head: "Read · write", cell: (x) => `${fmtRate(x.read_bps)} · ${fmtRate(x.write_bps)}` }} />
          </TableCard>
        </div>
      )
    case "disk_space": {
      const rows = c?.disk ?? []
      const max = Math.max(0, ...rows.map((r) => r.bytes))
      const at = h.disk_space?.at
      return (
        <div className="grid gap-6 xl:grid-cols-[2fr_3fr]">
          <ChartCard title="Volumes" desc={at ? `measured at ${clock(at)}, every 10 minutes` : "the first measurement is under way"}>
            <div className="space-y-3 text-xs">
              {(h.disk_space?.volumes ?? []).map((v) => (
                <div key={v.path} className="space-y-1">
                  <div className="flex justify-between"><span className="font-mono">{v.path}</span>
                    <span className="tabular">{fmtBytes(v.used)} of {fmtBytes(v.total)}</span></div>
                  <Meter value={v.total ? v.used / v.total : null} label={`${v.path} used`} />
                </div>
              ))}
              {h.disk_space && !h.disk_space.complete &&
                <p className="text-muted-foreground">Some folders took too long to add up; their sizes are partial.</p>}
            </div>
          </ChartCard>
          <TableCard title="What takes the space" desc="each conversation's folder, the caches, the agent's Docker and the other folders">
            {rows.length ? (
              <Table>
                <TableBody>
                  {rows.map((r) => (
                    <TableRow key={`${r.kind}:${r.name}`}>
                      <TableCell className="max-w-96">
                        <div className="truncate font-medium" title={r.name}>
                          <SessionLink session={r.session}>{r.session?.title ?? r.name}</SessionLink>
                        </div>
                        <div className="text-xs text-muted-foreground">{r.kind}{r.session && ` · ${r.name}`}</div>
                      </TableCell>
                      <TableCell className="w-1/3 min-w-32"><ShareBar value={r.bytes} max={max} kind="session" /></TableCell>
                      <TableCell className="tabular text-right font-medium">{fmtBytes(r.bytes)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : <p className="px-4 py-6 text-sm text-muted-foreground">Not measured yet.</p>}
          </TableCard>
        </div>
      )
    }
    case "docker":
      return (
        <TableCard title="Containers" desc="running in the agent's own Docker; linked to the conversation whose folder holds their compose project">
          {c?.containers.length ? (
            <Table>
              <TableHeader><TableRow>
                <TableHead>Container</TableHead><TableHead>Started for</TableHead>
                <TableHead className="text-right">CPU</TableHead><TableHead className="text-right">Memory</TableHead>
                <TableHead className="text-right">Net in · out</TableHead>
                <TableHead className="text-right">Disk read · write</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {[...c.containers].sort((a, b) => b.cpu_percent - a.cpu_percent).map((x) => (
                  <TableRow key={x.name}>
                    <TableCell>
                      <div className="font-medium">{x.name}</div>
                      <div className="text-xs text-muted-foreground">{x.image} · {x.status}</div>
                    </TableCell>
                    <TableCell className="max-w-72 truncate">
                      {x.session ? <SessionLink session={x.session}>{x.session.title}</SessionLink>
                        : <span className="text-muted-foreground">unknown</span>}
                    </TableCell>
                    <TableCell className="tabular text-right">{pct(x.cpu_percent)}</TableCell>
                    <TableCell className="tabular text-right">{fmtBytes(x.mem_bytes)}</TableCell>
                    <TableCell className="tabular text-right">{fmtRate(x.rx_bps)} · {fmtRate(x.tx_bps)}</TableCell>
                    <TableCell className="tabular text-right">{fmtRate(x.read_bps)} · {fmtRate(x.write_bps)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : <p className="px-4 py-6 text-sm text-muted-foreground">No containers running.</p>}
        </TableCard>
      )
    case "ports":
      return (
        <TableCard title="Listening ports" desc="what listens, and the session that started it: a server left running shows up here">
          {c?.ports.length ? (
            <Table>
              <TableHeader><TableRow>
                <TableHead>Port</TableHead><TableHead>Address</TableHead><TableHead>Process</TableHead>
                <TableHead>Consumer</TableHead>
              </TableRow></TableHeader>
              <TableBody>
                {c.ports.map((p) => (
                  <TableRow key={`${p.port}:${p.pid ?? "-"}:${p.address}`}>
                    <TableCell className="tabular font-medium">{p.port}</TableCell>
                    <TableCell className="font-mono text-xs">{p.address}</TableCell>
                    <TableCell className="text-sm">{p.process ?? <span className="text-muted-foreground">not visible</span>}
                      {p.pid != null && <span className="text-xs text-muted-foreground"> · pid {p.pid}</span>}</TableCell>
                    <TableCell className="max-w-80">
                      <div className="flex min-w-0 items-center gap-2">
                        <KindMark kind={p.kind} />
                        <span className="truncate"><SessionLink session={p.session}>{consumerLabel(p)}</SessionLink></span>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          ) : <p className="px-4 py-6 text-sm text-muted-foreground">Nothing is listening.</p>}
        </TableCard>
      )
  }
}

export function Host() {
  const [tab, setTab] = useState<Tab>("cpu")
  const host = useHost()
  const consumers = useHostConsumers()
  return (
    <Page title="Host" subtitle="What the machine the agent runs on is doing, and who is using it.">
      <QueryState query={host} rows={6}>
        {(h) => !h.available ? (
          <ErrorNote error="No samples from the runner yet. It sends one every 5 seconds once it's running." />
        ) : (
          <div className="space-y-6">
            {h.stale && <ErrorNote error={`The runner hasn't sent a sample for ${fmtDuration(h.age_s)}: these numbers are old.`} />}
            <Tiles h={h} />
            <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)}>
              <TabsList className="flex-wrap">
                {TABS.map((t) => <TabsTrigger key={t.key} value={t.key}>{t.label}</TabsTrigger>)}
              </TabsList>
            </Tabs>
            {consumers.error ? <ErrorNote error={consumers.error} /> : null}
            <CategoryView tab={tab} h={h} c={consumers.data} />
          </div>
        )}
      </QueryState>
    </Page>
  )
}
