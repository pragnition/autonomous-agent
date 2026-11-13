import { describe, expect, it } from "vitest"
import type { History, HistoryBucket, Tokens } from "@/api"
import { agentPeopleHourly } from "./history"

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
