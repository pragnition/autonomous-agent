---
name: find-features
description: Use when asked to find features worth building, gaps, improvement ideas or what to build next in a project, to scout or hunt for new features, or on a scheduled scouting run.
---

# Finding features worth building

The result is a short, ranked list of features with evidence that people need them, each ready for someone to pick up. You propose them; you build one only when someone asks.

## Where to look

Use subagents to cover these in parallel:

- **What users asked for and didn't get.** The app's own traces and logs (the project's debugging skill says where): requests it refused, couldn't do or answered badly, repeated retries, and flows people abandon.
- **What the team keeps asking for.** The Linear backlog and comments (Linear MCP): recurring requests, old ideas that keep coming back. Slack messages that reach you.
- **The product itself.** Run it and walk the main flows as a new user would (browser-testing). Note friction, missing empty or error states, and anything that takes too many steps. Screenshot what you find.
- **The code.** Clusters of TODOs, feature flags left off, half-built endpoints.
- **Comparable products.** Use a web search to see how others handle the same job, but only for ideas that already have evidence from above.

## What each proposal holds

- The user outcome, as the title: "Export a chat as PDF", not "PDF service".
- Who needs it, and the evidence: links, counts, quotes, screenshots.
- The smallest version worth shipping, its rough size (S, M or L), and its risks.

Drop any idea that has no evidence.

## Where it goes

1. Search Linear for each idea first, and add your evidence to an existing issue instead of creating a duplicate.
2. File the top three to five as Linear issues in the project's team.
3. Reply in the thread with the ranked list and links to the issues.
4. Save the ideas people turn down in your memory, so you don't propose them again.
