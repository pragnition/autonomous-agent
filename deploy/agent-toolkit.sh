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

plugin superpowers        # design, plan, test-first, subagent execution with review
plugin frontend-design    # a deliberate look, not the generated one
plugin pr-review-toolkit  # specialist reviewers before each PR
plugin code-review-anthropics  # the directory lists Anthropic's code-review under this name
plugin security-guidance
plugin context7           # current library docs

skill vercel-labs/agent-skills web-design-guidelines
skill vercel-labs/agent-skills vercel-react-best-practices
skill vercel-labs/agent-skills vercel-composition-patterns
skill anthropics/skills webapp-testing
skill supabase/agent-skills supabase-postgres-best-practices

claude plugin list
ls "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
if [ ${#failed[@]} -gt 0 ]; then
  printf 'FAILED: %s\n' "${failed[@]}" >&2
  exit 1
fi
