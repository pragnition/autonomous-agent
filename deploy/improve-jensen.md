# improve-jensen host setup

improve-jensen (`.github/workflows/improve-jensen.yml`; see `docs/design.md`) runs on two self-hosted GitHub Actions
runners on this host: one for the watch and one for the reviews, fixes, assessments, approvals and
improvement projects, which take turns. Both run as `pragnition`, the user that owns the live checkout `~/autonomous-agent`, with
Docker and passwordless sudo, so every run has the whole host. Only people with write access can start a run.
improve-jensen reaches Claude through the account proxy's `improve-jensen` origin.

The runners share one user, so they share one HOME and one state folder, `~/.local/state/improve-jensen`: every job
reads what the others wrote there (the watch's plan, the assessment, the proposals posted in Slack), and every
deploy takes the same lock in it. Their work folders sit in `~/actions-runners/`, outside the live
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
workflow stays disabled until step 10, which logs you out. Without the plugin, reviews fall back to plain subagent
reviews and say so.

## 4. The runners

Disable the workflow, so nothing runs on the new runners before step 10, and remove the old runner and its user:

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

Then register the two runners, labelled `improve-jensen` plus `watch` for one and `work` for the other. Most of the
host belongs to Jensen, so improve-jensen runs at most one job besides the watch: while a fix or project runs, the
approvals, reviews and assessment wait their turn.

```sh
v=$(gh release view -R actions/runner --json tagName --jq .tagName | sed 's/^v//')
for name in watch work-1; do
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

Put both runners below Jensen on CPU and disk, so their jobs only take what Jensen leaves idle:

```sh
for name in watch work-1; do
  unit=actions.runner.pragnition-autonomous-agent.improve-jensen-$name.service
  sudo mkdir -p /etc/systemd/system/$unit.d
  printf '[Service]\nNice=10\nCPUWeight=20\nIOWeight=20\n' | sudo tee /etc/systemd/system/$unit.d/low-priority.conf
done
sudo systemctl daemon-reload && sudo systemctl restart 'actions.runner.pragnition-autonomous-agent.improve-jensen-*'
```

Check: the organization's Settings → Actions → Runners shows the two runners Idle in the `improve-jensen` group, and
`curl -s http://127.0.0.1:18092/proxy/whoami` answers.

## 5. Fork PRs

The repo is public and the runners have the host. A fork's PR could change a workflow to run its own job on them.
- Settings → Actions → General → "Approval for running fork pull request workflows from contributors": choose
  "Require approval for all external contributors" (older UI: "all outside collaborators"). Or:

  ```sh
  gh api -X PUT repos/pragnition/autonomous-agent/actions/permissions/fork-pr-contributor-approval \
    -f approval_policy=all_external_contributors
  ```

- Never approve a fork run on this repo. An approved run executes the fork's own code on these runners, as
  `pragnition`, with passwordless sudo and the live secrets. Read a fork's PR as a diff; if it's wanted, someone with
  write access brings it over on a branch of their own.
- `improve-jensen.yml` has no PR or PR-review triggers (a PR review runs the workflow from the PR's own merge ref), so
  every run it starts uses `main`'s workflow. Mention `@improve-jensen` in an ordinary PR comment.
- With `restricted_to_workflows: true` on the runner group, a workflow from any other file or ref never gets these
  runners either. Without an organization runner group (repo runners, below), fork-run approval is the guard for a
  fork's own workflow file.

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
docker exec agent-control python -c "import sqlite3, sys; print(sqlite3.connect('/data/db/agent.db').execute(
  \"SELECT status, attempts, json_extract(result, '$.text') FROM work_items WHERE id = ?\",
  (sys.argv[1],)).fetchone())" <item id>
