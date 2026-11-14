import { useQueryClient } from "@tanstack/react-query"
import { ArrowLeft, ChevronRight, ExternalLink, XCircle } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import { Link, useParams } from "react-router"
import type { Conversation, SessionRow } from "@/api"
import { useAction, useConversation, useSessionEntries } from "@/api"
import { ActivityTimeline } from "@/components/ActivityTimeline"
import { Page } from "@/components/Page"
import { ErrorNote, QueryState } from "@/components/QueryState"
import { ProjectBadge, SourceBadge, StateBadge, elapsed } from "@/components/session"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { pairEntries, useSessionStream } from "@/lib/activity"
import { openByDefault } from "@/lib/conversation"
import { fmtDateTime, fmtDuration, fmtTokens, sumTokens } from "@/lib/format"
import { useNow } from "@/lib/hooks"
import { cn } from "@/lib/utils"

function CancelButton({ id }: { id: string }) {
  const cancel = useAction(`/api/sessions/${id}/cancel`)
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="outline" size="sm" disabled={cancel.isPending}>
          <XCircle className="size-4" aria-hidden /> Cancel
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Cancel work on this message?</AlertDialogTitle>
          <AlertDialogDescription>
            The agent stops working on this message at once. The conversation stays, and the next message continues
            it. Everything else keeps running. Your name is recorded in the activity log.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it running</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={() => cancel.mutate()}>Cancel</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
      {cancel.error && <span role="alert" className="text-xs text-critical">{cancel.error.message}</span>}
    </AlertDialog>
  )
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium">{children}</dd>
    </div>
  )
}

function LinkOut({ href, children }: { href: string; children: string }) {
  return (
    <a href={href} target="_blank" rel="noreferrer"
      className="inline-flex items-center gap-1 text-sm underline-offset-4 hover:underline">
      {children} <ExternalLink className="size-3" aria-hidden />
    </a>
  )
}

/** One turn's timeline: loaded once, then streamed while the turn is running or queued. */
function TurnActivity({ turn }: { turn: SessionRow }) {
  const client = useQueryClient()
  const initial = useSessionEntries(turn.id)
  const active = turn.state === "running" || turn.state === "queued"
  const cursor = initial.data ? { offset: initial.data.offset, line: initial.data.line } : undefined
  const stream = useSessionStream(turn.id, cursor, active)
  const items = useMemo(() => {
    const first = initial.data?.entries ?? []
    const have = new Set(first.map((e) => e.id))
    return pairEntries([...first, ...stream.entries.filter((e) => !have.has(e.id))])
  }, [initial.data, stream.entries])
  const bottom = useRef<HTMLDivElement>(null)
  const follow = useRef(true)

  useEffect(() => {
    const onScroll = () => {
      follow.current = window.innerHeight + window.scrollY >= document.body.scrollHeight - 160
    }
    window.addEventListener("scroll", onScroll, { passive: true })
    return () => window.removeEventListener("scroll", onScroll)
  }, [])

  useEffect(() => {
    if (stream.entries.length && follow.current) bottom.current?.scrollIntoView({ block: "end" })
  }, [stream.entries.length])

  useEffect(() => {
    if (stream.ended) client.invalidateQueries({ queryKey: ["conversation"] })
  }, [stream.ended, client])

  return (
    <div className="border-t px-2 py-2">
      {active && !stream.ended && (
        <div className="mb-1 flex items-center justify-between px-2">
          <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className={stream.broken ? "size-2 rounded-full bg-warning" : "size-2 animate-pulse rounded-full bg-good"}
              aria-hidden />
            {stream.broken ? "reconnecting…" : "streaming"}
          </span>
          <CancelButton id={turn.id} />
        </div>
      )}
      {turn.error && <div className="mb-2 px-2"><ErrorNote error={turn.error} /></div>}
      <QueryState query={initial} rows={4}>
        {() => <ActivityTimeline items={items} live={active && !stream.ended} />}
      </QueryState>
      <div ref={bottom} />
    </div>
  )
}

