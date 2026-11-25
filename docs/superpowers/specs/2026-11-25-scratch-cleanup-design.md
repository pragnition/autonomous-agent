# Cleaning up the conversations' folders without losing what Jensen made

## Intent

Every conversation gets its own folder under `/var/agent/scratch/<conversation>`, and nothing ever deletes them.
On 2026-11-25 there were 1,214 of them, holding 333 GB of the host's 937 GB disk. They were growing by roughly
28 GB a day, so the disk would have filled within about a week.

The folders aren't where Jensen's memory lives. Its memory notes are in `/var/agent/memory` and its transcripts are in
`/var/agent/claude`, and neither is touched here. A sample of 60 folders (16 GB) broke down like this:

- 46% installed dependencies: `node_modules` and Python environments;
- 53% clones of repos, nearly all of it already on GitHub (2 of 23 clones had uncommitted changes and 2 had
  unpushed commits);
- 0.3% files Jensen wrote itself: notes, specs, screenshots, Slack attachments and logs.

agentd now frees what can be rebuilt and keeps everything else. Deletion is permanent.

## Goals

- A conversation's folder that has been quiet for `[cleanup] idle_days` (default 3) loses:
  - installed dependencies and tool caches anywhere in it;
  - every clone whose work is all on its remote.
- Nothing Jensen made is lost: its own files, and any clone with uncommitted, untracked, stashed or unpushed work.
- A folder that is in use is never touched.
- A `CLEANUP.md` in the folder says what was removed and how to get each clone back, so a thread that comes back
  to life can carry on.
- `agentd cleanup --dry-run` shows what a pass would do without changing anything.

## Non-goals

- Jensen's own Docker (images, build cache, the test stacks' volumes). That's a separate change.
- Touching the memory folder, transcripts, Jensen's home or the playwright browsers.
- Summarising or distilling transcripts. They take 4 GB, so storage isn't a reason to.

## Anti-goals

- Deleting the only copy of anything: a file Jensen wrote, a commit, an untracked file, a stash or a `.env`.
- Leaving a half-deleted clone or `node_modules` that Jensen would trip over.
- Slowing the host down noticeably while it runs.

## Why 3 days

On 2026-11-25 the folders' last activity broke down as: under 1 day 140, 1–2 days 182, 2–3 days 176, 3–7 days 513,
over 7 days 190. A 7-day cutoff would free only 190 folders. A thread that comes back after 3 days costs Jensen
a re-clone and an install (a minute or two), and no work is lost. The setting can be changed; 0 turns the job off.

## Approaches considered

1. **Remove rebuildable parts of quiet folders (chosen).** Dependencies everywhere, plus clones whose work is all
   pushed. This frees about 99% of a quiet folder and loses nothing.
2. **Dependencies only.** Never touch clones. This frees about half, and clones keep piling up at about 15 GB a day.
3. **Archive whole folders** (compress, or move to cheaper storage). Dependencies and git objects barely compress,
   and Jensen would need a way to restore them. It's more machinery for less space.

## How it works

### When a folder is in use (skipped)

A folder is matched to every conversation whose id is its name, or whose `cwd` lies inside it (spawned agents
working in a parent's folder). It is skipped if any of the following holds:

- one of those conversations has a work item that is queued, claimed or delivered;
- it has an open park;
- it belongs to an active long project;
- it has a running spawned agent, as parent or child;
- its container is running;
- someone has kept its container or one of its servers;
- a container in Jensen's Docker is running from the folder (compose working folder or a bind mount), or a kept
  stack's compose working folder is in it;
- the latest of the conversations' `last_active_at` and the container's `last_turn_at` is less than `idle_days` ago;
- for a folder with no conversation row, the folder's own and its top-level entries' modification time is less than
  `idle_days` ago.

If Jensen's Docker can't be asked which folders its containers use, the pass does nothing. The in-use check runs
again for each folder right before anything in it is touched.

### What is removed

The pass walks the folder without following symlinks and never enters `.git`. It finds:

- **Dependency and cache folders**: `node_modules`, `__pycache__`, `.next`, `.nuxt`, `.svelte-kit`, `.turbo`,
  `.parcel-cache`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.gradle`, `.pnpm-store`, `.venv` and `venv` (only
  with a `pyvenv.cfg`), and any folder holding a `CACHEDIR.TAG`. Inside a repo, one that git tracks is left alone.
- **Clones**: folders with a `.git` folder. A clone is removed whole only if all of these hold, and is kept
  otherwise (its dependency folders are still removed):
  - it has a remote;
  - every commit on HEAD and on every local branch is on a remote-tracking branch;
  - there is no stash;
  - there are no uncommitted changes and no untracked files;
  - there are no submodules and no linked worktrees;
  - no rebase, merge, cherry-pick or bisect is in progress;
  - every ignored path in it is a generated one: the dependency and cache folders above, plus `dist`, `build`,
    `out`, `coverage`, `target`, `.cache`, `test-results`, `playwright-report`, `blob-report`, `.angular`,
    `storybook-static`, and `*.log`, `*.pyc`, `*.tsbuildinfo`, `.eslintcache` and `.DS_Store` files. Anything else
    ignored (a `.env`, run output, a downloaded file) keeps the clone.

  Any git error keeps the clone. No network is used. A pushed commit counts as safe even if the branch was later
  deleted on GitHub, since GitHub keeps every PR's commits.

Worktree folders (`.git` file) are never removed whole; only their dependency folders are.

### How it is removed

- Right after the in-use check, each target is renamed into `/var/agent/.cleanup-trash/`, which sits on the same
  volume, so the rename is instant and atomic. Jensen never sees a half-deleted folder.
- The trash is then deleted with agentd's own file walk, which counts the bytes freed.
- Files agentd (uid 1001) can't delete are removed through Jensen's Docker, which runs as root and mounts the same
  volume at the same path. Paths are checked to lie under `/var/agent/scratch/` or the trash. This covers things
  Jensen's stacks created as root, and any target that can't be renamed for the same reason.
- Trash left by an interrupted pass is deleted at the start of the next one.

### Bookkeeping

- `agentd_cleanups(folder, cleaned_at, activity_at, freed_bytes)` records each folder's last pass. A folder isn't
  walked again until it has been active since then.
- Folders are taken quietest first. A pass stops starting new work after 50 minutes and carries on in the next one.
  The first passes after deploy have hundreds of folders to get through.
- A pass runs every hour from `agentd serve`. A lock file (`/var/agent/.cleanup.lock`) keeps a pass and
  `agentd cleanup` from running at once.
- The pass's thread runs at the lowest CPU and disk priority (nice 19, best-effort I/O level 7; not the idle class,
  which could starve on a disk that is never quiet), and the git commands it starts inherit that priority.
- Each pass logs one summary line: folders cleaned, bytes freed, clones kept and why, and anything it failed to delete.

### CLEANUP.md

Each pass that changes a folder adds a section to `CLEANUP.md` at the folder's top: the date, the dependency folders
removed (count plus up to 20 paths), each removed clone with its remote URL (credentials stripped), branch and commit,
and a one-line command to get it back, plus each kept clone and why it was kept.

## Constraints

- agentd runs as uid 1001 in `agent-control` with the work volume at `/var/agent` and the Docker socket. git is in
  the image.
- Jensen's Docker (`agent-docker`) mounts the same volume at `/var/agent`.
- The host is overloaded (load in the hundreds), so the pass must be gentle and bounded.

## Left to the builder

- The exact layout of `CLEANUP.md` and the summary line.
- The git command lines, timeouts and output parsing.
