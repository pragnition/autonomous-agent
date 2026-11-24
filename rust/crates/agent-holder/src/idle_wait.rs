//! The PreToolUse hook on Bash that turns away commands that only wait (a port of src/agent/runner/idle_wait.py).
//!
//! A session that holds its turn open with a blocking wait can't read anything: messages sent into it are only
//! delivered between tool calls. Short sleeps (a server coming up) and anything run in the background still pass.
//! Only what the shell runs counts: quoted strings and heredoc bodies are text, but the script of `sh -c '...'` and a
//! command substitution inside double quotes run, so they are checked like the command itself.

use std::sync::LazyLock;

use regex::Regex;

/// The longest a foreground sleep may be, in seconds: enough for a server to come up, not enough to hold a turn open.
const MAX_SLEEP_S: f64 = 10.0;

pub const IDLE_WAIT_REASON: &str = "Blocked: this command only waits, and while a Bash call is running, no message \
from the people you work with can reach you. Background agents notify you when they finish, and your session stays \
open while they run, so end your turn now (say in a line what's still running) and let the notification wake you. To \
wait for a condition (a file, a port, a CI run), use the Monitor tool instead of a sleep or a polling loop.";

// `timeout` with its options, their values and its duration, before the command it runs (timeout -s KILL 5m sleep 299)
const TIMEOUT: &str = r"timeout(?:\s+(?:-\S+|\d[\d.]*[smhd]?|[A-Z]+))+";

/// Where a command starts: the start of the text or of a line, after a control operator or an opening bracket, or
/// after a word that runs the next one.
fn sep() -> String {
    format!(r"(?:^|[\n;&|(){{}}`!]|\$\(|\b(?:then|do|else|nohup|exec|command|env|time|{TIMEOUT})\s)")
}

const QUOTED: &str = r"\x00(\d+)\x00";

