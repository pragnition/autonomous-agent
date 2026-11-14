import type { ReactNode } from "react"
import { Card } from "@/components/ui/card"
import { cn } from "@/lib/utils"

export function StatTile({ label, value, sub, className }: {
  label: string
  value: ReactNode
  sub?: ReactNode
  className?: string
}) {
  return (
    <Card className={cn("gap-1 px-4 py-3.5 shadow-none", className)}>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-2xl font-semibold tracking-tight">{value}</div>
      {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
    </Card>
  )
}
