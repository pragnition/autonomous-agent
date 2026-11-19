---
name: ui-audit
description: Use when asked to polish, clean up, tidy or make consistent a product's UI or UX, when someone says it looks amateur, inconsistent or unpolished, or for a design or UI audit of an existing app.
---

# A measured UI audit

"It looks amateur" becomes numbers you can show and check after the fix. Count the drift, look at every main page, rank what you find, fix it back onto the design system, then count and look again.

## Before you start

- Read the repo's CLAUDE.md, its design doc and its design tokens (colour, type, spacing, radius), and any repo skill about its UI. If the repo already has an audit skill or scripts, use those and improve them rather than starting again.
- For what to check and how to judge it, use the impeccable plugin's `audit`, `critique` and `polish` (check `claude plugin list`). Without it, use frontend-design and the web-design-guidelines skill. Don't redo their checklists here: this skill adds the measuring and the loop.
- Run the app with realistic data and sign in as a real role would (playwright-cli). Keep your scripts in your conversation folder under `scripts/`, so you can run them again after the fix.

## 1. Measure

Split the work across subagents, one per area or group of pages, so no single pass runs into a time limit. Each one returns counts and file:line lists, not opinions. Count at least:

- **In the code:** distinct font sizes, weights, radii, colours and spacing values written as raw values instead of tokens; controls (buttons, inputs, selects, tables) built by hand instead of from the shared components; page headers, titles and empty states that don't use the shared pattern; one-off classes that copy a shared one.
- **In the browser:** a playwright-cli script that visits every main page and, from computed styles, counts distinct font sizes, radii and colours in use, and the controls left with browser default styling. It saves a full-page screenshot of each page to `.agent-out/screenshots/before-<page>.png`.

Look at every screenshot yourself.

## 2. Rank

Put each problem in one list: what it is, where, how many places, and the token or component it should use. Rank by how many pages and people it touches and how visible it is. Post the counts and the top of the list in the thread before you change anything big, so people can redirect you.

## 3. Fix

Fix the top of the list by moving code onto the existing tokens and components. Add a token or component only when a real gap has several users, and say so in the design doc. Don't change what a page does, its copy or its layout beyond the list. Fix by area with subagents in parallel, then build and run the tests.

## 4. Measure again

Run the same scripts. Every count you set out to reduce should drop, and nothing else should rise. Take `after-<page>.png` screenshots and compare them with the before ones. The PR shows the before and after counts in a table and attaches the screenshot pairs. If the repo has no UI skill yet, add one with the scripts and the rules you used, so the next pass starts from them.
