# Long Projects Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Jensen takes a spec and ships the product over days. Its requests keep running, it survives restarts, it reads Slack messages mid-turn, and it works through a skill built on Superpowers. It works in the repo the request names and merges its own PRs there.

**Architecture:** There's no new service. Carry-ons become silent and plentiful for requests, and stay capped for scheduled runs. The runner's 15-second heartbeat now carries the live session id and the messages that arrived mid-turn, in both directions. The runner writes those messages into the running Claude Code session and tracks which ones the model has read through `--replay-user-messages`. That protocol is written down for the control-plane phase. Core keeps a table of long projects and lists the active ones in every session's prompt. Any session passes a change to a project's build with `tell_project`, which queues it in the build's conversation. Memory becomes a folder of one-fact files plus an index, which the runner adds to every prompt. The project workflow is a core skill, `long-project`.

**Tech Stack:** Python 3.12, aiohttp, SQLite, claude-agent-sdk 0.2.180 (bundled Claude Code 2.1.321), pytest in the dev image.

**Spec:** `docs/superpowers/specs/2026-11-16-long-projects-design.md`

## Global Constraints

- Run tests with `just test <path>`, which uses the dev image. Run the full suite with `just test`.
- Core's only new tools are the three project tools in Task 6. They hold state only core has. No gates or pipeline steps; Jensen's workflow lives in skills.
- Core posts nothing in Slack on Jensen's behalf except real failures. Reactions are fine.
- Commit messages follow the repo's style (`feat: …` / `fix: …` as a plain sentence). No co-author trailer and no mention of Claude Code.
- The repo is public, so commit no secrets, tokens or `.env` content.
- Wherever docs or prompt text describe Jensen, it is a fully autonomous engineering agent, never a "coding agent".
- Docs and prompt text name no meetings or people. Core code and the core prompt never name a specific project.

## Review Focus

1. **Two messages sent seconds apart into a busy thread** must both reach the session in order, both be marked read, and both get ✅. Owned by Task 4 (`test_two_messages_are_both_read_in_order`).
2. **A runner restart while a delivered message is still unread.** The message goes back to the queue, is delivered again to the retried turn, and is answered once. Owned by Task 2 (`test_a_lost_lease_releases_its_unread_messages`).
3. **A change passed to a build that is mid-turn** must reach that running build at its next step. It must not start a second session on the same project. A change passed to an idle build starts it at once. Owned by Task 6 (`test_a_change_for_a_running_build_is_delivered_at_its_next_step`, `test_a_change_from_another_conversation_reaches_an_idle_build`).
4. **A scheduled run that keeps hitting its limit** (a hung feedback triage every 30 minutes) must stop after 3 carry-ons, not 100, or its runs pile up and fill the session slots. Owned by Task 1 (`test_a_scheduled_run_carries_on_at_most_three_times`).
5. **A message arrives while the runner can't take messages** (between attempts, or while a session is resuming). It stays queued with no ⏳, and a later beat delivers it. Owned by Task 3 (`test_a_runner_that_cannot_take_messages_gets_none`).

---

### Task 1: Carry-ons are silent; requests get 100, scheduled runs keep 3

**Files:**
- Modify: `src/agent/config.py:66` (`Limits`) and `src/agent/config.py:300-305` (parsing `[limits]`)
- Modify: `src/agent/core/conversations.py` (`on_result`: the cap, and the `if continuing:` text)
- Test: `tests/test_conversations.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Limits.continuations: int = 100`
  - `Limits.schedule_continuations: int = 3`, parsed from `[limits] schedule_continuations`
- In `on_result`, a turn whose `payload["source"] == "schedule"` carries on while `step < limits.schedule_continuations`. Every other turn uses `limits.continuations`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_conversations.py`, replace `test_turn_that_hits_a_limit_carries_on_by_itself` with:

```python
async def test_turn_that_hits_a_limit_carries_on_silently(convos, queue, slack, conn):
    await convos.handle_slack(mention("t40", "build it"))
    first = queue.claim()
    posts, reactions = len(slack.posts), len(slack.reactions)
    result = {"outcome": "timeout", "error": "no result within 5400 s", "session_id": "sess-1"}
    queue.complete(first.id, result)
    await convos.on_result(queue.get(first.id), result)
    assert len(slack.posts) == posts            # no "Still on it"
    assert len(slack.reactions) == reactions    # the message keeps its ⏳
    nxt = queue.claim()
    assert nxt.conversation_id == first.conversation_id and nxt.payload["continuation"] == 1
    assert nxt.payload["text"] == ("(Your last turn hit its 90-minute limit before you finished. Carry on from where "
                                   "you stopped.)")
    assert conn.execute("SELECT session_id FROM conversations WHERE id = ?",
                        (first.conversation_id,)).fetchone()[0] == "sess-1"
```

In `test_carrying_on_stops_after_the_configured_number_of_times`, set `convos.config.limits.continuations = 3` first and replace the `startswith("Still on it…")` assertion with `assert len(slack.posts) == posts_before` (record `posts_before = len(slack.posts)` right after intake). The final failure reply assertion stays as it is.

Add, using the file's `scheduled()` helper (around line 702):

```python
async def test_a_scheduled_run_carries_on_at_most_three_times(convos, queue, slack):
    convos.config.limits.continuations = 100
    await convos.start_scheduled(scheduled())
    for n in range(4):
        item = queue.claim()
        assert item.payload.get("continuation", 0) == n and item.payload["source"] == "schedule"
        queue.complete(item.id, {"outcome": "timeout"})
        await convos.on_result(queue.get(item.id), {"outcome": "timeout"})
    assert queue.claim() is None
    assert "didn't finish" in slack.posts[-1]["text"]      # the schedule's existing failure report
