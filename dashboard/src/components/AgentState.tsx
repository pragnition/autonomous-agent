import { useOverview } from "@/api"
import { cn } from "@/lib/utils"

/** Running or paused, as a small pill; the dot is never the only cue. */
export function AgentState({ className }: { className?: string }) {
  const { data } = useOverview()
  if (!data) return null
  const paused = data.paused
  return (
    <span className={cn("inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium",
      paused ? "border-critical/40 text-critical" : "text-foreground", className)}>
      <span className={cn("size-2 rounded-full", paused ? "bg-critical" : "bg-good animate-pulse")} aria-hidden />
      {paused ? "Paused" : data.counts.running ? `Running · ${data.counts.running} active` : "Running · idle"}
    </span>
  )
}