```

Once it shows `claimed`, note core's start time and restart core. Compose doesn't recreate a container whose image
hasn't changed, so force it:

```sh
docker inspect -f '{{.State.StartedAt}}' agent-control
cd ~/autonomous-agent && docker compose up -d --force-recreate core
docker inspect -f '{{.State.StartedAt}}' agent-control   # must be later than the first; else the control plane never restarted
```

Within about five minutes the item must end `done` with the text `done`. If the turn doesn't survive, stop here: core
deploys must also wait for an idle runner (`src/agent/watch/deploy.py`) before the engine runs unattended.

## 8. First deploy, labels, issues and the token check

```sh
cd ~/improve-jensen-setup
python3 scripts/deploy.py   # deploys main into ~/autonomous-agent, the same as `just deploy` run there
python3 scripts/jensen_watch.py seed deploy/improve-jensen-seed.json --repo pragnition/autonomous-agent
python3 scripts/jensen_watch.py gh api 'repos/<a repo Jensen works in>/actions/runs?per_page=1'
docker exec agent-control mkdir -p /var/agent/feedback
cd && rm -rf ~/improve-jensen-setup
```

`/var/agent/feedback` on the shared volume is where Jensen writes its notes about what gets in its way, and where
improve-jensen answers them and keeps `CHANGES.md`, the list of what changed for Jensen.

The `gh api` line must answer with JSON, not a 403. If it still 403s, give Jensen a classic token with the `repo` scope
as `GH_TOKEN` in `agent.env`; the agent container reads it when recreated
(`docker compose up -d --force-recreate agent` in `~/autonomous-agent`, while Jensen is idle).

From now on, deploy with `cd ~/autonomous-agent && just deploy`. Its exit codes:
- 0: deployed and checked, or nothing to deploy;
- 1: the check failed, logged as `check_failed` in `~/.local/state/improve-jensen/deploys.jsonl`; a later plain
  deploy retries the same services until a revert or a fix lands;
- 2: the live checkout isn't deployable (dirty, diverged, or the fetch failed; logged). Fix it by hand;
- 3: pending: another deploy holds the lock. The watch retries it. A busy Jensen never makes a deploy wait:
  a change to its container pauses core, gives a running turn up to `--wait-idle-s` to finish, swaps and resumes.

`seed` creates the labels (`jensen-watch`, `ships`, `works`, `long-runs`, `people`, `fixing`, `needs-person`,
`stuck`, `project`, `proposed`, `approved`, `rejected`, `learned`, `jensen-assessment`) and files the starting issues
that aren't open already. An install switched on before Slack approval or `learned` existed runs the same `seed` line
once more, from `~/autonomous-agent` and logged in to `gh` as in step 3 (log out afterwards): it adds the new labels
(`proposed`, `approved`, `rejected`, `learned`) and skips the issues that are open already.

## 9. Slack approval

improve-jensen changes Jensen only once a person says yes. The watch and the assessment each post the changes they
want as one short numbered list in `#autonomous-agent`:

```
*improve-jensen wants to change Jensen*
1. <what will change in Jensen> (#31)
2. <what will change in Jensen> (#40)
3. Learned: <what Jensen will keep> (from <name>) (#52)
Reply here: `approve` ships them all, `approve 1 3` ships some, `reject 2` drops one.
```

A `Learned:` item comes from the assessment: something that worked that Jensen should keep, such as a way of driving
it or a skill installed for one project, credited to whose session it came from. The credit is only in Slack.

- The list goes out as Jensen's Slack bot (`SLACK_BOT_TOKEN` in `data/secrets/agent.env`), which already has the
  scopes it needs. The bot must be in the channel: it joins a public channel by itself; invite it to a private one
  (`/invite @<the bot's name>` in the channel).
- To use another channel, set the repo variable `IMPROVE_JENSEN_SLACK_CHANNEL` to its name:
  `gh variable set IMPROVE_JENSEN_SLACK_CHANNEL --body <channel> -R pragnition/autonomous-agent`. Unset, it's
  `autonomous-agent`.
- The approvers are the people in `data/config/agent.toml`: the `slack` id of each `[people.*]` entry. Replies and
  reactions from anyone else are ignored; with no one listed, nothing is approved.
- Reply in the list's thread in your own words: approve all or some, drop one, add a condition, or ask about an
  item. The listener (below) puts :eyes: on the reply at once, has Claude read it against the items and their
  issues, and answers in the thread with what it took and what happens next. A condition goes on the issue as
  `Note from Slack: <note>`, in general terms, and the fix follows it. If Claude can't be reached, the thread says
  so, and the plain words still work: `approve` (or `yes`, `ok`, `lgtm`, `ship it`) ships them all, `approve 1 3`,
  `approve 1, 3` or `approve 1-3` ships some, `reject 2` (or `no`, `skip`, `drop`) drops one. Or react on the list
  itself: `:white_check_mark:` or `:ballot_box_with_check:` approves them all, `:x:`
  or `:-1:` rejects them all; a `:+1:` doesn't approve. A later reply overrides an earlier one for the items it
  names. A dropped item can still be approved later in the same thread, by its number (`approve 2`): a bare
  `approve` or a reaction never brings it back. An approved item is final. Items nobody answers within 14 days are
  released, and a later watch can propose them again.
- Every 10 minutes, and at once after the listener stores a decision, the `approvals` job applies the decisions,
  labels each issue `approved` or `rejected`, and starts the next approved fix and the next approved project, one of each at a time; while
  core's usage watch holds the pool it starts neither. Each fix or project run first checks that its issue is still
  approved and does nothing if not. When a fix or project ends, its thread hears whether it shipped or needs a
  person (the issue then has `stuck`); a project whose runs fail twice in a row is marked `stuck` too.
- Nothing checks a change after it ships, beyond the deploy's own check: an approved change that ships is done.

