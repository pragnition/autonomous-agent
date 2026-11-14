---
name: review-before-pr
description: Use right before open_pr, to have a fresh-context subagent review the diff against the request.
---

# Fresh eyes before the PR

Start a subagent with a clean context and give it only: the original request (quoted), the diff (`git diff <default-branch>...HEAD`), and the repository's CLAUDE.md. Ask it to report:

- Does the change do what was asked, all of it and nothing more?
- Bugs, missed edge cases, and error handling that would bite a user.
- Tests: do they prove the behaviour, or only that the code runs?
- Anything that breaks the repo's conventions.

Fix what it finds that is real, rerun the tests, and mention anything you chose not to change in the PR body.
