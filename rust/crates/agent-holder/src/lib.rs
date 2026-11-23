//! The process that runs inside every conversation's container: `serve` keeps Claude Code running for `agentd`,
//! `hook idle-wait` is Claude Code's PreToolUse hook, and `mcp` relays `agentd`'s tools to Claude Code.

pub mod listeners;
pub mod serve;
