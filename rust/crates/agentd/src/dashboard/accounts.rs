//! The account pool as the dashboard shows it (a port of dashboard/accounts.py), from the proxy's /proxy/usage and
//! /proxy/perf. The proxy's shapes are read defensively: a missing or odd field becomes null or zero, never an error.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{Map, Value, json};

use crate::proxy::SoftCap;

pub const HOURS: [i64; 4] = [6, 24, 168, 720];
const ERROR_KINDS: [&str; 4] = ["429", "limit", "5xx", "upstream_error"];
const TOKEN_KINDS: [&str; 4] = ["in", "out", "cache_read", "cache_write"];

fn d(v: &Value) -> &Value {
    static EMPTY: std::sync::LazyLock<Value> = std::sync::LazyLock::new(|| json!({}));
    if v.is_object() { v } else { &EMPTY }
}

/// A number as the proxy sent it, or None (booleans aren't numbers here).
fn num(v: &Value) -> Option<f64> {
    v.as_f64()
}

/// A count as JSON: whole numbers as integers.
fn n(x: f64) -> Value {
    if x.fract() == 0.0 && x.abs() < 9e15 { json!(x as i64) } else { json!(x) }
}

fn opt(v: &Value) -> Value {
    if v.is_number() { v.clone() } else { Value::Null }
}

type Tokens = [f64; 4];

fn tokens(v: &Value) -> Tokens {
    TOKEN_KINDS.map(|k| num(&d(v)[k]).unwrap_or(0.0))
}

fn add(into: &mut Tokens, more: &Tokens) {
    for i in 0..4 {
        into[i] += more[i];
    }
}

fn tokens_json(t: &Tokens) -> Value {
    Value::Object(TOKEN_KINDS.iter().zip(t).map(|(k, v)| (k.to_string(), n(*v))).collect())
}

fn window(rec: &Value, w: &str) -> (Option<f64>, Option<f64>) {
    match rec[w].as_array() {
        Some(pair) if pair.len() == 2 => (num(&pair[0]), num(&pair[1])),
        _ => (None, None),
    }
}

fn account(code: &str, rec: &Value, origins: &[String], cap: SoftCap, now: f64) -> Value {
    let (util_5h, reset_5h) = window(rec, "5h");
    let (util_7d, reset_7d) = window(rec, "7d");
    let by_origin: Vec<(String, Tokens)> = d(&rec["by_origin"]).as_object().into_iter().flatten()
        .map(|(name, o)| (name.clone(), tokens(&d(o)["tokens_60m"]))).collect();
    let (mut agent, mut people) = ([0.0; 4], [0.0; 4]);
    for (name, t) in &by_origin {
        add(if origins.contains(name) { &mut agent } else { &mut people }, t);
    }
    let errors = d(&d(&rec["throughput"])["60m"]);
    let latency = d(&d(&rec["latency"])["60m"]);
    let pace = rec["pace"].as_str().filter(|p| ["ahead", "on", "behind"].contains(p));
    json!({
        "code": code, "status": rec["status"].as_str().filter(|s| !s.is_empty()).unwrap_or(""),
        "rank": rec["rank"].as_i64(), "verified": crate::records::truthy(rec.get("verified")),
        "active": n(num(&rec["active"]).unwrap_or(0.0)),
        "util_5h": util_5h, "reset_5h": reset_5h, "util_7d": util_7d, "reset_7d": reset_7d, "pace": pace,
        "target_7d": opt(&rec["target_7d"]), "pace_delta": opt(&rec["pace_delta"]), "cap_5h": opt(&rec["cap_5h"]),
        // the share of the 5-hour window the account may use now: the soft cap, or 1 in the last stretch before reset
        "soft_cap": cap.cap(reset_5h, now), "over_soft_cap": cap.over(util_5h, reset_5h, now),
        "tokens_60m": tokens_json(&tokens(&rec["tokens_60m"])), "agent_60m": tokens_json(&agent),
        "people_60m": tokens_json(&people),
        "by_origin": Value::Object(by_origin.iter().map(|(k, t)| (k.clone(), tokens_json(t))).collect()),
        "errors_60m": Value::Object(ERROR_KINDS.iter().map(|k| (k.to_string(), n(num(&errors[*k]).unwrap_or(0.0)))).collect()),
        "latency_60m": {"requests": n(num(&latency["requests"]).unwrap_or(0.0)), "ttft_p50": opt(&latency["ttft_p50"]),
                        "ttft_p90": opt(&latency["ttft_p90"]), "tok_s": opt(&latency["tok_s"])},
    })
}

