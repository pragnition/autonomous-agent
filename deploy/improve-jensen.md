# improve-jensen host setup

improve-jensen (`.github/workflows/improve-jensen.yml`; see `docs/design.md`) runs on three self-hosted GitHub Actions
runners on this host. All three run as `pragnition`, the user that owns the live checkout `~/autonomous-agent`, with
Docker and passwordless sudo, so every run has the whole host. Only people with write access can start a run.
improve-jensen reaches Claude through the account proxy's `improve-jensen` origin.

The runners share one user, so they share one HOME and one state folder, `~/.local/state/improve-jensen`: the review
and fix jobs read what the watch wrote there, and every deploy takes the same lock in it. Their work folders sit in
`~/actions-runners/`, outside the live checkout.

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
a 403 on CI checks. Whoever holds Jensen's GitHub account widens it: on github.com, Settings → Developer settings →
Fine-grained tokens → the token → Edit, then give Actions, Checks and Commit statuses read access on every repo
Jensen works in. The token's value doesn't change, so `agent.env` stays as it is. This fixes the same gap for Jensen.
Until it's done, CI shows as `unknown` and no finding rests on it.

## 3. gh and the review plugin

The runners record `PATH` when they're configured, so `gh`, `just` and `docker` must be on it before step 4.
Install `gh` and the review plugin:

```sh
sudo apt-get install -y gh     # Ubuntu 24.04 and later; older releases: GitHub's apt repo, see cli.github.com
gh auth login                  # for the commands below; the jobs bring their own tokens
claude plugin marketplace add anthropics/claude-plugins-official   # skip if `claude plugin marketplace list` has it
claude plugin install pr-review-toolkit@claude-plugins-official
```

Without the plugin, reviews fall back to plain subagent reviews and say so.

## 4. The runners

Remove the old runner and its user:

```sh
sudo bash -c 'cd /home/gha-improve-jensen/actions-runner && ./svc.sh stop && ./svc.sh uninstall'
gh api repos/pragnition/autonomous-agent/actions/runners --jq '.runners[] | "\(.id) \(.name)"'
gh api -X DELETE repos/pragnition/autonomous-agent/actions/runners/<id of improve-jensen>
sudo userdel -r gha-improve-jensen
```

On a GitHub Team plan or above (`gh api orgs/pragnition --jq .plan.name`), put the runners in an organization runner
group that only this repo's `improve-jensen.yml` on `main` can use (see step 5):

```sh
repo_id=$(gh api repos/pragnition/autonomous-agent --jq .id)
gh api -X POST orgs/pragnition/actions/runner-groups --input - <<EOF
{"name": "improve-jensen", "visibility": "selected", "selected_repository_ids": [$repo_id],
 "restricted_to_workflows": true,
 "selected_workflows": ["pragnition/autonomous-agent/.github/workflows/improve-jensen.yml@refs/heads/main"]}
EOF
url=https://github.com/pragnition; tokens=orgs/pragnition; group="--runnergroup improve-jensen"
```

On the Free plan, register them on the repo instead:

```sh
url=https://github.com/pragnition/autonomous-agent; tokens=repos/pragnition/autonomous-agent; group=
```

Then register the three runners, labelled `improve-jensen` plus `watch`, `work` and `work`:

```sh
v=$(gh release view -R actions/runner --json tagName --jq .tagName | sed 's/^v//')
for name in watch work-1 work-2; do
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

Check: the repo's (or organization's) Settings → Actions → Runners shows the three runners Idle, and
`curl -s http://127.0.0.1:18092/proxy/whoami` answers.

## 5. Fork PRs

The repo is public and the runners have the host. A fork's PR could change a workflow to run its own job on them.
- Settings → Actions → General → "Approval for running fork pull request workflows from contributors": choose
  "Require approval for all external contributors" (older UI: "all outside collaborators"). Or:

  ```sh
  gh api -X PUT repos/pragnition/autonomous-agent/actions/permissions/fork-pr-contributor-approval \
    -f approval_policy=all_external_contributors
  ```

