# Schedules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** People ask the agent in Slack for work on a schedule; the agent writes it into a schedules file; core fires each due entry as an unattended turn through the normal queue.

**Architecture:** `src/agent/core/schedules.py` parses the file, computes cron slots, runs a `Scheduler.tick` that core calls every 60 s, and serves the `python -m agent schedules` check. `Conversations.start_scheduled` posts the `⏰` header, makes the conversation and queues the turn. The agent edits the file with its own tools, guided by a `schedules` skill.

**Tech Stack:** Python 3.12, `croniter==7.0.0`, `tomllib`, aiohttp core, SQLite `settings`, pytest (`just test`).

**Spec:** `docs/superpowers/specs/2026-11-15-schedules-design.md`

## Global Constraints

- The file is `/data/schedules/schedules.toml` in both core and the agent container (host `data/schedules/`).
- Schedule names match `^[a-z0-9-]{1,40}$`. `cron` is exactly five fields. Required: `cron`, `channel`, `prompt`, `by`. Optional: `link` (str), `paused` (bool). Any other key is an error for that entry.
- Times are in `agent.toml`'s `timezone`.
- Settings keys: `schedule.<name>.last` (JSON `{"slot": <iso>, "cron": <expr>}`), `schedules.errors_hash`.
- A firing posts `⏰ <name>`, and its turn's first line is `[schedule <name> · <cron> · set up by <who> · <link>]` (link part only when set).
- No new core tool. Run tests with `just test` (Docker dev image).

## Review Focus

- Editing an existing schedule's `cron` must not fire it on the next tick; it waits for its next slot under the new cron (Task 2 test `test_changed_cron_waits_for_its_next_slot`).
- Unpausing a schedule (or resuming the agent) after slots passed must not fire a backlog or fire immediately (Task 2 test `test_paused_and_stopped_slots_are_consumed`).
- A file that becomes invalid TOML mid-run keeps the last valid schedules firing and DMs admins once, not every minute (Task 2 test `test_broken_file_keeps_last_valid_set_and_reports_once`).
- A Slack post failure (bot not in channel) retries on the next tick without double-firing once it succeeds, and reports once (Task 2 test `test_failed_firing_retries_and_reports_once`).
- `channel` given as a person key goes to that person's DM; `by` that isn't a person key still fires with no actor (Task 3 tests).

---

### Task 1: Parsing, slots and the check command

**Files:**
- Create: `src/agent/core/schedules.py`
- Modify: `src/agent/__main__.py` (dispatch `schedules` like `kpis`; add it to the help list and docstring)
- Modify: `pyproject.toml` (add `"croniter==7.0.0"` to `dependencies`)
- Modify: `justfile` (recipe `schedules: docker compose exec core python -m agent schedules`)
- Modify: `src/agent/paths.py` (`SCHEDULES_FILE = Path(os.environ.get("AGENT_SCHEDULES_FILE", DATA / "schedules" / "schedules.toml"))`, `RUNNER_TIMEZONE = Path("/run/agent/timezone")`)
- Test: `tests/test_schedules.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Schedule: name: str; cron: str; channel: str; prompt: str; by: str; link: str | None = None; paused: bool = False`
  - `parse(text: str) -> tuple[list[Schedule] | None, list[str]]`: `None` when the text isn't valid TOML; errors are `"<name>: <problem>"` strings, file-level ones without a name.
  - `latest_slot(cron: str, now: datetime) -> datetime` (aware in, aware out; a slot exactly at `now`, to the minute, counts)
  - `next_slots(cron: str, now: datetime, n: int = 3) -> list[datetime]`
  - `main(argv: list[str]) -> int` (the CLI)

