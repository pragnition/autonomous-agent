import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

// Shapes of core's dashboard API (src/agent/dashboard/app.py and queries.py).

export type Tokens = { in: number; out: number; cache_read: number; cache_write: number }
export type SessionState = "queued" | "running" | "done" | "failed" | "cancelled"
export type Source = "slack" | "linear" | "handover" | "other"

export type SessionRow = {
  id: string
  kind: string
  state: SessionState
  project: string | null
  conversation_id: string | null
  task_id: string | null
  title: string
  message: string
  source: Source
  requester: string | null
  created_at: string
  started_at: string | null
  ended_at: string | null
  account: string | null
  account_name: string | null
  turns: number | null
  tokens: Tokens | null
  outcome: string | null
  error: string | null
  live: boolean
  links: { slack: string | null; linear: string | null; pr: string | null }
}

export type Entry = {
  id: string
  ts: string | null
  kind: "text" | "tool_call" | "tool_result" | "result" | "rate_limit" | "raw"
  role?: "assistant" | "user"
  text?: string
  tool?: string
  tool_use_id?: string
  summary?: string
  input?: Record<string, unknown>
  is_error?: boolean
  content?: string
  turns?: number | null
  usage?: Tokens
  subtype?: string | null
}

export type Me = { login: string; name: string; person: string | null; role: string | null; can_resume: boolean;
  agent: string }

export type Overview = {
  paused: boolean
  paused_by: { actor: string | null; at: string | null } | null
  running: (SessionRow & { recent: Entry[] })[]
  queued: SessionRow[]
  counts: { running: number; queued: number; done_today: number; failed_today: number }
}

export type SessionDetail = SessionRow & { entries: Entry[]; offset: number; line: number }

/** A conversation: every message in it is a turn (a SessionRow) that resumes the same Claude session. */
export type ConversationRow = Omit<SessionRow, "message"> & {
  messages: number
  busy_s: number
  last_active_at: string
}

/** `items` are its turns, oldest first (a row's own `turns` counts model calls). */
export type Conversation = ConversationRow & { items: SessionRow[]; focus: string | null }

export type Pace = "ahead" | "on" | "behind"

export type Account = {
  code: string
  name: string | null
  email: string | null
  status: string
  rank: number | null
  verified: boolean
  active: number
  util_5h: number | null
  reset_5h: number | null
  util_7d: number | null
  reset_7d: number | null
  pace: Pace | null
  target_7d: number | null
  pace_delta: number | null
  cap_5h: number | null
  tokens_60m: Tokens
  agent_60m: Tokens
  people_60m: Tokens
  by_origin: Record<string, Tokens>
  errors_60m: { "429": number; limit: number; "5xx": number; upstream_error: number }
  latency_60m: { requests: number; ttft_p50: number | null; ttft_p90: number | null; tok_s: number | null }
}

export type AccountsView = {
  available: boolean
  error: string | null
  agent_origins: string[]
  pool: {
    used_7d: number | null
    target_7d: number | null
    delta: number | null
    state: Pace | null
    counts: { ahead: number; on: number; behind: number }
    unknown: number
    tokens_60m: Tokens
    burn: { today: Tokens; week: Tokens; since: number | null }
    hourly: [number, number | null][]
    session_burn: number | null
  } | null
  accounts: Account[]
  origins: { name: string; is_agent: boolean; active: number; tokens_60m: Tokens; requests_60m: number }[]
}

export type HistoryBucket = {
  t: number
  by_account: Record<string, Tokens>
  by_origin: Record<string, Tokens>
  agent: Tokens
  people: Tokens
  requests: number
  errors: number
}

export type History = {
  hours: number
  bucket_s: number
  accounts: string[]
  origins: string[]
  buckets: HistoryBucket[]
  names?: Record<string, string>
  error?: string
}

export type Stat = { n: number; median_s: number | null; p90_s: number | null }

export type KpiView = {
  kpis: {
    since: string; until: string; project: string | null
    pickup: Stat; to_pr: Stat; to_merge: Stat; queue_wait: Stat
    tasks: number; prs: number; merged: number; closed: number; reverted: number; failed: number; cancelled: number
    merge_rate: number | null
  }
  daily: { date: string; tasks: number; prs: number; merged: number }[]
  projects: string[]
}

export type EventSource = "slack" | "linear" | "dashboard" | "command line" | "agent" | "other"
export type EventRow = { id: number; ts: string; type: string; source: EventSource; sender: string | null }
export type Events = { rows: EventRow[]; types: string[] }

