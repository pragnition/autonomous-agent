# improve-jensen engine v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** improve-jensen works out on its own what Jensen needs. It does this through four assessments a day, a
yardstick of real tasks, improvement projects that run over several days, and two-way notes with Jensen.

**Architecture:**
- Built on what's already on this branch (`src/agent/watch/`, the workflow, the instruction files).
- New modules:
  - `feedback.py`: Jensen's notes, replies and the change log, kept inside `agent-runner`;
  - `yardstick.py`: tasks, runs, scoring, the gate.
- New state for the gate and the assessment in `State`, and a gate check in `deploy.py`.
- Three new jobs (`assess`, `yardstick`, `project`) and their instruction files.
- One line added to Jensen's prompt.

**Tech Stack:** Python 3.12 stdlib, GitHub Actions, anthropics/claude-code-action@v1, Docker.

**Spec:** `docs/superpowers/specs/2026-11-18-improve-jensen-watch-design.md`. Sections §11–§14 are new. Goals 7–9,
the job table and §5, §6, §10 and §12 were edited.

## Global Constraints

- **Code.** Everything from the v1 plan still holds:
  - `src/agent/watch/` uses only the standard library plus `agent.scrub`, `agent.db` and `agent.secrets`;
  - each module starts with a docstring, then `from __future__ import annotations`;
  - commits use sentence style with no Co-Authored-By trailer;
  - tests run with `just test`;
  - the repo is public: no secrets, conversation text, people's or customers' names, product names or product file
    paths in issues, PRs, commits or logs.
- **The one allowed change outside `src/agent/watch/`** is the single prompt line in `src/agent/defaults/system.md`
  (Task 1). `src/agent/core` and `src/agent/runner` stay untouched. `system.md` stays under 12 KB.
- **Paths inside `agent-runner`:**
  - notes: `/var/agent/feedback/`;
  - change log: `/var/agent/feedback/CHANGES.md` (newest first, at most 30 entries);
  - yardstick work: `/var/agent/yardstick/<run>/<task>/`;
  - memory: `/var/agent/memory/`.
- **Yardstick runs:**
  - conversation id prefix `V-yardstick-`;
  - at most 4 Jensen turns at once;
  - a failed task gets one retry;
  - the gate fails on a score drop of 2 or more against the previous run on the same tasks, or on a regression
    (a task that passed its last 3 runs and fails both tries now);
  - only tasks with a mechanical `check` count toward the score.
- **Assessment:** a ranked list of at most 10 items; at most 3 `priority` labels; at most one project at a time.
  A project run lasts 6 hours or less, and a project older than 5 days must be re-decided.
- **Schedules (UTC):**
  - watch `17 * * * *` (unchanged);
  - assess `13 */6 * * *`;
  - nightly yardstick `7 19 * * *` (03:07 Singapore time).
- **Runners:** one with labels `[self-hosted, improve-jensen, watch]`, four with `[self-hosted, improve-jensen, work]`.
- **New labels:** `gating`, `priority`, `jensen-assessment`.

## Review Focus

1. **A yardstick turn that never finishes** (Jensen hangs, or the runner is down). The run must time out per task,
   record a timeout failure and move on, never hang the job. Pinned in Task 2.
2. **The memory cleanup must not delete a note Jensen wrote for real work during the run.** It removes only notes
   created during the run that also contain `/var/agent/yardstick/`. Pinned in Task 2.
3. **A note from Jensen containing a secret or a product name.** Its text reaches the digest only through the scrub,
   and the public issues only in general terms. Pinned in Task 1.
4. **Several schedules in one workflow.** The watch must not run on the assess or nightly crons, and vice versa.
   Pinned in Task 5.
5. **A gate that never ends** (the yardstick job dies). `gate.json` older than 12 hours counts as expired, so deploys
   and fixes don't stay blocked forever. Pinned in Task 4.

---

### Task 1: Jensen's notes, replies and change log

**Files:**
- Create: `src/agent/watch/feedback.py`, `src/agent/watch/runner_feedback.py` (runs inside `agent-runner` from stdin)
- Modify:
  - `src/agent/watch/host.py`: add `runner_python`;
  - `src/agent/watch/measure.py` and `src/agent/watch/digest.py`: a "Jensen reports" section, and new notes count as
    work;
  - `src/agent/watch/cli.py`: subcommands `feedback-reply` and `changes-add`;
  - `src/agent/defaults/system.md`: one line.
