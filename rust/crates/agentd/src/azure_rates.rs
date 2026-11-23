//! What Azure actually bills per token on each metered upstream's Foundry resource (a port of
//! core/azure_rates.py), so the meter prices the apps' calls at those rates instead of agent.toml's list prices.
//!
//! Azure keeps no per-caller record on a shared resource, so the tokens are Foundry's own usage report on each answer
//! the meter forwards, and only the rate comes from here: for each billing meter on the upstream's resource, Cost
//! Management's cost over its quantity for the last 30 days, which carries the discount, the cache-write price and
//! per-token add-ons such as Defender for AI. agentd reads them every 6 hours with the shared `az` login
//! ([cli.azure]), retrying a failed read after 30 minutes, and re-prices this month's calls. Until a read works, and
//! for models Azure hasn't billed on the resource yet, the meter uses agent.toml's prices.

use std::collections::BTreeSet;
use std::sync::Mutex;
use std::time::Duration as StdDuration;

use anyhow::{Result, anyhow, bail};
use chrono::{DateTime, Duration, Utc};
use rusqlite::Connection;
use serde_json::{Map, Value, json};

use crate::config::{CliRules, LlmPrice};
use crate::db::{get_setting, set_setting};
use crate::kpis::round6;
use crate::shared::Shared;

const RATES_KEY: &str = "llm_meter.azure_rates";
const ARM: &str = "https://management.azure.com";
const AZURE_HOSTS: [&str; 3] = [".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com"];
/// How a billing meter names each kind of token, after the model and context ("5.6 terra ShortCo Cd Wr Std Gl ...").
const KINDS: [(&str, &str); 4] = [("cdwr", "cache_write"), ("cdinp", "cached_input"), ("inp", "input"), ("opt", "output")];
const WINDOW_DAYS: i64 = 30;
/// How often agentd checks; a read older than FRESH_FOR is done again.
pub const CHECK_EVERY: StdDuration = StdDuration::from_secs(30 * 60);
const FRESH_FOR: Duration = Duration::hours(6);
const AZ_TIMEOUT: StdDuration = StdDuration::from_secs(120);

/// The Azure resource an upstream's URL belongs to: https://<name>.openai.azure.com/... gives <name>.
pub fn account_name(base_url: &str) -> Option<String> {
    let host = base_url.split_once("://").map_or("", |(_, rest)| rest).split(['/', '?', '#']).next().unwrap_or_default();
    let host = host.rsplit_once('@').map_or(host, |(_, h)| h).split(':').next().unwrap_or_default().to_lowercase();
    let name = host.split('.').next().unwrap_or_default().to_string();
    let valid = name.chars().next().is_some_and(|c| c.is_ascii_lowercase() || c.is_ascii_digit())
        && name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');
    (AZURE_HOSTS.iter().any(|h| host.ends_with(h)) && valid).then_some(name)
}

fn alnum(text: &str) -> String {
    text.to_lowercase().chars().filter(|c| c.is_ascii_lowercase() || c.is_ascii_digit()).collect()
}

/// How billing meters spell a model: gpt-5.6-terra as "5.6 terra", gpt-6-luna as "6-luna".
fn family(model: &str) -> String {
    let a = alnum(model);
    a.strip_prefix("gpt").map(str::to_string).unwrap_or(a)
}

/// Cost Management rows (Cost, UsageQuantity, Meter, UnitOfMeasure, MeterSubcategory) as USD per million tokens of
/// each kind for each model, and the per-token add-ons in USD per million.
pub fn parse_rates(rows: &[Value], models: &BTreeSet<String>) -> (Map<String, Value>, f64) {
    let mut totals: Vec<((String, &str), (f64, f64))> = Vec::new();
    let mut addon = (0.0, 0.0);
    for row in rows {
        let per = match row["UnitOfMeasure"].as_str().unwrap_or_default() { "1" => 1.0, "1K" => 1e3, "1M" => 1e6, _ => continue };
        let quantity = row["UsageQuantity"].as_f64().unwrap_or(0.0);
        if quantity <= 0.0 {
            continue;
        }
        let (spent, tokens) = (row["Cost"].as_f64().unwrap_or(0.0), quantity * per);
        if row["MeterSubcategory"].as_str().unwrap_or_default().to_lowercase().contains("defender") {
            addon = (addon.0 + spent, addon.1 + tokens);
            continue;
        }
        let meter = alnum(row["Meter"].as_str().unwrap_or_default());
        let Some(model) = models.iter().filter(|m| !family(m).is_empty() && meter.starts_with(&family(m)))
            .max_by_key(|m| family(m).len()) else { continue };
        let rest = &meter[family(model).len()..];
        if rest.starts_with("longco") { // the meter prices every call at the short-context rate
            continue;
        }
        let rest = rest.strip_prefix("shortco").unwrap_or(rest);
        let Some((_, kind)) = KINDS.iter().find(|(prefix, _)| rest.starts_with(prefix)) else { continue };
        match totals.iter_mut().find(|(k, _)| k.0 == *model && k.1 == *kind) {
            Some((_, t)) => *t = (t.0 + spent, t.1 + tokens),
            None => totals.push(((model.clone(), kind), (spent, tokens))),
        }
    }
    let mut rates = Map::new();
    for ((model, kind), (spent, tokens)) in totals {
        let entry = rates.entry(model).or_insert_with(|| json!({}));
        entry[kind] = json!(round6(spent / tokens * 1e6));
    }
    (rates, if addon.1 > 0.0 { round6(addon.0 / addon.1 * 1e6) } else { 0.0 })
}