export type LlmModelRow = { upstream: string; model: string; calls: number; errors: number; input_tokens: number;
  cached_tokens: number; output_tokens: number; cost_usd: number }
export type LlmMeter = {
  installed: true
  caps: { daily_usd: number | null; monthly_usd: number | null; changed_at_runtime: boolean }
  spend: { today_usd: number; month_usd: number }
  capped: string | null
  upstreams: string[]
  by_model: LlmModelRow[]
  daily: { day: string; calls: number; cost_usd: number }[]
}
export type LlmView = { installed: false } | LlmMeter

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, init)
  const body = resp.headers.get("content-type")?.includes("json") ? await resp.json() : await resp.text()
  if (!resp.ok) throw new ApiError(resp.status, (body as { error?: string })?.error ?? String(body))
  return body as T
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Dashboard": "1" },
      body: JSON.stringify(body ?? {}),
    }),
}

const LIVE = 5_000
const SLOW = 60_000
const live = { refetchInterval: LIVE, refetchIntervalInBackground: false } as const

export function query(params: Record<string, string | number | null | undefined>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v != null && v !== "") q.set(k, String(v))
  const s = q.toString()
  return s ? `?${s}` : ""
}

export const useMe = () => useQuery({ queryKey: ["me"], queryFn: () => api.get<Me>("/api/me"), staleTime: SLOW })

export const useOverview = () =>
  useQuery({ queryKey: ["overview"], queryFn: () => api.get<Overview>("/api/overview"), ...live })

export type SessionFilter = { state?: string; project?: string; source?: string; account?: string; q?: string;
  limit?: number; offset?: number }

export const useConversations = (f: SessionFilter) =>
  useQuery({
    queryKey: ["conversations", f],
    queryFn: () => api.get<{ rows: ConversationRow[]; total: number }>(`/api/conversations${query(f)}`),
    placeholderData: keepPreviousData,
    ...live,
  })

/** A conversation named by its id or one of its turns', polled; each turn's timeline is loaded once by
 * useSessionEntries and then streamed. */
export const useConversation = (id: string) =>
  useQuery({ queryKey: ["conversation", id], queryFn: () => api.get<Conversation>(`/api/conversations/${id}`), ...live })

export const useSessionEntries = (id: string) =>
  useQuery({
    queryKey: ["session-entries", id],
    queryFn: () => api.get<SessionDetail>(`/api/sessions/${id}`),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })

export const useAccounts = () =>
  useQuery({ queryKey: ["accounts"], queryFn: () => api.get<AccountsView>("/api/accounts"), ...live })

export const useHistory = (hours: number) =>
  useQuery({
    queryKey: ["history", hours],
    queryFn: () => api.get<History>(`/api/accounts/history?hours=${hours}`),
    placeholderData: keepPreviousData,
    refetchInterval: SLOW,
    refetchIntervalInBackground: false,
  })

export const useKpis = (days: number, project?: string) =>
  useQuery({
    queryKey: ["kpis", days, project ?? ""],
    queryFn: () => api.get<KpiView>(`/api/kpis${query({ days, project })}`),
    placeholderData: keepPreviousData,
    refetchInterval: SLOW,
    refetchIntervalInBackground: false,
  })

/** One page of the event log: the newest page refreshes itself; older pages (`before` set) don't change. */
export const useEvents = (type?: string, before?: number) =>
  useQuery({
    queryKey: ["events", type ?? "", before ?? null],
    queryFn: () => api.get<Events>(`/api/events${query({ type, before, limit: 100 })}`),
    placeholderData: keepPreviousData,
    ...(before == null ? live : { staleTime: Infinity }),
  })

export const useLlm = () =>
  useQuery({ queryKey: ["llm"], queryFn: () => api.get<LlmView>("/api/llm"), ...live })

export function useSetCaps() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (caps: { daily_usd?: number; monthly_usd?: number }) => api.post<LlmView>("/api/llm/caps", caps),
    onSuccess: (view) => client.setQueryData(["llm"], view),
  })
}

/** Stop, resume and cancel: on success, everything that shows state refreshes at once. */
export function useAction<T = unknown>(path: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: () => api.post<T>(path),
    onSettled: () => {
      for (const key of ["overview", "conversations", "conversation", "events"]) client.invalidateQueries({ queryKey: [key] })
    },
  })
}