- Test: `tests/test_watch_feedback.py`, plus additions to `tests/test_watch_measure.py` and
  `tests/test_watch_cli.py`

**Interfaces:**
- Produces:
  - `Host.runner_python(module_file: str | Path, args: list[str]) -> str`. Same contract as `core_python`, but runs
    `docker exec -i agent-runner python3 - <args>`.
  - `feedback.collect(host, since: datetime, until: datetime) -> list[dict]`. Each dict is
    `{name, text, modified}`. It returns notes in `/var/agent/feedback/` modified in `(since, until]`, excluding
    `CHANGES.md`. A missing folder gives `[]`.
  - `feedback.reply(host, name: str, status: str, now: datetime) -> None`. Appends
    `\nStatus (<ISO now>): <status>\n` to the note. It refuses names containing `/` or `..`, and names not ending
    in `.md`.
  - `feedback.add_change(host, text: str, now: datetime) -> None`. Prepends `- <YYYY-MM-DD>: <text>` to
    `CHANGES.md` (creating it with a `# What changed for Jensen` heading) and keeps the newest 30 entries.
  - `runner_feedback.py` subcommands `collect --since ISO --until ISO`, `reply --name N --status-b64 B --now ISO`,
    `change --text-b64 B --now ISO`, each printing JSON or `ok`.
  - CLI: `feedback-reply NAME STATUS` and `changes-add TEXT`.
  - `MeasureResult.has_work` is also true when the window has new notes.

- [ ] **Step 1: Write the failing tests:**
  - `test_runner_feedback_collect_window_and_skips_changes`: run `runner_feedback.collect` against a tmp folder,
    passing the root path as an extra `--root` arg that defaults to `/var/agent/feedback`. A note modified inside the
    window is returned; one outside it isn't; `CHANGES.md` never is.
  - `test_reply_appends_status_and_rejects_bad_names`: `../x.md`, `a/b.md` and `x.txt` raise `ValueError`.
  - `test_add_change_prepends_and_keeps_30`: after 31 adds, there are 30 entries, newest first.
  - `test_measure_digest_has_jensen_reports_scrubbed` (Review Focus 3): a note containing `xoxb-1234567890-abcdefghij`
    and an `agent.env` value shows in the digest's `## Jensen reports` section with both redacted, and the hour
    reports `has_work=true` even when no turns finished.
  - `test_system_prompt_asks_for_feedback_notes`: `src/agent/defaults/system.md` contains `/var/agent/feedback/` and
    `CHANGES.md`, and is under 12,288 bytes.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Implement.**
  - Notes reach `measure` through `feedback.collect`, and are scrubbed with the same scrubber as the tool lines.
  - The prompt line goes in the section about Jensen's own setup and reads: "When something in your own setup gets in
    your way (a missing tool or access, a limit you hit, background work you lost, an instruction or skill that didn't
    fit, a workaround you needed), write a short note to `/var/agent/feedback/<YYYY-MM-DD>-<slug>.md`: what you were
    doing, what got in the way, what would have helped. improve-jensen reads these and answers in the note with a
    Status: line; don't wait for it. When something about your setup seems different from what you remember, read
    `/var/agent/feedback/CHANGES.md`."
- [ ] **Step 4: Run the tests and confirm they pass,** then run `just test`.
- [ ] **Step 5: Commit** with the message `feat: Jensen notes what gets in its way, improve-jensen reads the notes
  every hour, answers in them and keeps a change log Jensen can read`.

### Task 2: The yardstick: tasks, runs, score, memory cleanup

**Files:**
- Create:
  - `src/agent/watch/yardstick.py`;
  - `src/agent/watch/core_items.py` (runs inside `agent-core` from stdin).
- Modify:
  - `src/agent/watch/core_ask.py`: `enqueue_turn(..., new_prefix: str = "V-review-")`, plus a `--new-prefix` CLI
    arg;
  - `src/agent/watch/state.py`: `is_improve_jensens_own` also matches the `V-yardstick-` prefix;
  - `src/agent/watch/cli.py`: `yardstick install DIR`, `yardstick add DIR`, `yardstick run [--tasks a,b]
    [--gate] [--github-output F]`, `yardstick score`.
- Test: `tests/test_watch_yardstick.py`

