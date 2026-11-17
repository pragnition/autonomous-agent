# Design and Anti-Slop Toolkit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the first toolkit cut (`f4a212b`) with a toolkit that is safe in the agent's runner and the prompt
lines that make the agent produce professional design and code on new and existing products.

**Architecture:** Nothing in core's Python changes. The toolkit lives in the agent's persistent Claude home, installed
by `deploy/agent-toolkit.sh`; the wiring is a handful of lines in `src/agent/defaults/system.md` and the
`long-project` skill, pinned by phrase tests in the repo's existing style.

**Tech Stack:** Claude Code plugins and skills, bash, pytest (`just test`, which runs in the Python 3.12 dev image).

**Spec:** `docs/superpowers/specs/2026-11-17-off-the-shelf-toolkit-design.md` (revised 2026-11-18).

## Global Constraints

- Toolkit, exactly: plugins `superpowers`, `frontend-design`, `pr-review-toolkit`; skills `web-design-guidelines`,
  `vercel-react-best-practices`, `vercel-composition-patterns`, `supabase-postgres-best-practices`; the user-scope MCP
  server `context7` (`npx -y @upstash/context7-mcp@4.7.3`).
- Never installed: `security-guidance`, `code-review-anthropics`, `webapp-testing`, the `context7` plugin, shadcn.
- Prompt voice: second person, plain words, short sentences, no headers; match the lines around each edit.
- No new core tools, no gates, no approval step.
- Commits: conventional prefix, no co-author or tool attribution. The repo is public: no secrets in any file.

## Review Focus

- **Existing product, new screen:** frontend-design fires on "new UI" in an existing app and restyles it. Expected:
  the screen reuses the product's components and tokens. Pinned by the "existing product" prompt phrase (Task 2);
  checked live in pilot B.
- **Fork:** `gh repo clone` of a fork makes the parent the PR target. Expected: PRs open in the repo the request
  names. Pinned by the `gh repo set-default` phrase (Task 2); checked live in pilot C.
- **A plugin changing how a turn ends:** a Stop or SessionStart hook added by the toolkit. Expected: none; only
  superpowers' SessionStart hook, already live. Pinned by the exact-set test (Task 1); checked by reading each kept
  plugin's `hooks.json` (Task 4).
- **Planning files in team repos:** superpowers commits `docs/superpowers/...` into a team repo for a one-line fix.
  Expected: they stay in the conversation folder unless the repo keeps such docs. Pinned by the "conversation folder"
  phrase (Task 2).
- **Screenshot baselines that never match CI:** baselines taken on the agent's machine differ from the CI runner's.
  Expected: both use Playwright's official Docker image. Pinned by the "official Docker image" phrase (Task 2).

---

### Task 1: The toolkit script installs exactly the vetted set

**Files:**
- Modify: `deploy/agent-toolkit.sh`
- Test: `tests/test_deploy_files.py`

**Interfaces:**
- Produces: the script's `plugin <name>` and `skill <repo> <name>` lines, read by the test.

- [ ] **Step 1: Write the failing test**

