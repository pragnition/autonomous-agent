# improve-jensen host setup

improve-jensen (`.github/workflows/improve-jensen.yml`; see `docs/design.md`) runs on five self-hosted GitHub Actions
runners on this host: one for the hourly watch and four for the reviews, fixes, assessments, the yardstick and
improvement projects. All five run as `pragnition`, the user that owns the live checkout `~/autonomous-agent`, with
Docker and passwordless sudo, so every run has the whole host. Only people with write access can start a run.
improve-jensen reaches Claude through the account proxy's `improve-jensen` origin.

The runners share one user, so they share one HOME and one state folder, `~/.local/state/improve-jensen`: every job
reads what the others wrote there (the watch's plan, the assessment, the yardstick's tasks and results, the gate
record), and every deploy takes the same lock in it. Their work folders sit in `~/actions-runners/`, outside the live
checkout.

Switch it on when Jensen is idle and a person is around, in this order. Run everything as `pragnition`.

## 1. Proxy origin

Add to `~/.claude/accounts/proxy.toml` under `[origins]`, then restart the proxy
(`systemctl --user restart claude-fallback-proxy`):

```toml
improve-jensen = 18092
```

To show its usage separately on the dashboard, leave it out of `[dashboard] agent_origins`.

## 2. Jensen's GitHub token

improve-jensen reads Jensen's PRs with Jensen's own token (`GH_TOKEN` in `data/secrets/agent.env`), which today gets
a 403 on CI checks. Fine-grained tokens have no Checks permission, so `gh pr checks` may keep failing; the watch and
the reviews then read the Actions runs on the PR's head commit instead.

Whoever holds Jensen's GitHub account widens the token: on github.com, Settings → Developer settings → Fine-grained
tokens → the token → Edit, then give Actions, Commit statuses, Pull requests and Contents read access on every repo
Jensen works in. The token's value doesn't change, so `agent.env` stays as it is. This fixes the same gap for Jensen.
Step 8 checks it. Until it works, CI shows as `unknown` and no finding rests on it.

## 3. gh and the review plugin

The runners record `PATH` when they're configured, so `gh`, `just` and `docker` must be on it before step 4.
Install `gh` and the review plugin:

```sh
sudo apt-get install -y gh     # Ubuntu 24.04 and later; older releases: GitHub's apt repo, see cli.github.com
gh auth login                  # for the commands below; the jobs bring their own tokens
claude plugin marketplace add anthropics/claude-plugins-official   # skip if `claude plugin marketplace list` has it
claude plugin install pr-review-toolkit@claude-plugins-official
```

Your `gh` login lands in the HOME the runners share, where any `gh` call without `GH_TOKEN` acts as you. The
workflow stays disabled until step 9, which logs you out. Without the plugin, reviews fall back to plain subagent
reviews and say so.

## 4. The runners

Disable the workflow, so nothing runs on the new runners before step 9, and remove the old runner and its user:

```sh
gh workflow disable improve-jensen.yml -R pragnition/autonomous-agent
sudo bash -c 'cd /home/gha-improve-jensen/actions-runner && ./svc.sh stop && ./svc.sh uninstall'
gh api repos/pragnition/autonomous-agent/actions/runners --jq '.runners[] | "\(.id) \(.name)"'
gh api -X DELETE repos/pragnition/autonomous-agent/actions/runners/<id of improve-jensen>
sudo userdel -r gha-improve-jensen
```

Put the runners in an organization runner group that only this repo's `improve-jensen.yml` on `main` can use (step 5).
Runner groups and organization registration tokens need the `admin:org` scope on your login:

```sh
gh auth refresh -h github.com -s admin:org
repo_id=$(gh api repos/pragnition/autonomous-agent --jq .id)
gh api -X POST orgs/pragnition/actions/runner-groups --input - \
  --jq '{allows_public_repositories, restricted_to_workflows, selected_workflows}' <<EOF
{"name": "improve-jensen", "visibility": "selected", "selected_repository_ids": [$repo_id],
 "allows_public_repositories": true, "restricted_to_workflows": true,
 "selected_workflows": ["pragnition/autonomous-agent/.github/workflows/improve-jensen.yml@refs/heads/main"]}
EOF
url=https://github.com/pragnition; tokens=orgs/pragnition; group="--runnergroup improve-jensen"
```

The response must show `allows_public_repositories: true`; without it, this public repo's jobs never get the runners.
It should also show `restricted_to_workflows: true`. If it comes back `false`, the plan doesn't honour the workflow
restriction, and fork-run approval (step 5) is the only guard. (On a plan without runner groups, register on the repo
instead: `url=https://github.com/pragnition/autonomous-agent; tokens=repos/pragnition/autonomous-agent; group=`.)

