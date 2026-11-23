//! KPIs (a port of core/kpis.py): what the agent worked on, how long it took, how much of it got done, what shipped,
//! and the tokens it used.
//!
//! Four numbers, the same whatever the work came from: Tasks, Time per task, Finished and Shipped. Tasks come from
//! the task record (task_record.rs); Shipped from GitHub's and Linear's own state (pollers.rs). Tokens come from the
//! runs table: each run counts to the project it was on when it ended, and to "general" when there was none, so a
//! window's projects add up to the window's total.

use anyhow::Result;
use chrono::{DateTime, Datelike, Duration, NaiveTime, TimeZone, Utc};
use chrono_tz::Tz;
use rusqlite::Connection;
use serde_json::{Map, Value, json};

use crate::db::{get_setting, set_setting};
use crate::shared::Shared;

pub const FMT: &str = "%Y-%m-%dT%H:%M:%SZ";
pub const SOURCES: [&str; 3] = ["slack", "linear", "schedule"];
pub const GENERAL: &str = "general";
const DIGEST_SETTING: &str = "kpi_digest_date"; // the local date of the last digest, so restarts don't post twice

/// Tokens summed over runs. No Claude cost: its estimate (API prices for a seat pool) reads as spend.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Usage {
    pub runs: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_read_tokens: i64,
    pub cache_write_tokens: i64,
}

impl Usage {
    fn add(self, o: Usage) -> Usage {
        Usage { runs: self.runs + o.runs, input_tokens: self.input_tokens + o.input_tokens,
                output_tokens: self.output_tokens + o.output_tokens,
                cache_read_tokens: self.cache_read_tokens + o.cache_read_tokens,
                cache_write_tokens: self.cache_write_tokens + o.cache_write_tokens }
    }

    pub fn json(&self) -> Map<String, Value> {
        let Usage { runs, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens } = *self;
        json!({"runs": runs, "input_tokens": input_tokens, "output_tokens": output_tokens,
               "cache_read_tokens": cache_read_tokens, "cache_write_tokens": cache_write_tokens})
            .as_object().cloned().unwrap_or_default()
    }
}

/// The apps' metered model calls (llm_usage): `runs` counts calls, and cost_usd is what the provider bills.
#[derive(Clone, Copy, Debug, Default)]
pub struct AppUsage {
    pub usage: Usage,
    pub cost_usd: f64,
}

pub fn total<'a>(parts: impl IntoIterator<Item = &'a Usage>) -> Usage {
    parts.into_iter().fold(Usage::default(), |a, b| a.add(*b))
}

pub fn ts(t: DateTime<Utc>) -> String {
    t.format(FMT).to_string()
}

pub fn round6(x: f64) -> f64 {
    (x * 1e6).round() / 1e6
}

/// Midnight of `t`'s local day in `tz`, in UTC.
pub fn local_midnight(t: DateTime<Utc>, tz: Tz) -> DateTime<Utc> {
    let day = t.with_timezone(&tz).date_naive();
    tz.from_local_datetime(&day.and_time(NaiveTime::MIN)).earliest().map_or(t, |d| d.with_timezone(&Utc))
}

/// The tokens of the runs that started in [since, until), by project ("general": none), most output first and
/// "general" last.
pub fn tokens_by_project(c: &Connection, since: DateTime<Utc>, until: DateTime<Utc>) -> Result<Vec<(String, Usage)>> {
    let mut stmt = c.prepare(
        "SELECT NULLIF(project, '') AS project, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens), \
         SUM(cache_write_tokens) FROM runs WHERE started_at >= ? AND started_at < ? GROUP BY 1")?;
    let rows = stmt.query_map([ts(since), ts(until)], |r| {
        Ok((r.get::<_, Option<String>>(0)?, Usage { runs: r.get(1)?, input_tokens: r.get(2)?, output_tokens: r.get(3)?,
                                                    cache_read_tokens: r.get(4)?, cache_write_tokens: r.get(5)? }))
    })?.collect::<rusqlite::Result<Vec<_>>>()?;
    let mut by: Vec<(String, Usage)> = Vec::new();
    for (project, u) in rows { // every run lands in exactly one row, so the rows add up to the window's total
        let key = project.unwrap_or_else(|| GENERAL.into());
        match by.iter_mut().find(|(k, _)| *k == key) {
            Some((_, have)) => *have = have.add(u),
            None => by.push((key, u)),
        }
    }
    let general = by.iter().position(|(k, _)| k == GENERAL).map(|i| by.remove(i));
    by.sort_by(|a, b| b.1.output_tokens.cmp(&a.1.output_tokens).then_with(|| a.0.cmp(&b.0)));
    by.extend(general);
    Ok(by)
}

