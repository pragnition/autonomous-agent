import type { Account } from "@/api"
import { Meter } from "@/components/charts"
import { fmtPct, fmtReset } from "@/lib/format"

/** An account's 5h and 7d windows; the 7d bar carries a tick at its pace target. */
export function UsageBars({ account, now, compact = false }: { account: Account; now: number; compact?: boolean }) {
  const rows = [
    { label: "5h", util: account.util_5h, reset: account.reset_5h, target: null },
    { label: "7d", util: account.util_7d, reset: account.reset_7d, target: account.target_7d },
  ]
  return (
    <div className={compact ? "space-y-1" : "space-y-2"}>
      {rows.map((r) => (
        <div key={r.label} className="grid grid-cols-[1.75rem_1fr_2.75rem] items-center gap-2 text-xs">
          <span className="text-muted-foreground">{r.label}</span>
          <Meter value={r.util} target={r.target} label={`${account.code} ${r.label} used`} />
          <span className="tabular text-right font-medium">{fmtPct(r.util)}</span>
          {!compact && (
            <span className="col-start-2 col-end-4 -mt-1 text-[11px] text-muted-foreground">
              resets {fmtReset(r.reset, now)}
              {r.target != null && ` · on-pace target ${fmtPct(r.target)}`}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}