**Interfaces:**
- **A task** is a folder `<id>/` with:
  - `task.toml`, holding `prompt` (str), `time_limit_min` (int, default 30) and an optional `rubric` (str);
  - `fixture/`, with a git repo inside;
  - `check` (executable; exit 0 means pass);
  - optional `hidden/`, copied into the task folder only after the turn ends.
- **The task folder** is `$STATE/yardstick/tasks/<id>/`, with retired tasks in `$STATE/yardstick/retired/`.
- **Functions:**
  - `load_tasks(root: Path) -> list[Task]`, where `Task` is a dataclass `id, prompt, time_limit_min, rubric,
    path, mechanical: bool`. `mechanical` is true when `check` exists and `rubric` is empty.
  - `install(src: Path, root: Path) -> list[str]` copies every task folder that isn't there yet and returns the ids it
    added.
  - `add(src_task: Path, root: Path) -> str` raises `FileExistsError` if the id already exists, never overwriting.
  - `run(host, state, tasks: list[Task], *, run_id: str, concurrency: int = 4, sleep=time.sleep,
    clock=time.monotonic) -> list[dict]` returns one result per task:
    `{run, ts, sha, task, passed: bool, minutes, judged: bool, reason}`. Each result is appended to
    `$STATE/yardstick/results.jsonl`.
  - `score(results: list[dict]) -> tuple[int, int]` returns `(passed, counted)` over mechanical tasks only.
  - `compare(state, current_run_id: str) -> dict` returns
    `{"previous": run_id|None, "drop": int, "regressions": [task ids], "gate_failed": bool}`.
- **Per task, a run does this:**
  1. Copy `fixture/` into `agent-runner` at `/var/agent/yardstick/<run>/<id>/` by piping a tar stream into
     `docker exec -i agent-runner sh -c "mkdir -p … && tar -x -C …"`. If the folder already exists, the result is
     `reason="folder exists"` and the task is not run. If `build.sh` is among the copied files, run it there
     (`sh build.sh`) so it creates the git repo and its first commit. Task 3's fixtures rely on this.
  2. Queue the turn: `core_ask.py --new --new-prefix V-yardstick-<run>- --text-b64 …`. The text is the task's
     `prompt`, preceded by a header: "This is a yardstick task in `<dir>`. Work only in that folder. It has no remote:
     don't push, don't open PRs, don't message anyone, and don't save memory notes or skills about it. Reply 'done'
     when finished."
  3. Wait for the turn. `core_items.py --ids …` returns `{id: {status, outcome}}`; poll every 30 s until the status
     is `done`, `failed` or `cancelled`, or until `time_limit_min` is up. A timeout gives `reason="timeout"`.
  4. Copy in `hidden/` and `check`, then run `docker exec agent-runner sh -c "cd <dir> && ./check"`.
  5. A failure is retried once in a fresh folder, `<id>-r`. Mechanical tasks only.
- **The `sha`** in each result is `git -C <live> rev-parse HEAD`.
- **The CLI `yardstick run`** first reads core's pool, through `core_collect.py` as `measure` does. While the pool is
  holding, it outputs `skip_reason=pool_hold` and runs nothing.
- **Memory cleanup** runs at the end of a run, inside `agent-runner`. It deletes files in `/var/agent/memory/` that
  were modified after the run started and contain `/var/agent/yardstick/`, and removes `MEMORY.md` lines naming
  those files.

- [ ] **Step 1: Write the failing tests,** with a fake `Host` that records docker calls and serves item statuses:
  - `test_load_tasks_and_mechanical_flag`;
  - `test_add_refuses_overwrite`;
  - `test_install_copies_only_new`;
  - `test_run_passes_and_records_results`;
  - `test_run_times_out_without_hanging` (Review Focus 1): the turn is still `claimed` after `time_limit_min`, so the
    result has `passed=False` and `reason="timeout"`, and `run` returns;
  - `test_hidden_copied_only_after_turn`: the fake records the order of calls;
  - `test_failed_task_retried_once`;
  - `test_judged_tasks_not_counted_in_score`;
  - `test_compare_gate_fails_on_drop_of_two_and_on_regression`;
  - `test_memory_cleanup_only_removes_marked_notes_from_this_run` (Review Focus 2): it runs the cleanup snippet
    against a tmp folder, where a new note with the marker is removed, a new note without it stays, and an old note
    with it stays;
  - `test_yardstick_conversations_count_as_improve_jensens_own`.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Implement.** The cleanup runs as a short Python snippet through `Host.runner_python` (Task 1). Its
  folder root is an argument, so the test can point it at a tmp folder.
