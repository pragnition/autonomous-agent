import type { ReactNode } from "react"
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"

// Categorical slots in their fixed, validated order (src/index.css). A ninth series never gets a new hue: callers
// fold the rest into "Other".
export const SERIES = Array.from({ length: 8 }, (_, i) => `var(--series-${i + 1})`)
export const OTHER = "var(--series-other)"
export const MAX_SERIES = 7 // seven named series plus Other

export const seriesColor = (i: number) => SERIES[i % SERIES.length]

export type Series = { key: string; label: string; color: string }

export function Legend({ series }: { series: Series[] }) {
  if (series.length < 2) return null
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      {series.map((s) => (
        <li key={s.key} className="flex items-center gap-1.5">
          <span className="size-2.5 rounded-[3px]" style={{ background: s.color }} aria-hidden />
          {s.label}
        </li>
      ))}
    </ul>
  )
}

type TooltipItem = { dataKey?: string | number; value?: number | string; color?: string; payload?: unknown }

export function ChartTooltip({ active, payload, label, series, format, labelFormat, total = true }: {
  active?: boolean
  payload?: readonly TooltipItem[]
  label?: string | number
  series: Series[]
  format: (v: number) => string
  labelFormat?: (v: string | number) => ReactNode
  total?: boolean
}) {
  if (!active || !payload?.length) return null
  const byKey = new Map(payload.map((p) => [String(p.dataKey), Number(p.value ?? 0)]))
  const rows = series.filter((s) => byKey.has(s.key)).reverse()
  const sum = rows.reduce((acc, s) => acc + (byKey.get(s.key) ?? 0), 0)
  return (
    <div className="min-w-44 rounded-lg border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-md">
      {label != null && <div className="mb-1.5 font-medium">{labelFormat ? labelFormat(label) : label}</div>}
      <ul className="space-y-1">
        {rows.map((s) => (
          <li key={s.key} className="flex items-center gap-2">
            <span className="size-2.5 rounded-[3px]" style={{ background: s.color }} aria-hidden />
            <span className="flex-1 text-muted-foreground">{s.label}</span>
            <span className="tabular font-medium">{format(byKey.get(s.key) ?? 0)}</span>
          </li>
        ))}
      </ul>
      {total && rows.length > 1 && (
        <div className="mt-1.5 flex justify-between border-t pt-1.5">
          <span className="text-muted-foreground">Total</span>
          <span className="tabular font-medium">{format(sum)}</span>
        </div>
      )}
    </div>
  )
}

const axis = { stroke: "var(--chart-axis)", fontSize: 11, tickLine: false, axisLine: false } as const

/** Columns over time, stacked (parts of a whole) or grouped (separate counts): thin bars, 2px surface gaps between
 * segments, a rounded data end, one baseline. */
export function StackedColumns({ data, series, xKey, xFormat, yFormat, labelFormat, height = 240, stacked = true }: {
  data: Record<string, number>[]
  series: Series[]
  xKey: string
  xFormat: (v: number) => string
  yFormat: (v: number) => string
  labelFormat?: (v: string | number) => ReactNode
  height?: number
  stacked?: boolean
}) {
  // The 2px surface gap between stacked segments is drawn as a stroke; on dense ranges a bar is narrower than the
  // gap, so the stroke would erase it.
  const gap = stacked && series.length > 1 && data.length <= 60 ? 2 : 0
  return (
    <div className="space-y-3">
      <Legend series={series} />
      <div style={{ height }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
            barCategoryGap={data.length > 60 ? 0 : "18%"} barGap={2}>
            <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
            <XAxis dataKey={xKey} tickFormatter={xFormat} {...axis} minTickGap={24} />
            <YAxis tickFormatter={yFormat} {...axis} width={52} />
            <Tooltip
              cursor={{ fill: "var(--accent)", opacity: 0.6 }}
              content={(p) => (
                <ChartTooltip active={p.active} payload={p.payload as readonly TooltipItem[]} label={p.label}
                  series={series} format={yFormat} labelFormat={labelFormat ?? ((v) => xFormat(Number(v)))}
                  total={stacked} />
              )}
            />
            {series.map((s, i) => (
              <Bar key={s.key} dataKey={s.key} stackId={stacked ? "stack" : undefined} fill={s.color} maxBarSize={24}
                stroke="var(--card)" strokeWidth={gap}
                radius={!stacked || i === series.length - 1 ? [4, 4, 0, 0] : 0} isAnimationActive={false} />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}

/** A thin meter: blue while comfortable, warning from 80%, critical from 95%; a tick marks a target if given. */
export function Meter({ value, target, label }: { value: number | null; target?: number | null; label: string }) {
  const pct = value == null ? 0 : Math.min(1, Math.max(0, value))
  const fill = value == null ? "transparent"
    : pct >= 0.95 ? "var(--status-critical)" : pct >= 0.8 ? "var(--status-warning)" : "var(--series-1)"
  return (
    <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-[color-mix(in_oklab,var(--series-1)_14%,transparent)]"
      role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={value == null ? undefined : Math.round(pct * 100)}>
      <div className="h-full rounded-full transition-[width]" style={{ width: `${pct * 100}%`, background: fill }} />
      {target != null && (
        <div className="absolute inset-y-0 w-0.5 bg-foreground/70" style={{ left: `calc(${Math.min(1, target) * 100}% - 1px)` }}
          aria-hidden />
      )}
    </div>
  )
}
