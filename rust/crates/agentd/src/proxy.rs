//! The host's Claude account proxy: which account a session is pinned to, each account's usage, and the soft cap on
//! each account's 5-hour window (a port of src/agent/proxy.py).

//!
//! An account counts as full at the soft cap (65%) of its 5-hour window until 90 minutes before the window resets,
//! and at 100% from then on: what is left would go unused at the reset anyway. An unknown reset time keeps the soft
//! cap.

use std::collections::BTreeMap;
use std::time::Duration;

use anyhow::{Result, anyhow, bail};
use serde_json::Value;

/// Account statuses that move a session at once.
pub const ACCOUNT_TROUBLE: [&str; 2] = ["rejected", "disabled"];

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct SoftCap {
    /// The share of the 5-hour window an account may use ...
    pub limit: f64,
    /// ... until this long before the window resets; then all of it.
    pub lift_s: f64,
}

impl Default for SoftCap {
    fn default() -> SoftCap {
        SoftCap { limit: 0.65, lift_s: 5400.0 }
    }
}

impl SoftCap {
    /// From SOFT_CAP_5H and SOFT_CAP_LIFT_S, as core read them.
    pub fn from_env(get: impl Fn(&str) -> Option<String>) -> SoftCap {
        let default = SoftCap::default();
        let num = |k: &str, d: f64| get(k).and_then(|v| v.trim().parse::<f64>().ok()).filter(|v| !v.is_nan()).unwrap_or(d);
        SoftCap { limit: num("SOFT_CAP_5H", default.limit).clamp(0.0, 1.0),
                  lift_s: num("SOFT_CAP_LIFT_S", default.lift_s).max(0.0) }
    }

    pub fn cap(&self, reset_5h: Option<f64>, now: f64) -> f64 {
        if reset_5h.is_some_and(|r| r - now <= self.lift_s) { 1.0 } else { self.limit }
    }