- [ ] **Step 4: Run the tests and confirm they pass,** then run `just test`.
- [ ] **Step 5: Commit** with the message `feat: a yardstick of real tasks runs Jensen in throwaway folders, scores
  only mechanical checks and flags regressions`.

### Task 3: Five seed yardstick tasks

**Files:**
- Create:
  - `deploy/yardstick/setup-facts/`;
  - `deploy/yardstick/authz-self-grant/`;
  - `deploy/yardstick/tests-that-can-fail/`;
  - `deploy/yardstick/background-result/`;
  - `deploy/yardstick/split-change/`.
  
  Each holds `task.toml`, `fixture/` (a script that builds the git repo, see Step 3), `check` and `hidden/` where
  needed. Reference solutions go in `tests/yardstick_solutions/<id>/`.
- Test: `tests/test_yardstick_seeds.py`

**What each task is.** All five use synthetic code, Python standard library only, with no network.

| id | Request to Jensen | `check` passes when |
|---|---|---|
| `setup-facts` | Write `answers.md` answering: the URL your apps use for model calls; your memory folder; the tool that posts files to Slack; where you write notes about what gets in your way | `answers.md` contains `172.30.0.10:8087/llm`, `/var/agent/memory`, `post_files` and `/var/agent/feedback` |
| `authz-self-grant` | A small `roles.py` validates ids case-insensitively but checks self-grants case-sensitively. "A role administrator was able to grant themselves a role. Fix it and add a regression test." | hidden tests pass (an upper-case self-grant is refused; normal grants still work; an unknown role is refused) and Jensen's own tests pass |
| `tests-that-can-fail` | Add `reserve(item, qty)` to `inventory.py` with the stated rules (stock never below zero, unknown items rejected, a reservation lowers available stock), with tests | hidden acceptance tests pass, Jensen's tests pass on its code, and Jensen's tests FAIL when `hidden/mutant.py` replaces `inventory.py` |
| `background-result` | Run `./slow_build.sh`, which takes about 3 minutes and writes `out/checksum.txt`, then put the checksum in `result.md` | `result.md` holds the exact checksum (`check` recomputes it) |
| `split-change` | Rename `calc_total` to `order_total` everywhere and fix the rounding bug in `discount()`, as two separate commits that each pass the tests | at least 2 new commits; the first one's tree passes the base tests; the hidden tests pass at HEAD; no `calc_total` is left |

- [ ] **Step 1: Write the failing tests.** For each seed: running `check` on the untouched fixture fails, and running
  it after applying the reference solution passes. Also `test_seeds_are_mechanical`, and `test_seed_prompts_are_general`
  (no product or people names).
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Build the five tasks.**
  - Each `fixture/` is built from a plain folder by a script, so the repo holds no nested `.git`:
    `fixture/build.sh` makes the git repo with its first commit when the task is copied.
  - `run` therefore runs `build.sh` inside the task folder before queuing the turn. Add this to Task 2's code if it
    isn't there yet, with a test.
- [ ] **Step 4: Run the tests and confirm they pass,** then run `just test`.
- [ ] **Step 5: Commit** with the message `feat: five synthetic yardstick tasks with checks that fail on the starting
  code and pass on a right answer`.

### Task 4: The gate and assessment state; deploy respects the gate

**Files:**
- Modify:
  - `src/agent/watch/state.py`;
  - `src/agent/watch/deploy.py`;
  - `src/agent/watch/cli.py`: `gate start --issue N --kind repo|host --handle H`, `gate status`, `gate end`,
    `assess-check [--github-output F]`, `assess-plan [--github-output F]`, `assess-commit`.
- Test: `tests/test_watch_gate_assess.py`, plus additions to `tests/test_watch_deploy.py`

**Interfaces:**
- **Gate state:**
  - `State.gate_start(issue: int, kind: str, handle: str, now: datetime) -> None` writes `gate.json`. It raises if a
    gate is already active.
  - `State.gate(now: datetime) -> dict | None` returns the active gate. A gate older than 12 hours counts as expired
    and returns `None` (Review Focus 5).
  - `State.gate_end() -> None`.
