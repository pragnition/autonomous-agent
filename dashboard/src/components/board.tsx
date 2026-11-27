import { ArrowUpRight, Ban, CheckCircle2, Circle, CircleDashed, CirclePause, Eye, Hand, Hash, Inbox, Loader2,
  MessageCircleQuestion, MessagesSquare, OctagonAlert, XCircle } from "lucide-react"
import { type ComponentType, type ReactNode, useState } from "react"
import { Link } from "react-router"
import type { Attention, BoardChannel, BoardState, BoardTask, Step, StepStatus } from "@/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { isActive, stepProgress } from "@/lib/board"
import { fmtAgo, fmtDuration } from "@/lib/format"
import { cn } from "@/lib/utils"

type Icon = ComponentType<{ className?: string }>

const STATES: Record<BoardState, { label: string; icon: Icon; className: string }> = {
  running: { label: "Running", icon: Loader2, className: "text-good [&_svg]:animate-spin" },
  queued: { label: "Queued", icon: CircleDashed, className: "text-muted-foreground" },
  waiting: { label: "Waiting on you", icon: Hand, className: "text-warning" },
  parked: { label: "Parked", icon: CirclePause, className: "text-muted-foreground" },
  idle: { label: "Idle", icon: Circle, className: "text-muted-foreground" },
  done: { label: "Done", icon: CheckCircle2, className: "text-good" },
  failed: { label: "Failed", icon: XCircle, className: "text-critical" },
  cancelled: { label: "Cancelled", icon: Ban, className: "text-muted-foreground" },
}