- [ ] **Step 1: Write the failing tests** in `tests/test_schedules.py`:
  - `test_parse_valid_entry`: the spec's example (with `link`, `paused = true`) → one `Schedule` with those values, no errors.
  - `test_parse_reports_each_bad_entry_and_keeps_good_ones`: entries with a missing `prompt`, a six-field cron `"0 0 9 * * *"`, an invalid cron `"61 * * * *"`, an unknown key `when = 1`, a name `"Bad_Name"`, `paused = "yes"`, plus one valid entry → exactly the valid one returned, one error per bad entry, each error starting with that entry's name.
  - `test_parse_top_level_value_is_an_error`: `timezone = "UTC"` at top level → error mentioning `timezone`.
  - `test_parse_invalid_toml_returns_none`: `"[x\n"` → `(None, [error])`, error starts with `"not valid TOML"`.
  - `test_parse_empty_text_is_no_schedules`: `""` → `([], [])`.
  - `test_latest_slot_counts_now_and_previous`: tz `Asia/Singapore`, cron `"0 9 * * *"`; now `09:00:30` → today 09:00; now `08:59` → yesterday 09:00.
  - `test_next_slots`: cron `"0 */3 * * *"`, now `10:15` → `[12:00, 15:00, 18:00]` same day.
  - `test_cli_prints_next_runs_and_exits_zero` / `test_cli_exits_nonzero_on_errors` / `test_cli_missing_file`: `main(["--file", str(path), "--timezone", "Asia/Singapore"])` with `capsys`; valid file → each name and three `YYYY-MM-DD HH:MM` times and the timezone in output, exit 0; a bad entry → its error in output, exit 1; missing file → output says no schedules file, exit 0.

- [ ] **Step 2: Run** `just test tests/test_schedules.py` — expect FAIL (module missing).

