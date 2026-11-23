//! Preparing a turn, as the runner did: the plugin bundle (core/bundles.py), the memory index and MCP servers
//! (worker.py), and Claude Code's command line and environment as the Agent SDK built them.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use anyhow::{Context, Result};
use holder_proto::ToolSpec;
use regex::{Captures, Regex};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::config::McpServer;

/// The token every session sends; the account proxy swaps in a real account's.
pub const PLACEHOLDER_TOKEN: &str = "sk-ant-oat01-container-via-host-proxy";
/// The PreToolUse hook that turns away a Bash call that only waits (agent-holder's idle_wait).
const IDLE_WAIT_HOOK: &str = "agent-holder hook idle-wait";
const MEMORY_RULES: &str = r#"# Memory

Your memory is the folder {dir}, shared by all your sessions. Its index, MEMORY.md, is below; read a fact's file when its line matters to what you're doing.

- One fact per file, named for it (`people-ivan-replies.md`, `project-roster-demo.md`), starting with:
  ---
  name: <short-kebab-name>
  description: <one line: what it is and when it matters>
  ---
  then the fact, with who said it, when and the link.
- Add one line for it to MEMORY.md: `- [Title](file.md) — hook`. The index holds pointers, never the facts.
- Before you save, look for a file that already covers it and update that one. When a fact turns out wrong, fix or delete its file and its line.
- Save what you'd want next time: what people want and how, decisions, conventions, gotchas, where things are. Not what the code, git or the task record already says.
- Never write what you learn into CLAUDE.md.

## MEMORY.md
"#;

/// What one turn is run with.
#[derive(Clone, Debug, Default)]
pub struct TurnSpec {
    pub work_item: String,
    pub conversation: String,
    /// The first message: the origin line, any note, then the text and its attachments.
    pub prompt: String,
    /// Who sent it, where, and its link, as the prompt's first line; "" for a turn without one.
    pub origin: String,
    pub cwd: PathBuf,
    pub model: String,
    pub system_prompt: String,
    /// A session that sends nothing for this long is stuck, and is stopped as stalled.
    pub idle_s: u64,
    /// The whole turn's time budget (a scheduled run has one).
    pub timeout_s: Option<u64>,
    /// The turn's own variables (the packs' test keys), over the shared ones.
    pub env: BTreeMap<String, String>,
    /// MCP servers with their variables filled in (`expand_mcp`).
    pub mcp_servers: Value,
    pub plugins: Vec<PathBuf>,
    /// Claude Code settings for this session only.
    pub settings: Map<String, Value>,
    /// agentd's tools, offered as the `agent` MCP server.
    pub tools: Vec<ToolSpec>,
    pub resume_session_id: Option<String>,
    /// The account `resume_session_id` last ran on, when known.
    pub resume_account: Option<String>,
    /// How many times this work item was started before.
    pub attempts: u32,
}

