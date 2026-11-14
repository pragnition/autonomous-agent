import { useQueryClient } from "@tanstack/react-query"
import { ArrowLeft, ExternalLink, XCircle } from "lucide-react"
import { useEffect, useMemo, useRef } from "react"
import { Link, useParams } from "react-router"
import { useAction, useSession, useSessionEntries } from "@/api"
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
import { fmtDateTime, fmtDuration, fmtTokens, sumTokens } from "@/lib/format"
import { useNow } from "@/lib/hooks"

function CancelButton({ id }: { id: string }) {
  const cancel = useAction(`/api/sessions/${id}/cancel`)
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>
        <Button variant="outline" size="sm" disabled={cancel.isPending}>
          <XCircle className="size-4" aria-hidden /> Cancel session
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Cancel this session?</AlertDialogTitle>
          <AlertDialogDescription>
            The agent stops this session at once. Everything else keeps running. Your name is recorded in the
            activity log.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Keep it running</AlertDialogCancel>
          <AlertDialogAction variant="destructive" onClick={() => cancel.mutate()}>Cancel session</AlertDialogAction>
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

export function SessionDetail() {
  const { id = "" } = useParams()
  const now = useNow(1000)
  const client = useQueryClient()
  const row = useSession(id)
  const initial = useSessionEntries(id)
  const active = row.data ? row.data.state === "running" || row.data.state === "queued" : false
  const cursor = initial.data ? { offset: initial.data.offset, line: initial.data.line } : undefined
  const stream = useSessionStream(id, cursor, active)
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
    if (stream.ended) client.invalidateQueries({ queryKey: ["session", id] })
  }, [stream.ended, client, id])

  return (
    <QueryState query={row} rows={6}>
      {(r) => (
        <Page
          title={<span className="break-words">{r.title}</span>}
          subtitle={
            <span className="flex flex-wrap items-center gap-2">
              <Link to="/sessions" className="inline-flex items-center gap-1 hover:text-foreground">
                <ArrowLeft className="size-3.5" aria-hidden /> Sessions
              </Link>
              <span aria-hidden>·</span>
              <span className="font-mono text-xs">{r.id}</span>
            </span>
          }
          actions={active ? <CancelButton id={r.id} /> : undefined}>
          <Card className="mb-6 shadow-none">
            <CardContent>
              <div className="mb-4 flex flex-wrap items-center gap-2">
                <StateBadge state={r.state} />
                <SourceBadge source={r.source} />
                <ProjectBadge project={r.project} />
                <Badge variant="outline" className="font-normal">{r.kind}</Badge>
                {r.live && <Badge variant="secondary" className="font-normal">live numbers</Badge>}
              </div>
              <dl className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4 lg:grid-cols-7">
                <Fact label="Asked by">{r.requester ?? "—"}</Fact>
                <Fact label="Created">{fmtDateTime(r.created_at)}</Fact>
                <Fact label={r.state === "queued" ? "Waiting" : "Duration"}>
                  <span className="tabular">{fmtDuration(elapsed(r, now))}</span>
                </Fact>
                <Fact label="Account">{r.account_name ?? r.account ?? "—"}</Fact>
                <Fact label="Model calls"><span className="tabular">{r.turns ?? "—"}</span></Fact>
                <Fact label="Tokens"><span className="tabular">{r.tokens ? fmtTokens(sumTokens(r.tokens)) : "—"}</span></Fact>
                <Fact label="Outcome">{r.outcome ?? (active ? "in progress" : "—")}</Fact>
              </dl>
              {(r.links.slack || r.links.linear || r.links.pr || r.task_id) && (
                <div className="mt-4 flex flex-wrap gap-x-5 gap-y-1 border-t pt-3">
                  {r.task_id && <span className="text-sm text-muted-foreground">Task <span className="font-mono">{r.task_id}</span></span>}
                  {r.links.slack && <LinkOut href={r.links.slack}>Slack thread</LinkOut>}
                  {r.links.linear && <LinkOut href={r.links.linear}>Linear issue</LinkOut>}
                  {r.links.pr && <LinkOut href={r.links.pr}>Pull request</LinkOut>}
                </div>
              )}
              {r.error && <div className="mt-3"><ErrorNote error={r.error} /></div>}
            </CardContent>
          </Card>

          <section aria-label="Activity">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-sm font-medium">Activity</h2>
              {active && !stream.ended && (
                <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
                  <span className={stream.broken ? "size-2 rounded-full bg-warning" : "size-2 animate-pulse rounded-full bg-good"}
                    aria-hidden />
                  {stream.broken ? "reconnecting…" : "streaming"}
                </span>
              )}
            </div>
            <Card className="py-2 shadow-none">
              <CardContent className="px-2">
                <QueryState query={initial} rows={8}>
                  {() => <ActivityTimeline items={items} live={active && !stream.ended} />}
                </QueryState>
              </CardContent>
            </Card>
            <div ref={bottom} />
          </section>
        </Page>
      )}
    </QueryState>
  )
}