- **Assessment state:**
  - `State.last_assess() -> datetime | None` and `State.set_last_assess(ts: datetime) -> None`.
  - `State.assess_plan() -> {"project": int | None}`, read from `assess-plan.json` and validated like `plan()`.
- **Labels:** the `seed` subcommand's state labels gain `gating`, `priority` and `jensen-assessment`.
- **Deploy:** in `_deploy_locked`, after the services are known, a non-empty service set with an active gate means
  log `pending`, set pending and return 3. Docs-only deploys still go through.
- **`assess-check`** outputs `has_work=true` when either:
  - since `last_assess` there is a new digest, a new note from Jensen (Task 1), a new yardstick result, or an open
    `project` issue with an `In progress` marker; or
  - there has never been an assessment.

  It outputs `false` while core's usage watch holds, reusing the measure's pool read. It also outputs `skip_reason`.
- **`assess-plan`** outputs `project` (a number or empty) and `has_project`, then deletes `assess-plan.json`.
- **`assess-commit`** sets `last_assess` to now.

- [ ] **Step 1: Write the failing tests:**
  - `test_gate_start_twice_raises`;
  - `test_gate_expires_after_12_hours`;
  - `test_deploy_pending_while_gate_active_but_docs_only_goes_through`;
  - `test_assess_check_first_time_and_nothing_new`;
  - `test_assess_check_new_feedback_or_yardstick_result`;
  - `test_assess_plan_outputs_and_clears`;
  - `test_seed_creates_the_new_state_labels`.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the tests and confirm they pass,** then run `just test`.
- [ ] **Step 5: Commit** with the message `feat: a gate holds other behaviour changes while the yardstick measures
  one, and the assessment knows when there is something new to assess`.

### Task 5: Workflow: assess, yardstick and project jobs

**Files:**
- Modify: `.github/workflows/improve-jensen.yml`
- Test: `tests/test_deploy_files.py`

**Interfaces:**
- **Consumes** the CLI subcommands from Tasks 1, 2 and 4.
- **Consumes these instruction file paths** (Task 6 writes them; create one-line placeholders here):
  - `.github/improve-jensen-assess.md`
  - `.github/improve-jensen-project.md`
- **Schedules:** `17 * * * *` (watch), `13 */6 * * *` (assess), `7 19 * * *` (nightly yardstick).
- **Dispatch:** the `mode` choice gains `assess`, `yardstick` and `project`. `target` carries the issue number for
  `project`, and `gate` for a gate run of `yardstick`.

| Job | `if` | runs-on | Steps |
|---|---|---|---|
| `watch` (existing) | `github.event.schedule == '17 * * * *'` or dispatch `mode == 'watch'` | watch | unchanged |
| `assess` | `github.event.schedule == '13 */6 * * *'` or dispatch `mode == 'assess'` | work, timeout 90 | 1. `assess-check`.<br>2. claude, if has_work (`--max-turns 200`, `--append-system-prompt-file .github/improve-jensen-assess.md`, prompt names the state folder and says to write `assessment.md`, update the pinned issue and write `assess-plan.json`).<br>3. `assess-plan`.<br>4. `assess-commit`, if claude succeeded. |
| `project` | `!cancelled()`, and either `needs.assess.outputs.has_project == 'true'` or dispatch `mode == 'project'` with a target | work, timeout 360; `concurrency: improve-jensen-project` (no cancel) | label work through the job token as in `fix`; claude (`timeout-minutes: 350`, `--max-turns 500`, `.github/improve-jensen-project.md`, prompt "Work on improvement project #N; if its branch and progress file exist, continue from them") |
| `yardstick` | `github.event.schedule == '7 19 * * *'` or dispatch `mode == 'yardstick'` | work, timeout 240 | 1. `yardstick run [--gate if target == 'gate']` with outputs `score`, `counted` and `gate_failed`.<br>2. `gate end`, if it was a gate run (`always()`).<br>3. claude, if `gate_failed == 'true'` (`.github/improve-jensen.md`, prompt "The yardstick gate failed for the change recorded in the gate record; revert it through its handle and record the failed attempt"). |

- Every new job keeps the shared setup steps.
- `fix` and `project` both use `persist-credentials: false`.
- No job puts a product name or URL in its prompt.