The listener, `deploy/improve-jensen-listen.service`, is also the workflow's clock: GitHub's cron never fires it, so
the listener sends the `watch`, `assess` and `approvals` runs as repository_dispatch events through Jensen's `gh`
token. It reaches Claude through `~/.local/bin/claude` (installed by the workflow's Claude steps) and the proxy's
`improve-jensen` origin. Install and start it once, from `~/autonomous-agent`:

```sh
sudo cp deploy/improve-jensen-listen.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now improve-jensen-listen
journalctl -u improve-jensen-listen -f   # only warnings; quiet while all is well
```

Its first start sends nothing and only records the clock (`clock.json` in the state folder). After a deploy moves
`~/autonomous-agent`, it exits and systemd starts it on the new code.

## 10. First watch and the token check

Turn the workflow on, switch the engine on, and log out straight away, so that no job can borrow your login (the
checks below rely on it):

```sh
gh workflow enable improve-jensen.yml -R pragnition/autonomous-agent
gh variable set IMPROVE_JENSEN_ENGINE --body on -R pragnition/autonomous-agent
gh auth logout                                               # the runners share this HOME
```

Until this line, `IMPROVE_JENSEN_ENGINE` is unset (anything but `on` means off): `respond` keeps matching today's one
runner and answering issues and `@improve-jensen` comments with a PR, the old way, and `watch`, `assess` and
`approvals` never fire on the listener's clock. Nothing about the issue flow changes before it.

Start the first watch from the repo's Actions tab (improve-jensen → Run workflow, mode `watch`) and follow it there.
Once it's done:

```sh
ls -t ~/.local/state/improve-jensen/digest-*.md | head -1   # the digest it read
cat ~/.local/state/improve-jensen/plan.json                 # the reviews and the changes it planned
```

The first window is the last hour. An hour with no finished turns, no new PRs and nothing that needs attention
(the control plane unreachable, a failed deploy) starts no Claude run and writes no plan, and neither does one while
agentd's usage watch is holding scheduled runs. The changes it proposes, if any, are in the Slack channel (step 9). From here the
listener (step 9) starts the watch every 4 hours at :17 UTC, the assessment at 00:47 and 08:47 UTC (08:47 and 16:47
in Singapore; it runs only when there's new conversation since the last one), and the approvals job every 10 minutes.

Then check, once, that the jobs work on their own tokens. The `fix` and `respond` jobs check out with
`persist-credentials: false`, so Claude pushes and opens PRs as the Claude app: a push made with a job's own
`GITHUB_TOKEN` never triggers `ci`, and `gh pr checks --watch` would then wait for checks that never come.

1. In this repo, open a trivial issue: a one-word wording fix in a comment of `deploy/improve-jensen.md`, say. Ask
   in it for the run to say in its issue comment whether `gh run list --workflow improve-jensen.yml --limit 1`
   answered (the watch relies on that call).
2. From the Actions tab, run improve-jensen with mode `fix` and that issue's number as `target`. A manual run is a
   person's own choice, so it needs no approval in Slack.
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

## 11. The first assessment

Start it from the Actions tab (improve-jensen → Run workflow, mode `assess`). When it's done, read what it found:

```sh
cat ~/.local/state/improve-jensen/assessment.md
```

The pinned `jensen-assessment` issue shows the same list in general terms, and the changes it proposes, projects
included, are in the Slack channel for a person to approve. An assessment with no new conversation since the last
one (no turn of Jensen's, no note from Jensen) starts no Claude run.

## Manual runs

- A `watch` run, an `assess` run and an `approvals` run are safe to start by hand.
- A `fix` run takes one of this repo's issues as its `target`, and a `project` run a `project` issue's number. Either
  is a person's own choice and skips the approval in Slack.
- A `review` run takes a product PR's URL as its `target`, and that URL then shows in this public repo's run: in its
  inputs and in the Claude step's prompt. Leave reviews to the watch, which names a PR only by an index into
  the state folder; start one by hand only for a PR whose URL may be public.
- Never re-run a run with debug logging. It writes every tool call's full output (digests, transcripts, product
  code) into the public log.

## Turning it off

```sh
gh variable set IMPROVE_JENSEN_ENGINE --body off -R pragnition/autonomous-agent
```

This takes `respond` back to today's one runner and stops `watch`, `assess` and `approvals` firing on the
listener's clock; a
manual `workflow_dispatch` run still works regardless. Issues and `@improve-jensen` comments still get answered with
a PR, the old way, for a person to merge — nothing about the issue flow changes either side of the switch.

For a full stop — nothing runs at all, not even `respond` — log in to `gh`, and log out afterwards:

```sh
gh workflow disable improve-jensen.yml -R pragnition/autonomous-agent
```

A run already going keeps going: list them with
`gh run list -R pragnition/autonomous-agent --workflow improve-jensen.yml --status in_progress`, and stop one with
`gh run cancel <id> -R pragnition/autonomous-agent`. A cancelled fix still clears `fixing`, but a change it merged may
not be deployed yet: check the end of `deploys.jsonl`. `gh workflow enable` turns it back on, and the next watch
catches up on the hours missed.
