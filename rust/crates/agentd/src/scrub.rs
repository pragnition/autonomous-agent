//! Secrets: core's own, read from its mounted file into memory only, and stripped from text that leaves agentd (a
//! port of scrub.py and secrets.py).

use std::collections::BTreeMap;
use std::path::Path;
use std::sync::LazyLock;

use regex::Regex;

pub const REDACTED: &str = "[redacted]";
/// A known secret shorter than this is too likely to occur in ordinary text to replace.
const MIN_EXACT: usize = 8;

static PATTERNS: LazyLock<Vec<Regex>> = LazyLock::new(|| {
    [r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", r"sk-ant-[A-Za-z0-9_-]{10,}",
     r"gh[pousr]_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"xox[abpr]-[A-Za-z0-9-]{10,}",
     r"lin_api_[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}"]
        .iter().map(|p| Regex::new(p).expect("valid")).collect()
});
static COMMENT: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+#").expect("valid"));

/// Known secret values (longest first), then token and key patterns, replaced with `[redacted]`.
pub fn scrub(text: &str, secrets: &[String]) -> String {
    let mut values: Vec<&String> = secrets.iter().filter(|v| v.chars().count() >= MIN_EXACT).collect();
    values.sort_by_key(|v| std::cmp::Reverse(v.len()));
    values.dedup();
    let mut text = text.to_string();
    for value in values {
        text = text.replace(value.as_str(), REDACTED);
    }
    for pattern in PATTERNS.iter() {
        text = pattern.replace_all(&text, REDACTED).into_owned();
    }
    text
}

/// `KEY=value` lines: `export ` allowed, a quoted value kept whole, ` #` starting a comment otherwise. A missing file
/// is no secrets.
pub fn load_secrets(path: &Path) -> BTreeMap<String, String> {
    let mut out = BTreeMap::new();
    let Ok(text) = std::fs::read_to_string(path) else { return out };
    for line in text.lines().map(str::trim) {
        if line.is_empty() || line.starts_with('#') || !line.contains('=') {
            continue;
        }
        let (key, value) = line.strip_prefix("export ").unwrap_or(line).split_once('=').unwrap_or_default();
        let value = value.trim();
        let quote = value.chars().next().filter(|q| *q == '"' || *q == '\'');
        let value = match quote.and_then(|q| value[1..].find(q).map(|end| &value[1..1 + end])) {
            Some(inside) => inside,
            None => COMMENT.splitn(value, 2).next().unwrap_or_default(),
        };
        out.insert(key.trim().to_string(), value.to_string());
    }
    out
}
