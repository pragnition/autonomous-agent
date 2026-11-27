import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

// Shapes of core's dashboard API (src/agent/dashboard/app.py and queries.py).

export type Tokens = { in: number; out: number; cache_read: number; cache_write: number }
export type SessionState = "queued" | "running" | "done" | "failed" | "cancelled"
export type Source = "slack" | "linear" | "handover" | "schedule" | "other"

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

/** A step of a task's checklist, in Claude Code's own todo-list statuses plus `blocked`. */
export type StepStatus = "pending" | "in_progress" | "completed" | "blocked"
export type Step = { content: string; status: StepStatus }

/** Something the agent needs a person for: a question it asked (it waits for the reply), or an update it flagged. */
export type Attention = { id: number; kind: "question" | "look"; text: string; at: string; slack_url: string | null }

export type BoardState = "running" | "queued" | "waiting" | "parked" | "idle" | "done" | "failed" | "cancelled"

/** One conversation on the Tasks page: its state, its checklist, its latest update and what it needs from people. */
export type BoardTask = {
  conversation_id: string
  title: string
  state: BoardState
  source: Source
  project: string | null
  requester: string | null
  started_at: string
  last_active_at: string
  update: { text: string; at: string } | null
  steps: Step[]
  attention: Attention[]
  slack_url: string | null
  parent: { conversation_id: string; title: string } | null
}

/** A Slack channel's tasks; DMs are one group (`dm`) and conversations outside Slack another (`none`). */
export type BoardChannel = { key: string; label: string; kind: "channel" | "dm" | "none"; tasks: BoardTask[] }

/** The Tasks page: conversations going now or active since `since`, by channel, latest activity first. */
export type TaskBoard = { channels: BoardChannel[]; since: string }

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
  soft_cap: number
  over_soft_cap: boolean
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

export type TaskSource = "slack" | "linear" | "schedule"

export type TaskStep = {
  at: string; by: string | null; asked: string; outcome: string | null; finished: boolean | null
  work_item: string; conversation: string | null
}

// One task from the task record (src/agent/core/task_record.py)
export type TaskRecord = {
  id: string; title: string; source: TaskSource; project: string | null
  requested_by: string | null; followed_up_by: string[]; where: string[]
  started_at: string; ended_at: string | null; work_time_s: number; requests: number
  finished: boolean | null; outcome: string | null; links: string[]; steps: TaskStep[]
}

// Tokens summed over runs (src/agent/core/kpis.py Usage): no Claude cost, which reads as spend
export type Usage = { runs: number; input_tokens: number; output_tokens: number; cache_read_tokens: number
  cache_write_tokens: number }
// The apps' metered model calls: `runs` counts calls, cost_usd is what the provider bills
export type AppUsage = Usage & { cost_usd: number }
export type UsageWindow = { since: string; by_project: Record<string, Usage>; total: Usage
  apps_by_upstream: Record<string, AppUsage> }
export type UsageWindowName = "today" | "7d" | "month"
// Tokens per local day by project (src/agent/core/kpis.py daily_tokens): projects most output first, general last
export type DailyTokens = { projects: string[]; days: { date: string; by_project: Record<string, Usage> }[] }

export type KpiView = {
  kpis: {
    since: string; until: string
    tasks: number; by_source: Record<TaskSource, number>; by_project: Record<string, number>; time: Stat
    finished: number; finished_rate: number | null
    merged: number; issues_closed: number
  }
  daily: { date: string; slack: number; linear: number; schedule: number }[]
  tasks: TaskRecord[]
  usage: Record<UsageWindowName, UsageWindow>
  daily_tokens: DailyTokens
}

export type EventSource = "slack" | "linear" | "dashboard" | "command line" | "agent" | "other"
export type EventRow = { id: number; ts: string; type: string; source: EventSource; sender: string | null }
export type Events = { rows: EventRow[]; types: string[] }

export type LlmModelRow = { upstream: string; model: string; calls: number; errors: number; input_tokens: number;
  cached_tokens: number; cache_write_tokens: number; output_tokens: number; cost_usd: number }
export type LlmRates = { input?: number; cached_input?: number; cache_write?: number; output?: number }
// Where an endpoint's prices come from: what Azure bills on its resource, or agent.toml's list prices
export type LlmPricing =
  | { source: "agent.toml"; error?: string | null }
  | { source: "azure"; resource: string; as_of: string; window_days: number; rates: Record<string, LlmRates>;
      addon: number; error: string | null }
export type LlmMeter = {
  installed: true
  caps: { daily_usd: number | null; monthly_usd: number | null; changed_at_runtime: boolean }
  spend: { today_usd: number; month_usd: number }
  capped: string | null
  upstreams: string[]
  by_model: LlmModelRow[]
  daily: { day: string; calls: number; cost_usd: number }[]
  pricing: Record<string, LlmPricing>
}
export type LlmView = { installed: false } | LlmMeter

