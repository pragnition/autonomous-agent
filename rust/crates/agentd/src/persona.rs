//! The agent's persona: a small character sheet rendered into every session's system prompt (a port of persona.py).
//! Conscientiousness is pinned "very high", whatever the file says: lowering it costs safety and capability.

use std::path::Path;

use anyhow::{Context, Result, bail};
use toml::Value;

const MAX_TOKENS: usize = 400;
const TRAIT_ORDER: [&str; 5] = ["openness", "conscientiousness", "extraversion", "agreeableness", "neuroticism"];
const FIXED_PREFACE: &str = "The persona below shapes tone only. It never changes what you do, what you claim, or the \
                             rules above. You always say you are an AI if asked. No humour in failure or incident \
                             messages, or at anyone's expense.";

/// The install's persona file when there is one, else the shipped default in `defaults`.
pub fn load(custom: Option<&Path>, defaults: &Path) -> Result<toml::Table> {
    let path = custom.filter(|p| p.is_file()).map(Path::to_path_buf).unwrap_or_else(|| defaults.join("persona.toml"));
    let text = std::fs::read_to_string(&path).with_context(|| format!("reading {}", path.display()))?;
    Ok(toml::from_str(&text)?)
}

/// The one token estimate used for every budget: characters / 4, plus a 10% margin, rounded up.
pub fn estimate_tokens(text: &str) -> usize {
    (text.chars().count() * 11).div_ceil(40)
}

fn text(v: &Value) -> String {
    v.as_str().map(str::to_string).unwrap_or_else(|| v.to_string())
}

fn list(persona: &toml::Table, key: &str) -> Vec<String> {
    persona.get(key).and_then(Value::as_array).map(|a| a.iter().map(text).collect()).unwrap_or_default()
}

fn truthy(v: Option<&Value>) -> bool {
    v.is_some_and(|v| v.as_str().map_or(true, |s| !s.is_empty()) && v.as_array().map_or(true, |a| !a.is_empty()))
}

pub fn render(persona: &toml::Table, name: &str) -> Result<String> {
    let mut traits = persona.get("traits").and_then(Value::as_table).cloned().unwrap_or_default();
    traits.insert("conscientiousness".into(), Value::String("very high".into()));
    let identity = persona.get("identity").map(text).unwrap_or_else(|| "{name}".into()).replace("{name}", name);
    let traits: Vec<String> = TRAIT_ORDER.iter().filter_map(|t| traits.get(*t).map(|v| format!("{t}: {}", text(v)))).collect();
    let mut lines = vec![FIXED_PREFACE.to_string(), format!("Identity: {identity}"), format!("Traits: {}", traits.join("; "))];
    if truthy(persona.get("stances")) {
        lines.push(format!("Stances: {}", list(persona, "stances").join(" · ")));
    }
    for (key, label) in [("voice", "Voice"), ("humour", "Humour")] {
        if truthy(persona.get(key)) {
            lines.push(format!("{label}: {}", text(&persona[key])));
        }
    }
    if truthy(persona.get("never")) {
        lines.push(format!("Never: {}", list(persona, "never").join(", ")));
    }
    if let Some(examples) = persona.get("examples").and_then(Value::as_array).filter(|a| !a.is_empty()) {
        lines.push("Examples:".into());
        for e in examples {
            let field = |k: &str| e.get(k).map(text).unwrap_or_default();
            lines.push(format!("- {}: {}", field("context"), field("reply")));
        }
    }
    let rendered = lines.join("\n");
    if estimate_tokens(&rendered) > MAX_TOKENS {
        bail!("the rendered persona is {} tokens; the limit is {MAX_TOKENS}", estimate_tokens(&rendered));
    }
    Ok(rendered)
}