/// The model calls the agent's apps made through the meter (llm_usage) in [since, until), by upstream.
pub fn app_tokens_by_upstream(c: &Connection, since: DateTime<Utc>, until: DateTime<Utc>) -> Result<Vec<(String, AppUsage)>> {
    let mut stmt = c.prepare(
        "SELECT upstream, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cached_tokens), SUM(cache_write_tokens), \
         SUM(cost_usd) AS usd FROM llm_usage WHERE at >= ? AND at < ? GROUP BY upstream ORDER BY usd DESC, upstream")?;
    let rows = stmt.query_map([ts(since), ts(until)], |r| {
        Ok((r.get(0)?, AppUsage { usage: Usage { runs: r.get(1)?, input_tokens: r.get(2)?, output_tokens: r.get(3)?,
                                                 cache_read_tokens: r.get(4)?, cache_write_tokens: r.get(5)? },
                                  cost_usd: round6(r.get(6)?) }))
    })?.collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(rows)
}

pub struct Window {
    pub name: &'static str,
    pub since: DateTime<Utc>,
    pub by_project: Vec<(String, Usage)>,
    pub total: Usage,
    pub apps_by_upstream: Vec<(String, AppUsage)>,
}

/// Today, the last 7 days and this month (days and months in `tz`): the agent's tokens by project with their total,
/// and its apps' metered model calls by upstream.
pub fn usage_windows(c: &Connection, now: DateTime<Utc>, tz: &str) -> Result<Vec<Window>> {
    let zone: Tz = tz.parse().unwrap_or(chrono_tz::UTC);
    let today = local_midnight(now, zone);
    let first = today.with_timezone(&zone).with_day(1).map_or(today, |d| d.with_timezone(&Utc));
    let mut out = Vec::new();
    for (name, since) in [("today", today), ("7d", now - Duration::days(7)), ("month", first)] {
        let by_project = tokens_by_project(c, since, now)?;
        let total = total(by_project.iter().map(|(_, u)| u));
        out.push(Window { name, since, by_project, total, apps_by_upstream: app_tokens_by_upstream(c, since, now)? });
    }
    Ok(out)
}

fn usages_json(rows: &[(String, Usage)]) -> Value {
    Value::Object(rows.iter().map(|(k, u)| (k.clone(), Value::Object(u.json()))).collect())
}

pub fn app_json(a: &AppUsage) -> Value {
    let mut m = a.usage.json();
    m.insert("cost_usd".into(), json!(a.cost_usd));
    Value::Object(m)
}

/// The windows as the dashboard reads them.
pub fn windows_json(windows: &[Window]) -> Value {
    Value::Object(windows.iter().map(|w| (w.name.to_string(), json!({
        "since": ts(w.since), "by_project": usages_json(&w.by_project), "total": Value::Object(w.total.json()),
        "apps_by_upstream": Value::Object(w.apps_by_upstream.iter().map(|(k, a)| (k.clone(), app_json(a))).collect()),
    }))).collect())
}

#[derive(Clone, Copy, Debug)]
pub struct Stat {
    pub n: usize,
    pub median_s: Option<f64>,
    pub p90_s: Option<f64>,
}

pub struct Kpis {
    pub since: DateTime<Utc>,
    pub until: DateTime<Utc>,
    pub tasks: i64,
    pub by_source: Vec<(String, i64)>,
    /// Each project's tasks, most first, then "general" (tasks on no project).
    pub by_project: Vec<(String, i64)>,
    /// The agent's own working time per task.
    pub time: Stat,
    pub finished: i64,
    pub merged: i64,
    pub issues_closed: i64,
    pub tokens_by_project: Vec<(String, Usage)>,
}

impl Kpis {
    pub fn finished_rate(&self) -> Option<f64> {
        (self.tasks > 0).then(|| self.finished as f64 / self.tasks as f64)
    }
}

