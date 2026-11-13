---
name: pr-description
description: Use before calling open_pr, to write the commit messages and the PR title and body a reviewer reads.
---

# Commits and the PR description

- Commit in small, meaningful steps. Imperative subject under 72 characters ("Add copy button to citations"), then a blank line and a short why if it isn't obvious.
- Follow the repository's own conventions (CLAUDE.md, recent `git log`) over these defaults.
- The `body` you pass to `open_pr` becomes the PR description under the first line core adds. Write it for a reviewer:
  - **What changed**, in two to five bullets, naming the files or areas.
  - **Why**, in one sentence tied to the request.
  - **How it was verified**: the tests you added or ran, what you checked in the running app, what the logs showed.
  - **Anything a reviewer should look at closely**, or "Nothing unusual".
- Plain and specific. No marketing words, no emoji, no claims you didn't verify.