```python
def test_agent_toolkit_installs_exactly_the_vetted_set():
    script = ROOT / "deploy" / "agent-toolkit.sh"
    text = script.read_text()
    plugins = set(re.findall(r"^plugin (\S+)", text, re.M))
    skills = set(re.findall(r"^skill \S+ (\S+)", text, re.M))
    assert plugins == {"superpowers", "frontend-design", "pr-review-toolkit"}
    assert skills == {"web-design-guidelines", "vercel-react-best-practices", "vercel-composition-patterns",
                      "supabase-postgres-best-practices"}
    assert "claude mcp add -s user context7 -- npx -y @upstash/context7-mcp@" in text
    assert os.access(script, os.X_OK)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `just test tests/test_deploy_files.py -k toolkit`
Expected: FAIL, the plugin set still has `security-guidance`, `code-review-anthropics` and `context7`.

- [ ] **Step 3: Remove the `security-guidance`, `code-review-anthropics`, `webapp-testing` and `context7` plugin lines; add Context7's local MCP server**

Add it with `claude mcp add -s user context7 -- npx -y @upstash/context7-mcp@4.7.3`, skipped when `claude mcp get
context7` already finds it, and list `claude mcp list` at the end.

Add one comment line above the plugin block naming what's left out and why, in a few words each (see the spec's
"Left out").

- [ ] **Step 4: Run the test and the script against a throwaway Claude home**

Run: `just test tests/test_deploy_files.py -k toolkit` → PASS.
Run: `HOME=$T CLAUDE_CONFIG_DIR=$T/cfg bash deploy/agent-toolkit.sh` with `T` an empty scratch directory → exit 0,
three plugins, four skills and `context7` connected. Run it a second time → exit 0.

- [ ] **Step 5: Commit**

```bash
git add deploy/agent-toolkit.sh tests/test_deploy_files.py
git commit -m "fix: the agent's toolkit leaves out plugins that would change how its turns end or duplicate its tools"
```

### Task 2: The prompt wires the toolkit for new and existing products

**Files:**
- Modify: `src/agent/defaults/system.md`
- Test: `tests/test_payloads.py::test_shipped_system_prompt`

**Interfaces:**
- Consumes: the toolkit names from Task 1.
- Produces: the phrases Task 3's skill refers to as "your prompt".

- [ ] **Step 1: Extend the test's phrase list**

Replace `"official template",` in `test_shipped_system_prompt` with:

```python
"official template", "frontend-design", "@axe-core/playwright", "official Docker image", "required checks",
"existing product", "gh repo set-default", "conversation folder", "superpowers:requesting-code-review",
"pr-review-toolkit", "before and an after", "bring down",
```

- [ ] **Step 2: Run it and watch it fail**

Run: `just test tests/test_payloads.py -k shipped_system_prompt`
Expected: FAIL on the first missing phrase.

- [ ] **Step 3: Edit `system.md`**

The working-directory line gains, after "reuse a clone you already have there.": "After cloning a fork, run `gh repo
set-default` with the fork's name: gh otherwise opens PRs on the repo it was forked from, and your PRs belong in the
repo the request names." It ends with: "Superpowers' specs and plans for a single change stay in your conversation
folder, out of the repo, unless the repo keeps such docs."

The branch-and-PR line's "In the PR, say who asked and link where they asked (...)" becomes "..., what changed and
how you verified it".

The new-product line is replaced by three lines:

```
- A new product starts from its stack's official template (create-next-app, the FastAPI full-stack template), never a blank repo. Before its first screen, set its look with frontend-design and commit it: the design tokens (colour, type, spacing, radius) in code, the reasoning in a design doc, and a CLAUDE.md that points to both and lists the commands. Every screen after that uses those tokens.
- A new product's CI runs the type check, lint, unit and end-to-end tests, @axe-core/playwright on its main screens (WCAG 2.2 AA) and Playwright screenshot comparisons, with the screenshots taken in Playwright's official Docker image so local and CI baselines match. Turn on CodeQL default setup and make all of these required checks on main where the repo's GitHub plan allows; where it doesn't, or your `gh` login can't, say so once and carry on.
- In an existing product, its current design is the design system: new UI reuses its components, tokens, type, spacing and patterns. Use frontend-design only for a new product's look or a redesign someone asked for.
```

The merge line is replaced by two lines:

```
- Before every PR, get a fresh review: superpowers:requesting-code-review against what was asked (all of it and nothing more; best-practice skills apply to the code you write or touch, not to the code around it), and the pr-review-toolkit agents the diff calls for (code-reviewer for the repo's own rules, silent-failure-hunter, pr-test-analyzer, type-design-analyzer). For a UI change, a fresh subagent also compares your screenshots with the product's design and web-design-guidelines. Fix what's real.
- Merge your own PRs once the tests and CI pass and that review found nothing blocking.
```

The other-sessions line becomes: "Other sessions run in this environment at the same time. Pick a free port when you
start a server, don't stop processes you didn't start, and bring down the browser sessions and apps you started when
you're done."

The browser line becomes:

```
- Test every new feature end to end in a real browser while you build it, with one named `playwright-cli` session per task: the whole path, including an empty state, an error and a long value, at desktop and phone widths, with the console checked. For a change to an existing screen, take a before and an after. Keep screenshots under `.agent-out/screenshots/` (never committed), read each one before you post it in the thread with post_files, and attach them to the PR (the playwright-cli skill shows how). Leave an end-to-end test for the main path in the project's own test layout. Stop testing model features when the meter answers `cost_cap_reached`.
```

- [ ] **Step 4: Run the test**

Run: `just test tests/test_payloads.py` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/defaults/system.md tests/test_payloads.py
git commit -m "feat: Jensen builds to a written design system on new products and to the existing design on others, with a fresh review before every PR"
```

### Task 3: `long-project` sets up the design system once and reviews every milestone

**Files:**
- Modify: `src/agent/plugins/core/skills/long-project/SKILL.md`
- Test: `tests/test_bundles.py::test_long_project_skill_runs_on_superpowers_and_an_hourly_schedule`

**Interfaces:**
- Consumes: Task 2's prompt lines, referred to as "your prompt".

- [ ] **Step 1: Extend the test's phrase list**

Replace `"web-design-guidelines"` with `"design doc"`, and add `"superpowers:requesting-code-review"` and
`"fresh subagent"`.

- [ ] **Step 2: Run it and watch it fail**

Run: `just test tests/test_bundles.py -k long_project` → FAIL on `"design doc"`.

- [ ] **Step 3: Edit the skill**

The plan bullet "For anything a user sees, set the UX direction with frontend-design ..." becomes: "For a new
product, the first milestone scaffolds it and sets up its look (frontend-design), design doc, CLAUDE.md and CI, as
your prompt describes. Point every later plan's UI tasks at the design doc and tokens, so each fresh subagent builds
to the same design."

Milestone step 3 becomes: "Test it end to end in a real browser (playwright-cli) as your prompt describes, and run the
full suite."

Milestone step 4 starts: "Get the fresh review your prompt describes (superpowers:requesting-code-review, the
pr-review-toolkit agents, and the screenshot review for UI), fix what's real, then open the PR." The rest of the step
is unchanged.