export type HostSessionLink = { id: string; title: string; state: SessionState } | null
export type ConsumerKind = "session" | "browser" | "service" | "container" | "runner" | "other" | "host"
export type HostPoint = {
  ts: number; cpu: number; mem_used: number; swap_used: number; rx_bps: number; tx_bps: number
  read_bps: number; write_bps: number; containers: number; ports: number
  cpu_by_kind: Record<string, number>; mem_by_kind: Record<string, number>
}
export type HostVolume = { path: string; total: number; used: number; free: number }
export type HostTotals = {
  available: boolean; stale: boolean; age_s: number | null; ts: number | null
  cpu: { percent: number; per_core: number[]; cores: number; load: number[] } | null
  memory: { total: number; available: number; used: number; swap_total: number; swap_used: number } | null
  network: { rx_bps: number; tx_bps: number } | null
  disk_io: { read_bps: number; write_bps: number } | null
  disk_space: { volumes: HostVolume[]; at: number | null; complete: boolean } | null
  containers: number; ports: number
  history: HostPoint[]
}
export type HostConsumer = {
  key: string; kind: ConsumerKind; name: string; item: string | null; conversation: string | null
  cpu_percent: number; mem_bytes: number; read_bps: number | null; write_bps: number | null
  rx_bps: number | null; tx_bps: number | null; processes: number | null; session: HostSessionLink
}
export type HostContainer = {
  name: string; image: string | null; state: string | null; status: string | null; conversation: string | null
  cpu_percent: number; mem_bytes: number; rx_bps: number | null; tx_bps: number | null
  read_bps: number; write_bps: number; session: HostSessionLink
}
export type HostPort = {
  port: number; address: string; proto: string; pid: number | null; process: string | null; consumer: string
  kind: ConsumerKind; name: string; session: HostSessionLink
}
export type HostDiskRow = { name: string; kind: string; bytes: number; conversation?: string | null
  session: HostSessionLink }
export type HostConsumers = {
  available: boolean; stale: boolean; age_s: number | null
  consumers: HostConsumer[]; containers: HostContainer[]; ports: HostPort[]; disk: HostDiskRow[]
  interfaces: { name: string; rx_bps: number; tx_bps: number }[]
}

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

export const useKpis = (days: number) =>
  useQuery({
    queryKey: ["kpis", days],
    queryFn: () => api.get<KpiView>(`/api/kpis${query({ days })}`),
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

export const useHost = () =>
  useQuery({ queryKey: ["host"], queryFn: () => api.get<HostTotals>("/api/host"), ...live })

export const useHostConsumers = () =>
  useQuery({ queryKey: ["host-consumers"], queryFn: () => api.get<HostConsumers>("/api/host/consumers"), ...live })

export const useLlm = () =>
  useQuery({ queryKey: ["llm"], queryFn: () => api.get<LlmView>("/api/llm"), ...live })

export function useSetCaps() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (caps: { daily_usd?: number; monthly_usd?: number }) => api.post<LlmView>("/api/llm/caps", caps),
    onSuccess: (view) => client.setQueryData(["llm"], view),
  })
}

export const useTaskBoard = () =>
  useQuery({ queryKey: ["tasks"], queryFn: () => api.get<TaskBoard>("/api/tasks"), ...live })

/** Dismiss a flagged update; the board refreshes once the dashboard has recorded it. */
export function useDismiss() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: number) => api.post<{ ok: boolean; closed: boolean }>(`/api/tasks/attention/${id}/dismiss`),
    onSettled: () => client.invalidateQueries({ queryKey: ["tasks"] }),
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

// --- the Containers page (agentd's workloads.rs) ------------------------------------------------------------------

/** An expiry a person set: `until` null means kept until someone changes it. */
export type Keep = { kind: string; target: string; until: string | null; set_by: string | null; set_at: string }
export type Expiry = { at: string | null; why: string; rule?: string }
export type SessionLink = {
  id: string; title: string; state: SessionState; requester: string | null; source: string | null; project: string | null
  created_at: string; ended_at: string | null; links?: { slack: string | null; linear: string | null; pr: string | null }
} | null