Then register the five runners, labelled `improve-jensen` plus `watch` for one and `work` for the other four. A
project or a yardstick run can hold a `work` runner for hours, so four keep the reviews within the hour:

```sh
v=$(gh release view -R actions/runner --json tagName --jq .tagName | sed 's/^v//')
for name in watch work-1 work-2 work-3 work-4; do
  mkdir -p ~/actions-runners/$name && cd ~/actions-runners/$name
  curl -fsSL -o runner.tar.gz \
    https://github.com/actions/runner/releases/download/v$v/actions-runner-linux-x64-$v.tar.gz
  tar xzf runner.tar.gz
  ./config.sh --unattended --url "$url" \
    --token "$(gh api -X POST $tokens/actions/runners/registration-token --jq .token)" \
    --name "improve-jensen-$name" --labels "improve-jensen,${name%-*}" $group
  sudo ./svc.sh install pragnition && sudo ./svc.sh start
done
```

An install that already has `watch`, `work-1` and `work-2` runs the loop for `work-3 work-4` only.

Check: the organization's Settings → Actions → Runners shows the five runners Idle in the `improve-jensen` group, and
`curl -s http://127.0.0.1:18092/proxy/whoami` answers.

## 5. Fork PRs

The repo is public and the runners have the host. A fork's PR could change a workflow to run its own job on them.
- Settings → Actions → General → "Approval for running fork pull request workflows from contributors": choose
  "Require approval for all external contributors" (older UI: "all outside collaborators"). Or:

  ```sh
  gh api -X PUT repos/pragnition/autonomous-agent/actions/permissions/fork-pr-contributor-approval \
    -f approval_policy=all_external_contributors
  ```

- Never approve a fork run on this repo. An approved run executes the fork's own code on these runners: a run
  started by a review on its PR checks out the PR's merge ref, installs it with `pip install -e` and follows its
  instruction files, all as `pragnition`, with passwordless sudo and the live secrets. Read a fork's PR as a diff;
  if it's wanted, someone with write access brings it over on a branch of their own.
- With `restricted_to_workflows: true`, the runner group runs only `improve-jensen.yml` from `main`, so a workflow
  from any other file or ref never gets these runners, a fork's included.
