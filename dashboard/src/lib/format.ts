import type { Tokens } from "@/api"

const DASH = "—"

export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return DASH
  const abs = Math.abs(n)
  for (const [unit, size] of [["B", 1e9], ["M", 1e6], ["k", 1e3]] as const) {
    if (abs >= size) return `${(n / size).toFixed(1).replace(/\.0$/, "")}${unit}`
  }
  return String(n)
}

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 2 })

export function fmtUsd(x: number | null | undefined): string {
  return x == null ? DASH : usd.format(x)
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null) return DASH
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`
}

export function secondsBetween(fromIso: string, to: number = Date.now()): number {
  return (to - Date.parse(fromIso)) / 1000
}

export function fmtAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return DASH
  const s = secondsBetween(iso, now)
  if (s < 60) return "just now"
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`
  return `${Math.floor(s / 86400)}d ago`
}

export function fmtPct(x: number | null | undefined): string {
  return x == null ? DASH : `${Math.round(x * 100)}%`
}

export function fmtReset(epoch: number | null | undefined, now: number = Date.now()): string {
  if (epoch == null) return DASH
  const s = epoch - now / 1000
  return s <= 0 ? "now" : `in ${fmtDuration(s)}`
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return DASH
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}

export function fmtDateTime(iso: string | null | undefined): string {
  if (!iso) return DASH
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  })
}

export function sumTokens(t: Tokens | null | undefined): number {
  return t ? t.in + t.out + t.cache_read + t.cache_write : 0
}
