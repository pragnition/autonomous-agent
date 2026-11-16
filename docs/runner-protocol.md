# Runner protocol

The contract between the agent container's runner (`src/agent/runner`) and whatever control plane it pulls work
from. Today that control plane is the Python core (`src/agent/core/agent_api.py`); a later Rust control plane
implements the same contract, so the runner, its skills and its prompt carry over unchanged.

## Tokens

Two kinds. The **runner token** is long-lived and pulls work and fetches bundles (`/agent/pull`, `/agent/bundle`).
A **session token** is minted per work item when it's claimed and does everything else for that item
(`/agent/heartbeat`, `/agent/events`, `/agent/tool`, `/agent/files`, `/agent/result`). Finishing, cancelling or
losing an item revokes its session token; a 401 means the item is gone.

## `POST /agent/pull`

Long-polls for up to 25 s; replies `204` when there's nothing claimable. Otherwise:

```json
{
  "work_item": {
    "id": "W-7e1c9f2b3a4d",
    "conversation_id": "c1",
    "kind": "turn",
    "payload": {
      "text": "fix the export",
      "origin": "[from Sam Taylor (sam) · DM · https://team.slack.com/archives/D1/p51]",
      "model": "claude-opus-5-5",
      "system_prompt": "...",
      "max_turns": 400,
      "timeout_s": 5400,
      "mcp": {},
      "resume_session_id": null,
      "project": "sample",
      "test_secrets": {}
    },
    "attempts": 0
  },
  "session_token": "a1b2c3...",
  "tools": [{"name": "status", "description": "d", "input_schema": {}}]
}
```

The runner reads these payload fields: `text` and `origin` (the prompt: the origin line, if any, then any note,
then the text), `model`, `system_prompt`, `max_turns`, `timeout_s`, `mcp` (server definitions, `${VAR}` filled in
from the runner's own env), `resume_session_id` and `test_secrets`. `attempts` is read from the work item itself,
not the payload, to decide whether a restart note is needed (see Leases). Every other field (`project`,
`conversation_id` inside the payload, `kind`, `actor`, ...) is the control plane's own bookkeeping.

## Leases

A claimed item holds a 90 s lease. Every heartbeat and every `/agent/events` call extends it. A lease that runs
out without being extended re-queues the item with `attempts + 1`, up to 5 attempts, after which it's marked
failed. `attempts > 0` together with a `resume_session_id` in the refreshed payload means this attempt is a
restart: the process that was running it died mid-turn, and the item came back around. The runner tells the
session so: the prompt becomes `origin + RESTARTED_NOTE + text` instead of `origin + text`, right after the
origin line and before anything else.

## One running item per conversation

A conversation never has two items claimed at once. A second message that arrives while one is running becomes
the next queued turn of the same conversation — or, once the running turn's heartbeat says it accepts messages,
is handed straight into it (see below) instead of waiting.

## `POST /agent/heartbeat`

Every `heartbeat_s`, the runner posts what the live session looks like right now and gets back whether to stop,
plus any new messages for it:

```json
// request
{"session_id": "s1", "read": ["W-9"], "accepts_messages": true}
// reply
{"cancel": false, "messages": [{"id": "W-2", "payload": {"text": "hi"}}]}
```

`session_id` is the live session's id (`None` before the SDK has assigned or resumed one). `read` lists every
message id this run has read so far, across forks, so the control plane can mark them done even if the item
later ends without a result. `accepts_messages` is true only while an attempt is still reading — not finished,
not told to stop; the control plane also overrides it to `false` once the item's own claim has already been
lost (its lease reaped as dead), so a message is never delivered into a turn that won't make it back around to
reading. A `SessionRevoked` reply (the token was already revoked) is treated as `{"cancel": true}`; any other
error just skips that beat. A `cancel: true` reply interrupts the session and ends the turn. Each delivered
message is written into the live session with `claude.send(run_id, item_id, origin + MIDTURN_NOTE + text)`.

This is the delivered → read → done lifecycle: a message handed to a running turn is `delivered`; once the CLI
echoes it back, the runner's next heartbeat (or its final result) lists it under `read` and the control plane
marks it `done`. A delivered item the turn never reads — because the turn ends first — goes back to the queue
as soon as that turn's result (or a lost lease) completes it, to be read by whichever turn runs the conversation
next. That released message runs as its own turn, with its own carry-on count starting back at 0 — it never
inherits the count of the turn it had been delivered into. And the control plane's delivery step itself never
hands over a carry-on (a queued turn whose payload says it's one): a carry-on waits behind whatever unread
message is ahead of it in the same conversation, and only starts once its own turn comes up, instead of being
folded into a turn that's already running.

## `POST /agent/events`, `POST /agent/tool`, `POST /agent/files`, `GET /agent/bundle`

- **`/agent/events`**: one SDK message per call, `{"event": {...}}`; stored to the run's transcript and extends
  the lease. Reply: `{"cancel": false}`.
- **`/agent/tool`**: an SDK tool call, `{"name": "status", "args": {"x": 1}}`, forwarded to the control plane's
  own tool registry. Reply: `{"text": "ran status"}`.
- **`/agent/files`**: multipart upload (a `text` field plus up to 10 `file` parts) to post to the conversation's
  thread. Reply: `{"ok": true, "posted": true}` (`posted` is `false` when there's no thread to post into).
- **`/agent/bundle`**: `GET`, runner token only. Replies with the plugins tarball (gzip bytes) and an
  `X-Bundle-Version` header the runner caches by.

## `POST /agent/result`

Every field the runner sends:

```json
{
  "result": {
    "outcome": "done",
    "text": "The PR is up.",
    "session_id": "sess-1",
    "turns": 3,
    "input_tokens": 1200,
    "output_tokens": 340,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "cost_usd_est": 0.5,
    "account_code": "acct-3",
    "forks": 0,
    "error": null,
    "read": ["W-2", "W-3"]
  }
}
```

`read` is handled first and on its own: before the item completes, the control plane marks each listed id done,
so a message this turn answered inline is never requeued even if the on-beat bookkeeping that usually does the
same thing failed or never ran. The reply is always `{"ok": true}`.

`outcome` is one of:
- **`done`**: the turn finished. The control plane posts `text` to the thread.
- **`timeout` / `max_turns`**: the turn ran out of time or steps. If it's under its carry-on cap (100 for a
  request, 3 for a scheduled run), the control plane carries on silently — nothing is posted, and a new turn is
  queued that resumes the session and says "carry on from where you stopped." Once the cap is spent, it's
  treated as a failure instead.
- **`error`**: never carries on. The session is dropped (the next turn starts fresh) and the failure, with
  `error`'s text, is posted.
- **`interrupted`**: never carries on either, but the session is kept (a later turn can still resume it); the
  failure is posted as "stopped before I finished it."
