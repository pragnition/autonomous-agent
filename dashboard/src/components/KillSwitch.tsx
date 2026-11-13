import { AlertTriangle, OctagonX, Play } from "lucide-react"
import { useAction, useMe, useOverview } from "@/api"
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle, AlertDialogTrigger } from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { fmtAgo } from "@/lib/format"

const title = (name: string) => name.charAt(0).toUpperCase() + name.slice(1)

function Failed({ error }: { error: unknown }) {
  if (!error) return null
  return (
    <span role="alert" className="inline-flex items-center gap-1 text-xs font-medium text-critical">
      <AlertTriangle className="size-3.5" aria-hidden />
      <span className="text-foreground">Didn't work: {error instanceof Error ? error.message : String(error)}</span>
    </span>
  )
}

/** Stop everything (anyone), or resume (admins). Both go through a confirmation. */
export function KillSwitch({ size = "sm" }: { size?: "sm" | "default" }) {
  const { data: overview } = useOverview()
  const { data: me } = useMe()
  const stop = useAction<{ ok: boolean; cancelled: string[] }>("/api/stop")
  const resume = useAction("/api/resume")
  if (!overview) return null

  if (overview.paused) {
    const by = overview.paused_by
    const button = (
      <Button size={size} variant="outline" disabled={!me?.can_resume || resume.isPending}>
        <Play className="size-4" aria-hidden /> Resume
      </Button>
    )
    return (
      <div className="flex items-center gap-2">
        <Failed error={resume.error} />
        {by?.actor && (
          <span className="hidden text-xs text-muted-foreground lg:inline">
            Stopped by {by.actor} {fmtAgo(by.at)}
          </span>
        )}
        {me?.can_resume ? (
          <AlertDialog>
            <AlertDialogTrigger asChild>{button}</AlertDialogTrigger>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Resume {title(me.agent)}?</AlertDialogTitle>
                <AlertDialogDescription>
                  Queued messages start running again, and new work is picked up as it arrives.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Keep paused</AlertDialogCancel>
                <AlertDialogAction onClick={() => resume.mutate()}>Resume</AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        ) : (
          <Tooltip>
            <TooltipTrigger asChild><span tabIndex={0}>{button}</span></TooltipTrigger>
            <TooltipContent>Only an admin can resume</TooltipContent>
          </Tooltip>
        )}
      </div>
    )
  }

  const running = overview.counts.running
  return (
    <div className="flex items-center gap-2">
      <Failed error={stop.error} />
      <AlertDialog>
        <AlertDialogTrigger asChild>
          <Button size={size} variant="destructive" disabled={stop.isPending}>
            <OctagonX className="size-4" aria-hidden /> Stop everything
          </Button>
        </AlertDialogTrigger>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Stop {me ? title(me.agent) : "the agent"}?</AlertDialogTitle>
            <AlertDialogDescription>
              This pauses all new work and cancels {running === 1 ? "the 1 session" : `the ${running} sessions`} running
              now. Queued messages wait until an admin resumes. Your name is recorded in the activity log.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction variant="destructive" onClick={() => stop.mutate()}>
              Stop everything
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