/** The question a task is waiting on, shown while its trigger is hovered, or tapped open on a touch screen. */
function QuestionPopover({ question, now, children }: { question: Attention; now: number; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  return (
    <HoverCard open={open} onOpenChange={setOpen} openDelay={0} closeDelay={150}>
      <HoverCardTrigger asChild onClick={() => setOpen((o) => !o)}>{children}</HoverCardTrigger>
      <HoverCardContent side="bottom" align="start" className="w-80 space-y-1.5 p-3 text-xs">
        <div className="flex items-center gap-1.5 font-medium">
          <MessageCircleQuestion className="size-3.5 text-warning" aria-hidden /> Waiting on your answer
          <span className="ml-auto font-normal text-muted-foreground">{fmtAgo(question.at, now)}</span>
        </div>
        <p className="whitespace-pre-line">{question.text}</p>
        {question.slack_url && (
          <a href={question.slack_url} target="_blank" rel="noreferrer"
            className="inline-flex items-center gap-0.5 text-muted-foreground hover:text-foreground">
            Reply in Slack <ArrowUpRight className="size-3" aria-hidden />
          </a>
        )}
      </HoverCardContent>
    </HoverCard>
  )
}

/** A task's state as an icon alone; hovering, focusing or clicking it names the state. A waiting task whose
 * checklist has no blocked step to hang the question on shows the question here instead. */
export function BoardStateIcon({ task, now }: { task: BoardTask; now: number }) {
  const [open, setOpen] = useState(false)
  const s = STATES[task.state] ?? STATES.idle
  const question = task.attention.find((a) => a.kind === "question")
  if (question && !task.steps.some((st) => st.status === "blocked")) {
    return (
      <QuestionPopover question={question} now={now}>
        <button type="button" aria-label="Waiting on your answer" className={cn("inline-flex rounded-sm p-0.5", STATES.waiting.className)}>
          <Hand className="size-4" aria-hidden />
        </button>
      </QuestionPopover>
    )
  }
  return (
    <Tooltip open={open} onOpenChange={setOpen}>
      <TooltipTrigger asChild>
        <button type="button" aria-label={s.label} onClick={() => setOpen((o) => !o)}
          className={cn("inline-flex rounded-sm p-0.5", s.className)}>
          <s.icon className="size-4" aria-hidden />
        </button>
      </TooltipTrigger>
      <TooltipContent side="top">{s.label}</TooltipContent>
    </Tooltip>
  )
}

export function BoardStateBadge({ state }: { state: BoardState }) {
  const s = STATES[state] ?? STATES.idle
  return (
    <span className={cn("inline-flex items-center gap-1 text-xs font-medium", s.className)}>
      <s.icon className="size-3.5" aria-hidden />
      <span className="text-foreground">{s.label}</span>
    </span>
  )
}

const STEPS: Record<StepStatus, { icon: Icon; className: string; label: string }> = {
  completed: { icon: CheckCircle2, className: "text-good", label: "done" },
  in_progress: { icon: Loader2, className: "text-foreground [&]:animate-spin", label: "in progress" },
  pending: { icon: Circle, className: "text-muted-foreground/60", label: "to do" },
  blocked: { icon: OctagonAlert, className: "text-serious", label: "blocked" },
}

/** One checklist step; a blocked step of a task waiting on someone opens the question it's blocked on. */
function StepRow({ step, question, now }: { step: Step; question?: Attention; now: number }) {
  const s = STEPS[step.status] ?? STEPS.pending
  const text = (
    <span className={cn("leading-snug", step.status === "completed" && "text-muted-foreground line-through decoration-muted-foreground/40",
      step.status === "in_progress" && "font-medium", step.status === "blocked" && "text-serious")}>
      {step.content}
    </span>
  )
  return (
    <li className="flex items-start gap-2 text-xs">
      <s.icon className={cn("mt-px size-3.5 shrink-0", s.className)} aria-label={s.label} />
      {question
        ? (
          <QuestionPopover question={question} now={now}>
            <button type="button" className="cursor-pointer text-left underline decoration-dotted decoration-serious/60 underline-offset-2">
              {text}
            </button>
          </QuestionPopover>
        )
        : text}
    </li>
  )
}

export function ChannelIcon({ kind, className }: { kind: BoardChannel["kind"]; className?: string }) {
  const I = kind === "channel" ? Hash : kind === "dm" ? MessagesSquare : Inbox
  return <I className={cn("size-3.5", className)} aria-hidden />
}

/** The task's title, opening its Slack thread; a task outside Slack opens its transcript instead. */
function TaskTitle({ task }: { task: BoardTask }) {
  const cls = "group inline font-medium leading-snug hover:underline"
  const arrow = <ArrowUpRight className="ml-0.5 inline size-3.5 align-[-2px] text-muted-foreground group-hover:text-foreground" aria-hidden />
  if (task.slack_url) {
    const cut = task.title.lastIndexOf(" ") + 1
    return (
      <a href={task.slack_url} target="_blank" rel="noreferrer" className={cls}>
        {task.title.slice(0, cut)}<span className="whitespace-nowrap">{task.title.slice(cut)}{arrow}</span>
      </a>
    )
  }
  return <Link to={`/sessions/${task.conversation_id}`} className={cls}>{task.title}</Link>
}

function Footer({ left, right }: { left: ReactNode; right: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
      <span className="min-w-0 truncate">{left}</span>
      <span className="shrink-0">{right}</span>
    </div>
  )
}

export function TaskCard({ task, now, onDismiss }: { task: BoardTask; now: number; onDismiss: (id: number) => void }) {
  const progress = stepProgress(task)
  const end = isActive(task) ? now : Date.parse(task.last_active_at)
  const question = task.attention.find((a) => a.kind === "question")
  const looks = task.attention.filter((a) => a.kind === "look")
  return (
    <Card className={cn("gap-3 px-4 py-3.5 shadow-none transition-colors hover:border-foreground/20",
      task.attention.length > 0 && "border-warning/60")}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <TaskTitle task={task} />
          {task.parent && (
            <div className="mt-1 text-xs text-muted-foreground">
              helper of <Link to={`/sessions/${task.parent.conversation_id}`} className="hover:underline">{task.parent.title}</Link>
            </div>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2 text-xs">
          <BoardStateIcon task={task} now={now} />
          <span className="tabular font-medium">{fmtDuration((end - Date.parse(task.started_at)) / 1000)}</span>
        </div>
      </div>

      {progress && (
        <div className="h-1 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuemin={0}
          aria-valuemax={progress.total} aria-valuenow={progress.done} aria-label="Steps done">
          <div className="h-full rounded-full bg-good" style={{ width: `${(progress.done / progress.total) * 100}%` }} />
        </div>
      )}

      {task.steps.length > 0 && (
        <ul className="space-y-1">
          {task.steps.map((s, i) => <StepRow key={i} step={s} now={now} question={s.status === "blocked" ? question : undefined} />)}
        </ul>
      )}

      {looks.map((a) => <AttentionNote key={a.id} item={a} now={now} onDismiss={onDismiss} />)}

      {task.update && !looks.some((a) => a.at === task.update?.at && a.text === task.update.text) && (
        <div className="space-y-0.5 border-t pt-2.5 text-xs">
          <Footer left="Latest update" right={fmtAgo(task.update.at, now)} />
          <p className="line-clamp-3 whitespace-pre-line">{task.update.text}</p>
        </div>
      )}

      <Footer left={task.requester ? `for ${task.requester}` : ""} right={`active ${fmtAgo(task.last_active_at, now)}`} />
    </Card>
  )
}

const ATTENTION: Record<Attention["kind"], { label: string; icon: Icon }> = {
  question: { label: "Waiting on your answer", icon: MessageCircleQuestion },
  look: { label: "Needs a look", icon: Eye },
}

function AttentionNote({ item, now, onDismiss }: { item: Attention; now: number; onDismiss: (id: number) => void }) {
  const a = ATTENTION[item.kind]
  return (
    <div className="rounded-md border border-warning/50 bg-warning/10 px-3 py-2 text-xs">
      <div className="mb-0.5 flex items-center gap-1.5 font-medium">
        <a.icon className="size-3.5 text-warning" aria-hidden /> {a.label}
        <span className="ml-auto font-normal text-muted-foreground">{fmtAgo(item.at, now)}</span>
      </div>
      <p className="line-clamp-3 whitespace-pre-line">{item.text}</p>
      {item.kind === "look" && (
        <Button variant="ghost" size="sm" className="mt-1 -ml-2 h-6 px-2 text-xs" onClick={() => onDismiss(item.id)}>
          Dismiss
        </Button>
      )}
    </div>
  )
}

export function ChannelCount({ n, alert }: { n: number; alert: boolean }) {
  return (
    <Badge variant="secondary" className={cn("h-4 min-w-4 px-1 text-[10px] tabular", alert && "bg-warning/25 text-foreground")}>
      {n}
    </Badge>
  )
}
