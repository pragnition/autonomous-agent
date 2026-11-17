# Off-the-shelf toolkit: professional design and code from established skills, on new and existing products

Revised 2026-11-18. The first version (shipped in `f4a212b`) was reviewed against what each plugin does inside the
agent's runner; this version drops three plugins that would regress the agent and adds the mechanisms that keep the
output from looking or reading like generated code.

## Why

The agent is about to receive whole product specs, and it keeps working on the team's existing products. On both it
has to produce what a strong product team would: a deliberate UI and UX that fits the product, a sound architecture,
tests that prove behaviour, focused diffs, and none of the "it runs, ship it" marks of generated code.

The core plugin's homegrown process skills (`build-feature`, `review-before-pr`, `find-features`, `debugging`,
`pr-description`, `browser-testing`) were a few paragraphs each, mostly about verification, with nothing on design.
Maintained skills and plugins from Anthropic, Vercel, Supabase and the superpowers project cover the same ground
better.

**Intent:** the agent builds with established, maintained skills and libraries. We write only what no outside tool can
know: the agent's environment, core's features, and the few lines that wire the tools together.

## Goals

- **G1** The agent's Claude home holds a vetted toolkit covering workflow, look and feel, UX rules, current library
  docs and independent review, and every piece of it is safe inside the agent's runner.
- **G2** The core plugin's six homegrown process skills are gone, and nothing they did that the agent still needs is
  lost.
- **G3** A new product gets a deliberate, written design system and deterministic quality checks from its first
  milestone.
- **G4** In an existing product the agent builds to the product's own design and conventions and changes nothing it
  wasn't asked to.
- **G5** Every PR, on any product, gets a fresh review that checks it against the request as well as for quality.

## Non-goals

- New skills, rubrics or checklists of our own.
- A second workflow framework (Spec Kit, BMAD, Task Master, Kiro) next to superpowers.
- Gates in core, or new core tools.
- An approval step. The agent stays fully autonomous; the prompt and `long-project` have it settle what skills would
  ask a person and say what it assumed.
- Long runs. Already on `main`: a turn carries on after a limit (100 times) or a restart, and `long-project` builds a
  spec milestone by milestone.
- A component library. shadcn/ui gives every product the same generated look and is left out; the look comes from
  each product's own design.

## Anti-goals

- The agent restyles an existing product, or rewrites code around a change to fit a best-practice skill.
- Two competing ways to do one job (two browser tools, two review plugins).
- A plugin that changes how a turn ends or adds minutes to every session start.
- A hand-curated catalog that rots: everything installed is maintained upstream.

## Where skills live

| Where | Skills | This change |
|---|---|---|
| Core plugin (`src/agent/plugins/core/skills/`) | `build-feature`, `review-before-pr`, `find-features`, `debugging`, `pr-description`, `browser-testing` | Deleted; what the agent still needs from them moves into the prompt (below) |
| | `long-project`, `schedules`, `task-history` (core's features), `playwright-cli` (vendored from Microsoft) | Kept; `long-project` wires in the toolkit |
| Project packs (`examples/pack-templates/`, deployed under `data/config/packs/`) | `chilliclaw-local`, `chilliclaw-debugging`, `azure-debugging` | Kept |
| The agent's Claude home (`/var/agent/claude/skills`) | skills the agent wrote itself | Untouched |

## The toolkit

Installed at user scope into the agent's persistent Claude home (`CLAUDE_CONFIG_DIR=/var/agent/claude`, on the
`agentwork` volume). Every session gets `CLAUDE_CONFIG_DIR` forced to it and loads user settings, so every session in
the one agent container loads the toolkit, whatever the project.

| Job | What | Source |
|---|---|---|
| Workflow: design, plans, test-first, subagent execution with per-task review, debugging, verification, the pre-merge review | `superpowers` | official plugin directory |
| A new product's look | `frontend-design` | official plugin directory (Anthropic) |
| UX, interaction and accessibility rules | `web-design-guidelines` | vercel-labs/agent-skills |
| React and Next.js code | `vercel-react-best-practices`, `vercel-composition-patterns` | vercel-labs/agent-skills |
| Specialist reviewers | `pr-review-toolkit` (agents: code-reviewer, silent-failure-hunter, pr-test-analyzer, type-design-analyzer, comment-analyzer, code-simplifier) | official plugin directory (Anthropic) |
| Current library docs | `context7` (Context7's hosted MCP server) | official plugin directory |
| Postgres | `supabase-postgres-best-practices` | supabase/agent-skills |
| Browser | `playwright-cli` (already in the image, skill vendored in the core plugin) | microsoft/playwright-cli |

### Left out, and why

- **security-guidance.** Its SessionStart hook installs the Agent SDK with a 180-second timeout on every session, and
  its Stop hook wakes the session again after it has finished, with review findings. The runner's turn handling
  (two-result turns, late messages, a turn ending only once its prompt is read) was not built for a wake-up after the
  result, so this risks duplicate or lost replies. Security comes instead from CodeQL in CI, silent-failure-hunter and
  the pre-merge review.
- **code-review (Anthropic).** Duplicates pr-review-toolkit, runs around ten haiku, sonnet and opus subagents per
  review, and is built to post comments on an existing PR.
- **webapp-testing.** Drives Python Playwright, which the image doesn't have, and would be a second browser tool next
  to `playwright-cli`.
- **shadcn/ui** (see non-goals), the **Playwright MCP plugin** (duplicates `playwright-cli` at several times the
  tokens), **feature-dev's command** (waits for a human at each step), **wshobson's catalog** and **ui-ux-pro-max**
  (filler, unvetted).
- **Optional, not installed: Mobbin's MCP** (`https://api.mobbin.com/mcp`), real app screens for reference. It needs a
  paid plan and a one-time OAuth login by an admin inside the agent container, returns images rather than rules, and
  its terms forbid copying designs.

pr-review-toolkit's `review-pr` command is not in the model's skill list, so the agent uses its agents directly.

## Design and anti-slop mechanisms

Four layers. Each catches what the one before it lets through.

1. **Direction.** A new product's look is set once, deliberately, with frontend-design: a token system (colour, type,
   spacing, radius) and principles chosen for this product, reviewed against the generic default before any code.
   An existing product's direction is its current design.
