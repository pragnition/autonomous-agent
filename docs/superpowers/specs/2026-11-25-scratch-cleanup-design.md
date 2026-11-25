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
- it has an open park, or a turn waiting out a usage limit;
- it belongs to an active long project;
- it has a running spawned agent, as parent or child;
- its container is running;
- someone has kept its container or one of its servers;
- a container in Jensen's Docker is running from the folder (compose working folder or a bind mount), or a kept
  stack's compose working folder is in it;
- one of Jensen's services (`/var/agent/services.toml`) runs in the folder or names it in its command;
- the latest of the conversations' `last_active_at` and the container's `last_turn_at` is less than `idle_days` ago;
- for a folder with no conversation row, the folder's own and its top-level entries' modification time is less than
  `idle_days` ago.

- any clone in it was used by git within `idle_days` (HEAD, index, reflog or last fetch changed), whoever did it.

If Jensen's Docker can't be asked which folders its containers use, or a running container mounts every folder, the
pass does nothing. The in-use check (DB, the folder's top, Jensen's Docker and services) runs again for each folder
right before anything in it is touched.

### What is removed

The pass walks the folder without following symlinks and never enters `.git`. It finds:

- **Dependency and cache folders**: `node_modules`, `__pycache__`, `.next`, `.nuxt`, `.svelte-kit`, `.turbo`,
  `.parcel-cache`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.gradle`, `.pnpm-store`, `.venv` and `venv` (only
  with a `pyvenv.cfg`), and any folder holding a `CACHEDIR.TAG`. Inside a repo, one that git tracks is left alone. A
  folder with a repo anywhere in it (an editable install's checkout, a fixture) isn't a dependency folder; it is walked
  like any other.
- **Clones**: folders with a `.git` folder. A clone is removed whole only if all of these hold, and is kept
  otherwise (its dependency folders are still removed):
  - it isn't the conversation's folder itself (an agent that cloned into `.`);
  - it has a remote; a remote that is a local folder counts only if it exists inside the work volume;
  - there is no stash, and every commit HEAD or a local branch holds is on that remote's tracking branches (not
    just any remote's). Tags aren't counted: offline, a tag fetched from the remote can't be told from one made
    locally, and the live clones hold many fetched tags on archived branches;
  - there are no uncommitted changes and no untracked files, and git read all of it (no warnings);
  - no files are hidden from `git status` (assume-unchanged, or skip-worktree files present on disk);
  - there are no submodules, linked worktrees or Git LFS objects;
  - no rebase, merge, cherry-pick or bisect is in progress;
  - no folder in it is unreadable, and it holds no other clone or worktree that stays;
  - every ignored path in it is a generated one (`git status --ignored=matching`): a folder an ignore rule names
    whose own name is one of the dependency and cache folders above or `dist`, `build`, `out`, `coverage`,
    `target`, `.cache`, `test-results`, `playwright-report`, `blob-report`, `.angular`, `storybook-static`; or a
    file named `*.log`, `*.pyc`, `*.tsbuildinfo`, `.eslintcache` or `.DS_Store`. Anything else ignored (a `.env`,
    even inside `build/`, run output, a downloaded file) keeps the clone.

  Any git error keeps the clone. No network is used. git runs without system or global config, without looking above
  the repo, and with nothing the repo's config names that would start a program: fsmonitor, hooks and signature
  checks are off, and every filter it names becomes a plain copy. A pushed commit counts as safe even if the branch
  was later deleted on GitHub, since GitHub keeps every PR's commits.

Worktree folders (`.git` file) are never removed whole; only their dependency folders are.

### How it is removed

- Right after the in-use check, each target is renamed into `/var/agent/.cleanup-trash/`, which sits on the same
  volume, so the rename is instant and atomic. Jensen never sees a half-deleted folder. A target is only ever a path
  at least one level inside a conversation's folder, with no symlink on the way.
- A target agentd (uid 1001) can't rename (a folder Jensen's stacks made as root) is moved into the trash as root at
  that same moment, through Jensen's Docker, which runs as root and mounts the same volume at the same path.
- The trash is then deleted with agentd's own file walk, which counts the bytes freed. What agentd can't delete is
  removed as root; root only ever removes paths right inside the trash.
- The scratch folder and the trash must be plain folders, not symlinks, or the pass does nothing.
- Trash left by an interrupted pass is deleted at the start of the next one.

### Bookkeeping

- `agentd_cleanups(folder, cleaned_at, activity_at, freed_bytes)` records each folder's last pass. A folder isn't
  walked again until it has been active since then. A folder where git timed out or something couldn't be moved
  isn't recorded, so the next pass tries again.
- Folder activity on disk is read before the database is locked, so a slow disk never holds up the rest of agentd.
- `agentd cleanup` refuses a real pass as root, so the trash and the lock stay agentd's.
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

## Jensen's own Docker (added after the first pass went live)

The first pass freed 128 GB from the folders. Jensen's own Docker (`agent-docker`) still held about 106 GB: images
24 GB, containers 6.7 GB, volumes 54 GB, build cache 21 GB. Its test stacks also never stopped, which kept memory and
load high. Of the volumes no container used, 13.7 GB were test-stack databases (83 Postgres volumes), 7 GB were
`node_modules` volumes and 3 GB were small anonymous ones.

The same hourly pass now runs a Docker step first. A first version decided from one snapshot per pass. A review
showed that could stop a stack in use and delete a database nobody was asked about: Postgres 18 keeps its data
deeper, a volume's age was taken for its time unused, and outside traffic is invisible from the shared network. So
the step now remembers what it sees (`agentd_cleanup_stacks`, `agentd_cleanup_volumes`, `agentd_cleanup_images`) and
decides from that history.

- **A running stack** (compose project, or a lone container) is stopped only once it has been seen unused for an hour
  and nobody kept it (by stack or container name). Each pass, a stack counts as used when any of these holds:
  - a conversation working in its folder is in use (a turn queued, running, parked or waiting on a usage limit, its
    container up, an active long project or spawned agent, a kept container) or was active in the last hour;
  - one of the agent's services runs in its folder or names it;
  - a running session's or service's process names the stack, one of its containers, its folder or a published port;
  - a TCP connection into one of its containers comes from outside the stack, read from inside each container
    (`/proc/<pid>/net/tcp`). That includes a person on the tailnet, a session or another stack. Loopback and the
    stack's own containers don't count;
  - its containers' network moved 16 KB or more since the last pass (`/proc/<pid>/net/dev`, loopback left out);
  - a container is new (first seen) or started within the hour.

  A session or service whose processes can't be read stops nothing that pass. Stopping is reversible: the folder's
  `CLEANUP.md` says how to start it again.
- **A stopped container** goes once its stack has sat unused for `idle_days`: nothing running in it, nobody keeping
  it, no service in its folder, its folders quiet, and stopped that long. A container whose own writable layer holds
  over 100 MB, or a database image with no volume, is kept: it may hold data.
- **An image** goes once no container (running or stopped) has used it for `idle_days`, by the passes' own record, not
  its build date. **Build cache** older than `idle_days` goes.
- **A volume** is tracked from when no container mounts it, with the folders, stacks, mount points and images that
  used it. Once it has been unused and its folders quiet for `idle_days`:
  - it goes if it is positively rebuildable: named after `node_modules` or a cache or `.venv`, or only ever mounted
    where dependencies or caches go;
  - it is a database by any sign: database files up to four levels down (`PG_VERSION`, MySQL `ibdata1`, Mongo
    `WiredTiger`, Redis dumps, SQLite), a database image (postgres, mysql, mongo, redis, elasticsearch, mssql, neo4j and
    others), a database's data path (Postgres 18's `/var/lib/postgresql` included), or the name of a named volume
    (postgres, pg, mysql, mongo, redis, db, data, sql). It is never deleted by the cleanup. agentd starts a Jensen
    turn (source `cleanup`), at most once a day, listing these volumes by exact name with their stack, image, mount
    point, conversation, thread and channel. Jensen asks in the project's channel, or works out the project from the
    stack's name when no channel is known. It says that only the named volumes will be deleted, and only once a person
    says so. Before removing one it checks that no container uses it. A volume is asked about once; if the turn
    fails, it is asked about again;
  - anything else is kept.
- Notes about stopped stacks and removed containers and volumes go into the folder's `CLEANUP.md`. Writing a note
  doesn't count as activity in the folder.
- A dry run reports what this pass would do from the history so far, and records nothing.
- A failure in the Docker step is reported and logged, and the folder step still runs. The pass logs the disk's free
  space before and after.