```

In `tests/test_config.py`, add `test_continuations_default_to_one_hundred_and_three_for_schedules`. A minimal valid config with no carry-on settings in `[limits]` loads with `config.limits.continuations == 100` and `config.limits.schedule_continuations == 3`. Setting `schedule_continuations = 0` in `[limits]` loads as 0.

- [ ] **Step 2: Run them to make sure they fail**

Run: `just test tests/test_conversations.py tests/test_config.py -k "carries_on or carrying_on or continuations"`
Expected: FAIL (a "Still on it" post is made; the default is 3; `schedule_continuations` doesn't exist)

- [ ] **Step 3: Implement**

Set the defaults to 100 and 3, and parse `schedule_continuations` with `_int`, like the others. In `on_result`, choose the cap by source. A continuing turn sets `text = ""`, so nothing is posted. Keep `limit_name` for the carry-on prompt.

- [ ] **Step 4: Run them to make sure they pass**

Run: `just test tests/test_conversations.py tests/test_config.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/config.py src/agent/core/conversations.py tests/test_conversations.py tests/test_config.py
git commit -m "feat: a turn that hits a limit carries on silently, up to 100 times for requests and 3 for scheduled runs"
```

---

### Task 2: The queue hands a running turn its conversation's messages, and survives more restarts

**Files:**
- Modify: `src/agent/db.py` (add `delivered_into TEXT` to `work_items` in `SCHEMA`, plus `("work_items", "delivered_into", "TEXT")` in `ADDED_COLUMNS`)
- Modify: `src/agent/core/sessions.py` (`WorkQueue`)
- Modify: `src/agent/dashboard/queries.py:81` (`_state`)
- Test: `tests/test_sessions.py`, `tests/test_db.py`, `tests/test_dashboard_queries.py`

**Interfaces:**
- Produces, on `WorkQueue`:
  - `__init__(conn, lease_s: int = 90, max_attempts: int = 5)`
  - `deliver(conversation_id: str, into: str) -> list[WorkItem]`: returns the queued `turn` items of that conversation other than `into`, oldest first, and sets them to status `'delivered'` with `delivered_into = into`.
  - `mark_read(item_ids: list[str], into: str) -> list[WorkItem]`: of those ids, the ones still `'delivered'` into `into` become `'done'` with result `{"outcome": "done", "text": "", "read_by": into}`. Returns the items it changed. It's idempotent.
  - `release(into: str) -> list[str]`: every item still `'delivered'` into `into` goes back to `'queued'` with `delivered_into = NULL`. Returns their ids.
  - `complete(item_id, result)`, `cancel(item_id)` and `_reap` (for every item it re-queues or fails) call `release(item_id)`.
- Status `'delivered'` is never claimable. The `_CLAIMABLE` filter already admits only `'queued'` and expired `'claimed'` items.

- [ ] **Step 1: Write the failing tests** (in `tests/test_sessions.py`, using its existing `conn` fixture and enqueue/claim style)

```python
def test_deliver_hands_over_a_busy_conversations_queued_turns(conn):
    q = WorkQueue(conn)
    a = q.enqueue("V1", "turn", {"text": "build it"}); q.claim()
    b = q.enqueue("V1", "turn", {"text": "how's it going?"})
    c = q.enqueue("V1", "turn", {"text": "also add dark mode"})
    d = q.enqueue("V2", "turn", {"text": "other thread"})
    assert [i.id for i in q.deliver("V1", a)] == [b, c]
    assert {q.get(b).status, q.get(c).status} == {"delivered"}
    assert q.deliver("V1", a) == []
    assert q.claim().id == d                     # delivered items are never claimed

def test_read_messages_are_done_once(conn):
    q = WorkQueue(conn)
    a = q.enqueue("V1", "turn", {}); q.claim()
    b = q.enqueue("V1", "turn", {}); q.deliver("V1", a)
    assert [i.id for i in q.mark_read([b], a)] == [b]
    assert q.get(b).status == "done" and q.get(b).result["read_by"] == a
    assert q.mark_read([b], a) == [] and q.mark_read([b], "W-other") == []

def test_unread_messages_go_back_to_the_queue_when_the_turn_completes(conn):
    q = WorkQueue(conn)
    a = q.enqueue("V1", "turn", {}); q.claim()
    b = q.enqueue("V1", "turn", {}); c = q.enqueue("V1", "turn", {})
    q.deliver("V1", a); q.mark_read([b], a)
    q.complete(a, {"outcome": "timeout"})
    assert q.get(c).status == "queued" and q.claim().id == c

def test_unread_messages_go_back_to_the_queue_when_the_turn_is_cancelled(conn):
    # same shape as above, with q.cancel(a) instead of complete
    ...

def test_a_lost_lease_releases_its_unread_messages(conn):
    q = WorkQueue(conn)
    a = q.enqueue("V1", "turn", {}); q.claim()
    b = q.enqueue("V1", "turn", {}); q.deliver("V1", a)
    q.reap(datetime.now(UTC) + timedelta(seconds=200))
    assert q.get(a).status == "queued" and q.get(b).status == "queued"
    assert q.claim().id == a                     # the turn runs again first; b is delivered to it again
    assert [i.id for i in q.deliver("V1", a)] == [b]

def test_a_turn_survives_four_lost_leases(conn):
    q = WorkQueue(conn)
    a = q.enqueue("V1", "turn", {})
    later = datetime.now(UTC)
    for n in range(4):
        q.claim(later); later += timedelta(seconds=200); q.reap(later)
        assert q.get(a).status == "queued"
    q.claim(later); q.reap(later + timedelta(seconds=200))
    assert q.get(a).status == "failed"
```

Change the existing `test_expired_lease_requeues_then_fails` to build its queue with `WorkQueue(conn, max_attempts=2)` so it keeps testing the same thing.

In `tests/test_db.py`, add `test_work_items_gain_delivered_into`: create a `work_items` table without the column, run `db.create_schema`, and assert `delivered_into` is in `PRAGMA table_info(work_items)`.

In `tests/test_dashboard_queries.py`, add `test_a_delivered_item_shows_as_running`: `assert queries._state("delivered") == "running"`.

- [ ] **Step 2: Run them to make sure they fail**

Run: `just test tests/test_sessions.py tests/test_db.py tests/test_dashboard_queries.py`
Expected: FAIL (`deliver` not defined; the column is missing; `_state` returns "delivered")

- [ ] **Step 3: Implement**

Add the three methods and the new default. `deliver` runs under `BEGIN IMMEDIATE`, like `claim`. Have `_reap` call `release` for each expired id. Map `'delivered'` to `"running"` in `_state`.

- [ ] **Step 4: Run them to make sure they pass**

Run: `just test tests/test_sessions.py tests/test_db.py tests/test_dashboard_queries.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/db.py src/agent/core/sessions.py src/agent/dashboard/queries.py tests/test_sessions.py tests/test_db.py tests/test_dashboard_queries.py
git commit -m "feat: the queue can hand a running turn its conversation's new messages, and a turn survives four restarts"
```

---

### Task 3: Core: each heartbeat saves the live session and hands over new messages

**Files:**
- Modify: `src/agent/core/agent_api.py` (`make_agent_app`, `heartbeat`, `result`)
- Modify: `src/agent/core/conversations.py` (new `on_beat`)
- Modify: `src/agent/core/app.py:166` (pass `on_beat=self.conversations.on_beat`)
- Test: `tests/test_agent_api.py`, `tests/test_conversations.py`

**Interfaces:**
- Consumes: `WorkQueue.deliver`, `WorkQueue.mark_read` (Task 2)
- Produces:
  - `make_agent_app(..., on_beat: Callable[[WorkItem, dict], Awaitable[dict]] | None = None)`
  - `POST /agent/heartbeat` takes the body `{"session_id": str | None, "read": [item_id], "accepts_messages": bool}`. It answers `{"cancel": bool, "messages": [{"id": str, "payload": dict}]}`. Core passes `accepts_messages` to `on_beat` only when the lease is still alive. An exception from `on_beat` is logged and answered with `"messages": []`.
  - `POST /agent/result`: when the result has `"read": [ids]`, core calls `on_beat(item, {"read": ids})` before `queue.complete`.
  - `Conversations.on_beat(item: WorkItem, beat: dict) -> dict`:
    1. If there's a `session_id` and the item has a conversation, save it with `UPDATE conversations SET session_id = ?`.
    2. Mark `beat["read"]` read with `queue.mark_read(..., item.id)`. For each changed item with a `message_ts`, react `DONE`, replacing `WORKING`.
    3. If `beat["accepts_messages"]`, call `queue.deliver(item.conversation_id, item.id)`. For each delivered item with a `message_ts`, react `WORKING`, replacing `SEEN`.
    4. Return `{"messages": [{"id": i.id, "payload": i.payload} for i in delivered]}` (an empty list when nothing was delivered).

- [ ] **Step 1: Write the failing tests**

`tests/test_conversations.py`:

```python
async def test_a_message_to_a_busy_thread_is_handed_to_the_running_turn(convos, queue, slack, conn):
    await convos.handle_slack(mention("t50", "build the app"))
    first = queue.claim()
    await convos.handle_slack(reply("t50", "how's it going?"))
    out = await convos.on_beat(first, {"session_id": "live-1", "read": [], "accepts_messages": True})
    [msg] = out["messages"]
    assert msg["payload"]["text"] == "how's it going?" and queue.get(msg["id"]).status == "delivered"
    assert slack.reactions[-1]["name"] == "hourglass_flowing_sand"
    assert convo(conn, "t50")["session_id"] == "live-1"     # a restarted turn resumes this session