- That also means a run started by a PR review or review comment (those run from the PR's merge ref) waits with no
  runner. Mention `@improve-jensen` in an ordinary PR comment instead.

## 6. Merge and protect main

Merge the PR that brings the engine to `main`. Then protect `main` so that CI's `python` and `dashboard` checks are
required:

```sh
gh api -X PUT repos/pragnition/autonomous-agent/branches/main/protection --input - <<'EOF'
{"required_status_checks": {"strict": false, "contexts": ["python", "dashboard"]},
 "enforce_admins": false, "required_pull_request_reviews": null, "restrictions": null}
EOF
```

improve-jensen opens and merges its own PRs. If opening one fails with a 403, turn on Settings → Actions → General →
"Allow GitHub Actions to create and approve pull requests".

## 7. The live core-rebuild check

A running turn has to survive a core restart, because `just deploy` rebuilds core without waiting for an idle runner.
The live checkout gets the new scripts only at the first deploy (step 8), so work from a scratch clone of `main`:

```sh
git clone https://github.com/pragnition/autonomous-agent ~/improve-jensen-setup && cd ~/improve-jensen-setup
cat > long-turn.md <<'EOF'
This is a live check of the stack, not a request from a person. Run this with Bash in the foreground, with a Bash
timeout of 300000 ms:
python3 -c "import time; time.sleep(240)"
Then reply with the single word done.
EOF
python3 scripts/ask_jensen.py --new --text-file long-turn.md   # prints the item id
```

A bare `sleep` would be moved to the background by Claude Code and end the turn early. The conversation is a
`V-review-*` one with no Slack thread, so nothing is posted. Read the item's state with:

```sh
docker exec agent-core python -c "import sqlite3, sys; print(sqlite3.connect('/data/db/agent.db').execute(
  \"SELECT status, attempts, json_extract(result, '$.text') FROM work_items WHERE id = ?\",
  (sys.argv[1],)).fetchone())" <item id>
```

Once it shows `claimed`, note core's start time and restart core. Compose doesn't recreate a container whose image
hasn't changed, so force it:

```sh
docker inspect -f '{{.State.StartedAt}}' agent-core
cd ~/autonomous-agent && docker compose up -d --force-recreate core
docker inspect -f '{{.State.StartedAt}}' agent-core   # must be later than the first; else core never restarted
```

Within about five minutes the item must end `done` with the text `done`. If the turn doesn't survive, stop here: core
deploys must also wait for an idle runner (`src/agent/watch/deploy.py`) before the engine runs unattended.

## 8. First deploy, labels, issues and the token check

```sh
cd ~/improve-jensen-setup
python3 scripts/deploy.py   # deploys main into ~/autonomous-agent, the same as `just deploy` run there
python3 scripts/jensen_watch.py seed deploy/improve-jensen-seed.json --repo pragnition/autonomous-agent
python3 scripts/jensen_watch.py yardstick install deploy/yardstick
python3 scripts/jensen_watch.py gh api 'repos/<a repo Jensen works in>/actions/runs?per_page=1'
docker exec agent-runner mkdir -p /var/agent/feedback
cd && rm -rf ~/improve-jensen-setup
```

`yardstick install` copies the seed tasks into the state folder (`~/.local/state/improve-jensen/yardstick/tasks/`)
before the first watch: a gate a job starts with no tasks installed can't be measured, and holds every deploy that
changes how Jensen behaves until it expires.

`/var/agent/feedback` in `agent-runner` is where Jensen writes its notes about what gets in its way, and where
improve-jensen answers them and keeps `CHANGES.md`, the list of what changed for Jensen.

The `gh api` line must answer with JSON, not a 403. If it still 403s, give Jensen a classic token with the `repo` scope
as `GH_TOKEN` in `agent.env`; the agent container reads it when recreated
(`docker compose up -d --force-recreate agent` in `~/autonomous-agent`, while Jensen is idle).

From now on, deploy with `cd ~/autonomous-agent && just deploy`. Its exit codes:
- 0: deployed and checked, or nothing to deploy;
- 1: the check failed, logged as `check_failed` in `~/.local/state/improve-jensen/deploys.jsonl`; a later plain
  deploy retries the same services until a revert or a fix lands;
- 2: the live checkout isn't deployable (dirty, diverged, or the fetch failed; logged). Fix it by hand;
- 3: pending: Jensen is busy or another deploy holds the lock. The hourly watch retries it.

`seed` creates the labels (`jensen-watch`, `ships`, `works`, `long-runs`, `people`, `fixing`, `fix-deployed`,
`needs-person`, `stuck`, `project`, `gating`, `priority`, `jensen-assessment`) and files the starting issues that
aren't open already. An install switched on before the assessment existed runs the same `seed` line once more, from
`~/autonomous-agent` and logged in to `gh` as in step 3 (log out afterwards): it adds the new labels (`gating`,
`priority`, `jensen-assessment`) and skips the issues that are open already. Before its next watch, it also runs
`python3 scripts/jensen_watch.py yardstick install deploy/yardstick` there.

## 9. First watch and the token check

Turn the workflow on and log out straight away, so that no job can borrow your login (the checks below rely on it):

```sh
gh workflow enable improve-jensen.yml -R pragnition/autonomous-agent
gh auth logout                                               # the runners share this HOME
```

Start the first watch from the repo's Actions tab (improve-jensen → Run workflow, mode `watch`) and follow it there.
Once it's done:

```sh
ls -t ~/.local/state/improve-jensen/digest-*.md | head -1   # the digest it read
cat ~/.local/state/improve-jensen/plan.json                 # the reviews and the fix it planned
```

The first window is the last hour. An hour with no finished turns, no new PRs, no `fix-deployed` issue and nothing
that needs attention (core unreachable, a failed deploy) starts no Claude run and writes no plan, and neither does
one while core's usage watch is holding scheduled runs. From here the watch runs every hour at :17, the assessment
every 6 hours at :13, and the yardstick nightly at 19:07 UTC (03:07 Singapore time) and as a gate after any job that
starts one.

Then check, once, that the jobs work on their own tokens. The `fix` and `respond` jobs check out with
`persist-credentials: false`, so Claude pushes and opens PRs as the Claude app: a push made with a job's own
`GITHUB_TOKEN` never triggers `ci`, and `gh pr checks --watch` would then wait for checks that never come.

1. In this repo, open a trivial issue: a one-word wording fix in a comment of `deploy/improve-jensen.md`, say. Ask
   in it for the run to say in its issue comment whether `gh run list --workflow improve-jensen.yml --limit 1`
   answered (the watch relies on that call).
2. From the Actions tab, run improve-jensen with mode `fix` and that issue's number as `target`.
3. When it's done, check that:
   - the PR's author is the Claude app, `claude[bot]`, not `github-actions[bot]`;
   - `ci`'s `python` and `dashboard` checks ran on the PR's last push (the PR's Checks tab);
   - the run's comment says `gh run list` answered, and its log shows `gh pr create` and `gh pr checks` answering
     with results, not an authentication error.

If any check fails, disable the workflow from the Actions tab (improve-jensen → ⋯ → Disable workflow), cancel any
run still going, and close the test PR and issue. A `github-actions[bot]` author, or a push that started no `ci`
run, means the push went out on the job's token: check that the Claude GitHub App is installed on this repo, and
that the `fix` and `respond` checkouts still say `persist-credentials: false`. A `gh` that isn't authenticated, or
a `git push` refused for lack of credentials, inside the Claude step means the action didn't hand on its token: the
Claude steps then need a token of their own, a workflow change for a person to make. Never log `gh` back in on this
host to paper over either.

## 10. The yardstick baseline

The seed tasks are in the state folder since step 8. Run the yardstick once to set the baseline that later runs are
compared with. Start the run from the Actions tab (improve-jensen → Run workflow, mode `yardstick`), or with
`gh workflow run improve-jensen.yml -R pragnition/autonomous-agent -f mode=yardstick` from a machine logged in to
`gh`. Start it at a quiet hour: it runs Jensen through each task in a silent `V-yardstick-` conversation, at most four
at once, and can take a few hours. When it's done:

```sh
cd ~/autonomous-agent && python3 scripts/jensen_watch.py yardstick score
```

## 11. The first assessment

Start it from the Actions tab (improve-jensen → Run workflow, mode `assess`). When it's done, read what it found:

```sh
cat ~/.local/state/improve-jensen/assessment.md
```

The pinned `jensen-assessment` issue shows the same list in general terms, and the same run's `project` job shows
whether it started an improvement project. An assessment with nothing new since the last one, and no project
waiting, starts no Claude run.

## The yardstick tasks

The tasks live on the host, in `~/.local/state/improve-jensen/yardstick/tasks/`: the seeds from `deploy/yardstick/`,
and the ones the assessment adds from Jensen's real failures (synthetic reproductions that never go into this repo).
improve-jensen adds tasks but never edits or deletes one. Only a person retires a task, by moving its folder:

```sh
mkdir -p ~/.local/state/improve-jensen/yardstick/retired
mv ~/.local/state/improve-jensen/yardstick/tasks/<id> ~/.local/state/improve-jensen/yardstick/retired/
```

New seeds in `deploy/yardstick/` reach the host with `yardstick install deploy/yardstick` again; it copies only the
tasks that aren't there yet.

## Manual runs

- A `watch` run, a `fix` run for one of this repo's issues, an `assess` run, and a `yardstick` run are safe to start
  by hand. A `project` run takes a `project` issue's number as its `target`.
- A gate run is improve-jensen's own. When a watch, fix, project or respond job ends with a gate active (it ran
  `gate start` after a change), the same run's `yardstick` job measures that change, and a failed gate reverts it.
  A manual `yardstick` run never gates or reverts anything: start one only while
  `python3 scripts/jensen_watch.py gate status` (in `~/autonomous-agent`) shows none, so it isn't compared with the
  change being measured.
- A `review` run takes a product PR's URL as its `target`, and that URL then shows in this public repo's run: in its
  inputs and in the Claude step's prompt. Leave reviews to the hourly watch, which names a PR only by an index into
  the state folder; start one by hand only for a PR whose URL may be public.
- Never re-run a run with debug logging. It writes every tool call's full output (digests, transcripts, product
  code) into the public log.

## Turning it off

Log in to `gh` for this, and log out afterwards:

```sh
gh workflow disable improve-jensen.yml -R pragnition/autonomous-agent
```

This stops the hourly watch, manual runs and the issue triggers. A run already going keeps going: list them with
`gh run list -R pragnition/autonomous-agent --workflow improve-jensen.yml --status in_progress`, and stop one with
`gh run cancel <id> -R pragnition/autonomous-agent`. A cancelled fix still clears `fixing`, but a change it merged may
not be deployed yet: check the end of `deploys.jsonl`. `gh workflow enable` turns it back on, and the next watch
catches up on the hours missed.

A gate the yardstick couldn't measure (its run died, the host suspended, or core's usage watch held it) stays
active, and the next job to end with it active, the hourly watch at the latest, starts its gate run again. Deploys
that change how Jensen behaves wait meanwhile, and the gate expires after 12 hours. To give up on it sooner and keep
the change, run `cd ~/autonomous-agent && python3 scripts/jensen_watch.py gate end --result passed`; the next watch
removes `gating` from its issue.
