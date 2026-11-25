//! The usage watch: every minute, the account pool's state from the proxy, and what agentd does about it (a port of
//! core/usage_watch.py).
//!
//! Each account is `ok`, `hot` or `out`: `out` while the proxy gives it no rank (as it does past the weekly cap);
//! `hot` once its 5-hour window is at its soft cap; `ok` again only COOL_MARGIN below the cap, so it doesn't flap (once
//! the cap has lifted, there is no margin). How fast an account spends its 7-day allowance doesn't count; only the
//! weekly cap does. The pool, counted in `ok` accounts: two
//! or more (or every verified account, in a smaller pool) is normal; fewer holds scheduled runs in the queue; none
//! tells the admins once. Turns a usage limit cut off are each carried on once an account is `ok` again. Every change
//! goes to the event log.

use anyhow::Result;
use chrono::{TimeZone, Utc};
use chrono_tz::Tz;
use serde_json::{Map, Value, json};

use crate::db::{append_event, get_setting, set_setting};
use crate::proxy::{SoftCap, weekly_capped};
use crate::shared::Shared;

const COOL_MARGIN: f64 = 0.10;
/// Out for longer than the 5-hour window: something is wrong with the account.
const OUT_TOO_LONG_S: f64 = 5.0 * 3600.0;
/// The last state seen, so a restart neither repeats nor misses a change.
const STATE_KEY: &str = "usage_watch";

fn num(v: &Value) -> Option<f64> {
    v.as_f64().filter(|_| v.is_number())
}

fn window(rec: &Value, w: &str) -> (Option<f64>, Option<f64>) {
    match rec[w].as_array() {
        Some(pair) if pair.len() == 2 => (num(&pair[0]), num(&pair[1])),
        _ => (None, None),
    }
}

pub fn account_state(rec: &Value, previous: Option<&str>, now: f64, soft: &SoftCap) -> &'static str {
    if !rec["rank"].is_i64() {
        return "out";
    }
    let (used, reset) = window(rec, "5h");
    let cap = soft.cap(reset, now);
    if soft.over(used, reset, now) {
        return "hot";
    }
    let cooling = cap < 1.0 && used.is_some_and(|u| u >= cap - COOL_MARGIN);
    if previous == Some("hot") && cooling { "hot" } else { "ok" }
}

/// When the account gets room again: its 7-day reset when that window is spent or past its weekly cap, else its
/// 5-hour one.
pub fn next_reset(rec: &Value) -> Option<f64> {
    let (used_7d, reset_7d) = window(rec, "7d");
    if used_7d.is_some_and(|u| u >= 1.0) || weekly_capped(rec) { reset_7d } else { window(rec, "5h").1 }
}

pub fn pool_level(ok: i64, verified: i64) -> &'static str {
    if ok == 0 {
        "empty"
    } else if ok >= 2.min(verified.max(1)) {
        "normal"
    } else {
        "hold_scheduled"
    }
}

fn when(tz: Tz, epoch: Option<f64>) -> String {
    epoch.and_then(|e| tz.timestamp_opt(e as i64, 0).single()).map(|t| t.format("%a %H:%M %Z").to_string()).unwrap_or_else(|| "unknown".into())
}