async def test_a_read_message_is_done_and_ticked(convos, queue, slack):
    await convos.handle_slack(mention("t51", "build the app"))
    first = queue.claim()
    await convos.handle_slack(reply("t51", "status?"))
    [msg] = (await convos.on_beat(first, {"accepts_messages": True}))["messages"]
    await convos.on_beat(first, {"read": [msg["id"]], "accepts_messages": True})
    assert queue.get(msg["id"]).status == "done"
    assert slack.reactions[-1]["name"] == "white_check_mark"

async def test_an_unread_message_runs_before_the_carry_on(convos, queue):
    await convos.handle_slack(mention("t52", "build the app"))
    first = queue.claim()
    await convos.handle_slack(reply("t52", "status?"))
    [msg] = (await convos.on_beat(first, {"accepts_messages": True}))["messages"]
    result = {"outcome": "timeout", "session_id": "live-1"}
    queue.complete(first.id, result)
    await convos.on_result(queue.get(first.id), result)
    assert queue.claim().id == msg["id"]

async def test_a_runner_that_cannot_take_messages_gets_none(convos, queue, slack):
    await convos.handle_slack(mention("t53", "build the app"))
    first = queue.claim()
    await convos.handle_slack(reply("t53", "status?"))
    reactions = len(slack.reactions)
    assert (await convos.on_beat(first, {"accepts_messages": False}))["messages"] == []
    assert len(slack.reactions) == reactions
    assert (await convos.on_beat(first, {"accepts_messages": True}))["messages"] != []
```

`tests/test_agent_api.py`: pass a recording `on_beat` through `_client(..., on_beat=...)`.

```python
async def test_heartbeat_passes_the_beat_and_returns_messages(conn, queue, parts):
    beats = []
    async def on_beat(item, beat):
        beats.append((item.id, beat)); return {"messages": [{"id": "W-2", "payload": {"text": "hi"}}]}
    # claim an item through /agent/pull, then:
    # POST /agent/heartbeat {"session_id": "s1", "read": ["W-9"], "accepts_messages": True}
    # -> {"cancel": False, "messages": [{"id": "W-2", "payload": {"text": "hi"}}]}
    # beats[-1][1] == {"session_id": "s1", "read": ["W-9"], "accepts_messages": True}

async def test_heartbeat_of_a_cancelled_turn_takes_no_messages(conn, queue, parts):
    # cancel the item, then heartbeat with accepts_messages True:
    # response cancel is True and on_beat saw accepts_messages False

async def test_heartbeat_survives_a_failing_on_beat(conn, queue, parts):
    # on_beat raises -> response {"cancel": False, "messages": []}, status 200

async def test_result_marks_read_messages_before_completing(conn, queue, parts):
    # on_beat records queue.get(item.id).status at call time
    # POST /agent/result {"result": {"outcome": "done", "read": ["W-2"]}}
    # -> on_beat called with {"read": ["W-2"]} while the item was still "claimed"
```

The existing `test_heartbeat_reports_cancel` keeps passing; an empty body means "no beat".

- [ ] **Step 2: Run them to make sure they fail**

Run: `just test tests/test_conversations.py tests/test_agent_api.py -k "beat or busy_thread or read_message or unread or cannot_take or marks_read"`
Expected: FAIL (`on_beat` doesn't exist)

- [ ] **Step 3: Implement** `Conversations.on_beat` and the two handler changes as specified in Interfaces. Wire `on_beat` in `app.py`.

- [ ] **Step 4: Run them to make sure they pass**

Run: `just test tests/test_conversations.py tests/test_agent_api.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/core/agent_api.py src/agent/core/conversations.py src/agent/core/app.py tests/test_agent_api.py tests/test_conversations.py
git commit -m "feat: the heartbeat saves a turn's live session and hands it the messages that arrived while it runs"
```

---

### Task 4: The Claude runner writes messages into the live session and knows which ones were read

**Files:**
- Modify: `src/agent/runner/claude.py` (`ClaudeRunner`)
- Modify: `src/agent/runner/base.py` (`RunResult.read`)
- Test: `tests/test_claude_runner.py`

**Interfaces:**
- Produces:
  - `RunResult.read: list[str] = field(default_factory=list)`: the ids of the messages the session read, in order.
  - `ClaudeRunner.accepting(run_id: str) -> bool`: true while an attempt of that run is reading and hasn't finished.
  - `ClaudeRunner.session_of(run_id: str) -> str | None`: the live attempt's session id, known from its start. It's the pre-assigned uuid, or the resumed id.
  - `ClaudeRunner.read_of(run_id: str) -> list[str]`: the ids read so far in this run, across attempts. It doesn't drain.
  - `ClaudeRunner.send(run_id: str, item_id: str, text: str) -> bool`: returns False unless `accepting`. Otherwise it records a fresh `uuid_factory()` uuid as pending for `item_id` and writes `{"type": "user", "uuid": <uuid>, "message": {"role": "user", "content": text}, "parent_tool_use_id": None}` through `client.query(<async iterable of that one dict>)`.
- Behavior:
  - `build_options` adds `extra_args={"replay-user-messages": None}`.
  - `_attempt` iterates `client.receive_messages()`.
  - A `UserMessage` whose `uuid` is pending moves that item id to the run's read list.
  - A `ResultMessage` ends the attempt only when nothing is pending. Otherwise reading goes on to the next result.
  - Accepting stops at the break and in `finally`.
  - A new attempt (a fork) starts with nothing pending; its reads are added to the same run list.
  - `run` returns `read` on the result and forgets the run's list.

- [ ] **Step 1: Write the failing tests**

Update `FakeClient`:
- Rename `receive_response` to `receive_messages`.
- `query(prompt)` appends a `str` to `queries` as before. For an async iterable, it appends each dict to `self.sent` and sets `self._sent` (an `asyncio.Event`).
- Add the script sentinel `WAIT_SENT`, which awaits `self._sent` and then clears it.
- Add the helper `replay(c) -> UserMessage(content=c.sent[-1]["message"]["content"], uuid=c.sent[-1]["uuid"])`.

```python
async def test_options_ask_the_cli_to_echo_messages(tmp_path):
    runner, _ = make_runner(tmp_path, [[]])
    assert runner.build_options(spec(tmp_path), session_id="s").extra_args == {"replay-user-messages": None}