/// The plugins every session loads: the core plugin's skills, the effortless plugin (picks each prompt's reasoning
/// effort) and every pack's plugin, copied under `bundles/<version>/plugins/`. The version is a hash of the contents,
/// so it changes only when they do. `src` holds the `core` and `effortless` plugins.
pub fn build_bundle(src: &Path, config_dir: &Path, bundles: &Path) -> Result<Vec<PathBuf>> {
    let mut files = Vec::new();
    for name in ["core", "effortless"] {
        collect(&src.join(name), &Path::new("plugins").join(name), &mut files)?;
    }
    let mut packs: Vec<PathBuf> = match std::fs::read_dir(config_dir.join("packs")) {
        Ok(entries) => entries.map(|e| Ok(e?.path())).collect::<std::io::Result<_>>()?,
        Err(_) => Vec::new(),
    };
    packs.sort();
    for pack in packs.iter().filter(|p| p.join("plugin").is_dir()) {
        let name = pack.file_name().unwrap_or_default();
        collect(&pack.join("plugin"), &Path::new("plugins/packs").join(name), &mut files)?;
    }
    let mut digest = Sha256::new();
    let mut contents = Vec::with_capacity(files.len());
    for (name, path) in files {
        let data = std::fs::read(&path).with_context(|| format!("reading {}", path.display()))?;
        digest.update([name.to_string_lossy().as_bytes(), b"\0", &data, b"\0"].concat());
        contents.push((name, data));
    }
    let version: String = digest.finalize()[..8].iter().map(|b| format!("{b:02x}")).collect();
    let dest = bundles.join(&version);
    if !dest.exists() {
        let tmp = bundles.join(format!(".{version}.{}.tmp", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        for (name, data) in &contents {
            std::fs::create_dir_all(tmp.join(name).parent().unwrap_or(&tmp))?;
            std::fs::write(tmp.join(name), data)?;
        }
        std::fs::create_dir_all(&tmp)?;
        // another turn may have built the same version meanwhile
        if std::fs::rename(&tmp, &dest).is_err() && dest.exists() {
            let _ = std::fs::remove_dir_all(&tmp);
        }
    }
    let plugins = dest.join("plugins");
    let mut out = vec![plugins.join("core"), plugins.join("effortless")];
    if let Ok(entries) = std::fs::read_dir(plugins.join("packs")) {
        let mut packs: Vec<PathBuf> = entries.map(|e| Ok(e?.path())).collect::<std::io::Result<_>>()?;
        packs.sort();
        out.extend(packs);
    }
    Ok(out)
}

/// Every file under `root`, as (its name under `prefix`, its path), in path order.
fn collect(root: &Path, prefix: &Path, out: &mut Vec<(PathBuf, PathBuf)>) -> Result<()> {
    let mut entries: Vec<PathBuf> = std::fs::read_dir(root).with_context(|| format!("reading {}", root.display()))?
        .map(|e| Ok(e?.path())).collect::<std::io::Result<_>>()?;
    entries.sort();
    for path in entries {
        let name = prefix.join(path.file_name().unwrap_or_default());
        if path.symlink_metadata()?.is_dir() {
            collect(&path, &name, out)?;
        } else if path.is_file() {
            out.push((name, path));
        }
    }
    Ok(())
}

/// The memory rules with the folder filled in, then its index (`MEMORY.md`) cut to `max_lines`.
pub fn memory_block(dir: &Path, max_lines: usize) -> String {
    // lossy: a write in progress can leave invalid UTF-8 mid-file; keep the readable lines
    let text = std::fs::read(dir.join("MEMORY.md")).map(|b| String::from_utf8_lossy(&b).into_owned()).unwrap_or_default();
    let lines = splitlines(&text);
    let index = if lines.is_empty() {
        "(empty)".to_string()
    } else {
        let mut index = lines[..lines.len().min(max_lines)].join("\n");
        if lines.len() > max_lines {
            index += &format!("\n(… {} more lines not shown: the index is too long, so tidy it)", lines.len() - max_lines);
        }
        index
    };
    MEMORY_RULES.replace("{dir}", &dir.display().to_string()) + &index
}

/// Python's `str.splitlines`: every line boundary it knows, `\r\n` as one.
fn splitlines(text: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let (mut start, mut chars) = (0, text.char_indices().peekable());
    while let Some((i, c)) = chars.next() {
        if matches!(c, '\n' | '\r' | '\x0b' | '\x0c' | '\x1c' | '\x1d' | '\x1e' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            lines.push(&text[start..i]);
            start = i + c.len_utf8();
            if c == '\r' && chars.next_if(|&(_, n)| n == '\n').is_some() {
                start += 1;
            }
        }
    }
    if start < text.len() {
        lines.push(&text[start..]);
    }
    lines
}

static VAR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\$\{(\w+)\}").expect("valid"));

/// MCP servers with `${VAR}` in their headers filled from `env`; a server with an unset variable is left out.
pub fn expand_mcp(servers: &BTreeMap<String, McpServer>, env: &BTreeMap<String, String>) -> Value {
    let mut out = Map::new();
    for (name, server) in servers {
        let missing: BTreeSet<&str> = server.headers.values()
            .flat_map(|h| VAR.captures_iter(h).filter_map(|c| c.get(1)).map(|m| m.as_str()))
            .filter(|v| env.get(*v).is_none_or(String::is_empty)).collect();
        if !missing.is_empty() {
            let missing: Vec<&str> = missing.into_iter().collect();
            tracing::warn!("leaving out MCP server {name}: {} not set yet", missing.join(", "));
            continue;
        }
        let headers: Map<String, Value> = server.headers.iter()
            .map(|(k, h)| (k.clone(), Value::from(VAR.replace_all(h, |c: &Captures| env[&c[1]].clone()).into_owned())))
            .collect();
        out.insert(name.clone(), json!({"type": server.kind, "url": server.url, "headers": headers}));
    }
    Value::Object(out)
}

/// `KEY=value` lines from each file in turn, a later file's over an earlier one's; a missing file is skipped.
pub fn read_env_files(paths: &[PathBuf]) -> BTreeMap<String, String> {
    let mut env = BTreeMap::new();
    for path in paths {
        let Ok(text) = std::fs::read_to_string(path) else { continue };
        for line in splitlines(&text) {
            if let Some((key, value)) = line.trim().split_once('=')
                && !key.is_empty() && !key.starts_with('#') {
                env.insert(key.to_string(), value.to_string());
            }
        }
    }
    env
}

/// Claude Code's command line: the Agent SDK's, with the idle-wait hook in the settings and agentd's tools as the
/// `agent` MCP server run by the holder. `resume` continues `session_id`; otherwise it names a new session.
pub fn claude_argv(spec: &TurnSpec, session_id: &str, resume: bool) -> Vec<String> {
    let mut argv: Vec<String> = ["claude", "--output-format", "stream-json", "--verbose", "--system-prompt"]
        .map(String::from).into();
    argv.extend([spec.system_prompt.clone(), "--model".into(), spec.model.clone(), "--permission-mode".into(),
                 "bypassPermissions".into()]);
    // the `=` form, so an id can never be read as a flag of its own
    argv.push(if resume { format!("--resume={session_id}") } else { format!("--session-id={session_id}") });
    let mut settings = spec.settings.clone();
    settings.insert("hooks".into(),
                    json!({"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": IDLE_WAIT_HOOK}]}]}));
    argv.extend(["--settings".into(), Value::Object(settings).to_string()]);
    let mut servers = spec.mcp_servers.as_object().cloned().unwrap_or_default();
    if !spec.tools.is_empty() {
        servers.insert("agent".into(),
                       json!({"type": "stdio", "command": "agent-holder", "args": ["mcp", "--conversation", spec.conversation]}));
    }
    if !servers.is_empty() {
        argv.extend(["--mcp-config".into(), json!({"mcpServers": servers}).to_string()]);
    }
    // "user" is the agent's own Claude home (CLAUDE_CONFIG_DIR): the skills, plugins and MCP servers it adds
    argv.push("--setting-sources=user,project".into());
    for plugin in &spec.plugins {
        argv.extend(["--plugin-dir".into(), plugin.display().to_string()]);
    }
    // the CLI echoes each user message it reads, with the uuid it was sent with
    argv.extend(["--replay-user-messages".into(), "--input-format".into(), "stream-json".into()]);
    argv
}

