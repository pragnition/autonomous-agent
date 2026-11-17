#!/bin/bash
# Install the agent's off-the-shelf skills and plugins into its persistent Claude home (CLAUDE_CONFIG_DIR, on the
# agentwork volume), so every session loads them. Safe to run again; run it whenever this list changes:
#   docker exec -i agent-runner bash < deploy/agent-toolkit.sh
# Spec: docs/superpowers/specs/2026-11-17-off-the-shelf-toolkit-design.md
set -u
failed=()

plugin() {  # the official marketplace is named anthropic-plugin-directory in the agent's home
  claude plugin install "$1@anthropic-plugin-directory" || claude plugin install "$1@claude-plugins-official" \
    || failed+=("plugin $1")
}

skill() {  # <repo> <skill>
  npx -y skills@1.9.0 add "$1" --skill "$2" -g -a claude-code --copy -y || failed+=("skill $2")
}

claude plugin marketplace list | grep -q -e anthropic-plugin-directory -e claude-plugins-official \
  || claude plugin marketplace add anthropics/claude-plugins-official

# Left out: security-guidance (its hooks install the Agent SDK at every session start and wake a session after it has
# ended, which the runner's turn handling doesn't expect), code-review (duplicates pr-review-toolkit) and
# webapp-testing (Python Playwright, a second browser tool next to playwright-cli).
plugin superpowers        # design, plans, test-first, subagent execution with review, the pre-merge review
plugin frontend-design    # a new product's look, chosen for it rather than the generated default
plugin pr-review-toolkit  # specialist reviewer agents

# Current library docs: Context7's local MCP server, which works without an account. The context7 plugin's hosted
# server needs an OAuth login, which a headless agent can't do.
claude mcp get context7 >/dev/null 2>&1 \
  || claude mcp add -s user context7 -- npx -y @upstash/context7-mcp@4.7.3 || failed+=("mcp context7")

skill vercel-labs/agent-skills web-design-guidelines
skill vercel-labs/agent-skills vercel-react-best-practices
skill vercel-labs/agent-skills vercel-composition-patterns
skill supabase/agent-skills supabase-postgres-best-practices

claude plugin list
claude mcp list
ls "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
if [ ${#failed[@]} -gt 0 ]; then
  printf 'FAILED: %s\n' "${failed[@]}" >&2
  exit 1
fi