    /// Unknown use is not over: the proxy's rank decides.
    pub fn over(&self, used_5h: Option<f64>, reset_5h: Option<f64>, now: f64) -> bool {
        used_5h.is_some_and(|u| u >= self.cap(reset_5h, now))
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct AccountUsage {
    pub status: String,
    pub rank: Option<i64>,
    pub verified: bool,
    pub used_5h: Option<f64>,
    pub reset_5h: Option<f64>,
}

#[derive(Clone, Debug)]
pub struct Usage {
    pub accounts: BTreeMap<String, AccountUsage>,
    pub soft_cap: SoftCap,
    /// When it was read, in epoch seconds.
    pub now: f64,
}

impl Usage {
    /// The proxy's `/proxy/usage` answer: one object per account code, plus `_all`.
    pub fn from_raw(raw: &Value, soft_cap: SoftCap, now: f64) -> Usage {
        let num = |v: Option<&Value>| v.filter(|v| v.is_number()).and_then(Value::as_f64);
        let accounts = raw.as_object().into_iter().flatten()
            .filter(|(code, rec)| *code != "_all" && rec.is_object())
            .map(|(code, rec)| {
                let window = rec.get("5h").and_then(Value::as_array).filter(|w| w.len() == 2);
                (code.clone(), AccountUsage {
                    status: rec.get("status").and_then(Value::as_str).unwrap_or_default().to_string(),
                    rank: rec.get("rank").and_then(Value::as_i64),
                    verified: rec.get("verified").and_then(Value::as_bool).unwrap_or(false),
                    used_5h: num(window.map(|w| &w[0])),
                    reset_5h: num(window.map(|w| &w[1])),
                })
            }).collect();
        Usage { accounts, soft_cap, now }
    }

    pub fn has_capacity(&self) -> bool {
        self.accounts.values().any(|a| a.rank.is_some())
    }

    pub fn status_of(&self, code: Option<&str>) -> Option<&str> {
        self.accounts.get(code?).map(|a| a.status.as_str())
    }

    pub fn in_trouble(&self, code: Option<&str>) -> bool {
        self.status_of(code).is_some_and(|s| ACCOUNT_TROUBLE.contains(&s))
    }

    pub fn over_cap(&self, code: Option<&str>) -> bool {
        code.and_then(|c| self.accounts.get(c))
            .is_some_and(|a| self.soft_cap.over(a.used_5h, a.reset_5h, self.now))
    }

    /// The account a fresh session would move to from `code`, which is over its soft cap: the proxy's top-ranked
    /// account, when that is another one under its own cap. Otherwise None, and the session stays: the proxy picks a
    /// fresh session's account by rank, so moving would land it on a capped account and move again.
    pub fn handoff_target(&self, code: Option<&str>) -> Option<String> {
        let (_, top) = self.accounts.iter().filter_map(|(c, a)| Some((a.rank?, c))).min()?;
        (Some(top.as_str()) != code && !self.over_cap(Some(top))).then(|| top.clone())
    }
}

/// The proxy's local API, as core and the runner used it.
#[derive(Clone)]
pub struct ProxyClient {
    base_url: String,
    http: reqwest::Client,
    pub soft_cap: SoftCap,
}

impl ProxyClient {
    pub fn new(base_url: &str, soft_cap: SoftCap) -> ProxyClient {
        let http = reqwest::Client::builder().timeout(Duration::from_secs(10)).build().expect("an HTTP client");
        ProxyClient { base_url: base_url.trim_end_matches('/').to_string(), http, soft_cap }
    }

    async fn get(&self, path: &str, query: &[(&str, &str)]) -> Result<reqwest::Response> {
        let resp = self.http.get(format!("{}{path}", self.base_url)).query(query).send().await
            .map_err(|e| anyhow!("proxy unreachable: {e}"))?;
        if resp.status().is_server_error() {
            bail!("proxy answered {}", resp.status());
        }
        Ok(resp)
    }

    /// The proxy's answer as it gave it (the usage watch reads more of it than `Usage` keeps).
    pub async fn raw_usage(&self) -> Result<Value> {
        Ok(self.get("/proxy/usage", &[]).await?.error_for_status()?.json().await?)
    }

    pub async fn usage(&self) -> Result<Usage> {
        let raw = self.raw_usage().await?;
        let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_secs_f64();
        Ok(Usage::from_raw(&raw, self.soft_cap, now))
    }

    /// The proxy's 5-minute performance rows for the last `hours`.
    pub async fn perf(&self, hours: i64) -> Result<Vec<Value>> {
        let body: Value = self.get("/proxy/perf", &[("hours", &hours.to_string())]).await?.error_for_status()?.json().await?;
        Ok(body["rows"].as_array().cloned().unwrap_or_default())
    }

    /// The account the proxy has pinned the session to; None when it has no pin.
    pub async fn whoami(&self, sid: &str) -> Result<Option<String>> {
        let resp = self.get("/proxy/whoami", &[("sid", sid)]).await?;
        if resp.status() == reqwest::StatusCode::NOT_FOUND {
            return Ok(None);
        }
        let body: Value = resp.error_for_status()?.json().await?;
        Ok(body.get("account").and_then(Value::as_str).map(str::to_string))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn soft_cap_lifts_near_reset_and_moves_only_to_an_account_under_its_cap() {
        let now = 1_000_000.0;
        let raw = json!({"_all": {"status": "ok"},
                         "a": {"status": "ok", "rank": 1, "verified": true, "5h": [0.70, now + 3.0 * 3600.0]},
                         "b": {"status": "ok", "rank": 0, "verified": true, "5h": [0.20, now + 3600.0]},
                         "c": {"status": "rejected", "rank": null, "verified": false}});
        let usage = Usage::from_raw(&raw, SoftCap::default(), now);
        assert_eq!(usage.accounts.len(), 3);
        assert_eq!(usage.status_of(Some("c")), Some("rejected"));
        assert!(usage.over_cap(Some("a")), "70% of a window resetting in 3 hours is over 65%");
        assert!(!usage.over_cap(Some("b")) && !usage.over_cap(Some("c")) && !usage.over_cap(None));
        assert_eq!(usage.handoff_target(Some("a")).as_deref(), Some("b"));
        assert_eq!(usage.handoff_target(Some("b")), None, "already the top-ranked account");
        let near_reset = json!({"a": {"status": "ok", "rank": 1, "5h": [0.70, now + 3600.0]}});
        assert!(!Usage::from_raw(&near_reset, SoftCap::default(), now).over_cap(Some("a")),
                "within 90 minutes of the reset the whole window is usable");
        let capped_top = json!({"a": {"status": "ok", "rank": 1, "5h": [0.70, now + 3.0 * 3600.0]},
                                "b": {"status": "ok", "rank": 0, "5h": [0.66, now + 3.0 * 3600.0]}});
        assert_eq!(Usage::from_raw(&capped_top, SoftCap::default(), now).handoff_target(Some("a")), None);
        assert_eq!(SoftCap::from_env(|k| (k == "SOFT_CAP_5H").then(|| "1.5".to_string())).limit, 1.0);
    }
}
