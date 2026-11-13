import { describe, expect, it } from "vitest"
import type { History, HistoryBucket, Tokens } from "@/api"
import { agentPeopleHourly, toSeries } from "./history"

const t = (i = 0, o = 0, cr = 0, cw = 0): Tokens => ({ in: i, out: o, cache_read: cr, cache_write: cw })

function bucket(ts: number, agent: Tokens, people: Tokens): HistoryBucket {
  return { t: ts, by_account: {}, by_origin: {}, agent, people, requests: 0, errors: 0 }
}

describe("agentPeopleHourly", () => {
  it("sums 5-minute buckets into hours, keeping empty hours", () => {
    const h: History = { hours: 24, bucket_s: 300, accounts: [], origins: [], buckets: [
      bucket(3600, t(1, 1), t(0, 2)),
      bucket(3900, t(2), t()),
      bucket(7200, t(), t()),
      bucket(10800, t(0, 0, 5), t(1)),
    ] }
    expect(agentPeopleHourly(h)).toEqual([
      { t: 3600, agent: 4, people: 2 },
      { t: 7200, agent: 0, people: 0 },
      { t: 10800, agent: 5, people: 1 },
    ])
  })

  it("is empty without buckets", () => {
    expect(agentPeopleHourly({ hours: 24, bucket_s: 300, accounts: [], origins: [], buckets: [] })).toEqual([])
  })
})

function hist(buckets: Partial<HistoryBucket>[], accounts: string[], origins: string[] = []): History {
  return {
    hours: 6, bucket_s: 300, accounts, origins,
    buckets: buckets.map((b, i) => ({ t: i * 300, by_account: {}, by_origin: {}, agent: t(), people: t(), requests: 0,
      errors: 0, ...b })),
  }
}

describe("toSeries", () => {
  it("series_by_account_has_every_bucket", () => {
    const h = hist([{ by_account: { a: t(1) } }, {}, { by_account: { b: t(0, 2) } }], ["a", "b"])
    const { data, series } = toSeries(h, "account")
    expect(data).toEqual([{ t: 0, a: 1, b: 0 }, { t: 300, a: 0, b: 0 }, { t: 600, a: 0, b: 2 }])
    expect(series.map((s) => s.key)).toEqual(["a", "b"])
  })

  it("series_by_kind_sums_accounts", () => {
    const h = hist([{ by_account: { a: t(1, 2, 3, 4), b: t(10, 20, 30, 40) } }], ["a", "b"])
    expect(toSeries(h, "kind").data).toEqual([{ t: 0, in: 11, out: 22, cache_read: 33, cache_write: 44 }])
    expect(toSeries(h, "kind").series.map((s) => s.label)).toEqual(["Input", "Output", "Cache read", "Cache write"])
  })

  it("series_by_origin_keys", () => {
    const h = hist([{ by_origin: { host: t(1), "agent-sandbox": t(2) } }], [], ["agent-sandbox", "host"])
    const { data, series } = toSeries(h, "origin")
    expect(series.map((s) => s.key)).toEqual(["agent-sandbox", "host"])
    expect(data).toEqual([{ t: 0, "agent-sandbox": 2, host: 1 }])
  })

  it("folds accounts past seven into Other, keeping the biggest by total", () => {
    const names = ["a", "b", "c", "d", "e", "f", "g", "h", "i"]
    const by_account = Object.fromEntries(names.map((n, i) => [n, t(i + 1)]))
    const { data, series } = toSeries(hist([{ by_account }], names), "account")
    expect(series.map((s) => s.key)).toEqual(["c", "d", "e", "f", "g", "h", "i", "other"])
    expect(data[0].other).toBe(1 + 2)
    expect(series.at(-1)?.label).toBe("Other")
  })
})
