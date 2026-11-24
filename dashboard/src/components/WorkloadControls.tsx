import { AlertTriangle, CheckCircle2, Clock } from "lucide-react"
import { type ReactNode, useState } from "react"
import { type Expiry, type Keep, type WorkloadAction, useWorkloadAction } from "@/api"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { fmtDateTime } from "@/lib/format"
import { cn } from "@/lib/utils"
import { untilText } from "@/lib/workloads"

const PRESETS: [string, number][] = [["1 hour", 1], ["4 hours", 4], ["1 day", 24], ["3 days", 72], ["1 week", 168], ["30 days", 720]]

/** When something stops, and why, in one line. */
export function ExpiryLine({ expires, keep, className }: { expires: Expiry; keep?: Keep | null; className?: string }) {
  return (
    <div className={cn("flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground", className)}>
      <Clock className="size-3.5 shrink-0" aria-hidden />
      {expires.at ? (
        <span><span className="font-medium text-foreground">{untilText(expires.at)}</span> · {fmtDateTime(expires.at)}</span>
      ) : (
        <span className="font-medium text-foreground">{keep && !keep.until ? "kept until changed" : "no expiry"}</span>
      )}
      <span>· {expires.why}</span>
    </div>
  )
}

/** Set when something stops: a preset, a number of hours, until changed, or back to the usual rules. */
export function ExpiryDialog({ what, base, keep, usual, children, open: controlled, onOpenChange }: {
  what: string
  base: Omit<WorkloadAction, "action">
  keep: Keep | null
  usual: string
  children?: ReactNode
  open?: boolean
  onOpenChange?: (open: boolean) => void
}) {
  const act = useWorkloadAction()
  const [own, setOwn] = useState(false)
  const open = controlled ?? own
  const setOpen = (o: boolean) => (onOpenChange ?? setOwn)(o)
  const [hours, setHours] = useState("")
  const run = (extra: Partial<WorkloadAction>) =>
    act.mutate({ ...base, action: "keep", ...extra } as WorkloadAction, { onSuccess: () => setOpen(false) })
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); act.reset() }}>
      {children && <DialogTrigger asChild>{children}</DialogTrigger>}
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Keep {what} running</DialogTitle>
          <DialogDescription>
            Without an expiry: {usual}.
            {keep && <> Now: {keep.until ? `kept until ${fmtDateTime(keep.until)}` : "kept until changed"}, set by {keep.set_by ?? "someone"}.</>}
          </DialogDescription>
        </DialogHeader>
        <div className="grid grid-cols-3 gap-2">
          {PRESETS.map(([label, h]) => (
            <Button key={label} variant="outline" size="sm" disabled={act.isPending} onClick={() => run({ hours: h })}>{label}</Button>
          ))}
        </div>
        <form className="flex items-center gap-2" onSubmit={(e) => { e.preventDefault(); const h = Number(hours); if (h > 0) run({ hours: h }) }}>
          <Input type="number" min="0.1" step="0.5" value={hours} onChange={(e) => setHours(e.target.value)} placeholder="Hours from now"
            className="h-8" aria-label="Hours from now" />
          <Button type="submit" size="sm" disabled={act.isPending || !(Number(hours) > 0)}>Set</Button>
        </form>
        <div className="flex flex-wrap gap-2 border-t pt-3">
          <Button size="sm" variant="secondary" disabled={act.isPending} onClick={() => run({ forever: true })}>Keep until changed</Button>
          <Button size="sm" variant="ghost" disabled={act.isPending || !keep} onClick={() => run({ clear: true })}>Back to the usual rules</Button>
        </div>
        <ActionNote action={act} />
      </DialogContent>
    </Dialog>
  )
}

/** What the last action did, or why it didn't. */
export function ActionNote({ action }: { action: { data?: { done: string }; error: unknown; isPending: boolean } }) {
  if (action.isPending) return <span className="text-xs text-muted-foreground">Working…</span>
  if (action.error) {
    return (
      <span role="alert" className="inline-flex items-start gap-1 text-xs text-[var(--status-critical)]">
        <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
        <span className="text-foreground">{action.error instanceof Error ? action.error.message : String(action.error)}</span>
      </span>
    )
  }
  if (action.data) {
    return (
      <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
        <CheckCircle2 className="size-3.5 text-[var(--status-good)]" aria-hidden />{action.data.done}
      </span>
    )
  }
  return null
}

/** A button that asks first. */
export function Confirm({ title, body, confirm, onConfirm, children, destructive = true }: {
  title: string
  body: ReactNode
  confirm: string
  onConfirm: () => void
  children: ReactNode
  destructive?: boolean
}) {
  return (
    <AlertDialog>
      <AlertDialogTrigger asChild>{children}</AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription asChild><div className="space-y-2 text-sm text-muted-foreground">{body}</div></AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction className={destructive ? "bg-destructive text-white hover:bg-destructive/90" : undefined}
            onClick={onConfirm}>{confirm}</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
