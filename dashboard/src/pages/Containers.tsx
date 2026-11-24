import { Box, Boxes, ExternalLink, Layers, MoreHorizontal, Play, Power, RotateCcw, Search, Server, SquareTerminal,
  Trash2, Wrench } from "lucide-react"
import { type ReactNode, Suspense, lazy, useMemo, useState } from "react"
import { Link, useSearchParams } from "react-router"
import { type AppContainer, type ContainerDetail, type ConversationBox, type Proc, type ServiceBox, type StackBox,
  type SystemBox, type Workloads, api, useAppDetail, useContainerDetail, useMe, useServiceDetail, useWorkloadAction,
  useWorkloads } from "@/api"
import { LogView, type LogSource } from "@/components/LogView"
import { Page } from "@/components/Page"
import { ErrorNote, QueryState } from "@/components/QueryState"
import { StatTile } from "@/components/StatTile"
import { ActionNote, Confirm, ExpiryDialog, ExpiryLine } from "@/components/WorkloadControls"
import { Meter } from "@/components/charts"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator,
  DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { fmtAgo, fmtBytes, fmtDateTime, fmtDuration, fmtRate } from "@/lib/format"
import { cn } from "@/lib/utils"
import { KIND_LABEL, type Selection, boxState, parseSelection, processTree, selectionKey, subtreeTotals,
  untilText } from "@/lib/workloads"

// the terminal (xterm) loads only when a shell is opened
const Terminal = lazy(() => import("@/components/Terminal").then((m) => ({ default: m.Terminal })))