pub struct AzureRates {
    /// Per upstream: {resource, deployments, rates, addon, as_of} after a read; `error` when the last one failed.
    state: Mutex<Map<String, Value>>,
    /// The [cli.azure] command and env; None reads nothing (prices from what was saved).
    rules: Option<CliRules>,
}

impl AzureRates {
    pub fn with_state(state: Map<String, Value>, rules: Option<CliRules>) -> AzureRates {
        AzureRates { state: Mutex::new(state), rules }
    }

    /// The rates saved by the last read, so a restart prices at them straight away.
    pub fn load(c: &Connection, rules: Option<CliRules>) -> Result<AzureRates> {
        let state = get_setting(c, RATES_KEY)?.and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
        Ok(AzureRates::with_state(state, rules))
    }

    fn info(&self, upstream: &str) -> Value {
        self.state.lock().unwrap_or_else(|e| e.into_inner()).get(upstream).cloned().unwrap_or_else(|| json!({}))
    }

    pub fn price(&self, upstream: &str, model: &str, listed: LlmPrice) -> LlmPrice {
        let info = self.info(upstream);
        if info.get("rates").is_none() {
            return listed;
        }
        let deployed = info["deployments"][model].as_str().unwrap_or(model);
        let billed = &info["rates"][deployed];
        let or = |k: &str, d: f64| billed[k].as_f64().unwrap_or(d);
        LlmPrice { input: or("input", listed.input), cached_input: or("cached_input", listed.cached_input),
                   output: or("output", listed.output), cache_write: billed["cache_write"].as_f64().or(listed.cache_write),
                   addon: info["addon"].as_f64().unwrap_or(0.0) }
    }

    pub fn view(&self, upstream: &str) -> Value {
        let info = self.info(upstream);
        if info.get("rates").is_none() {
            return json!({"source": "agent.toml", "error": info.get("error").cloned().unwrap_or(Value::Null)});
        }
        json!({"source": "azure", "resource": info["resource"].as_str().unwrap_or_default().rsplit('/').next(),
               "as_of": info["as_of"], "window_days": WINDOW_DAYS, "rates": info["rates"], "addon": info["addon"],
               "error": info.get("error").cloned().unwrap_or(Value::Null)})
    }

    /// Read the billed rates for every upstream on an Azure resource whose last read is stale or failed, then
    /// re-price this month's calls.
    pub async fn refresh(&self, s: &Shared, now: DateTime<Utc>) -> Result<()> {
        if self.rules.is_none() {
            return Ok(());
        }
        for (name, upstream) in &s.config.llm_meter.upstreams {
            let (Some(account), last) = (account_name(&upstream.base_url), self.info(name)) else { continue };
            let fresh = last["as_of"].as_str().and_then(|t| DateTime::parse_from_rfc3339(t).ok())
                .is_some_and(|t| last.get("error").is_none_or(Value::is_null) && now - t.with_timezone(&Utc) < FRESH_FOR);
            if fresh {
                continue;
            }
            let read = self.read(&account, now).await;
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            match read {
                Ok(info) => {
                    state.insert(name.clone(), info);
                }
                Err(e) => {
                    tracing::warn!("couldn't read Azure's billed rates for {name}: {e:#}");
                    let mut info = state.get(name).cloned().filter(Value::is_object).unwrap_or_else(|| json!({}));
                    info["error"] = json!(format!("AzureError: {e:#}").chars().take(300).collect::<String>());
                    state.insert(name.clone(), info);
                }
            }
        }
        let saved = serde_json::to_string(&*self.state.lock().unwrap_or_else(|e| e.into_inner()))?;
        let c = s.db.lock();
        set_setting(&c, RATES_KEY, &saved)?;
        crate::meter::reprice(&c, &s.config.llm_meter, Some(self), &s.config.timezone, now)
    }