pub async fn tick(s: &Shared) -> Result<()> {
    let Ok(raw) = s.proxy.raw_usage().await else { return Ok(()) }; // keep the last state while the proxy is away
    let (now, soft) = (Utc::now().timestamp_millis() as f64 / 1000.0, s.proxy.soft_cap);
    let tz: Tz = s.config.timezone.parse().unwrap_or(chrono_tz::UTC);
    let old: Value = serde_json::from_str(&get_setting(&s.db.lock(), STATE_KEY)?.unwrap_or_else(|| "{}".into())).unwrap_or_default();
    let mut accounts: Vec<(&String, &Value)> = raw.as_object().into_iter().flatten().filter(|(c, r)| *c != "_all" && r.is_object()).collect();
    accounts.sort_by_key(|(c, _)| *c);
    let mut seen = Map::new();
    let mut alerts = Vec::new();
    let event = |kind: &str, data: Value| append_event(&s.db.lock(), kind, None, Some("usage_watch"), Some(&data)).map(drop);
    for (code, rec) in &accounts {
        let before = &old["accounts"][code.as_str()];
        let state = account_state(rec, before["state"].as_str(), now, &soft);
        let (verified, weekly) = (rec["verified"].as_bool().unwrap_or(false), weekly_capped(rec));
        let (used, reset) = window(rec, "5h");
        let (cap, capped) = (soft.cap(reset, now), soft.over(used, reset, now));
        let since = if before["state"] == state { num(&before["since"]).unwrap_or(now) } else { now };
        let mut out_alerted = before["out_alerted"].as_bool().unwrap_or(false) && state == "out";
        let known = before["state"].is_string();
        if known && before["state"] != state {
            event("usage_account", json!({"account": code, "from": before["state"], "to": state, "used_5h": used, "cap": cap,
                                          "pace_delta": num(&rec["pace_delta"]), "weekly_capped": weekly}))?;
        }
        let cap_data = json!({"account": code, "used_5h": used, "cap": cap, "reset_5h": reset});
        if known && capped && before["capped"] != true {
            event("usage_cap_reached", cap_data.clone())?;
        }
        if known && cap >= 1.0 && num(&before["cap"]).unwrap_or(soft.limit) < 1.0 {
            event("usage_cap_lifted", cap_data)?;
        }
        if before["verified"] == true && !verified {
            event("usage_unverified", json!({"account": code}))?;
            alerts.push(format!("Account {code} is no longer verified by the proxy; it gets no new sessions until someone signs it in again."));
        }
        // past its weekly cap, an account is out by design until its week resets
        if verified && state == "out" && !weekly && !out_alerted && now - since > OUT_TOO_LONG_S {
            out_alerted = true;
            event("usage_out_too_long", json!({"account": code, "since": since}))?;
            alerts.push(format!("Account {code} has had no capacity for over {} hours (next reset {}, status {}).",
                                (OUT_TOO_LONG_S / 3600.0) as i64, when(tz, next_reset(rec)),
                                rec["status"].as_str().filter(|v| !v.is_empty()).unwrap_or("none")));
        }
        seen.insert(code.to_string(), json!({"state": state, "since": since, "verified": verified, "out_alerted": out_alerted,
                                              "cap": cap, "capped": capped}));
    }
    let ok = seen.values().filter(|e| e["state"] == "ok").count() as i64;
    let verified = seen.values().filter(|e| e["verified"] == true).count() as i64;
    let level = pool_level(ok, verified);
    set_setting(&s.db.lock(), crate::queue::HOLD_SCHEDULED, if level == "normal" { "0" } else { "1" })?;
    let old_pool = old["pool"].as_str();
    if old_pool.is_some_and(|p| p != level) {
        event("usage_pool", json!({"from": old_pool, "to": level, "ok": ok}))?;
    }
    if level == "empty" && old_pool != Some("empty") {
        let earliest = accounts.iter().filter(|(c, _)| seen[c.as_str()]["verified"] == true).filter_map(|(_, r)| next_reset(r))
            .min_by(f64::total_cmp);
        alerts.push(format!("Every Claude account is at its 5-hour soft cap, hot or out of capacity. People's requests still start on \
                             an account while one has room; scheduled runs wait. Earliest reset: {}.", when(tz, earliest)));
    } else if old_pool == Some("empty") && level != "empty" {
        alerts.push(format!("Claude capacity is back: {ok} account{} with room.", if ok != 1 { "s" } else { "" }));
    }
    set_setting(&s.db.lock(), STATE_KEY, &json!({"accounts": seen, "pool": level}).to_string())?;
    for text in alerts {
        s.dm_admins(&text).await;
    }
    if ok > 0 {
        let waits: Vec<String> = s.db.lock().prepare("SELECT work_item_id FROM limit_waits WHERE resumed_at IS NULL ORDER BY created_at")?
            .query_map([], |r| r.get(0))?.collect::<rusqlite::Result<_>>()?;
        for item in waits {
            let new = crate::conversations::resume_after_limit(s, &item)?;
            event("usage_limit_resumed", json!({"work_item": item, "resumed_as": new}))?;
        }
    }
    Ok(())
}
