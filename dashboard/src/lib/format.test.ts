import { describe, expect, it } from "vitest"
import { fmtAgo, fmtDuration, fmtPct, fmtReset, fmtTokens, fmtUsd, sumTokens } from "./format"

const NOW = Date.parse("2026-11-13T12:00:00Z")

describe("format", () => {
  it("fmtTokens", () => {
    expect(fmtTokens(0)).toBe("0")
    expect(fmtTokens(999)).toBe("999")
    expect(fmtTokens(1234)).toBe("1.2k")
    expect(fmtTokens(2_500_000)).toBe("2.5M")
    expect(fmtTokens(3_200_000_000)).toBe("3.2B")
    expect(fmtTokens(null)).toBe("—")
  })

  it("fmtUsd", () => {
    expect(fmtUsd(null)).toBe("—")
    expect(fmtUsd(0.0762)).toBe("$0.08")
    expect(fmtUsd(1234.5)).toBe("$1,234.50")
  })

  it("fmtDuration matches the KPI units", () => {
    expect(fmtDuration(45)).toBe("45s")
    expect(fmtDuration(45 * 60)).toBe("45m")
    expect(fmtDuration(3 * 3600 + 5 * 60)).toBe("3h 5m")
    expect(fmtDuration(2 * 86400 + 4 * 3600)).toBe("2d 4h")
    expect(fmtDuration(null)).toBe("—")
  })

  it("fmtAgo", () => {
    expect(fmtAgo("2026-11-13T11:59:30Z", NOW)).toBe("just now")
    expect(fmtAgo("2026-11-13T11:15:00Z", NOW)).toBe("45m ago")
    expect(fmtAgo("2026-11-13T09:00:00Z", NOW)).toBe("3h ago")
    expect(fmtAgo("2026-11-10T12:00:00Z", NOW)).toBe("3d ago")
    expect(fmtAgo(null, NOW)).toBe("—")
  })

  it("fmtPct", () => {
    expect(fmtPct(0.554)).toBe("55%")
    expect(fmtPct(0)).toBe("0%")
    expect(fmtPct(null)).toBe("—")
  })

  it("fmtReset", () => {
    expect(fmtReset(NOW / 1000 + 3 * 3600 + 10 * 60, NOW)).toBe("in 3h 10m")
    expect(fmtReset(NOW / 1000 - 5, NOW)).toBe("now")
    expect(fmtReset(null, NOW)).toBe("—")
  })

  it("sumTokens", () => {
    expect(sumTokens({ in: 1, out: 2, cache_read: 3, cache_write: 4 })).toBe(10)
    expect(sumTokens(null)).toBe(0)
  })
})
