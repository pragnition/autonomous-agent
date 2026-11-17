# Off-the-shelf toolkit: established skills and plugins replace the core plugin's own

## Why

The agent is about to receive whole product specs. What it builds has to look and behave like a professionally
built product: a deliberate UI and UX, a sound architecture, tests that prove behaviour, and none of the "it runs,
ship it" look of generated code.

Until now the agent's idea of how to build and review came from six homegrown skills in the core plugin:
`build-feature`, `review-before-pr`, `find-features`, `debugging`, `pr-description` and `browser-testing`. They were a
few paragraphs each, mostly about verification, with nothing on design, and they came from no established standard.
Maintained skills and plugins from Anthropic, Vercel, Supabase and the superpowers project cover the same ground far
better.

**Intent:** the agent builds with established, maintained skills, plugins and templates. We write as little of our
own process as possible: only what no outside tool can know, which is the agent's own environment and core's own
features.

## Goals

- **G1** The agent's Claude home holds a vetted set of off-the-shelf skills and plugins covering workflow, look and
  feel, UX rules, current library docs, independent review, browser testing and security.
- **G2** The core plugin's six homegrown process skills are deleted.
- **G3** A new product starts from its stack's official template and has CodeQL and required CI checks before its
  first merge.
- **G4** A pilot spec, built before and after the change, shows the difference.

## Non-goals

- Writing new skills, rubrics or checklists of our own.
- Spec Kit, BMAD, Task Master, Kiro or any second workflow framework next to superpowers.
- A separate evaluator harness beyond superpowers' subagent-driven-development, which already has a fresh reviewer
  subagent check each task against the spec and for code quality.
- New core tools, or gates in core.
- An approval step. The agent stays fully autonomous: the `long-project` skill and the prompt already have it settle
  what skills would ask a person, and say what it assumed.
- Long runs. Already handled on `main`: a turn carries on by itself after a limit (100 times) or a restart, and the
  `long-project` skill runs a spec milestone by milestone.

## Anti-goals

- Two competing workflows (ours and superpowers').
- A hand-curated catalog that rots: everything installed is maintained upstream.
- A component library that gives every product the same generated look. shadcn/ui is left out for this reason; the
  look comes from frontend-design and each product's own design.

## Approaches considered

1. **Toolkit swap only (chosen).** Install the off-the-shelf set, delete the homegrown skills, adjust a few prompt
   lines.
2. **Toolkit plus our own product baseline and evaluator loop.** Stronger enforcement, but it is our own crafted
   process again, which is what this change removes.
3. **Spec Kit as the backbone.** It overlaps superpowers, so the agent would run two workflows, and reviewers of it
   report markdown sprawl that agents drift from.

## Where skills live, and what this changes

| Where | Skills | This change |
|---|---|---|
| Core plugin (`src/agent/plugins/core/skills/`) | `build-feature`, `review-before-pr`, `find-features`, `debugging`, `pr-description`, `browser-testing` | Deleted |
| | `long-project`, `schedules`, `task-history` (core's own features), `playwright-cli` (vendored from Microsoft) | Kept; `long-project` now names the new skills |
| Project packs (`examples/pack-templates/`, deployed under `data/config/packs/`) | `chilliclaw-local`, `chilliclaw-debugging`, `azure-debugging` | Kept; one description stops naming `browser-testing` |
| The agent's Claude home (`/var/agent/claude/skills`) | skills the agent wrote itself (`chilliclaw-context`, `rosterrolo-context`, ...) | Untouched; the toolkit is added beside them |

## The toolkit

Installed at user scope into the agent's persistent Claude home (`CLAUDE_CONFIG_DIR=/var/agent/claude`, on the
`agentwork` volume). Every session gets `CLAUDE_CONFIG_DIR` forced to it and loads user settings, so every session
in the one agent container loads the toolkit, whatever the project.

| Layer | What | Source |
|---|---|---|
| Workflow | `superpowers` | official plugin directory |
| Look and feel | `frontend-design` | official plugin directory (Anthropic) |
| UX and accessibility rules | `web-design-guidelines`, `vercel-react-best-practices`, `vercel-composition-patterns` | vercel-labs/agent-skills |
| Current library docs | `context7` | official plugin directory |
| Independent review | `pr-review-toolkit`, `code-review-anthropics` | official plugin directory (Anthropic) |
| Browser testing | `webapp-testing`, plus the vendored `playwright-cli` skill | anthropics/skills, microsoft/playwright-cli |
| Security | `security-guidance` | official plugin directory (Anthropic) |
| Postgres | `supabase-postgres-best-practices` | supabase/agent-skills |

Left out: shadcn/ui (see anti-goals), the Playwright MCP plugin (duplicates `playwright-cli` at several times the
tokens), `feature-dev`'s command (waits for a human at each step), wshobson's catalog and ui-ux-pro-max (filler,
unvetted), `web-artifacts-builder` (for claude.ai artifacts).

**Optional, not installed:** Mobbin's MCP (`https://api.mobbin.com/mcp`), which searches 600k+ real app screens for
reference. It needs a paid Mobbin plan and an OAuth login an admin does once inside the agent container. It returns
images, not rules, and its terms forbid copying designs verbatim.

`security-guidance` adds a model review after commits and at session end, which adds to Foundry spend. The pilot
shows whether its findings are worth it.

### How it gets installed

`deploy/agent-toolkit.sh` holds the installs. It is idempotent, falls back between the official marketplace's two
names, reports anything that failed and exits non-zero. Run it in the agent container on the deploy host, and again
whenever the list changes:

```bash
docker exec -i agent-runner bash < deploy/agent-toolkit.sh
```

Core runs nothing for it before a session, so a failed install never fails a turn.

## Prompt and skill changes

- `system.md`: the browser-testing line names no skill and keeps what only the agent's environment knows (one named
  `playwright-cli` session per task, screenshots under `.agent-out/screenshots/`, read before posting with
  `post_files`, stop on `cost_cap_reached`). A new line: start a new product from its stack's official template,
  turn on CodeQL default setup and required CI checks before the first merge, and say what an admin must turn on
  when `gh` can't.
- `long-project`: set the UX direction with frontend-design before the first screen and check screens against
  web-design-guidelines; test with playwright-cli and webapp-testing; review each branch with pr-review-toolkit
  before its PR.

## Verification

1. **Install:** the script exits 0 and lists every plugin and skill.
2. **Load:** a headless session on that Claude home reports in its init message the six plugins, the five skills,
   the superpowers skills and the context7 MCP server. Checked against a throwaway home before shipping; repeated on
   the live agent after deploy.
3. **Use:** in the pilot, count the skill calls in the transcript. If frontend-design, web-design-guidelines,
   pr-review-toolkit or superpowers' execution skills never fire, the prompt needs a nudge before the real specs.

## Pilot

Give the agent one representative spec (a small product with a few screens) in a new repo, and compare the result
with what the agent built before this change, or with the same spec built on a pre-change image: how the UI looks,
the design, the tests, review findings, and axe and Lighthouse results on the main screens.