static TAIL_NOTHING: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\btail\s+(?:-\S*\s+)*(?:-f|-F|--follow\S*)\s+(?:-\S*\s+)*/dev/(?:null|zero)\b").unwrap()
});
static SLEEP: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(&(sep() + r"\s*sleep\s+((?:\d*\.?\d+[smhd]?\s*)+|inf(?:inity)?\b)")).unwrap()
});
static READ_T: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&(sep() + r"\s*read\s+(?:-\S+\s+)*-\w*t\s*(\d*\.?\d+)")).unwrap());
static SHELL_C: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&(r"\b(?:ba|da|k|z)?sh\s+(?:-\S+\s+)*-[a-z]*c[a-z]*\s+".to_string() + QUOTED)).unwrap());
static PYTHON_C: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(&(r"\bpython[\d.]*\s+(?:-\S+\s+)*-c\s+".to_string() + QUOTED)).unwrap());
static PY_SLEEP: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\btime\.sleep\(\s*(\d*\.?\d+)\s*\)").unwrap());
// A command substitution inside double quotes still runs: "$(sleep 99)"
static SUBST: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\$\(([^()]*)\)|`([^`]*)`").unwrap());
static SLEEP_ARG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"(\d*\.?\d+)([smhd]?)").unwrap());

fn sleep_seconds(args: &str) -> f64 {
    if args.starts_with("inf") {
        return f64::INFINITY;
    }
    SLEEP_ARG.captures_iter(args).map(|c| {
        let unit = match &c[2] { "m" => 60.0, "h" => 3600.0, "d" => 86400.0, _ => 1.0 };
        c[1].parse::<f64>().unwrap_or(0.0) * unit
    }).sum()
}

fn word_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// The end markers of the heredocs a line starts (`<<EOF`, `<<-'EOF'`, `<< "EOF"`), not here-strings (`<<<`).
fn heredoc_ends(line: &str) -> Vec<String> {
    let c: Vec<char> = line.chars().collect();
    let mut ends = Vec::new();
    let mut i = 0;
    while i + 1 < c.len() {
        if c[i] == '<' && c[i + 1] == '<' && (i == 0 || c[i - 1] != '<') && c.get(i + 2) != Some(&'<') {
            let mut j = i + 2;
            if c.get(j) == Some(&'-') {
                j += 1;
            }
            while c.get(j).is_some_and(|ch| ch.is_whitespace()) {
                j += 1;
            }
            if c.get(j) == Some(&'\\') {
                j += 1;
            }
            let quote = c.get(j).copied().filter(|q| *q == '\'' || *q == '"');
            if quote.is_some() {
                j += 1;
            }
            if c.get(j).is_some_and(|ch| ch.is_alphabetic() || *ch == '_') {
                let start = j;
                while c.get(j).is_some_and(|ch| word_char(*ch) || *ch == '.' || *ch == '-') {
                    j += 1;
                }
                if quote.is_none() || c.get(j) == quote.as_ref() {
                    ends.push(c[start..j].iter().collect());
                    i = j + usize::from(quote.is_some());
                    continue;
                }
            }
        }
        i += 1;
    }
    ends
}

/// The command without the bodies of its heredocs: what a heredoc feeds a command is its input, not commands.
fn drop_heredoc_bodies(command: &str) -> String {
    let mut lines = Vec::new();
    let mut ends: Vec<String> = Vec::new();
    for line in command.split('\n') {
        if !ends.is_empty() {
            if line.trim_start_matches('\t') == ends[0] {
                ends.remove(0);
            }
            continue;
        }
        lines.push(line);
        ends = heredoc_ends(line);
    }
    lines.join("\n")
}

/// (the command with each quoted string replaced by its place, the quoted strings, the command substitutions inside
/// double quotes). A quoted string is one argument (a pattern, a message, a script), not a command.
fn take_out_quotes(command: &str) -> (String, Vec<String>, Vec<String>) {
    let c: Vec<char> = command.chars().collect();
    let (mut out, mut quoted, mut substituted) = (String::new(), Vec::new(), Vec::new());
    let (mut i, n) = (0, c.len());
    while i < n {
        let ch = c[i];
        if ch == '\\' {
            out.extend(&c[i..(i + 2).min(n)]);
            i += 2;
            continue;
        }
        if ch != '\'' && ch != '"' {
            out.push(ch);
            i += 1;
            continue;
        }
        let mut j = i + 1;
        while j < n && c[j] != ch {
            j += if ch == '"' && c[j] == '\\' { 2 } else { 1 };
        }
        let text: String = c[(i + 1).min(n)..j.min(n)].iter().collect();
        if ch == '"' {
            for cap in SUBST.captures_iter(&text) {
                substituted.push(cap.get(1).or(cap.get(2)).map_or("", |m| m.as_str()).to_string());
            }
        }
        out.push_str(&format!("\x00{}\x00", quoted.len()));
        quoted.push(text);
        i = j + 1;
    }
    (out, quoted, substituted)
}

/// Whether the command just holds the shell open: a follow of /dev/null, or a sleep (or `read -t`) longer than
/// MAX_SLEEP_S in any position the shell runs it, including inside `timeout`, a chain, a loop or a `sh -c` script.
pub fn idle_wait(command: &str) -> bool {
    let (cmd, quoted, substituted) = take_out_quotes(&drop_heredoc_bodies(command.trim()));
    let quote = |m: &str| m.parse::<usize>().ok().and_then(|i| quoted.get(i)).map_or("", String::as_str);
    if TAIL_NOTHING.is_match(&cmd)
        || SLEEP.captures_iter(&cmd).any(|c| sleep_seconds(c[1].trim()) > MAX_SLEEP_S)
        || READ_T.captures_iter(&cmd).any(|c| c[1].parse::<f64>().unwrap_or(0.0) > MAX_SLEEP_S)
        || PYTHON_C.captures_iter(&cmd).any(|c| {
            PY_SLEEP.captures_iter(quote(&c[1])).any(|s| s[1].parse::<f64>().unwrap_or(0.0) > MAX_SLEEP_S)
        })
    {
        return true;
    }
    SHELL_C.captures_iter(&cmd).any(|c| idle_wait(quote(&c[1]))) || substituted.iter().any(|s| idle_wait(s))
}

fn truthy(v: Option<&serde_json::Value>) -> bool {
    match v {
        Some(serde_json::Value::Bool(b)) => *b,
        Some(serde_json::Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(serde_json::Value::String(s)) => !s.is_empty(),
        Some(serde_json::Value::Array(a)) => !a.is_empty(),
        Some(serde_json::Value::Object(o)) => !o.is_empty(),
        _ => false,
    }
}

/// The hook's answer to one PreToolUse event: the deny decision for a foreground Bash command that only waits, else
/// nothing.
pub fn decide(event: &serde_json::Value) -> Option<serde_json::Value> {
    let input = event.get("tool_input");
    if event.get("tool_name").and_then(|v| v.as_str()) != Some("Bash")
        || truthy(input.and_then(|i| i.get("run_in_background")))
    {
        return None;
    }
    let command = input.and_then(|i| i.get("command")).and_then(|v| v.as_str()).unwrap_or("");
    idle_wait(command).then(|| serde_json::json!({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": IDLE_WAIT_REASON}}))
}

#[cfg(test)]
mod tests {
    use super::*;

    const CAUGHT: &[&str] = &[
        "timeout 290 tail -f /dev/null; echo waited",
        "tail -f /dev/null",
        "tail -F /dev/null",
        "sleep 300",
        "sleep 5m",
        "sleep 1h 30m",
        "sleep infinity",
        "timeout 300 sleep 290",
        "cd repo && sleep 60 && echo done",
        "echo start; sleep 120",
        "(sleep 120)",
        "x=$(sleep 99)",
        "nohup sleep 100",
        "while true; do sleep 30; done",
        "read -t 300",
        "read -r -t 120 line",
        "python -c 'import time; time.sleep(200)'",
        "bash -c 'sleep 300'",
        "sh -c \"cd /tmp && sleep 300\"",
        "timeout 300 bash -c 'sleep 299'",
        "timeout -s KILL 300 sleep 299",
        "timeout -k 5 --preserve-status 5m sleep 299",
        "cd repo\nsleep 300",
        "docker exec core sh -c 'sleep 400'",
        "x=\"$(sleep 99)\"",
    ];

    const PASSED: &[&str] = &[
        "sleep 2",
        "sleep 3 && curl -s localhost:3000",
        "npm run dev > dev.log 2>&1 & sleep 5; curl -s localhost:3000",
        "npm test & npm run lint & wait",
        "until curl -s localhost:3000; do sleep 1; done",
        "tail -f dev.log",
        "tail -n 50 /var/log/app.log",
        "echo sleep 300",
        "grep -n 'sleep(' src/app.py",
        "pytest -q",
        "git log --oneline -5",
        "grep -rn 'sleep(30)' src/",
        "sed -i 's/sleep(30)/sleep(1)/' app.py",
        "node -e 'await sleep(15000)'",
        "git commit -m \"fix: drop the retry loop; sleep 15 was never enough\"",
        "git commit -F - <<'EOF'\nfix: drop the retry loop; sleep 15 was never enough\nEOF",
        "cat > poll.py <<'EOF'\nimport time\nwhile True:\n    time.sleep(30)\nEOF\npython -m py_compile poll.py",
        "cat > wait.sh <<EOF\n#!/bin/sh\nsleep 300\nEOF",
        "wait",
        "echo a >&2; wait",
        "true && wait",
    ];

    #[test]
    fn idle_waits_are_caught() {
        for command in CAUGHT {
            assert!(idle_wait(command), "not caught: {command:?}");
        }
    }

    #[test]
    fn real_work_passes() {
        for command in PASSED {
            assert!(!idle_wait(command), "wrongly caught: {command:?}");
        }
    }

    #[test]
    fn reason_matches_python() {
        assert_eq!(IDLE_WAIT_REASON, "Blocked: this command only waits, and while a Bash call is running, no message from the people you work with can reach you. Background agents notify you when they finish, and your session stays open while they run, so end your turn now (say in a line what's still running) and let the notification wake you. To wait for a condition (a file, a port, a CI run), use the Monitor tool instead of a sleep or a polling loop.");
    }
}
