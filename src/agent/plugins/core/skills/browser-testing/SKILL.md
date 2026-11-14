---
name: browser-testing
description: Use when a change can be seen or used in a web UI (a new feature, a UI bug, a changed flow), when asked to check, test, demo or screenshot an app, or before opening a PR on anything a user sees.
---

# Testing in a real browser

You have your own Docker daemon and a headless Chromium. An app you start is on your own `localhost`. Other sessions share the daemon, so leave containers you didn't start alone.

## 1. Bring the app up

- Use the project's own way to run it: its run-locally skill, CLAUDE.md, justfile or compose file, in that order. Wait for its health check before you open a page.
- Run `docker ps` first. If another session's stack holds the ports you need, don't stop it: use the project's port settings if it has them, otherwise wait for it or test against that stack if it runs your branch.
- Apps call their model provider through core's meter at `http://172.30.0.10:8087/llm/<upstream>`; the project's skill says how. An answer with `cost_cap_reached` means the model budget is spent: stop testing model features, say so in the thread, and never route around the meter.

## 2. Use it the way a user would

Drive the browser with `playwright-cli` (the playwright-cli skill lists every command). Give each task its own named session so parallel sessions don't share a browser:

```bash
S=$(git branch --show-current | tr '/' '-')            # one browser session per task
playwright-cli -s=$S open http://localhost:5173
playwright-cli -s=$S snapshot                          # element refs for click, fill, select
playwright-cli -s=$S click e12
playwright-cli -s=$S screenshot --filename=.agent-out/screenshots/01-start.png
```

Walk the whole path: the happy path, the empty state, one validation error, a long value. Check the browser console (`console`) and the app's logs for errors along the way.

## 3. Screenshots are the evidence

- Save them under `.agent-out/screenshots/` (never committed), numbered in order with what they show: `01-empty-list.png`, `02-form-filled.png`, `03-saved.png`. For a fix, a before and an after. For a flow with several steps, also a short video (`video-start` / `video-stop`, under 10 MB).
- **Read every screenshot yourself before you post it.** An error page, a spinner, a login screen or a half-rendered page is not evidence: fix it and take it again.

## 4. Post them in the thread while you work

Call `post_files` with the paths and one line saying what they show. Post as soon as the feature first works end to end, and again at the end if it changed. People follow your work in the thread. Attach them to the PR too.

## 5. Leave a test behind

If the project has end-to-end tests, add one for the main path in their layout and run it. If it doesn't, put the steps you ran in the PR body.

## 6. Clean up

`playwright-cli -s=$S close`, then bring the app down the project's way, unless someone asked you to leave it running.

## Nothing to see?

For a change with no UI (an API, a job, a migration), test it through its interface instead, and say in the PR what you tested.