2. **Written down, so it holds.** Long projects run each task in a fresh subagent; a direction that lives only in one
   session's context is lost by the next task. A new product commits its tokens in code, the reasoning in a design
   doc, and a `CLAUDE.md` pointing at both, and every milestone plan points its UI tasks at them. Later work on the
   product then treats that design as the existing one.
3. **Checks that can't be talked around.** A new product's CI runs the type check, lint, unit and end-to-end tests,
   `@axe-core/playwright` (WCAG 2.2 AA) on its main screens, and Playwright screenshot comparisons, with CodeQL, as
   required checks before merge. Screenshot comparisons also stop a later change from silently altering screens it
   wasn't meant to touch. Baselines are taken in Playwright's official Docker image so they match CI.
4. **Fresh eyes.** An agent grading its own work skews positive. Before every PR: superpowers' `requesting-code-review`
   (a reviewer that sees only the request and the diff, and checks all of it was done and nothing more), the
   pr-review-toolkit agents the diff calls for, and, for a UI change, a fresh subagent comparing the screenshots with
   the product's design and web-design-guidelines.

**On an existing product** the same layers apply with the product's own design and conventions as the standard:
frontend-design is not used unless someone asks for a redesign, web-design-guidelines applies to the screens the
agent builds or changes, best-practice skills apply to code it writes or touches, and the repo's own CI is used as is.

## Prompt changes (`src/agent/defaults/system.md`)

- **Forks.** After cloning a fork, `gh repo set-default` it: `gh repo clone` makes the parent the default target, so
  `gh pr create` would open the PR there. The request names the repo; this keeps PRs in it.
- **Planning files.** Superpowers' specs and plans for a single change stay in the conversation folder, out of the
  repo, unless the repo keeps such docs. (`long-project` keeps its plans in the repo on purpose.)
- **New product.** Official template; frontend-design look committed as tokens, design doc and `CLAUDE.md`; the CI
  above; CodeQL and required checks where the repo's GitHub plan allows, otherwise said once and carried on.
- **Existing product.** Its design is the design system; frontend-design only for a new product or a requested
  redesign.
- **Review.** The fresh review above before every PR, and merging only when it found nothing blocking.
- **Browser testing.** What the deleted skills held that the agent still needs: one named `playwright-cli` session
  per task; the whole path with an empty state, an error and a long value, at desktop and phone widths, console
  checked; a before and an after for a change to an existing screen; screenshots read before posting with
  `post_files` and attached to the PR (the playwright-cli skill shows how); an end-to-end test for the main path in
  the project's own layout; stop on `cost_cap_reached`; bring down what you started.
- **PR body.** What changed and how it was verified, besides who asked.

`long-project`: the first milestone of a new product sets up the design system and CI the prompt describes, every
plan points UI tasks at the design doc, and each milestone gets the fresh review before its PR.

## How it gets installed

`deploy/agent-toolkit.sh`, idempotent, run in the agent container on the deploy host and again when the list changes:

```bash
docker exec -i agent-runner bash < deploy/agent-toolkit.sh
```

It falls back between the official marketplace's two names, reports failures and exits non-zero. Core runs nothing
for it before a session, so a failed install never fails a turn. Plugins or skills it doesn't list are left alone.

## Verification

1. **Install:** the script exits 0 and lists the plugins and skills.
2. **Load:** a live session's init message shows `superpowers`, `frontend-design`, `pr-review-toolkit` and `context7`
   among its plugins, the four skills, the pr-review-toolkit agents and the context7 MCP server.
3. **Pilot A, new product:** a small spec in a new repo. Pass when the repo has a design doc with tokens in code, a
   `CLAUDE.md` pointing at them, CI with axe and screenshot comparisons as required checks, CodeQL on, zero serious or
   critical axe violations, the transcripts show frontend-design, writing-plans, subagent-driven-development and
   requesting-code-review in use, and the team finds nothing generic-looking in the screenshots.
4. **Pilot B, existing product:** a small UI feature in an existing product. Pass when the new screen uses the
   product's existing components and tokens, no global style or unrelated file changed, frontend-design was not
   used, and the PR has before and after screenshots and a review.
5. **Pilot C, fork:** ask the agent to clone a fork the team owns and say which repo `gh pr create` would target. Pass
   when it names the fork.

New prompt lines reach new conversations; a resumed conversation keeps the prompt it started with.
