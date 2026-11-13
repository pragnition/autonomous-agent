---
name: fix-bug
description: Use when asked to fix something broken (a crash, an error, wrong behaviour, a failing test), to gather every signal yourself, reproduce it, fix it and prove the fix.
---

# Fixing a bug

Nobody should have to tell you where to look. Work through these steps and keep notes in a progress file in the worktree (`.agent-progress.md`) so you can pick up after an interruption.

1. **Track it.** Call `track_task` (kind `bug`) if the conversation isn't tracking a task yet.
2. **Gather signals before touching code.**
   - Production: call `ask_production` with a specific question ("what fails in citation export, since when, how often, with which stack traces?"). Ask follow-ups until you know the failing path. Check the project's debugging skill for where its logs and traces live.
   - The code: recent commits around the failing area (`git log -p --since=...`), the stack trace's files, related tests.
   - The report: the Slack thread (`read_thread`) or the Linear issue (`linear_issue`).
   - Use subagents to read code and logs in parallel when that's faster.
3. **Reproduce it with a failing test.** Write the smallest test that triggers the bug as the evidence describes, and run it to see it fail for the right reason. If the bug only shows in the running app, reproduce it there (see verify-in-app). If you can't reproduce it, say so later and fix from the evidence anyway.
4. **Fix it.** The smallest change that makes the test pass. Run the full test suite and the project's checks.
5. **Verify.** If the bug is visible in the app, check it there too (verify-in-app). Have a subagent review the diff (review-before-pr).
6. **Open the PR** with `open_pr` (see pr-description). Then reply in the thread: the cause, the fix, and how you verified it.