function Turn({ turn, n, open, onToggle, who, now }: {
  turn: SessionRow
  n: number
  open: boolean
  onToggle: () => void
  who: string | null
  now: number
}) {
  return (
    <Card className="gap-0 py-0 shadow-none">
      <button type="button" onClick={onToggle} aria-expanded={open}
        className="flex w-full items-center gap-3 rounded-xl px-4 py-2.5 text-left hover:bg-muted/40">
        <ChevronRight className={cn("size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")}
          aria-hidden />
        <span className="tabular w-6 shrink-0 text-xs text-muted-foreground">#{n}</span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {turn.message}
          {who && <span className="ml-2 font-normal text-muted-foreground">from {who}</span>}
        </span>
        <StateBadge state={turn.state} />
        <span className="hidden w-28 shrink-0 text-right text-xs text-muted-foreground sm:block">
          {fmtDateTime(turn.created_at)}
        </span>
        <span className="tabular w-14 shrink-0 text-right text-xs">{fmtDuration(elapsed(turn, now))}</span>
        <span className="tabular hidden w-14 shrink-0 text-right text-xs text-muted-foreground sm:block">
          {turn.tokens ? fmtTokens(sumTokens(turn.tokens)) : "—"}
        </span>
      </button>
      {open && <TurnActivity turn={turn} />}
    </Card>
  )
}

function ConversationView({ c }: { c: Conversation }) {
  const now = useNow(1000)
  const [toggled, setToggled] = useState<Record<string, boolean>>({})
  const defaults = openByDefault(c.items, c.focus)
  const active = c.state === "running" || c.state === "queued"

  return (
    <Page
      title={<span className="break-words">{c.title}</span>}
      subtitle={
        <span className="flex flex-wrap items-center gap-2">
          <Link to="/sessions" className="inline-flex items-center gap-1 hover:text-foreground">
            <ArrowLeft className="size-3.5" aria-hidden /> Sessions
          </Link>
          <span aria-hidden>·</span>
          <span className="font-mono text-xs">{c.id}</span>
        </span>
      }>
      <Card className="mb-6 shadow-none">
        <CardContent>
          <div className="mb-4 flex flex-wrap items-center gap-2">
            <StateBadge state={c.state} />
            <SourceBadge source={c.source} />
            <ProjectBadge project={c.project} />
            {c.live && <Badge variant="secondary" className="font-normal">live numbers</Badge>}
          </div>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4 lg:grid-cols-8">
            <Fact label="Asked by">{c.requester ?? "—"}</Fact>
            <Fact label="Started">{fmtDateTime(c.created_at)}</Fact>
            <Fact label="Messages"><span className="tabular">{c.messages}</span></Fact>
            <Fact label="Work time"><span className="tabular">{fmtDuration(c.busy_s)}</span></Fact>
            <Fact label="Account">{c.account_name ?? c.account ?? "—"}</Fact>
            <Fact label="Model calls"><span className="tabular">{c.turns ?? "—"}</span></Fact>
            <Fact label="Tokens"><span className="tabular">{c.tokens ? fmtTokens(sumTokens(c.tokens)) : "—"}</span></Fact>
            <Fact label="Outcome">{c.outcome ?? (active ? "in progress" : "—")}</Fact>
          </dl>
          {(c.links.slack || c.links.linear || c.links.pr || c.task_id) && (
            <div className="mt-4 flex flex-wrap gap-x-5 gap-y-1 border-t pt-3">
              {c.task_id && <span className="text-sm text-muted-foreground">Task <span className="font-mono">{c.task_id}</span></span>}
              {c.links.slack && <LinkOut href={c.links.slack}>Slack thread</LinkOut>}
              {c.links.linear && <LinkOut href={c.links.linear}>Linear issue</LinkOut>}
              {c.links.pr && <LinkOut href={c.links.pr}>Pull request</LinkOut>}
            </div>
          )}
        </CardContent>
      </Card>

      <section aria-label="Messages" className="space-y-2">
        <h2 className="mb-2 text-sm font-medium">Messages</h2>
        {c.items.map((t, i) => (
          <Turn key={t.id} turn={t} n={i + 1} now={now}
            who={t.requester && t.requester !== c.requester ? t.requester : null}
            open={toggled[t.id] ?? defaults.has(t.id)}
            onToggle={() => setToggled((prev) => ({ ...prev, [t.id]: !(prev[t.id] ?? defaults.has(t.id)) }))} />
        ))}
      </section>
    </Page>
  )
}

/** A session is a conversation: every message in it is a turn that resumes the same Claude session. The id may be
 * the conversation's or one of its turns' (links from the overview), which opens that turn. */
export function SessionDetail() {
  const { id = "" } = useParams()
  const convo = useConversation(id)
  return <QueryState query={convo} rows={6}>{(c) => <ConversationView c={c} />}</QueryState>
}