- [ ] **Step 1: Write the failing tests:**
  - `test_each_schedule_runs_only_its_job` (Review Focus 4): parse the `if` expressions and assert each job's `if`
    names its own cron string and no other job's;
  - `test_new_jobs_runners_timeouts_and_files`;
  - `test_project_concurrency`;
  - `test_yardstick_gate_end_always`;
  - `test_dispatch_modes_include_new_modes`.
- [ ] **Step 2: Run the tests and confirm they fail.**
- [ ] **Step 3: Edit the workflow.** Run actionlint:
  `docker run --rm --user "$(id -u):$(id -g)" -v "$PWD":/repo -w /repo rhysd/actionlint:latest`.
- [ ] **Step 4: Run the tests and confirm they pass,** then run `just test`.
- [ ] **Step 5: Commit** with the message `feat: improve-jensen assesses Jensen four times a day, runs the yardstick
  nightly and after behaviour changes, and runs one improvement project at a time`.

### Task 6: Instructions and docs

**Files:**
- Create: `.github/improve-jensen-assess.md`, `.github/improve-jensen-project.md`
- Modify:
  - `.github/improve-jensen-watch.md`;
  - `.github/improve-jensen.md` (the charter);
  - `deploy/improve-jensen.md`;
  - `docs/design.md`.
- Test: `tests/test_deploy_files.py` (anchor tests)

**What each file must say** (the wording is the builder's; the rules come from the spec):
- **`improve-jensen-assess.md`** (§11):
  - the goal and its rank order;
  - what to read, and which commands read it (`count`, `yardstick score`, the digests, `feedback` notes,
    `gh issue list`);
  - every item needs evidence from Jensen's own work, and research only explains or solves;
  - at most 10 items;
  - write `assessment.md`, keeping dated copies;
  - rewrite the pinned `jensen-assessment` issue in general terms;
  - file `jensen-watch` issues for new items, with `project` when project-scale;
  - move `priority` so that at most 3 issues have it;
  - add yardstick tasks with `yardstick add`, never editing one;
  - start at most one project via `assess-plan.json`, resuming an unfinished one first, and decide again on any
    project older than 5 days;
  - the public-repo rule, verbatim;
  - no Slack.
- **`improve-jensen-project.md`** (§13):
  - a design note comment first;
  - the branch `improve-jensen/project-<n>`, with a progress file at
    `docs/improve-jensen/projects/<n>-progress.md` (in general terms);
  - small steps, tests first, subagents;
  - PR, CI, fresh review, merge and `just deploy`, as in the charter;
  - the live behaviour test for a change to how turns run;
  - then `gate start` and dispatching the yardstick gate;
  - leave progress for the next run;
  - the public-repo rule.
- **`improve-jensen.md`, additions:**
  - after a change that alters how Jensen behaves, run `gate start` (repo: merge sha; host or Claude home: backup
    paths) and `gh workflow run improve-jensen.yml -f mode=yardstick -f target=gate`;
  - `changes-add` with a plain-words line after each such deploy;
  - `feedback-reply` on the note when an issue came from a Jensen report;
  - a section on the failed gate: revert through the handle, then record the failed attempt.
- **`improve-jensen-watch.md`, additions:**
  - pick `priority` issues first;
  - pick no new fix while an issue has `gating` or an active gate exists;
  - the "Jensen reports" section of the digest: record each with `note` and reply with `feedback-reply`;
  - a note about visible harm qualifies.
- **`deploy/improve-jensen.md`:**
  - five runners;
  - the new labels;
  - `yardstick install deploy/yardstick` and one baseline run (`gh workflow run … -f mode=yardstick`);
  - the first assessment;
  - creating `/var/agent/feedback` (`docker exec agent-runner mkdir -p /var/agent/feedback`).
- **`docs/design.md`:** extend the improve-jensen section with the assessment, the yardstick, projects and the notes.

- [ ] **Step 1: Write the failing anchor tests:**
  - assess has `assessment.md`, `jensen-assessment`, `priority`, `yardstick add` and `assess-plan.json`;
  - project has `improve-jensen/project-` and `gate start`;
  - the charter has `gate start`, `changes-add` and `feedback-reply`;
  - watch has `gating` and `priority`.
- [ ] **Step 2: Run them and confirm they fail.**
- [ ] **Step 3: Write the files.**
- [ ] **Step 4: Run `just test` and confirm it passes.**
- [ ] **Step 5: Commit** with the message `docs: instructions for the assessment, improvement projects, the yardstick
  gate and Jensen's notes, and the switch-on steps for them`.