const pct = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(v < 10 ? 1 : 0)}%`)
const short = (s: string, n = 90) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

// --- the list on the left -------------------------------------------------------------------------------------------

function Dot({ tone }: { tone: "live" | "on" | "off" | "gone" }) {
  const color = { live: "var(--series-1)", on: "var(--status-good)", off: "var(--muted-foreground)", gone: "var(--status-critical)" }[tone]
  return <span className={cn("inline-block size-2 shrink-0 rounded-full", tone === "live" && "animate-pulse")} style={{ background: color }} aria-hidden />
}

function Row({ selected, onClick, children }: { selected: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" onClick={onClick}
      className={cn("block w-full border-b px-3 py-2.5 text-left transition-colors last:border-b-0 hover:bg-accent/60",
        selected && "bg-accent")}>
      {children}
    </button>
  )
}

function Section({ icon, title, count, note, children }: { icon: ReactNode; title: string; count: ReactNode; note?: string; children: ReactNode }) {
  return (
    <div>
      <div className="sticky top-0 z-10 flex items-center gap-2 border-b bg-muted/70 px-3 py-1.5 text-xs font-medium backdrop-blur">
        {icon}<span>{title}</span><span className="text-muted-foreground">{count}</span>
        {note && <span className="ml-auto truncate font-normal text-muted-foreground">{note}</span>}
      </div>
      {children}
    </div>
  )
}

function Numbers({ cpu, mem, limit }: { cpu: number | null | undefined; mem: number | null | undefined; limit?: number | null }) {
  return (
    <div className="w-24 shrink-0 text-right text-xs tabular">
      <div>{pct(cpu)} <span className="text-muted-foreground">CPU</span></div>
      <div>{fmtBytes(mem)}</div>
      {limit ? <div className="mt-1"><Meter value={(mem ?? 0) / limit} label="memory of its cap" /></div> : null}
    </div>
  )
}

function ConversationItem({ c }: { c: ConversationBox }) {
  const st = boxState(c)
  return (
    <div className="flex items-start gap-3">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <Dot tone={st.tone} />
          <span className="truncate text-sm font-medium">{c.session?.title || c.conversation}</span>
        </div>
        <div className="mt-0.5 truncate pl-4 text-xs text-muted-foreground">
          {c.conversation}{c.session?.requester && ` · ${c.session.requester}`} · {st.label}
          {c.last_turn_at && ` · last message ${fmtAgo(c.last_turn_at)}`}
        </div>
        <div className="mt-0.5 truncate pl-4 text-xs text-muted-foreground">
          {c.state === "running" ? (c.expires.at ? `stops ${untilText(c.expires.at)}` : c.expires.why) : `deleted ${untilText(c.expires.at)}`}
          {(c.keep || c.servers_kept.length > 0) && <Badge variant="outline" className="ml-1.5 h-4 px-1 text-[10px]">kept</Badge>}
        </div>
      </div>
      {c.state === "running" && <Numbers cpu={c.cpu_percent} mem={c.mem_bytes} limit={c.mem_limit} />}
    </div>
  )
}

function List({ w, sel, select }: { w: Workloads; sel: Selection | null; select: (s: Selection) => void }) {
  const [q, setQ] = useState("")
  const [stopped, setStopped] = useState(false)
  const match = (...parts: (string | null | undefined)[]) => !q || parts.some((p) => p?.toLowerCase().includes(q.toLowerCase()))
  const is = (s: Selection) => sel != null && selectionKey(sel) === selectionKey(s)
  const running = w.conversations.filter((c) => c.state === "running")
  const convos = w.conversations.filter((c) => (stopped || c.state === "running") &&
    match(c.conversation, c.session?.title, c.session?.requester, c.listeners.join(" ")))
  const stacks = Array.isArray(w.stacks) ? w.stacks : []
  return (
    <Card className="flex max-h-[calc(100svh-15rem)] min-h-96 flex-col gap-0 overflow-hidden py-0 shadow-none">
      <div className="flex items-center gap-2 border-b p-2">
        <div className="relative flex-1">
          <Search className="absolute top-2 left-2 size-4 text-muted-foreground" aria-hidden />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search threads, people, ports, names" className="h-8 pl-8" />
        </div>
        <Button size="sm" variant={stopped ? "secondary" : "ghost"} onClick={() => setStopped(!stopped)}>
          {stopped ? "Hide stopped" : `Stopped (${w.conversations.length - running.length})`}
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <Section icon={<Box className="size-3.5" />} title="Conversations" count={`${running.length} running`}
          note={`${fmtBytes(w.rules.memory_bytes)} cap each`}>
          {convos.length === 0 && <p className="px-3 py-3 text-xs text-muted-foreground">Nothing here.</p>}
          {convos.map((c) => (
            <Row key={c.conversation} selected={is({ kind: "container", name: c.conversation })}
              onClick={() => select({ kind: "container", name: c.conversation })}>
              <ConversationItem c={c} />
            </Row>
          ))}
        </Section>
        <Section icon={<Server className="size-3.5" />} title="Services" count={`${w.services.items.filter((s) => s.running).length} of ${w.services.items.length}`}
          note="services.toml">
          {w.services.error && <p className="px-3 py-2 text-xs text-[var(--status-critical)]">{w.services.error}</p>}
          {w.services.items.filter((s) => match(s.name, s.note, s.cmd, s.ports.join(" "))).map((s) => (
            <Row key={s.name} selected={is({ kind: "service", name: s.name })} onClick={() => select({ kind: "service", name: s.name })}>
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2"><Dot tone={s.running ? "on" : "gone"} />
                    <span className="truncate text-sm font-medium">{s.name}</span></div>
                  <div className="mt-0.5 truncate pl-4 text-xs text-muted-foreground">
                    {s.ports.length ? `:${s.ports.join(", :")} · ` : ""}{s.note || short(s.cmd, 60)}
                  </div>
                  {s.expires.at && <div className="pl-4 text-xs text-muted-foreground">removed {untilText(s.expires.at)}</div>}
                </div>
                <Numbers cpu={s.cpu_percent} mem={s.mem_bytes} />
              </div>
            </Row>
          ))}
        </Section>
        <Section icon={<Boxes className="size-3.5" />} title="Stacks in the agent's Docker"
          count={`${stacks.reduce((a, s) => a + s.running, 0)} containers up`} note="no memory cap">
          {!Array.isArray(w.stacks) && <p className="px-3 py-2 text-xs text-[var(--status-critical)]">{w.stacks.error}</p>}
          {stacks.filter((s) => match(s.name, s.session?.title, s.conversation, s.ports.join(" "))).map((s) => (
            <Row key={s.name} selected={is({ kind: "stack", name: s.name })} onClick={() => select({ kind: "stack", name: s.name })}>
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2"><Dot tone={s.running ? "on" : "off"} />
                    <span className="truncate text-sm font-medium">{s.name}</span></div>
                  <div className="mt-0.5 truncate pl-4 text-xs text-muted-foreground">
                    {s.running}/{s.total} up{s.ports.length ? ` · :${s.ports.join(", :")}` : ""}
                    {s.session ? ` · ${s.session.title}` : ""}
                  </div>
                  {s.expires.at && <div className="pl-4 text-xs text-muted-foreground">stops {untilText(s.expires.at)}</div>}
                </div>
                {s.running > 0 && <Numbers cpu={s.cpu_percent} mem={s.mem_bytes} />}
              </div>
            </Row>
          ))}
        </Section>
        <Section icon={<Layers className="size-3.5" />} title="The stack itself" count={w.system.length}>
          {w.system.filter((s) => match(s.name, s.service)).map((s) => (
            <Row key={s.name} selected={is({ kind: "system", name: s.name })} onClick={() => select({ kind: "system", name: s.name })}>
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2"><Dot tone={s.state === "running" ? "on" : "gone"} />
                    <span className="truncate text-sm font-medium">{s.name}</span></div>
                  <div className="mt-0.5 truncate pl-4 text-xs text-muted-foreground">{s.note || s.status}</div>
                </div>
                <Numbers cpu={s.cpu_percent} mem={s.mem_bytes} />
              </div>
            </Row>
          ))}
        </Section>
      </div>
    </Card>
  )
}

// --- the tiles on top ------------------------------------------------------------------------------------------------

function Tiles({ w }: { w: Workloads }) {
  const m = w.host.memory
  const running = w.conversations.filter((c) => c.state === "running")
  const turns = running.filter((c) => c.turn_running).length
  const capped = running.length * w.rules.memory_bytes
  const stacks = Array.isArray(w.stacks) ? w.stacks : []
  const apps = w.system.find((s) => s.note)
  const vol = w.host.disk?.volumes?.[0]
  const inConvos = running.reduce((a, c) => a + (c.mem_bytes ?? 0), 0)
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
      <StatTile label="Memory in use" value={m ? fmtBytes(m.used) : "—"}
        sub={m ? <>{fmtBytes(m.available)} free of {fmtBytes(m.total)}<div className="mt-1.5"><Meter value={m.used / m.total} label="memory used" /></div></> : undefined} />
      <StatTile label="CPU" value={w.host.cpu ? pct(w.host.cpu.percent) : "—"}
        sub={w.host.cpu ? `${w.host.cpu.cores} cores · load ${w.host.cpu.load[0]?.toFixed(1)}` : undefined} />
      <StatTile label="Conversations" value={`${running.length} up`}
        sub={`${turns} with a turn running · ${fmtBytes(inConvos)} used · caps add to ${fmtBytes(capped)}`} />
      <StatTile label="Services" value={`${w.services.items.filter((s) => s.running).length} running`}
        sub={`${fmtBytes(w.services.items.reduce((a, s) => a + s.mem_bytes, 0))} in use`} />
      <StatTile label="Agent's Docker" value={`${stacks.reduce((a, s) => a + s.running, 0)} containers`}
        sub={`${stacks.filter((s) => s.running).length} stacks · ${fmtBytes(apps?.mem_bytes ?? stacks.reduce((a, s) => a + s.mem_bytes, 0))} · no cap`} />
      <StatTile label="Disk free" value={vol ? fmtBytes(vol.free) : "—"} sub={vol ? `of ${fmtBytes(vol.total)}` : "measuring…"} />
    </div>
  )
}

// --- shared pieces of the detail panel --------------------------------------------------------------------------------

function Fact({ label, value, sub, warn }: { label: string; value: ReactNode; sub?: ReactNode; warn?: boolean }) {
  return (
    <div className={cn("rounded-lg border px-3 py-2.5", warn && "border-[var(--status-critical)]/50 bg-[var(--status-critical)]/5")}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tracking-tight tabular">{value}</div>
      {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
    </div>
  )
}

function Header({ title, sub, badges, children }: { title: ReactNode; sub?: ReactNode; badges?: ReactNode; children?: ReactNode }) {
  return (
    <div className="space-y-2 border-b px-4 py-3">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2"><h2 className="truncate text-base font-semibold">{title}</h2>{badges}</div>
          {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
        </div>
        <div className="flex flex-wrap items-center gap-1.5">{children}</div>
      </div>
    </div>
  )
}

function ShellOrNote({ admin, kind, target }: { admin: boolean; kind: "container" | "services" | "app"; target: string }) {
  if (!admin) return <p className="text-sm text-muted-foreground">Only an admin can open a shell: it runs with all of the agent's credentials.</p>
  return (
    <Suspense fallback={<p className="text-sm text-muted-foreground">Loading the terminal…</p>}>
      <Terminal kind={kind} target={target} className="h-[calc(100svh-24rem)] min-h-80" />
    </Suspense>
  )
}

// --- processes ---------------------------------------------------------------------------------------------------------

function ProcessTable({ procs, where, turnPid, onLogs, onKeep }: {
  procs: Proc[]
  where: { kind: "container" | "services"; target: string }
  turnPid?: boolean
  onLogs: (p: Proc) => void
  /** Keep a server (a conversation's container only). */
  onKeep?: (port: number) => void
}) {
  const act = useWorkloadAction()
  const tree = processTree(procs)
  const kill = (p: Proc, signal: "TERM" | "KILL") =>
    act.mutate({ kind: "process", target: where.target, action: "kill", pid: p.pid, signal })
  return (
    <div className="space-y-2">
      <ActionNote action={act} />
      <div className="overflow-x-auto rounded-lg border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-16">PID</TableHead>
              <TableHead>What</TableHead>
              <TableHead className="text-right">CPU</TableHead>
              <TableHead className="text-right">Memory</TableHead>
              <TableHead className="text-right">Running</TableHead>
              <TableHead className="w-10" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {tree.map(({ p, depth }) => {
              const totals = subtreeTotals(procs, p.pid)
              const locked = p.kind === "init" || p.kind === "holder" || (p.kind === "claude" && turnPid)
              return (
                <TableRow key={p.pid} className={cn(p.kind === "server" && "bg-[var(--series-3)]/5")}>
                  <TableCell className="tabular text-xs text-muted-foreground">{p.pid}</TableCell>
                  <TableCell className="max-w-[36rem]">
                    <div className="flex items-start gap-1.5" style={{ paddingLeft: `${Math.min(depth, 8) * 14}px` }}>
                      {depth > 0 && <span className="text-muted-foreground" aria-hidden>└</span>}
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Badge variant={p.kind === "server" ? "default" : "outline"} className="h-4 px-1.5 text-[10px]">{KIND_LABEL[p.kind]}</Badge>
                          {p.in_turn && <Badge variant="secondary" className="h-4 px-1.5 text-[10px]">this turn</Badge>}
                          {p.ports.map((port) => (
                            <Badge key={port} variant="outline" className={cn("h-4 px-1.5 font-mono text-[10px]", p.kept && "border-[var(--series-3)]")}>
                              :{port}{p.kept ? " kept" : ""}
                            </Badge>
                          ))}
                          {p.log && <button type="button" className="text-[11px] text-primary hover:underline" onClick={() => onLogs(p)}>logs</button>}
                        </div>
                        <div className="mt-0.5 truncate font-mono text-[11.5px]" title={`${p.cmd}${p.cwd ? `\nin ${p.cwd}` : ""}`}>{p.cmd}</div>
                        {p.cwd && <div className="truncate text-[11px] text-muted-foreground" title={p.cwd}>in {p.cwd}</div>}
                      </div>
                    </div>
                  </TableCell>
                  <TableCell className="tabular text-right text-xs">
                    {pct(p.cpu_percent)}{totals.count > 1 && <div className="text-muted-foreground" title="with what it started">{pct(totals.cpu)} all</div>}
                  </TableCell>
                  <TableCell className="tabular text-right text-xs">
                    {fmtBytes(p.pss)}{totals.count > 1 && <div className="text-muted-foreground" title="with what it started">{fmtBytes(totals.pss)} all</div>}
                    {p.swap > 0 && <div className="text-[var(--status-warning)]">{fmtBytes(p.swap)} swap</div>}
                  </TableCell>
                  <TableCell className="tabular text-right text-xs text-muted-foreground">
                    {p.started ? fmtDuration(Date.now() / 1000 - p.started) : "—"}
                  </TableCell>
                  <TableCell>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button size="icon-xs" variant="ghost" aria-label={`Actions for ${p.pid}`}><MoreHorizontal /></Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="w-60">
                        <DropdownMenuLabel className="truncate text-xs">{p.pid} · {short(p.cmd, 40)}</DropdownMenuLabel>
                        {p.log && <DropdownMenuItem onSelect={() => onLogs(p)}>Follow its log</DropdownMenuItem>}
                        {onKeep && p.ports.map((port) => (
                          <DropdownMenuItem key={`keep-${port}`} onSelect={() => onKeep(port)}>Keep the server on :{port}…</DropdownMenuItem>
                        ))}
                        {onKeep && p.ports.map((port) => (
                          <DropdownMenuItem key={`stop-${port}`}
                            onSelect={() => act.mutate({ kind: "server", target: where.target, action: "stop", port })}>
                            Stop the server on :{port}
                          </DropdownMenuItem>
                        ))}
                        <DropdownMenuSeparator />
                        <DropdownMenuItem disabled={locked} onSelect={() => kill(p, "TERM")}>
                          Stop it{totals.count > 1 ? ` and ${totals.count - 1} under it` : ""}
                        </DropdownMenuItem>
                        <DropdownMenuItem disabled={locked} variant="destructive" onSelect={() => kill(p, "KILL")}>Kill it now (KILL)</DropdownMenuItem>
                        {locked && <DropdownMenuLabel className="text-[11px] font-normal text-muted-foreground">
                          {p.kind === "claude" ? "The running turn: cancel the turn instead." : "It runs the container."}</DropdownMenuLabel>}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

/** The biggest users of the container's memory, as bars. */
function MemoryBars({ procs, total }: { procs: Proc[]; total: number }) {
  const top = [...procs].sort((a, b) => b.pss - a.pss).slice(0, 8)
  const max = Math.max(1, ...top.map((p) => p.pss))
  return (
    <div className="space-y-1.5">
      {top.map((p) => (
        <div key={p.pid} className="grid grid-cols-[minmax(0,1fr)_5rem] items-center gap-2 text-xs">
          <div className="min-w-0">
            <div className="truncate font-mono text-[11px]" title={p.cmd}><span className="text-muted-foreground">{KIND_LABEL[p.kind]}</span> {short(p.cmd, 70)}</div>
            <div className="mt-0.5 h-1.5 overflow-hidden rounded-full bg-muted">
              <div className="h-full rounded-full" style={{ width: `${(p.pss / max) * 100}%`,
                background: p.kind === "claude" ? "var(--series-1)" : p.kind === "server" ? "var(--series-3)" : "var(--series-other)" }} />
            </div>
          </div>
          <div className="tabular text-right">{fmtBytes(p.pss)}<span className="text-muted-foreground"> {total ? `${Math.round((p.pss / total) * 100)}%` : ""}</span></div>
        </div>
      ))}
    </div>
  )
}

// --- a conversation's container ---------------------------------------------------------------------------------------

function ContainerPanel({ id, admin }: { id: string; admin: boolean }) {
  const q = useContainerDetail(id)
  const act = useWorkloadAction()
  const [tab, setTab] = useState("overview")
  const [log, setLog] = useState<LogSource>({ source: "container", kind: "container", target: id })
  const [keepPort, setKeepPort] = useState<number | null>(null)
  return (
    <QueryState query={q} rows={6}>
      {(d: ContainerDetail) => {
        const c = d.summary
        const cg = d.cgroup
        const st = boxState(c)
        const running = c.state === "running"
        const memNow = cg?.memory_current ?? c.mem_bytes ?? 0
        const memMax = cg?.memory_max ?? d.container.memory_limit ?? c.mem_limit
        const logs = d.processes.filter((p) => p.log || p.err)
        const followProcess = (p: Proc) => { setLog({ source: "process", kind: "container", target: id, pid: p.pid }); setTab("logs") }
        return (
          <div>
            <Header
              title={c.session?.title || c.conversation}
              badges={<Badge variant={st.tone === "live" ? "default" : "outline"}>{st.label}</Badge>}
              sub={<span className="flex flex-wrap gap-x-2">
                <span className="font-mono">{c.name}</span>
                {c.session?.requester && <span>asked by {c.session.requester}</span>}
                {c.session && <Link className="text-primary hover:underline" to={`/sessions/${encodeURIComponent(c.session.id)}`}>open the conversation</Link>}
                {c.session?.links?.slack && <a className="inline-flex items-center gap-0.5 text-primary hover:underline" href={c.session.links.slack}
                  target="_blank" rel="noreferrer">Slack <ExternalLink className="size-3" /></a>}
              </span>}>
              <ExpiryDialog what="this container" base={{ kind: "container", target: id }} keep={c.keep}
                usual="it stops an hour after the last message when nothing runs in it, and 24 hours after it whatever runs">
                <Button size="sm" variant="outline"><Wrench /> Set expiry</Button>
              </ExpiryDialog>
              {c.turn_running && c.work_item && (
                <Confirm title="Cancel the running turn?" body="Claude Code is interrupted; the conversation keeps its session." confirm="Cancel the turn"
                  onConfirm={() => api.post(`/api/sessions/${encodeURIComponent(c.work_item ?? "")}/cancel`).then(() => q.refetch())}>
                  <Button size="sm" variant="outline">Cancel turn</Button>
                </Confirm>
              )}
              {running ? (
                <Confirm title="Stop this container?" confirm="Stop"
                  body={<>Everything running in it stops{c.listeners.length ? <>, including the servers on :{c.listeners.join(", :")}</> : null}.
                    Its files stay; the conversation's next message starts it again.</>}
                  onConfirm={() => act.mutate({ kind: "container", target: id, action: "stop" })}>
                  <Button size="sm" variant="outline" disabled={c.turn_running}><Power /> Stop</Button>
                </Confirm>
              ) : c.state !== "missing" && (
                <Button size="sm" variant="outline" onClick={() => act.mutate({ kind: "container", target: id, action: "start" })}><Play /> Start</Button>
              )}
              <Confirm title="Delete this container?" confirm="Delete"
                body="Its system files, installed packages and /tmp go. Its folder on the shared volume and the conversation's saved session stay; the next message gets a new container from the newest image."
                onConfirm={() => act.mutate({ kind: "container", target: id, action: "remove" })}>
                <Button size="icon-sm" variant="ghost" disabled={c.turn_running} aria-label="Delete the container"><Trash2 /></Button>
              </Confirm>
            </Header>
            <div className="space-y-1 border-b px-4 py-2">
              <ExpiryLine expires={c.expires} keep={c.keep} />
              {c.servers_kept.map((s) => (
                <div key={s.port} className="text-xs text-muted-foreground">
                  Server :{s.port} kept {s.keep.until ? `until ${fmtDateTime(s.keep.until)}` : "until changed"} by {s.keep.set_by ?? "someone"}
                </div>
              ))}
              <ActionNote action={act} />
            </div>
            {keepPort != null && (
              <ExpiryDialog open onOpenChange={(o) => !o && setKeepPort(null)} what={`the server on :${keepPort}`}
                base={{ kind: "server", target: id, port: keepPort }}
                keep={c.servers_kept.find((s) => s.port === keepPort)?.keep ?? null}
                usual="it stops with its container, 24 hours after the conversation's last message; kept, it holds the container up and everything else in it is stopped once the container is past its usual stop" />
            )}
            <Tabs value={tab} onValueChange={setTab} className="px-4 py-3">
              <TabsList>
                <TabsTrigger value="overview">Overview</TabsTrigger>
                <TabsTrigger value="processes">Processes{d.processes.length ? ` (${d.processes.length})` : ""}</TabsTrigger>
                <TabsTrigger value="logs">Logs</TabsTrigger>
                <TabsTrigger value="shell" disabled={!running}><SquareTerminal className="size-3.5" /> Shell</TabsTrigger>
              </TabsList>
              <TabsContent value="overview" className="space-y-4 pt-2">
                <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                  <Fact label="CPU" value={pct(c.cpu_percent)} sub={`of the host · cap ${cg?.cpus ?? (d.container.nano_cpus ?? 0) / 1e9} CPUs`} />
                  <Fact label="Memory" value={fmtBytes(memNow)}
                    sub={<>{memMax ? `of ${fmtBytes(memMax)} cap` : "no cap"}{cg?.memory_peak ? ` · peak ${fmtBytes(cg.memory_peak)}` : ""}
                      {memMax ? <div className="mt-1"><Meter value={memNow / memMax} label="memory of its cap" /></div> : null}</>} />
                  <Fact label="Killed for memory" value={cg?.oom_kills ?? (d.container.oom_killed ? "yes" : "0")}
                    sub="processes the kernel killed in it" warn={(cg?.oom_kills ?? 0) > 0} />
                  <Fact label="Processes" value={cg?.pids_current ?? c.processes ?? "—"} sub={`at most ${cg?.pids_max ?? d.container.pids_limit ?? "—"}`} />
                  <Fact label="Network" value={fmtRate(c.rx_bps)} sub={`in · ${fmtRate(c.tx_bps)} out`} />
                  <Fact label="Disk I/O" value={fmtRate(c.read_bps)} sub={`read · ${fmtRate(c.write_bps)} write`} />
                  <Fact label="Its own files" value={fmtBytes(d.container.size_rw)} sub="packages, /tmp, caches outside the shared volume" />
                  <Fact label="Conversation folder" value={d.folder.bytes == null ? "measuring…" : fmtBytes(d.folder.bytes)}
                    sub={<span className="font-mono" title={d.folder.path}>{short(d.folder.path, 40)}</span>} />
                </div>
                {cg && (
                  <div className="grid gap-4 lg:grid-cols-[3fr_2fr]">
                    <div className="rounded-lg border p-3">
                      <div className="mb-2 text-sm font-medium">What holds the memory</div>
                      <MemoryBars procs={d.processes} total={memNow} />
                    </div>
                    <div className="rounded-lg border p-3 text-xs">
                      <div className="mb-2 text-sm font-medium">Memory by type</div>
                      {([["Program memory", cg.anon], ["File cache", cg.file], ["Kernel", cg.kernel], ["Shared (/dev/shm, tmpfs)", cg.shmem]] as const).map(([l, v]) => (
                        <div key={l} className="flex justify-between border-b py-1 last:border-b-0"><span className="text-muted-foreground">{l}</span><span className="tabular">{fmtBytes(v)}</span></div>
                      ))}
                      <div className="mt-2 text-muted-foreground">File cache is given back under pressure; program memory isn't.</div>
                      {(cg.throttled_us ?? 0) > 0 && <div className="mt-1 text-muted-foreground">CPU held back at its cap for {fmtDuration((cg.throttled_us ?? 0) / 1e6)} in all.</div>}
                    </div>
                  </div>
                )}
                {d.processes_error && <ErrorNote error={`Processes: ${d.processes_error}`} />}
                <div className="grid gap-4 lg:grid-cols-2">
                  <div className="rounded-lg border p-3 text-xs">
                    <div className="mb-2 text-sm font-medium">Container</div>
                    {([["Image", d.container.image], ["Image id", d.container.image_id], ["Created", fmtDateTime(d.container.created)],
                       ["Started", fmtDateTime(d.container.started_at)], ["Last message", fmtDateTime(c.last_turn_at)],
                       ["Stopped", c.stopped_at ? fmtDateTime(c.stopped_at) : "—"]] as const).map(([l, v]) => (
                      <div key={l} className="flex justify-between gap-3 border-b py-1 last:border-b-0"><span className="text-muted-foreground">{l}</span>
                        <span className="truncate font-mono">{v ?? "—"}</span></div>
                    ))}
                  </div>
                  <div className="rounded-lg border p-3 text-xs">
                    <div className="mb-2 text-sm font-medium">Recent turns</div>
                    {d.sessions.length === 0 && <div className="text-muted-foreground">None.</div>}
                    {d.sessions.slice(0, 8).map((s) => (
                      <Link key={s.id} to={`/sessions/${encodeURIComponent(s.id)}`} className="flex justify-between gap-3 border-b py-1 last:border-b-0 hover:bg-accent/50">
                        <span className="truncate">{s.title || s.id}</span>
                        <span className="shrink-0 text-muted-foreground">{s.state} · {fmtAgo(s.created_at)}</span>
                      </Link>
                    ))}
                  </div>
                </div>
              </TabsContent>
              <TabsContent value="processes" className="pt-2">
                {d.processes_error ? <ErrorNote error={d.processes_error} /> : (
                  <ProcessTable procs={d.processes} where={{ kind: "container", target: id }} turnPid={c.turn_running}
                    onLogs={followProcess} onKeep={setKeepPort} />
                )}
              </TabsContent>
              <TabsContent value="logs" className="space-y-2 pt-2">
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                  <span className="text-muted-foreground">Follow:</span>
                  <Button size="xs" variant={log.source === "container" ? "secondary" : "ghost"}
                    onClick={() => setLog({ source: "container", kind: "container", target: id })}>container output</Button>
                  {logs.map((p) => (
                    <Button key={p.pid} size="xs" variant={log.pid === p.pid ? "secondary" : "ghost"} onClick={() => followProcess(p)} title={p.cmd}>
                      {p.ports.length ? `:${p.ports[0]} ` : ""}{short(p.cmd.split(" ").slice(0, 3).join(" "), 32)}
                    </Button>
                  ))}
                  {c.session && <Link to={`/sessions/${encodeURIComponent(c.session.id)}`} className="ml-auto text-primary hover:underline">the turn's transcript →</Link>}
                </div>
                <LogView key={JSON.stringify(log)} src={log} className="h-[calc(100svh-26rem)] min-h-72" />
              </TabsContent>
              <TabsContent value="shell" className="pt-2">
                {tab === "shell" && <ShellOrNote admin={admin} kind="container" target={id} />}
              </TabsContent>
            </Tabs>
          </div>
        )
      }}
    </QueryState>
  )
}

// --- a service ----------------------------------------------------------------------------------------------------------

function ServicePanel({ s, admin }: { s: ServiceBox; admin: boolean }) {
  const q = useServiceDetail(s.name)
  const act = useWorkloadAction()
  const [tab, setTab] = useState("output")
  const [log, setLog] = useState<LogSource>({ source: "service", kind: "services", target: "agent-services", service: s.name })
  return (
    <div>
      <Header title={s.name} badges={<Badge variant={s.running ? "default" : "destructive"}>{s.running ? "running" : "not running"}</Badge>}
        sub={<>{s.note && <div>{s.note}</div>}<div className="font-mono">{s.cmd}</div>{s.cwd && <div>in {s.cwd}</div>}</>}>
        <ExpiryDialog what={s.name} base={{ kind: "service", target: s.name }} keep={s.keep}
          usual="a service runs until it is removed from services.toml">
          <Button size="sm" variant="outline"><Wrench /> Set expiry</Button>
        </ExpiryDialog>
        <Button size="sm" variant="outline" onClick={() => act.mutate({ kind: "service", target: s.name, action: "restart" })}><RotateCcw /> Restart</Button>
        <Confirm title={`Remove ${s.name}?`} confirm="Remove"
          body={<>It comes out of services.toml, with the comment above it, and stops within 30 seconds.</>}
          onConfirm={() => act.mutate({ kind: "service", target: s.name, action: "remove" })}>
          <Button size="sm" variant="outline"><Trash2 /> Remove</Button>
        </Confirm>
      </Header>
      <div className="space-y-1 border-b px-4 py-2">
        <ExpiryLine expires={s.expires} keep={s.keep} />
        <ActionNote action={act} />
      </div>
      <div className="grid grid-cols-2 gap-2 px-4 pt-3 lg:grid-cols-4">
        <Fact label="CPU" value={pct(s.cpu_percent)} sub="with what it started" />
        <Fact label="Memory" value={fmtBytes(s.mem_bytes)} sub={`${s.processes} process${s.processes === 1 ? "" : "es"}`} />
        <Fact label="Ports" value={s.ports.length ? s.ports.map((p) => `:${p}`).join(" ") : "—"} />
        <Fact label="Up for" value={s.started ? fmtDuration(Date.now() / 1000 - s.started) : "—"} sub={s.log_bytes != null ? `output ${fmtBytes(s.log_bytes)}` : undefined} />
      </div>
      <Tabs value={tab} onValueChange={setTab} className="px-4 py-3">
        <TabsList>
          <TabsTrigger value="output">Output</TabsTrigger>
          <TabsTrigger value="processes">Processes</TabsTrigger>
          <TabsTrigger value="shell"><SquareTerminal className="size-3.5" /> Shell</TabsTrigger>
        </TabsList>
        <TabsContent value="output" className="pt-2">
          <LogView key={JSON.stringify(log)} src={log} className="h-[calc(100svh-30rem)] min-h-72" />
        </TabsContent>
        <TabsContent value="processes" className="pt-2">
          <QueryState query={q}>
            {(d) => <ProcessTable procs={d.processes} where={{ kind: "services", target: "agent-services" }}
              onLogs={(p) => { setLog({ source: "process", kind: "services", target: "agent-services", pid: p.pid }); setTab("output") }} />}
          </QueryState>
        </TabsContent>
        <TabsContent value="shell" className="pt-2">
          {tab === "shell" && <ShellOrNote admin={admin} kind="services" target="agent-services" />}
        </TabsContent>
      </Tabs>
    </div>
  )
}

// --- a stack in the agent's Docker ----------------------------------------------------------------------------------------

function AppInfo({ name }: { name: string }) {
  const q = useAppDetail(name)
  return (
    <QueryState query={q}>
      {(d) => (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            <Fact label="Its own files" value={fmtBytes(d.size_rw)} sub={`image ${fmtBytes(d.size_root_fs)}`} />
            <Fact label="Restarts" value={d.restart_count} sub={d.oom_killed ? "last stop: out of memory" : `last exit ${d.exit_code}`} warn={d.oom_killed} />
            <Fact label="Memory cap" value={d.memory_limit ? fmtBytes(d.memory_limit) : "none"} />
            <Fact label="Started" value={fmtAgo(d.started_at)} sub={fmtDateTime(d.started_at)} />
          </div>
          <div className="overflow-x-auto rounded-lg border">
            <Table>
              <TableHeader><TableRow><TableHead>PID</TableHead><TableHead>Command</TableHead><TableHead className="text-right">CPU</TableHead>
                <TableHead className="text-right">Memory</TableHead><TableHead className="text-right">Running</TableHead></TableRow></TableHeader>
              <TableBody>
                {d.processes.map((p, i) => (
                  <TableRow key={i}>
                    <TableCell className="tabular text-xs text-muted-foreground">{p.pid}</TableCell>
                    <TableCell className="max-w-[32rem] truncate font-mono text-[11.5px]" title={p.cmd}>{p.cmd}</TableCell>
                    <TableCell className="tabular text-right text-xs">{pct(p.cpu_percent)}</TableCell>
                    <TableCell className="tabular text-right text-xs">{fmtBytes(p.rss)}</TableCell>
                    <TableCell className="tabular text-right text-xs text-muted-foreground">{p.elapsed}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {d.mounts.length > 0 && (
            <div className="rounded-lg border p-3 text-xs">
              <div className="mb-1 font-medium">Mounts</div>
              {d.mounts.map((m, i) => <div key={i} className="truncate font-mono text-muted-foreground">{m.name ?? m.source} → {m.destination}</div>)}
            </div>
          )}
        </div>
      )}
    </QueryState>
  )
}

function StackPanel({ s, admin }: { s: StackBox; admin: boolean }) {
  const act = useWorkloadAction()
  const [picked, setPicked] = useState<string>(s.containers[0]?.name ?? "")
  const [tab, setTab] = useState("logs")
  const current = s.containers.find((c) => c.name === picked) ?? s.containers[0]
  const one = (c: AppContainer, action: "stop" | "start" | "restart") => act.mutate({ kind: "app", target: c.name, action })
  return (
    <div>
      <Header title={s.name} badges={<Badge variant={s.running ? "default" : "outline"}>{s.running}/{s.total} up</Badge>}
        sub={<span className="flex flex-wrap gap-x-2">
          {s.session && <Link className="text-primary hover:underline" to={`/sessions/${encodeURIComponent(s.session.id)}`}>from: {s.session.title}</Link>}
          {s.folder && <span className="font-mono">{s.folder}</span>}
        </span>}>
        <ExpiryDialog what={s.name} base={{ kind: "stack", target: s.name }} keep={s.keep}
          usual="a stack in the agent's Docker runs until someone stops it">
          <Button size="sm" variant="outline"><Wrench /> Set expiry</Button>
        </ExpiryDialog>
        {s.running > 0 ? (
          <Confirm title={`Stop ${s.name}?`} confirm="Stop" body="Every container in it stops. Its data volumes stay; start it again from here."
            onConfirm={() => act.mutate({ kind: "stack", target: s.name, action: "stop" })}>
            <Button size="sm" variant="outline"><Power /> Stop all</Button>
          </Confirm>
        ) : (
          <Button size="sm" variant="outline" onClick={() => act.mutate({ kind: "stack", target: s.name, action: "start" })}><Play /> Start all</Button>
        )}
        <Button size="sm" variant="outline" onClick={() => act.mutate({ kind: "stack", target: s.name, action: "restart" })}><RotateCcw /> Restart</Button>
        <Confirm title={`Delete ${s.name}'s containers?`} confirm="Delete"
          body="Its containers are removed (their data volumes stay). Bringing it back means running its compose file again."
          onConfirm={() => act.mutate({ kind: "stack", target: s.name, action: "remove" })}>
          <Button size="icon-sm" variant="ghost" aria-label="Delete the stack's containers"><Trash2 /></Button>
        </Confirm>
      </Header>
      <div className="space-y-1 border-b px-4 py-2">
        <ExpiryLine expires={s.expires} keep={s.keep} />
        <ActionNote action={act} />
      </div>
      <div className="px-4 pt-3">
        <div className="overflow-x-auto rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Container</TableHead><TableHead>Ports</TableHead><TableHead className="text-right">CPU</TableHead>
                <TableHead className="text-right">Memory</TableHead><TableHead className="text-right">Net in/out</TableHead>
                <TableHead className="text-right">Disk r/w</TableHead><TableHead className="w-10" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {s.containers.map((c) => (
                <TableRow key={c.name} className={cn("cursor-pointer", current?.name === c.name && "bg-accent/60")} onClick={() => setPicked(c.name)}>
                  <TableCell>
                    <div className="flex items-center gap-2"><Dot tone={c.state === "running" ? "on" : "off"} /><span className="font-medium">{c.service ?? c.name}</span></div>
                    <div className="pl-4 text-xs text-muted-foreground">{c.image} · {c.status}</div>
                  </TableCell>
                  <TableCell className="font-mono text-xs">{c.ports || "—"}</TableCell>
                  <TableCell className="tabular text-right text-xs">{c.state === "running" ? pct(c.cpu_percent) : "—"}</TableCell>
                  <TableCell className="tabular text-right text-xs">{c.state === "running" ? fmtBytes(c.mem_bytes) : "—"}{c.pids != null && <div className="text-muted-foreground">{c.pids} procs</div>}</TableCell>
                  <TableCell className="tabular text-right text-xs">{fmtBytes(c.net_rx)} / {fmtBytes(c.net_tx)}</TableCell>
                  <TableCell className="tabular text-right text-xs">{fmtBytes(c.block_read)} / {fmtBytes(c.block_write)}</TableCell>
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild><Button size="icon-xs" variant="ghost" aria-label={`Actions for ${c.name}`}><MoreHorizontal /></Button></DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {c.state === "running" ? <DropdownMenuItem onSelect={() => one(c, "stop")}>Stop</DropdownMenuItem>
                          : <DropdownMenuItem onSelect={() => one(c, "start")}>Start</DropdownMenuItem>}
                        <DropdownMenuItem onSelect={() => one(c, "restart")}>Restart</DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>
      {current && (
        <Tabs value={tab} onValueChange={setTab} className="px-4 py-3">
          <TabsList>
            <TabsTrigger value="logs">Logs · {current.service ?? current.name}</TabsTrigger>
            <TabsTrigger value="details">Details</TabsTrigger>
            <TabsTrigger value="shell" disabled={current.state !== "running"}><SquareTerminal className="size-3.5" /> Shell</TabsTrigger>
          </TabsList>
          <TabsContent value="logs" className="pt-2">
            <LogView key={current.name} src={{ source: "container", kind: "app", target: current.name }} className="h-[calc(100svh-30rem)] min-h-72" />
          </TabsContent>
          <TabsContent value="details" className="pt-2">{tab === "details" && <AppInfo name={current.name} />}</TabsContent>
          <TabsContent value="shell" className="pt-2">
            {tab === "shell" && <ShellOrNote admin={admin} kind="app" target={current.name} />}
          </TabsContent>
        </Tabs>
      )}
    </div>
  )
}

// --- one of the stack's own containers --------------------------------------------------------------------------------------

function SystemPanel({ s, admin }: { s: SystemBox; admin: boolean }) {
  const [tab, setTab] = useState("logs")
  return (
    <div>
      <Header title={s.name} badges={<Badge variant={s.state === "running" ? "default" : "destructive"}>{s.state}</Badge>}
        sub={<>{s.note && <div>{s.note}</div>}<span className="font-mono">{s.image}</span> · {s.status}</>} />
      <div className="grid grid-cols-3 gap-2 px-4 pt-3">
        <Fact label="CPU" value={pct(s.cpu_percent)} />
        <Fact label="Memory" value={fmtBytes(s.mem_bytes)} />
        <Fact label="Processes" value={s.processes ?? "—"} />
      </div>
      <Tabs value={tab} onValueChange={setTab} className="px-4 py-3">
        <TabsList>
          <TabsTrigger value="logs">Logs</TabsTrigger>
          {s.shell && <TabsTrigger value="shell"><SquareTerminal className="size-3.5" /> Shell</TabsTrigger>}
        </TabsList>
        <TabsContent value="logs" className="pt-2">
          <LogView key={s.name} src={{ source: "container", kind: s.shell ? "services" : "system", target: s.name }} className="h-[calc(100svh-26rem)] min-h-72" />
        </TabsContent>
        {s.shell && <TabsContent value="shell" className="pt-2">{tab === "shell" && <ShellOrNote admin={admin} kind="services" target={s.name} />}</TabsContent>}
      </Tabs>
    </div>
  )
}

// --- the page --------------------------------------------------------------------------------------------------------------

function Detail({ w, sel, admin }: { w: Workloads; sel: Selection | null; admin: boolean }) {
  const body = useMemo(() => {
    if (!sel) return null
    if (sel.kind === "container") return w.conversations.some((c) => c.conversation === sel.name) ? <ContainerPanel key={sel.name} id={sel.name} admin={admin} /> : null
    if (sel.kind === "service") {
      const s = w.services.items.find((x) => x.name === sel.name)
      return s ? <ServicePanel key={s.name} s={s} admin={admin} /> : null
    }
    if (sel.kind === "stack") {
      const s = Array.isArray(w.stacks) ? w.stacks.find((x) => x.name === sel.name) : undefined
      return s ? <StackPanel key={s.name} s={s} admin={admin} /> : null
    }
    const s = w.system.find((x) => x.name === sel.name)
    return s ? <SystemPanel key={s.name} s={s} admin={admin} /> : null
  }, [w, sel, admin])
  return (
    <Card className="min-h-96 gap-0 overflow-hidden py-0 shadow-none">
      {body ?? (
        <div className="flex h-full min-h-96 flex-col items-center justify-center gap-2 p-8 text-center text-sm text-muted-foreground">
          <Boxes className="size-8" aria-hidden />
          {sel ? "That's gone: pick something else on the left." : "Pick a container, service or stack on the left to see what runs in it, follow its logs, open a shell, stop it or set when it stops."}
        </div>
      )}
    </Card>
  )
}

export function Containers() {
  const q = useWorkloads()
  const { data: me } = useMe()
  const [params, setParams] = useSearchParams()
  const sel = parseSelection(params.get("sel"))
  const select = (s: Selection) => setParams((p) => { p.set("sel", selectionKey(s)); return p }, { replace: true })
  const w = q.data
  return (
    <Page title="Containers"
      subtitle={w ? <>Everything that runs for the agent: a container per conversation (stopped {w.rules.idle_stop_minutes} minutes after
        its last message when idle, {w.rules.max_age_hours} hours after it whatever runs), its services, and the stacks in its own Docker.</> : undefined}>
      <QueryState query={q} rows={8}>
        {(w) => (
          <div className="space-y-4">
            <Tiles w={w} />
            {w.host.stale && <ErrorNote error="The host numbers are stale: the sampler hasn't reported for a while." />}
            <div className="grid items-start gap-4 lg:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
              <List w={w} sel={sel} select={select} />
              <Detail w={w} sel={sel} admin={!!me?.can_resume} />
            </div>
          </div>
        )}
      </QueryState>
    </Page>
  )
}