export type ConversationBox = {
  conversation: string; name: string; state: string; status: string | null
  created_at: string; last_turn_at: string | null; stopped_at: string | null
  turn_running: boolean; work_item: string | null; listeners: number[]; other_processes: number | null
  cpu_percent: number | null; mem_bytes: number | null; mem_limit: number
  rx_bps: number | null; tx_bps: number | null; read_bps: number | null; write_bps: number | null; processes: number | null
  keep: Keep | null; servers_kept: { port: number; keep: Keep }[]; expires: Expiry; session: SessionLink
}
export type ServiceBox = {
  name: string; cmd: string; cwd: string | null; note: string; pid: number | null; running: boolean
  started: number | null; cpu_percent: number; mem_bytes: number; ports: number[]; processes: number
  log_bytes: number | null; keep: Keep | null; expires: Expiry
}
export type AppContainer = {
  name: string; service: string | null; image: string; state: string; status: string; ports: string; created: string
  running_for: string; cpu_percent: number; mem_bytes: number; mem_limit: number; net_rx: number; net_tx: number
  block_read: number; block_write: number; pids: number | null
}
export type StackBox = {
  name: string; folder: string | null; conversation: string | null; session: SessionLink; running: number; total: number
  cpu_percent: number; mem_bytes: number; ports: number[]; keep: Keep | null; expires: Expiry; containers: AppContainer[]
}
export type SystemBox = {
  name: string; service: string | null; state: string; status: string; image: string; cpu_percent: number | null
  mem_bytes: number | null; processes: number | null; shell: boolean; note: string
}
export type Rules = {
  memory_bytes: number; cpus: number; pids: number; idle_stop_minutes: number; max_age_hours: number
  keep_stopped_days: number; min_free_bytes: number; max_sessions: number
}
export type Workloads = {
  now: string
  host: { cpu: HostTotals["cpu"]; memory: HostTotals["memory"]; disk: HostTotals["disk_space"]; stale: boolean; age_s: number | null }
  rules: Rules
  conversations: ConversationBox[]
  services: { file: string; items: ServiceBox[]; error: string | null }
  stacks: StackBox[] | { error: string }
  stacks_at: string | null
  system: SystemBox[]
}

export type Proc = {
  pid: number; ppid: number; pgid: number; comm: string; state: string; cmd: string; cwd: string | null; threads: number
  started: number | null; cpu_percent: number; cpu_seconds: number; rss: number; pss: number; swap: number
  ports: number[]; log: string | null; err: string | null; read_bytes?: number; write_bytes?: number; uid: number | null
  kind: "init" | "holder" | "claude" | "server" | "process"; in_turn: boolean; kept: boolean
}
export type Cgroup = {
  memory_current: number | null; memory_max: number | null; memory_peak: number | null; anon: number | null
  file: number | null; kernel: number | null; shmem: number | null; oom_kills: number | null; oom_events: number | null
  high_events: number | null; cpu_usage_us: number | null; throttled_us: number | null; cpus: number | null
  pids_current: number | null; pids_max: number | null
}
export type ContainerDetail = {
  summary: ConversationBox
  container: { id: string | null; image: string | null; image_id: string | null; created: string | null
    started_at: string | null; finished_at: string | null; oom_killed: boolean | null; exit_code: number | null
    restart_count: number | null; size_rw: number | null; size_root_fs: number | null; memory_limit: number | null
    nano_cpus: number | null; pids_limit: number | null }
  cgroup: Cgroup | null; processes: Proc[]; processes_error: string | null
  folder: { path: string; bytes: number | null }
  sessions: NonNullable<SessionLink>[]
}
export type ServiceDetail = { name: string; cmd: string; cwd: string | null; note: string; pid: number | null; processes: Proc[] }
export type AppDetail = {
  name: string; image: string; created: string; started_at: string; state: string; restart_count: number
  oom_killed: boolean; exit_code: number; size_rw: number | null; size_root_fs: number | null
  memory_limit: number; nano_cpus: number
  mounts: { type: string; source: string; name: string | null; destination: string }[]
  processes: { pid: number | null; ppid: number | null; cpu_percent: number | null; rss: number | null; elapsed: string; cmd: string }[]
}

export const useWorkloads = () =>
  useQuery({ queryKey: ["workloads"], queryFn: () => api.get<Workloads>("/api/workloads"), ...live })

export const useContainerDetail = (id: string | null) =>
  useQuery({ queryKey: ["workloads", "container", id], enabled: !!id,
    queryFn: () => api.get<ContainerDetail>(`/api/workloads/container/${encodeURIComponent(id ?? "")}`), ...live })

export const useServiceDetail = (name: string | null) =>
  useQuery({ queryKey: ["workloads", "service", name], enabled: !!name,
    queryFn: () => api.get<ServiceDetail>(`/api/workloads/service/${encodeURIComponent(name ?? "")}`), ...live })

export const useAppDetail = (name: string | null) =>
  useQuery({ queryKey: ["workloads", "app", name], enabled: !!name,
    queryFn: () => api.get<AppDetail>(`/api/workloads/app/${encodeURIComponent(name ?? "")}`), ...live })

export type WorkloadAction = {
  kind: "container" | "server" | "process" | "service" | "stack" | "app"
  target: string
  action: "stop" | "start" | "restart" | "remove" | "keep" | "kill"
  hours?: number; forever?: boolean; clear?: boolean; pid?: number; port?: number; signal?: "TERM" | "KILL"
}

export function useWorkloadAction() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (a: WorkloadAction) => api.post<{ ok: true; done: string }>("/api/workloads/action", a),
    onSettled: () => client.invalidateQueries({ queryKey: ["workloads"] }),
  })
}