fn stat(mut values: Vec<f64>) -> Stat {
    if values.is_empty() {
        return Stat { n: 0, median_s: None, p90_s: None };
    }
    values.sort_by(f64::total_cmp);
    let n = values.len();
    let median = if n % 2 == 1 { values[n / 2] } else { (values[n / 2 - 1] + values[n / 2]) / 2.0 };
    Stat { n, median_s: Some(median), p90_s: Some(values[(0.9 * n as f64).ceil() as usize - 1]) }
}

/// Tasks that started in [since, until); PRs merged and Linear issues completed in it.
pub fn compute_kpis(c: &Connection, since: DateTime<Utc>, until: DateTime<Utc>) -> Result<Kpis> {
    let (lo, hi) = (ts(since), ts(until));
    let rows: Vec<(String, Option<String>, i64, Option<i64>)> = c
        .prepare("SELECT source, project, work_time_s, finished FROM task_records WHERE started_at >= ? AND started_at < ?")?
        .query_map([&lo, &hi], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)))?
        .collect::<rusqlite::Result<_>>()?;
    let mut by_source: Vec<(String, i64)> = SOURCES.iter().map(|s| (s.to_string(), 0)).collect();
    let mut projects: Vec<(String, i64)> = Vec::new();
    for (source, project, _, _) in &rows {
        match by_source.iter_mut().find(|(k, _)| k == source) {
            Some((_, n)) => *n += 1,
            None => by_source.push((source.clone(), 1)),
        }
        if let Some(p) = project.as_ref().filter(|p| !p.is_empty()) {
            match projects.iter_mut().find(|(k, _)| k == p) {
                Some((_, n)) => *n += 1,
                None => projects.push((p.clone(), 1)),
            }
        }
    }
    projects.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    let general = rows.iter().filter(|r| r.1.as_deref().unwrap_or_default().is_empty()).count() as i64;
    if general > 0 {
        projects.push((GENERAL.into(), general));
    }
    let count = |sql: &str| -> Result<i64> { Ok(c.query_row(sql, [&lo, &hi], |r| r.get(0))?) };
    Ok(Kpis {
        since, until, tasks: rows.len() as i64, by_source, by_project: projects,
        time: stat(rows.iter().map(|r| r.2 as f64).collect()),
        finished: rows.iter().filter(|r| r.3.unwrap_or(0) != 0).count() as i64,
        merged: count("SELECT COUNT(*) FROM agent_prs WHERE merged_at >= ? AND merged_at < ?")?,
        issues_closed: count("SELECT COUNT(*) FROM linear_issues WHERE completed_at >= ? AND completed_at < ?")?,
        tokens_by_project: tokens_by_project(c, since, until)?,
    })
}

fn pairs(rows: &[(String, i64)]) -> Value {
    Value::Object(rows.iter().map(|(k, n)| (k.clone(), json!(n))).collect())
}

/// The KPIs as the dashboard reads them.
pub fn kpis_json(k: &Kpis) -> Value {
    json!({"since": ts(k.since), "until": ts(k.until), "tasks": k.tasks, "by_source": pairs(&k.by_source),
           "by_project": pairs(&k.by_project),
           "time": {"n": k.time.n, "median_s": k.time.median_s, "p90_s": k.time.p90_s},
           "finished": k.finished, "merged": k.merged, "issues_closed": k.issues_closed,
           "tokens_by_project": usages_json(&k.tokens_by_project), "finished_rate": k.finished_rate()})
}

pub fn format_duration(seconds: f64) -> String {
    let s = seconds as i64;
    match s {
        _ if s < 60 => format!("{s}s"),
        _ if s < 3600 => format!("{}m", s / 60),
        _ if s < 86400 => format!("{}h {}m", s / 3600, s % 3600 / 60),
        _ => format!("{}d {}h", s / 86400, s % 86400 / 3600),
    }
}

pub fn format_tokens(n: i64) -> String {
    if n < 1000 {
        n.to_string()
    } else if n < 1_000_000 {
        format!("{:.1}k", n as f64 / 1000.0).replace(".0k", "k")
    } else {
        format!("{:.1}M", n as f64 / 1_000_000.0).replace(".0M", "M")
    }
}