async def test_a_message_sent_mid_turn_reaches_the_session_and_is_read(tmp_path):
    runner, clients = make_runner(tmp_path, [[assistant, WAIT_SENT, replay, result()]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    assert runner.session_of("R1") == "00000000-0000-4000-8000-000000000001"
    assert await runner.send("R1", "W-2", "how's it going?")
    out = await task
    assert clients[0].sent[0]["message"]["content"] == "how's it going?" and clients[0].sent[0]["uuid"]
    assert out.read == ["W-2"] and out.outcome == "done"
    assert not runner.accepting("R1") and not await runner.send("R1", "W-3", "late")

async def test_the_turn_keeps_reading_until_every_sent_message_is_read(tmp_path):
    runner, _ = make_runner(tmp_path, [[assistant, WAIT_SENT, result(num_turns=1), replay, result(num_turns=7)]])
    task = asyncio.create_task(runner.run(spec(tmp_path)))
    await wait_until(lambda: runner.accepting("R1"))
    await runner.send("R1", "W-2", "also say hi")
    out = await task
    assert out.read == ["W-2"] and out.turns == 7

async def test_two_messages_are_both_read_in_order(tmp_path):
    # script: assistant, WAIT_SENT, WAIT_SENT, replay_first, replay_second, result()
    # (replay_first and replay_second echo c.sent[0] and c.sent[1])
    # send W-2 then W-3 -> out.read == ["W-2", "W-3"]

async def test_a_fork_does_not_report_an_unread_message(tmp_path):
    # mirror test_fork_on_403_then_success:
    # attempt 1: assistant, WAIT_SENT, then that test's 403 result (no replay ever comes)
    # attempt 2: assistant, result()
    # send W-2 during attempt 1 -> out.read == [] and out.forks == 1
```

`wait_until(cond)` is a tiny helper in the test module: poll every 0.01 s for up to 2 s.

- [ ] **Step 2: Run them to make sure they fail**

Run: `just test tests/test_claude_runner.py`
Expected: FAIL (no `send`/`accepting`; `extra_args` empty)

- [ ] **Step 3: Implement** as in Interfaces. Import `UserMessage` from `claude_agent_sdk`. Keep the per-run read lists in `self._read: dict[str, list[str]]`.

- [ ] **Step 4: Run them to make sure they pass**

Run: `just test tests/test_claude_runner.py`
Expected: PASS, including every existing test in the file

- [ ] **Step 5: Commit**

```bash
git add src/agent/runner/claude.py src/agent/runner/base.py tests/test_claude_runner.py
git commit -m "feat: the runner writes new messages into a running session and keeps reading until each one is read"
```

---

### Task 5: The worker's heartbeat carries the beat, and a restarted turn knows it was restarted

**Files:**
- Modify: `src/agent/runner/link.py` (`CoreLink.heartbeat`)
- Modify: `src/agent/runner/worker.py` (`MIDTURN_NOTE`, `RESTARTED_NOTE`, `_heartbeat`, `handle`, `_prepare`)
- Create: `docs/runner-protocol.md`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `ClaudeRunner.accepting/session_of/read_of/send` and `RunResult.read` (Task 4); the heartbeat body and reply (Task 3).
- Produces:
  - `CoreLink.heartbeat(tok: str, beat: dict) -> dict`: posts `beat` and returns the JSON reply.
  - `Runner._heartbeat`: every `heartbeat_s`, it sends `{"session_id": claude.session_of(id), "read": claude.read_of(id), "accepts_messages": claude.accepting(id)}`.
    - `SessionRevoked` counts as `{"cancel": True}`. Any other error skips that beat.
    - `cancel` interrupts and returns.
    - For each message, it calls `claude.send(id, m["id"], origin_of(m["payload"]) + MIDTURN_NOTE + m["payload"]["text"])`.
  - The result sent to core gains `"read": result.read`.
  - `_prepare`: when `item.get("attempts")` and `payload.get("resume_session_id")`, the prompt is `origin + RESTARTED_NOTE + text`.
- Exact copy:

```python
MIDTURN_NOTE = ("(A new message arrived while you're working. Answer it now with post_update, or a Linear thought in a "
                "Linear session, because your final reply only posts when this whole turn ends. Then carry on, and "
                "change course if it asks you to.)\n\n")
RESTARTED_NOTE = ("(Your machine restarted partway through this turn. Your session picks up where it stopped: check "
                  "what you had already done, then carry on. The message you were working on:)\n\n")
```

- [ ] **Step 1: Write the failing tests** (in `tests/test_runner.py`)

Update the fakes:
- `FakeLink.heartbeat(tok, beat)` records `beat` in `self.beats` and returns `{"cancel": self.cancel, "messages": self.messages.pop(0) if self.messages else []}`. `self.messages` is a list of per-beat message lists.
- `FakeClaude` gains `accepting` (True while `run` is in progress), `session_of` (returns `"live-1"`), `read_of` (returns `self.read`), and `send`, which records `(run_id, item_id, text)` in `self.sent` and returns True.
- `FakeClaude.run` returns `RunResult(..., read=list(self.read))`.

```python
async def test_heartbeat_reports_the_live_session_and_what_was_read(tmp_path):
    link, claude = FakeLink(), FakeClaude(hang=True)
    claude.read = ["W-2"]
    # handle(item()) in a task; wait for one beat; interrupt
    # link.beats[0] == {"session_id": "live-1", "read": ["W-2"], "accepts_messages": True}

async def test_messages_from_the_heartbeat_go_into_the_session(tmp_path):
    link, claude = FakeLink(), FakeClaude(hang=True)
    link.messages = [[{"id": "W-2", "payload": {"text": "how's it going?", "origin": "[from Sam (sam) · DM · <link>]"}}]]
    # after a beat:
    # claude.sent == [("W-V-1", "W-2", "[from Sam (sam) · DM · <link>]\n" + MIDTURN_NOTE + "how's it going?")]

async def test_result_reports_every_message_the_session_read(tmp_path):
    # FakeClaude whose run returns read=["W-2", "W-3"] -> link.results[-1]["read"] == ["W-2", "W-3"]

async def test_a_restarted_turn_is_told_and_resumes(tmp_path):
    it = item(resume_session_id="live-1", origin="[from bob · DM]")
    it["attempts"] = 1
    # handle(it) -> claude.specs[0].prompt == "[from bob · DM]\n" + RESTARTED_NOTE + "fix the export"
    # and claude.specs[0].resume_session_id == "live-1"

async def test_a_first_attempt_gets_no_restart_note(tmp_path):
    # attempts 0 with resume_session_id set -> the prompt has no RESTARTED_NOTE
```

Update `test_cancel_via_heartbeat_interrupts` and `test_revoked_token_interrupts` for the new `heartbeat(tok, beat)` signature. Update the link test at `tests/test_runner.py:237` so it posts `{"session_id": None, "read": [], "accepts_messages": False}` and returns the dict.

- [ ] **Step 2: Run them to make sure they fail**

Run: `just test tests/test_runner.py`
Expected: FAIL

- [ ] **Step 3: Implement** as in Interfaces.

- [ ] **Step 4: Run the whole suite**

Run: `just test`
Expected: PASS, every test

- [ ] **Step 5: Write `docs/runner-protocol.md`**

This is the contract between the runner and whatever control plane it pulls from. A later control plane implements it so the runner, the skill and the prompt carry over unchanged. Write it from the code as it now stands, one section each:
- **Tokens:** the runner token for pull and bundle; a session token per work item for everything else.
- **`POST /agent/pull`:** long-poll for up to 25 s; 204 when there's nothing. The reply holds `work_item` (`id`, `conversation_id`, `kind`, `payload`, `attempts`), `session_token` and `tools`. Also list the payload fields the runner reads.
- **Leases:** 90 s. Every heartbeat and event extends it. A lost lease re-queues the item, up to 5 attempts. `attempts > 0` with a `resume_session_id` means "restarted": the prompt gets `RESTARTED_NOTE`.
- **One running item per conversation.**
- **`POST /agent/heartbeat`:** the body and reply from Task 3, and the delivered → read → done lifecycle. A delivered item that is never read goes back to the queue when its turn ends.
- **`POST /agent/events`**, **`/agent/tool`**, **`/agent/files`**, **`GET /agent/bundle`:** one line each on what they carry.
- **`POST /agent/result`:** every field the runner sends, including `read`, and the outcomes (`done`, `error`, `timeout`, `max_turns`, `interrupted`) with what core does for each: reply, carry on (and the two caps), or post the failure.

Give each endpoint a JSON example taken from the tests. Keep it under 150 lines.

- [ ] **Step 6: Commit**

```bash
git add src/agent/runner/link.py src/agent/runner/worker.py tests/test_runner.py docs/runner-protocol.md
git commit -m "feat: the runner's heartbeat carries the live session and new messages, a restarted turn resumes where it stopped, and the runner protocol is written down"
```

---

### Task 6: Projects that core knows about, and changes for a project's build from any conversation

A long project's build runs in one conversation. Core keeps track of each project's name, repo and build conversation. Every session sees the active projects in its prompt. `tell_project` puts a change into the build's queue, sent by the real person who asked, and Tasks 2–5 deliver it into the running build at its next step.

**Files:**
- Modify: `src/agent/db.py` (a `projects` table in `SCHEMA`)
- Create: `src/agent/core/projects.py` (table helpers and `register_project_tools`)
- Modify: `src/agent/core/app.py` (call `register_project_tools(self.router)` next to `register_kpi_tools`)
- Modify: `src/agent/core/payloads.py` (`active_projects`; `{active_projects}` in `system_prompt`; the prompt re-rendered in `refresh_turn`)
- Modify: `src/agent/defaults/system.md` (the active-projects lines)
- Test: `tests/test_projects.py` (new, using the fixtures `tests/test_agent_tools.py` uses), `tests/test_payloads.py`

**Interfaces:**
- Consumes: `AgentToolRouter.register`, `.turn_payload`, `.queue` and `.slack`; `WorkQueue.deliver` (Task 2)
- Produces:
  - The table:
    ```sql
    CREATE TABLE IF NOT EXISTS projects (
        name TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        conversation_id TEXT NOT NULL,
        thread_link TEXT,
        started_by TEXT,
        status TEXT NOT NULL DEFAULT 'active',   -- active | done
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    ```
  - In `src/agent/core/projects.py`:
    - `start(conn, *, name, repo, conversation_id, started_by, thread_link) -> None`: insert, or update the row and set it active.
    - `get(conn, name) -> sqlite3.Row | None`
    - `active(conn) -> list[sqlite3.Row]`: sorted by name.
    - `finish(conn, name) -> None`
    - `build_state(conn, conversation_id) -> str`: `"running"` when the conversation has a claimed turn, `"queued"` when it has a queued one, else `"idle"`.
  - `register_project_tools(router) -> None` registers three tools, all at tier `"act"`:
    - **`start_project {name, repo}`.** "Register this conversation as the build of a long project, so every session sees it and can pass it changes."
      - The name must match `^[a-z0-9][a-z0-9-]{1,40}$`.
      - It fails with `this conversation can't run a project` when there's no conversation.
      - It fails with `<name> is already being built in <thread_link>` when another conversation's build holds that name and is active.
      - `thread_link` comes from `router.slack.chat_getPermalink(channel=ctx.channel, message_ts=ctx.thread_ts)`. It's `None` when there's no channel or the call fails.
      - Reply: `<name> registered; every session now sees it.`
    - **`finish_project {name}`.** "Mark a long project done." It works only from the project's own conversation; anywhere else it replies `only <name>'s build can finish it`. Reply: `<name> marked done.`
    - **`tell_project {name, message}`.** "Pass a change or a question to a project's build. It arrives as a message from the person you're talking to."
      - Unknown name: `no project named <name>; active: <names>`.
      - Called from the build itself: `this is <name>'s build; act on it here.`
      - Otherwise it enqueues a turn in the project's conversation:
        - start from `router.turn_payload(conversation_id, message, ctx.actor, False)`
        - `origin`: the asking turn's own `payload["origin"]`
        - `source`: the asking turn's source, else `"slack"`
        - `requester`: the asking turn's requester, else `ctx.actor`
        - `follows`: `ctx.work_item_id`
        - no `message_ts`, so there are no reactions
      - A done project goes back to active.
      - Reply: `Passed to <name>'s build.`, then either ` It's running, so it reads this at its next step.` or ` It was idle and starts on this now.`, then ` Thread: <thread_link>`.
  - `Payloads.active_projects() -> str`: one line per active project, `- {name}: https://github.com/{repo}, thread {thread_link or "(none)"}, build {build_state}`, or `- none right now`.
  - `Payloads.system_prompt()` also replaces `{active_projects}`. `turn` and `refresh_turn` both set `system_prompt` through one helper, `_full_prompt()`, which is the system prompt plus the persona. So a turn sees the projects as they are when it starts, not when it was queued. Update the `refresh_turn` docstring.
- The `system.md` lines go right after the `{projects}` block:
  ```markdown
  - Projects you're building, each in its own build conversation:
  {active_projects}
    When someone anywhere asks for a change to one of them, settle what they want, then pass it to its build with tell_project. Answer questions about them yourself. The long-project skill has the details.
  ```

- [ ] **Step 1: Write the failing tests**

`tests/test_projects.py`:

```python
async def test_start_project_registers_this_conversation_as_the_build(router):
    build = ctx_for(router, actor="ann")
    reply = await router.call(build, "start_project", {"name": "roster-demo", "repo": "pragnition/roster-demo"})
    assert "registered" in reply.text
    [p] = projects.active(router.conn)
    assert (p["name"], p["repo"], p["conversation_id"], p["started_by"]) == (
        "roster-demo", "pragnition/roster-demo", build.conversation_id, "ann")

async def test_a_name_another_build_holds_is_refused(router):
    await router.call(ctx_for(router), "start_project", {"name": "roster-demo", "repo": "o/r"})
    reply = await router.call(ctx_for(router), "start_project", {"name": "roster-demo", "repo": "o/r"})
    assert "already being built" in reply.text

async def test_a_change_from_another_conversation_reaches_an_idle_build(router):
    build = ctx_for(router, actor="ann")
    await router.call(build, "start_project", {"name": "roster-demo", "repo": "o/r"})
    side_id = add_item(router.queue, add_conversation(router.conn, channel="D-bob"), actor="bob",
                       origin="[from Bob (bob) · DM · https://slack/x]", source="slack", requester="bob")
    side = router.context_for(side_id)
    reply = await router.call(side, "tell_project", {"name": "roster-demo", "message": "add xyz to the admin page"})
    item = latest_item(router)
    assert item.conversation_id == build.conversation_id and item.payload["text"] == "add xyz to the admin page"
    assert item.payload["origin"] == "[from Bob (bob) · DM · https://slack/x]"
    assert (item.payload["requester"], item.payload["follows"]) == ("bob", side_id)
    assert "message_ts" not in item.payload
    assert "starts on this now" in reply.text

async def test_a_change_for_a_running_build_is_delivered_at_its_next_step(router):
    build = ctx_for(router, actor="ann")
    await router.call(build, "start_project", {"name": "roster-demo", "repo": "o/r"})
    running = router.queue.claim()            # the build's own turn
    reply = await router.call(ctx_for(router, actor="bob"), "tell_project",
                              {"name": "roster-demo", "message": "add xyz"})
    assert "next step" in reply.text
    assert [i.payload["text"] for i in router.queue.deliver(build.conversation_id, running.id)] == ["add xyz"]

async def test_an_unknown_project_lists_the_active_ones(router): ...      # "no project named x; active: roster-demo"
async def test_the_build_cannot_tell_itself(router): ...                  # "this is roster-demo's build"
async def test_only_the_build_finishes_its_project(router): ...           # another ctx gets the refusal; the build's ctx marks it done
```

In `tests/test_payloads.py`:

```python
def test_a_turn_sees_the_projects_active_when_it_starts(payloads, conn):
    cid = add_conversation(conn)
    queued = payloads.turn(row(conn, cid), "x", "bob")
    assert "- none right now" in queued["system_prompt"]
    projects.start(conn, name="roster-demo", repo="pragnition/roster-demo", conversation_id=cid,
                   started_by="ann", thread_link="https://slack/t")
    started = payloads.refresh_turn(row(conn, cid), queued)
    assert ("- roster-demo: https://github.com/pragnition/roster-demo, thread https://slack/t, build idle"
            in started["system_prompt"])
    assert "tell_project" in started["system_prompt"]
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `just test tests/test_projects.py tests/test_payloads.py`
Expected: FAIL (no `projects` module, no table, no placeholder)

- [ ] **Step 3: Implement** as in Interfaces.

- [ ] **Step 4: Run them to make sure they pass**

Run: `just test tests/test_projects.py tests/test_payloads.py tests/test_agent_tools.py tests/test_db.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/db.py src/agent/core/projects.py src/agent/core/app.py src/agent/core/payloads.py src/agent/defaults/system.md tests/test_projects.py tests/test_payloads.py
git commit -m "feat: core keeps the long projects being built, every session sees them, and any conversation can pass a change to a project's build"
```

---

### Task 7: Memory is a folder of facts with an index, not a growing CLAUDE.md

Today Jensen appends what it learns to `~/.claude/CLAUDE.md`, which loads in full into every session. The folder the runner set up for memory (`/var/agent/memory`) has never been used. The cause is the runner's own system prompt: it replaces Claude Code's, along with Claude Code's memory instructions. This task gives memory a fixed shape:
- one file per fact
- a `MEMORY.md` index with one line per fact
- the rules and the index added to every session's system prompt by the runner, which knows the folder

**Files:**
- Modify: `src/agent/runner/worker.py` (`MEMORY_RULES`, `memory_block`, `_prepare`)
- Modify: `src/agent/defaults/system.md` (the first "Memory and context" bullet)
- Test: `tests/test_runner.py`, `tests/test_payloads.py`

**Interfaces:**
- `memory_block(memory_dir: Path, max_lines: int = 200) -> str`: `MEMORY_RULES` with `{dir}` filled in, then the index.
  - The index is the first `max_lines` lines of `memory_dir / "MEMORY.md"`.
  - A longer index is followed by `(… <n> more lines not shown: the index is too long, so tidy it)`.
  - A missing or empty index shows as `(empty)`.
- In `_prepare`, when `self.memory_dir` is set, the session's system prompt is `payload["system_prompt"] + "\n\n" + memory_block(self.memory_dir)`. The `autoMemoryDirectory` setting stays.
- Exact copy:

```python
MEMORY_RULES = """# Memory

Your memory is the folder {dir}, shared by all your sessions. Its index, MEMORY.md, is below; read a fact's file when its line matters to what you're doing.

- One fact per file, named for it (`people-ivan-replies.md`, `project-roster-demo.md`), starting with:
  ---
  name: <short-kebab-name>
  description: <one line: what it is and when it matters>
  ---
  then the fact, with who said it, when and the link.
- Add one line for it to MEMORY.md: `- [Title](file.md) — hook`. The index holds pointers, never the facts.
- Before you save, look for a file that already covers it and update that one. When a fact turns out wrong, fix or delete its file and its line.
- Save what you'd want next time: what people want and how, decisions, conventions, gotchas, where things are. Not what the code, git or the task record already says.
- Never write what you learn into CLAUDE.md.

## MEMORY.md
"""
```

- The new first bullet of `system.md`'s "Memory and context" section, word for word:
  `- Your memory is a folder shared by all your sessions; the Memory section at the end of this prompt says how it works. Save what's worth knowing next time there (decisions, preferences, conventions, gotchas), never in CLAUDE.md, with who, when and the link.`

- [ ] **Step 1: Write the failing tests**

`tests/test_runner.py`:

```python
def test_memory_block_carries_the_rules_and_the_index(tmp_path):
    (tmp_path / "MEMORY.md").write_text("- [Reply style](people-ivan-replies.md) — short replies\n")
    text = memory_block(tmp_path)
    assert str(tmp_path) in text and "One fact per file" in text and "people-ivan-replies.md" in text

def test_an_empty_memory_says_so(tmp_path):
    assert "(empty)" in memory_block(tmp_path)

def test_a_long_index_is_cut_and_says_how_much(tmp_path):
    (tmp_path / "MEMORY.md").write_text("".join(f"- line {i}\n" for i in range(250)))
    text = memory_block(tmp_path, max_lines=200)
    assert "- line 199" in text and "- line 200" not in text and "50 more lines" in text

async def test_every_session_gets_the_memory_block(tmp_path):
    # make_runner(..., memory_dir=tmp_path / "memory") with a one-line MEMORY.md; handle(item())
    # claude.specs[0].system_prompt starts with "SYS" and contains that line and "One fact per file"
```

In `tests/test_payloads.py`, `test_shipped_system_prompt_says_how_memory_and_context_work` swaps the phrase `"Claude Code memory is shared by all your sessions"` for `"memory is a folder shared by all your sessions"` and `"never in CLAUDE.md"`.

- [ ] **Step 2: Run them to make sure they fail**

Run: `just test tests/test_runner.py tests/test_payloads.py`
Expected: FAIL

- [ ] **Step 3: Implement** as in Interfaces.

- [ ] **Step 4: Run them to make sure they pass**

Run: `just test tests/test_runner.py tests/test_payloads.py`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agent/runner/worker.py src/agent/defaults/system.md tests/test_runner.py tests/test_payloads.py
git commit -m "feat: memory is a folder of one-fact files with an index every session sees, not a growing CLAUDE.md"
```

---

### Task 8: The prompt and the long-project skill

**Files:**
- Modify: `src/agent/defaults/system.md` (two lines under "How you work:")
- Create: `src/agent/plugins/core/skills/long-project/SKILL.md`
- Test: `tests/test_payloads.py`, `tests/test_bundles.py`

**Interfaces:**
- Consumes: the existing `schedules`, `browser-testing`, `pr-description` and `review-before-pr` skills; the `post_update` and `post_files` tools.

- [ ] **Step 1: Write the failing tests**

`tests/test_payloads.py`:

```python
def test_shipped_system_prompt_sends_long_work_to_the_skill_and_lets_it_merge(payloads):
    text = payloads.system_prompt()
    assert "long-project skill" in text
    assert "Merge your own PRs once the tests and CI pass" in text
```

`tests/test_bundles.py`: add `"long-project"` to the names `test_core_skills_present` expects. Then add:

```python
def test_long_project_skill_runs_on_superpowers_and_an_hourly_schedule():
    text = (ROOT / "src/agent/plugins/core/skills/long-project/SKILL.md").read_text()
    for phrase in ("superpowers@anthropic-plugin-directory", "subagent-driven-development", "writing-plans",
                   '"0 * * * *"', "gh pr checks --watch", "gh pr merge", "memory note",
                   "When the team changes direction", "From another conversation", "start_project",
                   "tell_project", "finish_project"):
        assert phrase in text
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `just test tests/test_payloads.py tests/test_bundles.py`
Expected: FAIL

- [ ] **Step 3: Add the two prompt lines.** Put them right after the line that starts "- Start a new branch for each piece of work":

```markdown
- Merge your own PRs once the tests and CI pass and your review found nothing blocking.
- A spec, or any work that will take more than a few hours: use the long-project skill. Your turn carries on by itself when it hits a limit or your machine restarts, so keep going until it's done.
```

- [ ] **Step 4: Create the skill** with exactly this content:

````markdown
---
name: long-project
description: Use when someone hands you a spec or asks for a whole product, app or service to be built (work that takes hours or days), and whenever anyone, anywhere, asks about or asks for a change to a project you're building.
---

# Building a project from a spec

You build the whole thing yourself, over as many hours or days as it takes, with nobody driving. Your turn carries on by itself when it hits a time or step limit and after a restart, so keep working until the last milestone is merged and verified. Stop early only when you need something only a person can give (a secret, access, a decision about what they want), and say exactly what.

## Start

1. **Superpowers.** Check `claude plugin list` for superpowers. If it's missing, run `claude plugin install superpowers@anthropic-plugin-directory`. Its skills load from your next session; until then, read them from the plugin's folder under `~/.claude/plugins/cache/`.
2. **Repo.** Work in the repo the request names, and clone it into this conversation's folder.
3. **Spec.** The spec may be a message, a file in the thread (PDF, Word) or a link. Read all of it. Commit the original and a Markdown copy under `docs/` if they aren't in the repo yet.
4. **Plan.** Read the repo's CLAUDE.md, its roadmap and its existing plans first, so your milestones fit what's already there. Then use superpowers:writing-plans:
   - Split the work into milestones. Each one is something a person can run and see.
   - Give each milestone its own plan, in the folder where the repo keeps its plans (`docs/plans/` if it has none).
   - Settle the questions the skills would ask you yourself, and post what you assumed in the thread.
5. **Register it.** Call `start_project` with a short name and the repo. Every session, in any conversation, then sees the project in its prompt and can pass you changes.
6. **Memory note.** Save a memory file `project-<name>.md` with:
   - this conversation's folder (`pwd`), where your clone lives
   - the plan files and where the ledger lives
   - the progress schedule's name

   Sessions answering questions about the project read it next to core's project list.
7. **Hourly progress.** Add a schedule named `<project>-progress` (schedules skill) with `cron = "0 * * * *"`, using the thread's channel as its channel. Write its prompt for a fresh session:
   - Read the project's memory note.
   - Fetch the repo and check out the newest pushed commit of the milestone in progress (main when none is).
   - Run the app on a free port and screenshot its main screens (browser-testing).
   - Post the screenshots in the project thread with two or three lines: what merged since the last update, what's being built now, and what's next, taken from the ledger and `git log`. A scheduled run has no thread of its own, so upload with Slack's `files.getUploadURLExternal` and `files.completeUploadExternal` using `$SLACK_BOT_TOKEN`, the channel and the thread's `thread_ts`.
   - Skip the post when nothing changed since the last one.
8. **Kickoff.** Post a short message in the thread: the milestones, and when the first one should land.

## Each milestone

1. Branch from main, named the way the repo names its branches.
2. Run its plan with superpowers:subagent-driven-development: a fresh implementer subagent and a reviewer for each task, with the ledger in the repo's `.superpowers/sdd/`. Push the branch after every task, so the hourly run and anyone else can see it.
3. Test it end to end in a real browser (browser-testing) and run the full suite.
4. Open the PR (review-before-pr, pr-description). Wait for CI with `gh pr checks --watch` and fix what fails. Then merge it with `gh pr merge --squash --delete-branch` (or the repo's own style), and check that main still builds and runs.
5. Post in the thread what landed, with the PR link and the screenshots (`post_files`).

## When someone asks how it's going

Messages that arrive mid-turn reach you between steps. Answer at once with post_update: what's merged (PR links), what's in progress, what's next and anything blocked. Take it from the ledger, `git log` and `gh pr list`, not from what you remember.

## When the team changes direction

A message that changes the spec or the priorities beats the plan, whether it was posted in the thread or passed on from another conversation. At the next step:

1. Let the task in flight finish, or stop it. Don't merge work the change makes wrong.
2. Commit the change to the spec in `docs/`, with who asked and the link.
3. Rewrite the plans for the milestones that aren't merged yet. Close PRs and delete branches the change makes obsolete.
4. Update the memory note, and the progress schedule's prompt if it names what changed.
5. Post in the thread what changes, what's dropped and the new milestones.

## From another conversation

People talk to you about a project anywhere: a DM, another channel, a new thread. Your prompt lists the projects being built and each one's build. If you're not that build:

1. Answer questions yourself. Read the project's memory note, the ledger, `git log`, `gh pr list` and the code; clone the repo into your own folder if you need it. "Does the admin page have xyz?" is answered from the code.
2. When they ask for a change, settle with them what they want. Then call `tell_project` with the project's name and one message: who wants what, what you two established, and anything the build needs to know. The build gets it at its next step, from the person who asked.
3. Tell them it's passed on, with the project thread's link from `tell_project`'s reply.
4. If you can't tell whether they mean a change to the build or separate work, ask them once.

## Finish

When the last milestone is merged and verified:
- Call `finish_project`, delete the progress schedule and update the memory note.
- End with what shipped, how to run it and how you verified it.
- Deploy only where you have the credentials and the spec or request asks for it: staging, never production. If deploying needs access you don't have, say what.
````

- [ ] **Step 5: Run them to make sure they pass**

Run: `just test tests/test_payloads.py tests/test_bundles.py`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/agent/defaults/system.md src/agent/plugins/core/skills/long-project/SKILL.md tests/test_payloads.py tests/test_bundles.py
git commit -m "feat: a long-project skill, so Jensen builds a spec milestone by milestone with Superpowers and merges its own PRs"
```

---

### Task 9: Ship it and switch it on

This task runs on the live install. Every command on the workstation and every Slack message to Jensen goes to the user to run. The executor prepares the commands and checks the results.

- [ ] **Step 1: Final review and full suite**

Run: `just test`
Expected: PASS. Then do a whole-branch review with `superpowers:requesting-code-review`, and fix every Critical and Important finding.

- [ ] **Step 2: Scan for secrets and merge**

Run: `git diff origin/main --stat` and `git diff origin/main | grep -nEi "token|secret|key=|password"`
Expected: no secrets. With the user's OK, push and fast-forward `origin/main`.

- [ ] **Step 3: Deploy (the user runs this)**

First check that nothing is running:
```bash
ssh workstation 'docker exec agent-core python -c "import sqlite3; c=sqlite3.connect(\"/data/db/agent.db\"); print(c.execute(\"select count(*) from work_items where status=\x27claimed\x27\").fetchone())"'
```
When it prints `(0,)`, deploy:
```bash
ssh workstation 'cd ~/autonomous-agent && git pull --ff-only origin main && docker compose up -d --build core agent'
```

- [ ] **Step 4: Jensen installs Superpowers (the user sends this in Slack)**

DM Jensen: "install the superpowers plugin from the plugin directory for your whole setup". Then check:
```bash
ssh workstation 'docker exec agent-runner grep -c superpowers /var/agent/claude/plugins/installed_plugins.json'
```
Expected: `1` or more

- [ ] **Step 5: Live check: a message sent mid-turn (the user sends these)**

DM Jensen: "run `sleep 240` in bash, then tell me done". About 30 s later, reply in that thread: "what are you doing right now?"
Expected: an answer in the thread within about a minute, before "done", and a ✅ on the second message.

- [ ] **Step 6: Live check: restart**

DM Jensen: "run `sleep 300` in bash, then tell me done". After 30 s, the user runs:
```bash
ssh workstation 'cd ~/autonomous-agent && docker compose restart agent'
```
Expected:
- No "That was stopped" post.
- About 2 minutes later the turn runs again, its prompt starts with the restart note (check the dashboard's session timeline), and it ends with "done".

- [ ] **Step 7: Live check: a change from another conversation (the user sends these)**

1. In one DM thread: "register this conversation as project smoke-test (repo pragnition/autonomous-agent) with start_project, then run `sleep 300` in bash, then tell me done".
2. Start a new DM message: "what projects are you building?". Expected: it names smoke-test, which it sees in its prompt.
3. In that new DM: "tell the smoke-test build to add the word PINEAPPLE to its final message".
   Expected:
   - the reply says it's passed on, with the first thread's link
   - the first thread's final message includes PINEAPPLE
4. In the first thread: "finish the smoke-test project". Expected: a new session's prompt no longer lists it.

- [ ] **Step 8: Jensen moves its memory into the folder (the user sends this in Slack)**

DM Jensen:

> "Move every fact in ~/.claude/CLAUDE.md and ~/.claude/memory/MEMORY.md into your memory folder, one file per fact with an index line. Drop the ones that are stale, then empty those two files. Then set up a nightly schedule memory-tidy at 03:00 that merges duplicates, fixes or deletes stale facts (check them against the code, config and schedules) and keeps the index under 150 lines."

Check:
```bash
ssh workstation 'docker exec agent-runner sh -c "wc -l /var/agent/memory/MEMORY.md; ls /var/agent/memory | wc -l; wc -c /var/agent/claude/CLAUDE.md; grep -c memory-tidy /data/schedules/schedules.toml"'
```
Expected:
- the index has lines, and there's one file per fact plus the index
- CLAUDE.md is 0 bytes
- the schedule is there

- [ ] **Step 9: Set up the first project (RosterRolo demo)**

1. **The repo.** The team makes a new repo for Jensen. Check that Jensen's token reaches it:
   ```bash
   ssh workstation 'docker exec agent-runner gh repo view <org>/<repo> --json nameWithOwner,viewerPermission'
   ```
   Expected: `viewerPermission` of `ADMIN` (it merges there).
2. **App keys.** Put the keys the app needs in `data/secrets/projects/<project>.env` on the workstation, then run `docker compose restart core`. For RosterRolo's code, that's `OPENROUTER_API_KEY` from its `.env.example`; its two other secrets have local fallbacks.
3. **Handover message.** Posted in a Slack thread: the tender specification (attached or linked), the repo to build in, and what the demo has to show.

- [ ] **Step 10: Watch the first run**

The first run is the live check for the long-project skill, the hourly schedule and merging. Check:
- the kickoff post
- the project in core's list (a new DM: "what projects are you building?")
- the `project-<name>.md` memory file
- the `<project>-progress` entry in `data/schedules/schedules.toml`
- the first hourly post
- that each milestone's PR is merged into the repo's main (`gh pr list -R <org>/<repo> --state merged`)

Fix what breaks as it comes up.
