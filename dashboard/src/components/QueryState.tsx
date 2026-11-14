import { AlertTriangle } from "lucide-react"
import type { ReactNode } from "react"
import { ApiError } from "@/api"
import { Skeleton } from "@/components/ui/skeleton"

/** Loading and error states for one query; renders children only once there is data. */
export function QueryState<T>({ query, children, rows = 3 }: {
  query: { data: T | undefined; error: unknown; isLoading: boolean }
  children: (data: T) => ReactNode
  rows?: number
}) {
  if (query.data !== undefined) return <>{children(query.data)}</>
  if (query.error) return <ErrorNote error={query.error} />
  return (
    <div className="space-y-2">
      {Array.from({ length: rows }, (_, i) => <Skeleton key={i} className="h-10 w-full" />)}
    </div>
  )
}

export function ErrorNote({ error }: { error: unknown }) {
  const text = error instanceof ApiError
    ? error.status === 403 ? `Not allowed: ${error.message}. Open the dashboard through Tailscale.` : error.message
    : String(error)
  return (
    <div className="flex items-start gap-2 rounded-lg border border-dashed px-4 py-3 text-sm text-muted-foreground">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-serious" aria-hidden />
      <span>{text}</span>
    </div>
  )
}
