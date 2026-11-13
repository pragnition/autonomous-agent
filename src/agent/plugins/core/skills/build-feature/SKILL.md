---
name: build-feature
description: Use when asked to add or change a feature, to plan briefly, build it in small commits, test it and show it working.
---

# Building a feature

1. **Track it** with `track_task` (kind `feature`). If the request is genuinely ambiguous (two reasonable readings that lead to different code), `ask` one clear question; otherwise decide and say what you assumed.
2. **Plan briefly.** Read the relevant code and the repo's CLAUDE.md. Write a short plan in `.agent-progress.md`: the files you'll touch, the tests you'll add, how you'll show it working. Search the web for library docs when you're unsure of an API.
3. **Build in small steps.** Test first where it's practical. Commit as you go (see pr-description).
4. **Test.** The full suite and the project's checks must pass.
5. **Show it working.** For anything a user sees, follow verify-in-app and post screenshots.
6. **Review and open the PR**: review-before-pr, then `open_pr`. Reply with what you built, what you assumed and how you verified it.