- [ ] **Step 4: Run the test**

Run: `just test tests/test_bundles.py` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agent/plugins/core/skills/long-project/SKILL.md tests/test_bundles.py
git commit -m "feat: long-project commits a new product's design system in its first milestone and reviews each milestone with fresh eyes"
```

### Task 4: Regression review, then ship to main

**Files:** none new; fixes land where the review points.

- [ ] **Step 1: Full suite** — `just test` → all pass.
- [ ] **Step 2: Hooks of the kept plugins** — in the throwaway home from Task 1, list every `hooks.json` under
  `cfg/plugins/cache`. Expected: only superpowers', with a SessionStart hook and nothing on Stop.
- [ ] **Step 3: Load check** — a headless `claude -p` with `--setting-sources user --output-format stream-json
  --verbose` on the throwaway home. Expected in the init message: the three plugins, the four skills, the six
  `pr-review-toolkit:*` agents and the `context7` MCP server connected.
- [ ] **Step 4: Fresh regression review** — a subagent with no context of this work gets the spec, this plan,
  `git diff 327f5d2..HEAD` and read access to the repo, and reports anything that would make the agent worse than
  before `f4a212b`: lost behaviour, contradictions with other prompt lines or skills, references to deleted skills,
  conflicts with the runner. Fix what's real, rerun `just test`, commit.
- [ ] **Step 5: Push** — secret scan of the diff, then `git push origin main`.

### Task 5: Deploy and verify live (when the workstation is reachable)

- [ ] **Step 1: No turn in flight** — in agent-core, `work_items` has no row in a running state.
- [ ] **Step 2: Deploy core** — `ssh workstation 'cd ~/autonomous-agent && git pull --ff-only origin main && docker compose up -d --build core'`.
- [ ] **Step 3: Install the toolkit** — `ssh workstation 'cd ~/autonomous-agent && docker exec -i agent-runner bash < deploy/agent-toolkit.sh'` → exit 0.
- [ ] **Step 4: Live load check** — one V-smoke turn (the way `scripts/smoke.py` queues one); its transcript's init
  message in `/var/agent/transcripts/<item>.jsonl` shows the same plugins, skills, agents and MCP server as Task 4
  Step 3.
- [ ] **Step 5: Leftovers** — `grep -rl "browser-testing\|build-feature\|review-before-pr\|pr-description" /var/agent/claude/skills data/config/packs`
  in the containers; reword any hit to name no deleted skill.

### Task 6: Pilots

Run by the team, or by a V-smoke conversation, after Task 5. Pass criteria are the spec's Verification 3 to 5.

## Revisions after the regression review (Task 4 Step 4)

The fresh review found eleven issues; these changed the tasks above:

- `system.md`: the new-product lines moved into `long-project`; a new product is defined as a new repo, and an
  existing repo's CI, settings and branch protection change only when asked. The review covers the whole branch.
  pr-review-toolkit agents run when the diff changes logic. The long-project line also covers new products, and
  smaller work skips design docs, plan files and subagent runs. No git worktrees. The e2e test is added only where the
  project has e2e tests. A general model-meter line. The PR body also gives why, the calls made and what was left
  unchanged.
- `long-project`: self-contained first-milestone setup (non-interactive scaffolding, template workflows removed,
  stubs in CI, deterministic screenshots, accessible primitives) and its own browser test and review per milestone;
  subagent-driven-development's final review counts as the requesting-code-review.
- `find-features` restored (no standard equivalent), with `browser-testing` replaced by `playwright-cli`.
- ChilliClaw pack: `chilliclaw-debugging` points at superpowers:systematic-debugging; `chilliclaw-local` names its
  browser session `-s=<branch>` instead of an undefined `$S`.
- Task 5 Step 5 also covers `<project>-progress` schedule prompts in `data/schedules/schedules.toml`.