pub fn format_usage(u: &Usage) -> String {
    format!("{} in · {} out · {} cache read · {} cache write", format_tokens(u.input_tokens),
            format_tokens(u.output_tokens), format_tokens(u.cache_read_tokens), format_tokens(u.cache_write_tokens))
}

fn plural(count: i64, one: &str, many: &str) -> String {
    format!("{count} {}", if count == 1 { one } else { many })
}

/// Tokens by project for today, 7 days and this month, and the apps' metered calls by upstream.
pub fn format_usage_windows(windows: &[Window]) -> String {
    let mut lines = Vec::new();
    for w in windows {
        let label = match w.name { "today" => "today", "7d" => "last 7 days", _ => "this month" };
        lines.push(format!("Tokens {label}: {}", format_usage(&w.total)));
        lines.extend(w.by_project.iter().map(|(p, u)| format!("  {p}: {}", format_usage(u))));
        lines.extend(w.apps_by_upstream.iter().map(|(name, a)| format!(
            "  app meter, {name}: {} · {} · ${:.2} billed", plural(a.usage.runs, "call", "calls"), format_usage(&a.usage),
            a.cost_usd)));
    }
    lines.join("\n")
}

pub fn format_kpis(k: &Kpis, name: &str) -> String {
    let days = ((k.until - k.since).num_seconds() as f64 / 86400.0).round_ties_even().max(1.0) as i64;
    let split = k.by_project.iter().map(|(p, n)| format!("{p} {n}")).collect::<Vec<_>>().join(" · ");
    let time = match (k.time.n, k.time.median_s, k.time.p90_s) {
        (n, Some(m), Some(p)) if n > 0 => format!("median {} · p90 {}", format_duration(m), format_duration(p)),
        _ => "n/a".into(),
    };
    let rate = k.finished_rate().map_or("n/a".into(), |r| format!("{:.0}%", r * 100.0));
    let mut lines = vec![
        format!("{name}, last {}", plural(days, "day", "days")),
        format!("Tasks: {}{}", k.tasks, if split.is_empty() { String::new() } else { format!(" ({split})") }),
        format!("Time per task: {time}"),
        format!("Finished: {rate} ({} of {})", k.finished, k.tasks),
        format!("Shipped: {} merged · {} closed", plural(k.merged, "PR", "PRs"),
                plural(k.issues_closed, "Linear issue", "Linear issues")),
        format!("Tokens: {}", format_usage(&total(k.tokens_by_project.iter().map(|(_, u)| u)))),
    ];
    lines.extend(k.tokens_by_project.iter().map(|(p, u)| format!("  {p}: {}", format_usage(u))));
    lines.join("\n")
}

/// The `kpis` tool's reply: the KPIs over `days` and the token windows.
pub fn tool_text(c: &Connection, days: Option<&Value>, now: DateTime<Utc>, name: &str, tz: &str) -> Result<String> {
    let days = match days {
        None => 7,
        Some(Value::Number(n)) => match n.as_i64() {
            Some(d) if (1..=365).contains(&d) => d,
            _ => return Ok("days must be a whole number from 1 to 365".into()),
        },
        _ => return Ok("days must be a whole number from 1 to 365".into()),
    };
    let k = compute_kpis(c, now - Duration::days(days), now)?;
    Ok(format!("{}\n\n{}", format_kpis(&k, name), format_usage_windows(&usage_windows(c, now, tz)?)))
}

/// The daily KPI digest: once per local day, at or after digest_time, to digest_channel.
pub async fn post_digest(s: &Shared, now: DateTime<Utc>) -> Result<bool> {
    let ob = &s.config.observability;
    if ob.digest_channel.is_empty() {
        return Ok(false);
    }
    let local = now.with_timezone(&s.config.timezone.parse::<Tz>().unwrap_or(chrono_tz::UTC));
    let today = local.date_naive().to_string();
    let text = {
        let c = s.db.lock();
        if local.format("%H:%M").to_string() < ob.digest_time || get_setting(&c, DIGEST_SETTING)?.as_deref() == Some(&today) {
            return Ok(false);
        }
        format_kpis(&compute_kpis(&c, now - Duration::days(7), now)?, &s.config.name)
    };
    s.slack.post(&ob.digest_channel, None, &text).await?;
    set_setting(&s.db.lock(), DIGEST_SETTING, &today)?;
    Ok(true)
}