/// Claude Code's environment, over the container's own (which holds no keys): `shared` (agent.env, the Linear
/// token, the git identity, re-read every turn), the turn's own variables, then what no key file or pack can
/// override: the Claude home, the function-hooks flag the effortless plugin needs, the work item and conversation
/// (so the host monitor can count each process for its session), and the account proxy.
pub fn claude_env(spec: &TurnSpec, shared: &BTreeMap<String, String>, proxy_url: &str, claude_home: &Path)
                  -> BTreeMap<String, String> {
    let mut env = BTreeMap::from([("CLAUDE_CODE_ENTRYPOINT".to_string(), "sdk-py".to_string())]);
    env.extend(shared.iter().map(|(k, v)| (k.clone(), v.clone())));
    env.extend(spec.env.iter().map(|(k, v)| (k.clone(), v.clone())));
    let forced = [("CLAUDE_CONFIG_DIR", claude_home.display().to_string()),
                  ("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS", "1".into()),
                  ("AGENT_WORK_ITEM", spec.work_item.clone()),
                  ("AGENT_CONVERSATION", spec.conversation.clone()),
                  ("ANTHROPIC_BASE_URL", proxy_url.into()),
                  ("CLAUDE_CODE_OAUTH_TOKEN", PLACEHOLDER_TOKEN.into()),
                  // the proxy signs each session in as some teammate's seat: their claude.ai connectors aren't Jensen's
                  ("ENABLE_CLAUDEAI_MCP_SERVERS", "false".into()),
                  ("PWD", spec.cwd.display().to_string())];
    env.extend(forced.map(|(k, v)| (k.to_string(), v)));
    env
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::collections::BTreeMap;
    use std::path::{Path, PathBuf};

    #[test]
    fn memory_block_matches_python() {
        let expected: BTreeMap<String, String> =
            serde_json::from_str(&std::fs::read_to_string("tests/fixtures/memory/expected.json").unwrap()).unwrap();
        for (case, want) in expected {
            let (dir, max_lines) = case.split_once('|').unwrap();
            assert_eq!(memory_block(Path::new(dir), max_lines.parse().unwrap()), want, "{case}");
        }
    }

    #[test]
    fn expand_mcp_drops_server_with_unset_variable() {
        let server = |url: &str, header: &str| crate::config::McpServer {
            kind: "http".into(), url: url.into(), headers: BTreeMap::from([("Authorization".into(), header.into())]) };
        let servers = BTreeMap::from([("linear".to_string(), server("https://l", "Bearer ${LINEAR_APP_TOKEN}")),
                                      ("unset".into(), server("https://u", "Bearer ${NOT_SET}")),
                                      ("empty".into(), server("https://e", "Bearer ${EMPTY}")),
                                      ("plain".into(), server("https://p", "none"))]);
        let env = BTreeMap::from([("LINEAR_APP_TOKEN".to_string(), "tok".to_string()), ("EMPTY".into(), "".into())]);
        assert_eq!(expand_mcp(&servers, &env), json!({
            "linear": {"type": "http", "url": "https://l", "headers": {"Authorization": "Bearer tok"}},
            "plain": {"type": "http", "url": "https://p", "headers": {"Authorization": "none"}},
        }));
    }

    fn spec() -> TurnSpec {
        TurnSpec { work_item: "W-1".into(), conversation: "V-1".into(), cwd: "/var/agent/scratch/V-1".into(),
                   model: "claude-opus-4-8".into(), system_prompt: "be useful".into(),
                   plugins: vec![PathBuf::from("/b/plugins/core"), PathBuf::from("/b/plugins/effortless")],
                   settings: json!({"autoMemoryDirectory": "/var/agent/memory"}).as_object().unwrap().clone(),
                   ..Default::default() }
    }

    #[test]
    fn env_forces_proxy_and_config_dir_last() {
        let mut spec = spec();
        spec.env = BTreeMap::from([("CLAUDE_CONFIG_DIR".to_string(), "/elsewhere".to_string()),
                                   ("ANTHROPIC_BASE_URL".into(), "http://evil".into()), ("TEST_KEY".into(), "t".into())]);
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("agent.env"), "GH_TOKEN=old\n# NOTE=x\nnot a line\n TEST_KEY=shared\n").unwrap();
        std::fs::write(dir.path().join("linear.env"), "GH_TOKEN=gh\n").unwrap();
        let shared = read_env_files(&[dir.path().join("agent.env"), dir.path().join("missing.env"),
                                      dir.path().join("linear.env")]);
        assert_eq!(shared, BTreeMap::from([("GH_TOKEN".to_string(), "gh".to_string()), ("TEST_KEY".into(), "shared".into())]));
        let env = claude_env(&spec, &shared, "http://proxy:1", Path::new("/var/agent/claude"));
        for (key, value) in [("GH_TOKEN", "gh"), ("TEST_KEY", "t"), ("CLAUDE_CONFIG_DIR", "/var/agent/claude"),
                             ("ANTHROPIC_BASE_URL", "http://proxy:1"), ("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS", "1"),
                             ("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-container-via-host-proxy"),
                             ("ENABLE_CLAUDEAI_MCP_SERVERS", "false"), ("CLAUDE_CODE_ENTRYPOINT", "sdk-py"),
                             ("AGENT_WORK_ITEM", "W-1"), ("AGENT_CONVERSATION", "V-1"), ("PWD", "/var/agent/scratch/V-1")] {
            assert_eq!(env.get(key).map(String::as_str), Some(value), "{key}");
        }
    }

    #[test]
    fn argv_resume_and_fresh() {
        let mut spec = spec();
        let fresh = claude_argv(&spec, "S-new", false);
        assert_eq!(fresh[..4], ["claude", "--output-format", "stream-json", "--verbose"]);
        assert_eq!(fresh[fresh.len() - 2..], ["--input-format", "stream-json"]);
        assert!(fresh.contains(&"--session-id=S-new".to_string()) && !fresh.iter().any(|a| a.starts_with("--resume")));
        assert!(!fresh.contains(&"--mcp-config".to_string()), "no servers and no tools: no MCP config");
        let flag = |argv: &[String], name: &str| argv[argv.iter().position(|a| a == name).unwrap() + 1].clone();
        assert_eq!(flag(&fresh, "--system-prompt"), "be useful");
        assert_eq!(flag(&fresh, "--model"), "claude-opus-4-8");
        assert_eq!(flag(&fresh, "--permission-mode"), "bypassPermissions");
        assert!(fresh.contains(&"--setting-sources=user,project".to_string()));
        assert!(fresh.contains(&"--replay-user-messages".to_string()));
        assert_eq!(fresh.iter().filter(|a| *a == "--plugin-dir").count(), 2);
        let settings: serde_json::Value = serde_json::from_str(&flag(&fresh, "--settings")).unwrap();
        assert_eq!(settings["autoMemoryDirectory"], "/var/agent/memory");
        assert_eq!(settings["hooks"]["PreToolUse"][0]["matcher"], "Bash");
        assert_eq!(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "agent-holder hook idle-wait");

        spec.tools = vec![holder_proto::ToolSpec { name: "post_update".into(), description: "d".into(),
                                                   input_schema: json!({"type": "object"}) }];
        spec.mcp_servers = json!({"linear": {"type": "http", "url": "https://l", "headers": {}}});
        let resumed = claude_argv(&spec, "S-old", true);
        assert!(resumed.contains(&"--resume=S-old".to_string()) && !resumed.iter().any(|a| a.starts_with("--session-id")));
        let mcp: serde_json::Value = serde_json::from_str(&flag(&resumed, "--mcp-config")).unwrap();
        assert_eq!(mcp, json!({"mcpServers": {
            "linear": {"type": "http", "url": "https://l", "headers": {}},
            "agent": {"type": "stdio", "command": "agent-holder", "args": ["mcp", "--conversation", "V-1"]},
        }}));
    }

    #[test]
    fn bundle_version_stable_for_same_content() {
        let dir = tempfile::tempdir().unwrap();
        let (src, config, bundles) = (dir.path().join("src"), dir.path().join("config"), dir.path().join("bundles"));
        for (path, text) in [("src/core/skills/a/SKILL.md", "a"), ("src/effortless/hooks/h.py", "h"),
                             ("config/packs/zeta/plugin/skills/z/SKILL.md", "z"), ("config/packs/alpha/plugin/x.md", "x"),
                             ("config/packs/no-plugin/pack.toml", "")] {
            std::fs::create_dir_all(dir.path().join(path).parent().unwrap()).unwrap();
            std::fs::write(dir.path().join(path), text).unwrap();
        }
        let first = build_bundle(&src, &config, &bundles).unwrap();
        let root = first[0].parent().unwrap().to_path_buf();
        assert_eq!(first, [root.join("core"), root.join("effortless"), root.join("packs/alpha"), root.join("packs/zeta")]);
        assert_eq!(std::fs::read_to_string(root.join("packs/zeta/skills/z/SKILL.md")).unwrap(), "z");
        assert_eq!(build_bundle(&src, &config, &bundles).unwrap(), first, "same content: same version");
        std::fs::write(src.join("core/skills/a/SKILL.md"), "changed").unwrap();
        let second = build_bundle(&src, &config, &bundles).unwrap();
        assert_ne!(second[0], first[0], "new content: new version");
        assert_eq!(std::fs::read_to_string(&second[0].join("skills/a/SKILL.md")).unwrap(), "changed");
    }
}
