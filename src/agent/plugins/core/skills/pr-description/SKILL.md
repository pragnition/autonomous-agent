---
name: pr-description
description: Use before opening a PR, to write the commit messages and the PR title and body a reviewer reads.
---

# Commits and the PR description

- Commit in small, meaningful steps. Imperative subject under 72 characters ("Add copy button to citations"), then a blank line and a short why if it isn't obvious.
- Follow the repository's own conventions (CLAUDE.md, recent `git log`) over these defaults.
- Open the PR with `gh pr create`, using the repo's PR template if it has one. Its first line says you're an AI agent, who asked and where (from the first line of their message), and `--assignee` is the person who asked when you have their GitHub login. Write the rest for a reviewer:
  - **What changed**, in two to five bullets, naming the files or areas.
  - **Why**, in one sentence tied to the request.
  - **How it was verified**: the tests you added or ran, what you checked in the running app (attach the screenshots with `--attach`, see playwright-cli), what the logs showed.
  - **Anything a reviewer should look at closely**, or "Nothing unusual".
- Plain and specific. No marketing words, no emoji, no claims you didn't verify.