pub fn accounts_view(raw: Option<&Value>, origins: &[String], error: Option<&str>, cap: SoftCap, now: f64) -> Value {
    let Some(raw) = raw.and_then(Value::as_object) else {
        return json!({"available": false, "error": error.unwrap_or("no usage from the proxy"), "agent_origins": origins,
                      "pool": null, "accounts": [], "origins": []});
    };
    let mut accounts: Vec<Value> = raw.iter().filter(|(code, _)| *code != "_all")
        .map(|(code, rec)| account(code, d(rec), origins, cap, now)).collect();
    accounts.sort_by(|a, b| {
        let key = |x: &Value| (x["rank"].is_null(), x["rank"].as_i64().unwrap_or(0), x["code"].as_str().unwrap_or("").to_string());
        key(a).cmp(&key(b))
    });
    let pool_raw = d(raw.get("_all").unwrap_or(&Value::Null));
    let (pace, burn) = (d(&pool_raw["pace"]), d(&pool_raw["burn"]));
    let counts = d(&pace["accounts"]);
    let hourly: Vec<Value> = burn["hourly"].as_array().into_iter().flatten()
        .filter_map(|p| p.as_array().filter(|p| p.len() == 2).map(|p| json!([num(&p[0]), num(&p[1])]))).collect();
    let pool = json!({
        "used_7d": opt(&pace["used_7d"]), "target_7d": opt(&pace["target_7d"]), "delta": opt(&pace["delta"]),
        "state": pace.get("state").cloned().unwrap_or(Value::Null),
        "counts": Value::Object(["ahead", "on", "behind"].iter().map(|k| (k.to_string(), n(num(&counts[*k]).unwrap_or(0.0)))).collect()),
        "unknown": n(num(&pace["unknown"]).unwrap_or(0.0)), "tokens_60m": tokens_json(&tokens(&pool_raw["tokens_60m"])),
        "burn": {"today": tokens_json(&tokens(&burn["today"])), "week": tokens_json(&tokens(&burn["7d"])),
                 "since": opt(&burn["since"])},
        "hourly": hourly, "session_burn": opt(&pool_raw["session_burn"]),
    });
    let mut active: BTreeMap<String, f64> = BTreeMap::new();
    for rec in raw.values() {
        for (name, o) in d(&d(rec)["by_origin"]).as_object().into_iter().flatten() {
            *active.entry(name.clone()).or_default() += num(&d(o)["active"]).unwrap_or(0.0);
        }
    }
    let by_origin: BTreeMap<&String, &Value> = d(&pool_raw["by_origin"]).as_object().into_iter().flatten().collect();
    let pool_origins: Vec<Value> = by_origin.into_iter().map(|(name, o)| json!({
        "name": name, "is_agent": origins.contains(name), "active": n(active.get(name).copied().unwrap_or(0.0)),
        "tokens_60m": tokens_json(&tokens(&d(o)["tokens_60m"])),
        "requests_60m": n(num(&d(&d(&d(o)["latency"])["60m"])["requests"]).unwrap_or(0.0)),
    })).collect();
    json!({"available": true, "error": null, "agent_origins": origins, "pool": pool, "accounts": accounts,
           "origins": pool_origins})
}

pub fn bucket_seconds(hours: i64) -> i64 {
    if hours <= 24 { 300 } else if hours <= 168 { 3600 } else { 21600 }
}

/// The proxy's 5-minute perf rows summed into fixed buckets covering the last `hours`, every bucket present.
pub fn history_view(rows: &[Value], hours: i64, origins: &[String], now: f64) -> Value {
    let size = bucket_seconds(hours);
    let count = hours * 3600 / size;
    let last = (now / size as f64).floor() as i64 * size;
    let start = last - (count - 1) * size;
    struct Bucket {
        by_account: Map<String, Value>,
        by_origin: Map<String, Value>,
        agent: Tokens,
        people: Tokens,
        requests: f64,
        errors: f64,
    }
    let mut buckets: Vec<Bucket> = (0..count).map(|_| Bucket { by_account: Map::new(), by_origin: Map::new(), agent: [0.0; 4],
                                                               people: [0.0; 4], requests: 0.0, errors: 0.0 }).collect();
    let (mut accounts, mut seen_origins) = (BTreeSet::new(), BTreeSet::new());
    let sum_into = |map: &mut Map<String, Value>, key: &str, t: &Tokens| {
        let mut have = map.get(key).map_or([0.0; 4], tokens);
        add(&mut have, t);
        map.insert(key.into(), tokens_json(&have));
    };
    for row in rows {
        let Some(t) = row.is_object().then(|| num(&row["t"])).flatten() else { continue };
        if !(start as f64 <= t && t < (last + size) as f64) {
            continue;
        }
        let b = &mut buckets[((t - start as f64) / size as f64).floor() as usize];
        for (code, rec) in d(&row["accounts"]).as_object().into_iter().flatten() {
            accounts.insert(code.clone());
            sum_into(&mut b.by_account, code, &tokens(&d(rec)["tokens"]));
        }
        for (name, rec) in d(&row["origins"]).as_object().into_iter().flatten() {
            seen_origins.insert(name.clone());
            let tk = tokens(&d(rec)["tokens"]);
            sum_into(&mut b.by_origin, name, &tk);
            add(if origins.contains(name) { &mut b.agent } else { &mut b.people }, &tk);
        }
        let whole = d(&row["all"]);
        b.requests += num(&whole["requests"]).unwrap_or(0.0);
        b.errors += ERROR_KINDS.iter().map(|k| num(&whole[*k]).unwrap_or(0.0)).sum::<f64>();
    }
    json!({"hours": hours, "bucket_s": size, "accounts": accounts, "origins": seen_origins,
           "buckets": buckets.iter().enumerate().map(|(i, b)| json!({
               "t": start + i as i64 * size, "by_account": b.by_account, "by_origin": b.by_origin,
               "agent": tokens_json(&b.agent), "people": tokens_json(&b.people), "requests": n(b.requests),
               "errors": n(b.errors)})).collect::<Vec<_>>()})
}