- [ ] **Step 3: Implement** `schedules.py` and the CLI. Use `croniter.is_valid` plus a five-field check; `latest_slot` uses `croniter(cron, now.replace(second=0, microsecond=0) + timedelta(seconds=1)).get_prev(datetime)`. CLI flags: `--file` (default `paths.SCHEDULES_FILE`), `--timezone` (default: `agent.toml`'s timezone when `paths.CONFIG_DIR / "agent.toml"` exists, else the text of `paths.RUNNER_TIMEZONE` when it exists, else `UTC`). Output per schedule: name, cron, channel, `paused` when paused, then `next: <t1>, <t2>, <t3> (<tz>)`; then `error: <e>` lines. Add `croniter` to `pyproject.toml` and rebuild the dev image (`just test` does it).

- [ ] **Step 4: Run** `just test tests/test_schedules.py` — expect PASS.

- [ ] **Step 5: Commit** `feat: a schedules file and python -m agent schedules to check it`

### Task 2: The scheduler

**Files:**
- Modify: `src/agent/core/schedules.py`
- Test: `tests/test_schedules.py`

**Interfaces:**
- Consumes: Task 1's `Schedule`, `parse`, `latest_slot`.
- Produces: `class Scheduler: __init__(self, conn, *, path: Path, timezone: str, fire: Callable[[Schedule], Awaitable[object]], report: Callable[[str], Awaitable[None]])`; `async def tick(self, now: datetime | None = None) -> list[str]` (names fired).

Tick, in order: read the file (missing → `""`); `parse`; on `None` keep `self._valid` (initially `[]`), else replace it. For each schedule in `self._valid`: `slot = latest_slot(cron, now in tz)`; read `schedule.<name>.last`. If absent, or its `cron` differs, store `{slot, cron}` and move on. If `slot` is newer than the stored slot: when the entry is paused or `db.get_setting(conn, "paused", "0") == "1"`, store it without firing; otherwise `await fire(s)`, then store it; if `fire` raises, log it, add `"<name>: couldn't start: <exception>"` to the errors and don't store. After the loop: if there are errors and their joined text's sha256 differs from `schedules.errors_hash`, `await report(text)` and store the hash; with no errors, store `""`.

- [ ] **Step 1: Write the failing tests** (a temp file, a real `db` connection, `fire` and `report` fakes that record calls; `now` passed explicitly in `Asia/Singapore`):
  - `test_first_sight_records_without_firing`: cron `"0 9 * * *"`, tick at 09:30 → no fire; tick at next day 09:00 → fired once.
  - `test_fires_once_per_slot`: ticks at 09:00, 09:01, 09:59 → one fire.
  - `test_downtime_fires_once_on_return`: seen at 09:00 day 1 (recorded), next tick day 4 10:00 → exactly one fire.
  - `test_changed_cron_waits_for_its_next_slot`: seen with `"0 9 * * *"` at day 1 09:00; file changed to `"0 */3 * * *"`; tick at 13:00 → no fire; tick at 15:00 → one fire.
  - `test_paused_and_stopped_slots_are_consumed`: an entry with `paused = true` passes a slot → no fire; set `paused = false`, tick just after → no fire; next slot fires. Same with the global `paused` setting `"1"` then `"0"`.
  - `test_broken_file_keeps_last_valid_set_and_reports_once`: valid file seen; file replaced by `"[x\n"`; ticks at the next slot and the minute after → the schedule fires once, `report` called once with text containing `"not valid TOML"`; file fixed → next tick stores `""` and doesn't report.
  - `test_failed_firing_retries_and_reports_once`: `fire` raises `RuntimeError("not_in_channel")` twice then succeeds → three ticks within the slot: report once (text contains `not_in_channel`), fired successfully once, slot stored after the success, a fourth tick in the same slot → no fire.
  - `test_missing_file_is_quiet`: no file → no fire, no report.

- [ ] **Step 2: Run** `just test tests/test_schedules.py` — expect the new tests to FAIL.

- [ ] **Step 3: Implement** `Scheduler` as described above.

- [ ] **Step 4: Run** `just test tests/test_schedules.py` — expect PASS.

- [ ] **Step 5: Commit** `feat: core's scheduler fires each due schedule once per slot`

### Task 3: Firing into Slack and wiring core

**Files:**
- Modify: `src/agent/core/conversations.py` (add `start_scheduled`)
- Modify: `src/agent/core/app.py` (build the `Scheduler`, the 60 s loop, `dm_admins`, schedules folder and timezone file in `prepare_agent_dirs`)
- Modify: `compose.yaml` (`${AGENT_DATA:-./data}/schedules:/data/schedules` in `core` and `agent`)
- Test: `tests/test_conversations.py`, `tests/test_wiring.py`, `tests/test_deploy_files.py`

**Interfaces:**
- Consumes: `Schedule`, `Scheduler` (Tasks 1–2).
- Produces: `async def Conversations.start_scheduled(self, s: Schedule) -> str` (the work item id); `async def CoreApp.dm_admins(self, text: str) -> None`.

`start_scheduled`: channel is the person's DM (`conversations_open(users=person.slack)`) when `s.channel` is a key in `config.people`, else `s.channel` as written. Post `⏰ <name>`; create the conversation on `(resp["channel"], resp["ts"])` with `project=await self._project_for_channel(resp["channel"])`; actor is `s.by` when it is a person key, else `None`; who is built like `_slack_origin`'s (Slack name through `_answer(self.person_name(key))`, then `(key, GitHub @login)`), or `s.by` as written for an unknown key; queue with `unattended=True` and the origin line from Global Constraints.

- [ ] **Step 1: Write the failing tests:**
  - `test_conversations.py::test_scheduled_run_starts_a_thread_and_an_unattended_turn`: `Schedule(name="nightly-sweep", cron="0 22 * * *", channel="C-SAMPLE", prompt="Sweep it", by="ann", link="https://l")` → one post `{"channel": "C-SAMPLE", "text": "⏰ nightly-sweep"}`; one queued turn whose conversation has that channel and thread ts and project `sample`, payload `text == "Sweep it"`, `actor == "ann"`, `unattended is True`, origin starting `"[schedule nightly-sweep · 0 22 * * * · set up by "` and ending `" · https://l]"` and containing `GitHub @ann-gh`.
  - `test_scheduled_run_for_a_person_goes_to_their_dm`: `channel="bob"` → `slack.opened == [{"users": "U2"}]`, post channel `"D-U2"`.
  - `test_scheduled_run_by_someone_unknown_has_no_actor`: `by="carol"` → actor `None`, origin contains `set up by carol`.
  - `test_reply_in_a_scheduled_thread_continues_it`: after `start_scheduled`, `handle_slack(reply(ts, "more"))` from U1 → the second turn goes to the same conversation.
  - `test_wiring.py::test_prepare_agent_dirs_hands_them_to_the_agent`: extend — `data/schedules` exists and is chowned to 1001 when root; `data/runner/timezone` contains the config's timezone.
  - `test_wiring.py::test_dm_admins_messages_each_admin`: config admin `sam` → one `conversations_open` for sam's Slack id and one post with the text.
  - `test_deploy_files.py::test_schedules_folder_is_shared`: `"${AGENT_DATA:-./data}/schedules:/data/schedules"` in both `core` and `agent` volumes.

- [ ] **Step 2: Run** `just test tests/test_conversations.py tests/test_wiring.py tests/test_deploy_files.py` — expect the new tests to FAIL.

- [ ] **Step 3: Implement** `start_scheduled`, `dm_admins` (each person with `role == "admin"` and a Slack id; one failing admin doesn't stop the others), the scheduler (`path=self.data_dir / "schedules" / "schedules.toml"`, `timezone=self.config.timezone`, `fire=self.conversations.start_scheduled`, `report=` a function that DMs admins `"Problems in the schedules file (data/schedules/schedules.toml):\n" + text`), `self._every(60, self.scheduler.tick)` in `serve`, and in `prepare_agent_dirs` create `schedules/`, write `runner/timezone`, and chown `schedules/` and its files when root.

- [ ] **Step 4: Run** the same tests — expect PASS; then `just test` — expect everything to pass.

- [ ] **Step 5: Commit** `feat: core fires due schedules as turns in a new Slack thread`

### Task 4: The agent's side and the docs

**Files:**
- Create: `src/agent/plugins/core/skills/schedules/SKILL.md`
- Modify: `src/agent/defaults/system.md` (one schedules line; the branch/PR line)
- Modify: `src/agent/plugins/core/skills/pr-description/SKILL.md` (first line of the PR: who asked and where; drop "you're an AI agent")
- Modify: `tests/test_bundles.py` (`test_core_skills_present` includes `"schedules"`)
- Modify: `docs/design.md`, `examples/config/agent.toml`, the spec (paths and decisions as built)

- [ ] **Step 1: Update `test_core_skills_present`** to include `"schedules"`; run `just test tests/test_bundles.py` — expect FAIL.

- [ ] **Step 2: Write the skill.** Frontmatter `name: schedules`, description: when someone asks for something on a schedule or to change, pause, list or stop one. Body: the file and format (the spec's example), `by` = the asker's person key from the first line, `link` = the message's link, the channel (a person key for "send me", else the channel they named or the one they asked in; the bot must be in a channel, join a public one with `conversations.join`), cron is in the agent's timezone, use `sudo` if the file isn't writable, run `python -m agent schedules` after every edit and fix what it reports, reply with the next three run times, pause with `paused = true`, remove by deleting the table, list by running the check. The prompt is self-contained: each run is a fresh session that knows earlier runs only from Linear, GitHub and memory, so write "skip what's already open" when it matters.

- [ ] **Step 3: Edit `system.md`.** Add after the Claude Code home line: `- People can ask for work on a schedule ("every morning at 9", "every night"): use the schedules skill. A scheduled run starts with a [schedule …] line instead of a message.` Replace "Work on your own branch, push it, and open the PR with `gh pr create`." with "Start a new branch for each piece of work, named the way the repo names its branches; push it and open the PR to the default branch with `gh pr create` and the repo's usual labels." Edit `pr-description` line 10: "Its first line says who asked and where (from the first line of their message)".

- [ ] **Step 4: Docs.** `docs/design.md`: a "Schedules" section after "Linear" (file, format, firing rules, errors, CLI), the 60 s schedules loop in core's loop list, the `/data/schedules` mount in the agent container's paths, `just schedules`; drop "branch prefix" from `name` in `docs/design.md` (lines 9 and 415) and `examples/config/agent.toml` line 2. Update the spec's file section (both containers at `/data/schedules`), `by` (actor only when a person key), errors (parse and firing errors, once per distinct text), the cron-change rule and the PR wording change.

- [ ] **Step 5: Run** `just test` — expect everything to pass.

- [ ] **Step 6: Commit** `feat: the agent sets up schedules itself, and its PRs follow the repo's conventions`

### Task 5: Review and ship

- [ ] **Step 1:** Whole-branch review by a fresh reviewer against the spec; fix Critical/Important findings; `just test` passes.
- [ ] **Step 2:** Secret scan of the diff (`git diff origin/main... | grep -nE 'xox[abp]-|ghp_|sk-|BEGIN .*PRIVATE|api[_-]?key\s*='` finds nothing real).
- [ ] **Step 3:** Merge into `main` (fast-forward from `origin/main`), push `origin main`.
- [ ] **Step 4:** Deploy on the workstation: pull `main`, `mkdir -p data/schedules`, rebuild and restart `core` and `agent`, then check `just schedules` answers and core's log shows no scheduler errors.
