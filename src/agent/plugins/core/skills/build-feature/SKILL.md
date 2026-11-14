---
name: build-feature
description: Use when asked to add, change or extend a feature, or when picking up a feature issue from Linear.
---

# Building a feature

Every new feature is built against the running app and tested end to end in a real browser, and its screenshots go in the thread. `open_pr` refuses a new feature's first PR without them, unless `no_ui` explains that nothing a user sees changed.

1. **Track it** with `track_task` (kind `feature`). If the request is genuinely ambiguous (two reasonable readings that lead to different code), `ask` one clear question. Otherwise decide, and say what you assumed.
2. **Plan briefly** in `.agent-progress.md`: the files you'll touch, the tests you'll add, the user path you'll walk in the browser, and the screenshots that will prove it works. Read the repo's CLAUDE.md first.
3. **Bring the app up before you write code** (browser-testing, step 1), so you build against it, not blind. If the feature changes an existing screen, screenshot it as it is now.
4. **Build in small steps.** Test first where it's practical, and commit as you go (pr-description). After each visible step, look at it in the browser and fix what looks wrong.
5. **Test it end to end** (browser-testing): walk the whole user path in a fresh browser session, including the empty state, one error and a long value. Read the screenshots, then post them with `post_files`. Run the full suite and the project's checks.
6. **Leave a test** for the main path, in the project's end-to-end test layout if it has one.
7. **Review and open the PR**: review-before-pr, then `open_pr`. Reply with what you built, what you assumed and how you verified it.
