---
name: verify-in-app
description: Use when a change is visible in the running app, to bring the app up in the sandbox, check it with Playwright and share screenshots.
---

# Verifying in the running app

1. **Bring the app up** with the project's own way of running it (its `docker compose` file, or the commands in CLAUDE.md or the project's skills). The sandbox has its own Docker; nothing you start here touches anyone else. Wait for its health check.
2. **Check the change with Playwright.** Script the steps a user would take, assert on what they should see, and take screenshots at the key moments (before and after when it's a fix). Record a video for flows with several steps.
3. **Look at the screenshots yourself** before claiming anything. If something looks wrong, fix it and check again.
4. **Share the evidence** with `post_files`: the screenshots (and video), with one line saying what each shows.
5. **Leave the test behind** when it's worth keeping: add the Playwright script to the repo's e2e tests if the project has them.
6. Bring the app down when you're done.