    async fn read(&self, account: &str, now: DateTime<Utc>) -> Result<Value> {
        let graph = json!({"query": format!("resources | where type =~ 'microsoft.cognitiveservices/accounts' and name =~ '{account}' | project id")});
        let found = self.az(&["rest", "--method", "post", "--url",
                              &format!("{ARM}/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"),
                              "--body", &graph.to_string()]).await?;
        let ids: Vec<&str> = found["data"].as_array().into_iter().flatten().filter_map(|r| r["id"].as_str()).collect();
        if ids.len() != 1 {
            bail!("the az login sees {} Azure AI resources named {account}", ids.len());
        }
        let resource = ids[0].to_string();
        let listed = self.az(&["rest", "--url", &format!("{ARM}{resource}/deployments?api-version=2024-10-01")]).await?;
        let deployments: Map<String, Value> = listed["value"].as_array().into_iter().flatten()
            .filter_map(|d| Some((d["name"].as_str()?.to_string(), d["properties"]["model"]["name"].clone())))
            .collect();
        let grouping: Vec<Value> = ["Meter", "UnitOfMeasure", "MeterSubcategory"].iter()
            .map(|n| json!({"type": "Dimension", "name": n})).collect();
        let query = json!({"type": "ActualCost", "timeframe": "Custom",
            "timePeriod": {"from": (now - Duration::days(WINDOW_DAYS)).format("%Y-%m-%dT00:00:00Z").to_string(),
                           "to": now.format("%Y-%m-%dT%H:%M:%SZ").to_string()},
            "dataset": {"granularity": "None",
                        "aggregation": {"Cost": {"name": "Cost", "function": "Sum"},
                                        "UsageQuantity": {"name": "UsageQuantity", "function": "Sum"}},
                        "grouping": grouping,
                        "filter": {"dimensions": {"name": "ResourceId", "operator": "In", "values": [resource]}}}});
        let subscription = resource.split('/').nth(2).unwrap_or_default();
        let billed = self.az(&["rest", "--method", "post", "--url",
                               &format!("{ARM}/subscriptions/{subscription}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"),
                               "--body", &query.to_string()]).await?;
        let columns: Vec<String> = billed["properties"]["columns"].as_array().ok_or_else(|| anyhow!("no columns in the cost answer"))?
            .iter().map(|c| c["name"].as_str().unwrap_or_default().to_string()).collect();
        let rows: Vec<Value> = billed["properties"]["rows"].as_array().into_iter().flatten()
            .map(|r| Value::Object(columns.iter().cloned().zip(r.as_array().cloned().unwrap_or_default()).collect())).collect();
        let models: BTreeSet<String> = deployments.values().filter_map(|m| m.as_str().map(str::to_string)).collect();
        let (rates, addon) = parse_rates(&rows, &models);
        Ok(json!({"resource": resource, "deployments": deployments, "rates": rates, "addon": addon,
                  "as_of": now.format("%Y-%m-%dT%H:%M:%SZ").to_string()}))
    }

    async fn az(&self, args: &[&str]) -> Result<Value> {
        let rules = self.rules.as_ref().ok_or_else(|| anyhow!("no [cli.azure]"))?;
        let mut cmd = tokio::process::Command::new(&rules.command);
        cmd.args(args).args(["--output", "json"]).envs(&rules.env).stdin(std::process::Stdio::null()).kill_on_drop(true);
        let out = tokio::time::timeout(AZ_TIMEOUT, cmd.output()).await
            .map_err(|_| anyhow!("az {} took over {}s", args[0], AZ_TIMEOUT.as_secs()))??;
        if !out.status.success() {
            let err = String::from_utf8_lossy(&out.stderr);
            let last = err.lines().filter(|l| !l.trim().is_empty()).next_back();
            bail!("{}", last.map_or(format!("az {} exited {}", args[0], out.status.code().unwrap_or(-1)), str::to_string));
        }
        if out.stdout.iter().all(u8::is_ascii_whitespace) {
            return Ok(json!({}));
        }
        Ok(serde_json::from_slice(&out.stdout)?)
    }
}
