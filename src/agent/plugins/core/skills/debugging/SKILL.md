---
name: debugging
description: Use when something is broken or behaves unexpectedly - an error report, a crash, a failing test or check, a regression, an alert, a "why does X happen" question, or a bug ticket from Slack or Linear.
---

# Debugging

Nobody should have to tell you where to look. Find the cause before you change code; a fix without a known cause is a guess. Keep notes in `.agent-progress.md` (never committed) so you can pick up after an interruption.

1. **Track it.** `track_task` with kind `bug` when you'll change code. If the request is only a question ("why does X happen?"), investigate and answer; no task, no PR.

2. **Gather every signal before touching code.** Use subagents to read logs and code in parallel when that's faster.
   - The report: the Slack thread (`read_thread`) or the Linear issue (Linear MCP). Exact error, who, when, steps, screenshots.
   - Deployed environments: logs, traces and metrics with `az`. The project's debugging skill says which subscription, resource group, workspace and queries. Narrow the time window around the report, count failures by type, then follow one failing request end to end.
   - Recent changes: `git log -p --since=...` on the failing area, recent deploys, the PRs that touched it.
   - Memory: check your notes on the area. It may have happened before.

3. **Reproduce it.** Write the smallest test that fails for the reason the evidence shows, and watch it fail. If the bug only shows in the UI, reproduce it in the browser (browser-testing) and screenshot the broken state. If you can't reproduce it, say what you tried and fix from the evidence.

4. **Find the root cause.** Trace from the symptom back to the first wrong value, and write the cause in one sentence before fixing. If three fixes in a row don't work, stop and question your model of the bug instead of trying a fourth.

5. **Fix it** with the smallest change that makes the failing test pass. Run the full suite and the project's checks.

6. **Verify it the way the reporter would.** Run the reproduction again. For a UI bug, post the before and after screenshots together (`post_files`). For an error in the logs, give the query that will show it's gone after deploy.

7. **Close the loop.** review-before-pr, then push your branch and open the PR with `gh pr create` (pr-description). Reply in the thread with the link, the cause, the fix and the evidence.
