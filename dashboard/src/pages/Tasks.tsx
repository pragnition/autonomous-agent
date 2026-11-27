import { Link, useSearchParams } from "react-router"
import { type BoardChannel, type TaskBoard, useDismiss, useTaskBoard } from "@/api"
import { BoardStateBadge, ChannelCount, ChannelIcon, TaskCard } from "@/components/board"
import { Page } from "@/components/Page"
import { QueryState } from "@/components/QueryState"
import { Card, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { split } from "@/lib/board"
import { fmtAgo, secondsBetween } from "@/lib/format"
import { useNow } from "@/lib/hooks"

function ChannelTasks({ channel, since, now, onDismiss }: {
  channel: BoardChannel
  since: string
  now: number
  onDismiss: (id: number) => void
}) {
  const { active, finished } = split(channel.tasks)
  return (
    <div className="space-y-6">
      {active.length === 0
        ? <p className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">Nothing running here.</p>
        : (
          <div className="grid gap-3 lg:grid-cols-2 2xl:grid-cols-3">
            {active.map((t) => <TaskCard key={t.conversation_id} task={t} now={now} onDismiss={onDismiss} />)}
          </div>
        )}
      {finished.length > 0 && (
        <Card className="gap-0 py-0 shadow-none">
          <CardHeader className="px-4 py-3">
            <CardTitle className="text-sm font-medium">Finished in the last {Math.round(secondsBetween(since, now) / 3600)} hours</CardTitle>
          </CardHeader>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-28">State</TableHead>
                <TableHead>Task</TableHead>
                <TableHead className="hidden md:table-cell">Last update</TableHead>
                <TableHead className="text-right">Ended</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {finished.map((t) => (
                <TableRow key={t.conversation_id}>
                  <TableCell><BoardStateBadge state={t.state} /></TableCell>
                  <TableCell className="max-w-[24rem]">
                    {t.slack_url
                      ? <a href={t.slack_url} target="_blank" rel="noreferrer" className="block truncate font-medium hover:underline">{t.title}</a>
                      : <Link to={`/sessions/${t.conversation_id}`} className="block truncate font-medium hover:underline">{t.title}</Link>}
                  </TableCell>
                  <TableCell className="hidden max-w-[28rem] truncate text-muted-foreground md:table-cell">{t.update?.text ?? "—"}</TableCell>
                  <TableCell className="text-right text-muted-foreground">{fmtAgo(t.last_active_at, now)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      )}
    </div>
  )
}

/** The board, one tab per channel; the open tab is in the URL (?channel=) so a channel's page can be bookmarked. */
export function TaskBoardView({ board, now, onDismiss }: { board: TaskBoard; now: number; onDismiss: (id: number) => void }) {
  const [params, setParams] = useSearchParams()
  if (board.channels.length === 0) {
    return <p className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">Nothing in the last day.</p>
  }
  const asked = params.get("channel")
  const tab = board.channels.some((c) => c.key === asked) ? asked! : board.channels[0].key
  return (
    <Tabs value={tab} onValueChange={(v) => setParams({ channel: v }, { replace: true })}>
      <TabsList variant="line" className="h-auto max-w-full flex-wrap justify-start">
        {board.channels.map((ch) => (
          <TabsTrigger key={ch.key} value={ch.key} className="flex-none">
            <ChannelIcon kind={ch.kind} /> {ch.label}
            <ChannelCount n={split(ch.tasks).active.length} alert={ch.tasks.some((t) => t.attention.length > 0)} />
          </TabsTrigger>
        ))}
      </TabsList>
      {board.channels.map((ch) => (
        <TabsContent key={ch.key} value={ch.key} className="mt-4">
          <ChannelTasks channel={ch} since={board.since} now={now} onDismiss={onDismiss} />
        </TabsContent>
      ))}
    </Tabs>
  )
}

export function Tasks() {
  const now = useNow(5000)
  const board = useTaskBoard()
  const dismiss = useDismiss()
  return (
    <Page title="Tasks" subtitle="What the agent is working on in each Slack channel.">
      <QueryState query={board} rows={4}>
        {(b) => <TaskBoardView board={b} now={now} onDismiss={(id) => dismiss.mutate(id)} />}
      </QueryState>
    </Page>
  )
}