- The runner group from step 4 runs only `improve-jensen.yml` from `main`, so a workflow from any other file or ref
  never gets these runners, a fork's included. Without the group, the approval setting is the only guard.
- With the group, a run started by a PR review or review comment (those run from the PR's merge ref) waits with no
  runner. Mention `@improve-jensen` in an ordinary PR comment instead.

## 6. Merge and protect main

Disable the workflow first, so the hourly watch doesn't start before the switch-on is done. Then merge, and protect
`main` so that CI's `python` and `dashboard` checks are required:

```sh
gh workflow disable improve-jensen.yml -R pragnition/autonomous-agent
# merge the PR that brings the engine to main
gh api -X PUT repos/pragnition/autonomous-agent/branches/main/protection --input - <<'EOF'
{"required_status_checks": {"strict": false, "contexts": ["python", "dashboard"]},
 "enforce_admins": false, "required_pull_request_reviews": null, "restrictions": null}
EOF
```

improve-jensen opens and merges its own PRs. If opening one fails with a 403, turn on Settings → Actions → General →
"Allow GitHub Actions to create and approve pull requests".

## 7. The live core-rebuild check

A running turn has to survive a core rebuild, because `just deploy` rebuilds core without waiting for an idle runner.
The live checkout gets the new scripts only at the first deploy (step 8), so work from a scratch clone of `main`.
Build core's image first, so that the rebuild during the turn is quick:

```sh
cd ~/autonomous-agent && docker compose build core
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

Once it shows `claimed`, rebuild core: `cd ~/autonomous-agent && docker compose up -d --build core`. Within about
five minutes the item must end `done` with the text `done`. If the turn doesn't survive, stop here: core deploys must
also wait for an idle runner (`src/agent/watch/deploy.py`) before the engine runs unattended.

## 8. First deploy, labels and issues

```sh
cd ~/improve-jensen-setup
python3 scripts/deploy.py   # deploys main into ~/autonomous-agent, the same as `just deploy` run there
python3 scripts/jensen_watch.py seed deploy/improve-jensen-seed.json --repo pragnition/autonomous-agent
cd && rm -rf ~/improve-jensen-setup
```

From now on, deploy with `cd ~/autonomous-agent && just deploy`. Its exit codes:
- 0: deployed and checked, or nothing to deploy;
- 1: the check failed, logged as `check_failed` in `~/.local/state/improve-jensen/deploys.jsonl`; a later plain
  deploy retries the same services until a revert or a fix lands;
- 2: the live checkout isn't deployable (dirty, diverged, or the fetch failed; logged). Fix it by hand;
- 3: pending: Jensen is busy or another deploy holds the lock. The hourly watch retries it.

`seed` creates the labels (`jensen-watch`, `ships`, `works`, `long-runs`, `people`, `fixing`, `fix-deployed`,
`needs-person`, `stuck`, `project`) and files the starting issues that aren't open already.

## 9. First watch

```sh
gh workflow enable improve-jensen.yml -R pragnition/autonomous-agent
gh workflow run improve-jensen.yml -R pragnition/autonomous-agent -f mode=watch
gh run watch -R pragnition/autonomous-agent
ls -t ~/.local/state/improve-jensen/digest-*.md | head -1   # the digest it read
cat ~/.local/state/improve-jensen/plan.json                 # the reviews and the fix it planned
```

The first window is the last hour. An hour with no finished turns, no new PRs and no `fix-deployed` issue starts no
Claude run, and neither does one while core's usage watch is holding scheduled runs. From here the watch runs every
hour at :17.

## Turning it off

```sh
gh workflow disable improve-jensen.yml -R pragnition/autonomous-agent
```

This stops the hourly watch, manual runs and the issue triggers. A run already going keeps going: list them with
`gh run list -R pragnition/autonomous-agent --workflow improve-jensen.yml --status in_progress`, and stop one with
`gh run cancel <id>`. A cancelled fix still clears `fixing`, but a change it merged may not be deployed yet: check
the end of `deploys.jsonl`. `gh workflow enable` turns it back on, and the next watch catches up on the hours missed.
