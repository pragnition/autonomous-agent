//! The model-call meter (a port of core/llm_meter.py): the apps the agent runs and tests call their model provider
//! through agentd instead of directly. agentd adds the real key, reads the token usage from each answer, prices it,
//! and refuses calls once the day's or the month's cap is spent, so a test loop can't run up a bill.
//!
//! Apps point their OpenAI-compatible base URL at http://172.30.0.10:<port>/llm/<upstream> with any placeholder
//! key. The caps start from agent.toml's [llm_meter] and can be changed at runtime (the dashboard); days and months
//! follow the config's timezone. A stop pauses these calls too.

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Result, bail};
use axum::Router;
use axum::body::{Body, Bytes};
use axum::extract::{DefaultBodyLimit, Path, RawQuery, State};
use axum::http::{HeaderMap, HeaderName, HeaderValue, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get};
use chrono::{DateTime, Datelike, Duration as Days, TimeZone, Utc};
use chrono_tz::Tz;
use rusqlite::Connection;
use serde_json::{Value, json};

use crate::azure_rates::AzureRates;
use crate::config::{LlmMeterSettings, LlmPrice};
use crate::db::{append_event, get_setting, set_setting, utcnow};
use crate::kpis::{local_midnight, round6};
use crate::shared::Shared;

const DAILY_KEY: &str = "llm_meter.daily_usd";
const MONTHLY_KEY: &str = "llm_meter.monthly_usd";
const CAPPED_ON: &str = "llm_meter.capped_on";
const FMT: &str = "%Y-%m-%dT%H:%M:%SZ";
/// Not forwarded: hop-by-hop headers, the app's placeholder key, and compression (answers are read for their usage).
const REQUEST_DROP: [&str; 13] = ["host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer",
                                  "upgrade", "proxy-authorization", "proxy-connection", "api-key", "authorization",
                                  "accept-encoding"];
const RESPONSE_DROP: [&str; 5] = ["content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"];

/// The start of today and of this month in `tz`, as UTC timestamps like the table's.
pub fn period_starts(tz: &str, now: DateTime<Utc>) -> (String, String) {
    let zone: Tz = tz.parse().unwrap_or(chrono_tz::UTC);
    let day = local_midnight(now, zone);
    let local = day.with_timezone(&zone);
    let month = zone.with_ymd_and_hms(local.year(), local.month(), 1, 0, 0, 0).earliest().map_or(day, |d| d.with_timezone(&Utc));
    (day.format(FMT).to_string(), month.format(FMT).to_string())
}

fn setting_f64(c: &Connection, key: &str) -> Result<Option<f64>> {
    Ok(get_setting(c, key)?.and_then(|v| v.parse().ok()))
}

pub fn caps(c: &Connection, settings: &LlmMeterSettings) -> Result<Value> {
    let (daily, monthly) = (setting_f64(c, DAILY_KEY)?, setting_f64(c, MONTHLY_KEY)?);
    Ok(json!({"daily_usd": daily.unwrap_or(settings.daily_usd), "monthly_usd": monthly.unwrap_or(settings.monthly_usd),
              "changed_at_runtime": daily.is_some() || monthly.is_some()}))
}

/// Change the caps at runtime (the dashboard). They outlast restarts and config edits until changed again.
pub fn set_caps(c: &Connection, actor: &str, daily_usd: Option<f64>, monthly_usd: Option<f64>) -> Result<Value> {
    let mut changes = serde_json::Map::new();
    for (kind, value) in [("daily", daily_usd), ("monthly", monthly_usd)] {
        let Some(v) = value else { continue };
        if !v.is_finite() || v < 0.0 {
            bail!("{kind}_usd must be a non-negative number of US dollars");
        }
        changes.insert(format!("{kind}_usd"), json!(v));
    }
    for (key, value) in &changes {
        set_setting(c, if key == "daily_usd" { DAILY_KEY } else { MONTHLY_KEY }, &format!("{:?}", value.as_f64().unwrap_or(0.0)))?;
    }
    let changes = Value::Object(changes);
    if changes.as_object().is_some_and(|m| !m.is_empty()) {
        append_event(c, "llm_caps_changed", None, Some(actor), Some(&changes))?;
    }
    Ok(changes)
}

/// (today, this month) in US dollars.
pub fn spend(c: &Connection, tz: &str, now: DateTime<Utc>) -> Result<(f64, f64)> {
    let (day, month) = period_starts(tz, now);
    let (today, total): (f64, f64) = c.query_row(
        "SELECT COALESCE(SUM(CASE WHEN at >= ? THEN cost_usd END), 0), COALESCE(SUM(cost_usd), 0) FROM llm_usage WHERE at >= ?",
        [day, month], |r| Ok((r.get(0)?, r.get(1)?)))?;
    Ok((round6(today), round6(total)))
}

pub fn over_cap(c: &Connection, settings: &LlmMeterSettings, tz: &str, now: DateTime<Utc>) -> Result<Option<String>> {
    let (limit, (today, month)) = (caps(c, settings)?, spend(c, tz, now)?);
    let (daily, monthly) = (limit["daily_usd"].as_f64().unwrap_or(0.0), limit["monthly_usd"].as_f64().unwrap_or(0.0));
    Ok(if today >= daily {
        Some(format!("today's cap of ${daily:.2} is spent (${today:.2})"))
    } else if month >= monthly {
        Some(format!("this month's cap of ${monthly:.2} is spent (${month:.2})"))
    } else {
        None
    })
}

/// The price listed for the model, else for the longest listed name inside it (a deployment named
/// "team-gpt-5.6-terra" gets "gpt-5.6-terra"), else the default.
pub fn price_for(prices: &BTreeMap<String, LlmPrice>, model: &str) -> LlmPrice {
    if let Some(p) = prices.get(model) {
        return p.clone();
    }
    let inside = prices.keys().filter(|n| *n != "default" && model.contains(n.as_str())).max_by_key(|n| n.chars().count());
    inside.and_then(|n| prices.get(n)).or_else(|| prices.get("default")).cloned()
        .unwrap_or(LlmPrice { input: 0.0, cached_input: 0.0, output: 0.0, cache_write: None, addon: 0.0 })
}

/// A call's tokens: every input token (including cached and cache-written ones), the cached ones, output, and the
/// ones written to the cache.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct Tokens {
    pub input: i64,
    pub cached: i64,
    pub output: i64,
    pub cache_write: i64,
}

fn int(v: &Value) -> i64 {
    v.as_i64().or_else(|| v.as_f64().map(|f| f as i64)).unwrap_or(0)
}

/// The tokens an OpenAI-style answer or stream event reports: chat completions, responses or embeddings.
pub fn usage_of(answer: &Value) -> Option<Tokens> {
    let usage = match &answer["usage"] {
        u if u.is_object() => u,
        _ if answer["response"].is_object() => &answer["response"]["usage"], // the responses API's response.completed
        _ => return None,
    };
    if !usage.is_object() {
        return None;
    }
    let pick = |a: &str, b: &str| if usage.get(a).is_some() { &usage[a] } else { &usage[b] };
    let details = [&usage["prompt_tokens_details"], &usage["input_tokens_details"]].into_iter()
        .find(|d| crate::records::truthy(Some(d))).filter(|d| d.is_object()).cloned().unwrap_or_else(|| json!({}));
    Some(Tokens { input: int(pick("prompt_tokens", "input_tokens")), cached: int(&details["cached_tokens"]),
                  output: int(pick("completion_tokens", "output_tokens")), cache_write: int(&details["cache_write_tokens"]) })
}

pub fn cost(price: &LlmPrice, t: Tokens) -> f64 {
    let cached = t.cached.min(t.input);
    let cache_write = t.cache_write.min(t.input - cached);
    let fresh = t.input - cached - cache_write;
    let write_price = price.cache_write.unwrap_or(price.input);
    (fresh as f64 * price.input + cached as f64 * price.cached_input + cache_write as f64 * write_price
        + t.output as f64 * price.output + (fresh + cache_write + t.output) as f64 * price.addon) / 1e6
}

/// Azure's billed rates for the model on the upstream's resource when agentd has them, else agent.toml's price.
pub fn price_of(settings: &LlmMeterSettings, rates: Option<&AzureRates>, upstream: &str, model: &str) -> LlmPrice {
    let listed = price_for(&settings.prices, model);
    match rates {
        Some(r) => r.price(upstream, model, listed),
        None => listed,
    }
}

/// The last usage object in a server-sent event stream, fed in chunks that may split lines anywhere.
#[derive(Default)]
pub struct SseUsage {
    buffer: Vec<u8>,
    pub usage: Option<Tokens>,
}

impl SseUsage {
    pub fn feed(&mut self, chunk: &[u8]) {
        self.buffer.extend_from_slice(chunk);
        while let Some(i) = self.buffer.iter().position(|b| *b == b'\n') {
            let line: Vec<u8> = self.buffer.drain(..=i).collect();
            self.line(&line[..line.len() - 1]);
        }
    }

    pub fn close(&mut self) {
        let rest = std::mem::take(&mut self.buffer);
        self.line(&rest);
    }

    fn line(&mut self, line: &[u8]) {
        let line = line.trim_ascii();
        let Some(data) = line.strip_prefix(b"data:") else { return };
        if !line.windows(7).any(|w| w == b"\"usage\"") {
            return;
        }
        if let Some(found) = serde_json::from_slice::<Value>(data).ok().as_ref().and_then(usage_of) {
            self.usage = Some(found);
        }
    }
}

/// The body to send and the model it names. A streaming chat completion is asked to report its usage in the last
/// chunk, or the meter couldn't count it.
pub fn prepare_body(body: &[u8], path: &str) -> (Vec<u8>, String) {
    let Ok(Value::Object(mut data)) = serde_json::from_slice::<Value>(body) else { return (body.to_vec(), String::new()) };
    let model = data.get("model").and_then(Value::as_str).unwrap_or_default().to_string();
    if path.trim_end_matches('/').ends_with("chat/completions") && data.get("stream") == Some(&Value::Bool(true)) {
        let mut options = data.get("stream_options").filter(|o| o.is_object()).cloned().unwrap_or_else(|| json!({}));
        if !crate::records::truthy(options.get("include_usage")) {
            options["include_usage"] = json!(true);
            data.insert("stream_options".into(), options);
            return (crate::records::py_dumps(&Value::Object(data)).into_bytes(), model);
        }
    }
    (body.to_vec(), model)
}

pub fn record(c: &Connection, upstream: &str, model: &str, status: u16, usage: Option<Tokens>, price: &LlmPrice) -> Result<f64> {
    let t = usage.unwrap_or_default();
    let spent = cost(price, t);
    c.execute("INSERT INTO llm_usage(at, upstream, model, status, input_tokens, cached_tokens, output_tokens, \
               cache_write_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
              rusqlite::params![utcnow(), upstream, model, status, t.input, t.cached, t.output, t.cache_write, spent])?;
    if usage.is_none() && status < 400 {
        tracing::warn!("{upstream} answered {status} for {model} without a usage report; the call is counted as free");
    }
    Ok(spent)
}

/// Price this month's calls again at the current rates, so the month's spend is all at what Azure bills.
pub fn reprice(c: &Connection, settings: &LlmMeterSettings, rates: Option<&AzureRates>, tz: &str, now: DateTime<Utc>) -> Result<()> {
    let (_, month) = period_starts(tz, now);
    let rows: Vec<(i64, String, String, Tokens)> = c.prepare(
        "SELECT id, upstream, model, input_tokens, cached_tokens, output_tokens, cache_write_tokens FROM llm_usage WHERE at >= ?")?
        .query_map([month], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?,
                                    Tokens { input: r.get(3)?, cached: r.get(4)?, output: r.get(5)?, cache_write: r.get(6)? })))?
        .collect::<rusqlite::Result<_>>()?;
    for (id, upstream, model, t) in rows {
        c.execute("UPDATE llm_usage SET cost_usd = ? WHERE id = ?",
                  rusqlite::params![cost(&price_of(settings, rates, &upstream, &model), t), id])?;
    }
    Ok(())
}

/// What the dashboard shows: caps, spend, this month by model, spend per day for 30 days, and where the prices come
/// from.
pub fn meter_view(c: &Connection, settings: &LlmMeterSettings, tz: &str, now: DateTime<Utc>, rates: Option<&AzureRates>)
                  -> Result<Value> {
    let (_, month) = period_starts(tz, now);
    let by_model: Vec<Value> = c.prepare(
        "SELECT upstream, model, COUNT(*) AS calls, SUM(status >= 400) AS errors, SUM(input_tokens) AS input_tokens, \
         SUM(cached_tokens) AS cached_tokens, SUM(cache_write_tokens) AS cache_write_tokens, \
         SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd \
         FROM llm_usage WHERE at >= ? GROUP BY upstream, model ORDER BY cost_usd DESC")?
        .query_map([&month], crate::db::row_json)?.collect::<rusqlite::Result<_>>()?;
    let zone: Tz = tz.parse().unwrap_or(chrono_tz::UTC);
    let mut days: BTreeMap<String, (i64, f64)> = BTreeMap::new();
    let since = (now - Days::days(30)).format(FMT).to_string();
    let rows: Vec<(String, f64)> = c.prepare("SELECT at, cost_usd FROM llm_usage WHERE at >= ?")?
        .query_map([since], |r| Ok((r.get(0)?, r.get(1)?)))?.collect::<rusqlite::Result<_>>()?;
    for (at, spent) in rows {
        let Ok(t) = DateTime::parse_from_rfc3339(&at) else { continue };
        let entry = days.entry(t.with_timezone(&zone).date_naive().to_string()).or_default();
        *entry = (entry.0 + 1, entry.1 + spent);
    }
    let (today, month_usd) = spend(c, tz, now)?;
    let names: Vec<&String> = settings.upstreams.keys().collect();
    Ok(json!({
        "caps": caps(c, settings)?, "spend": {"today_usd": today, "month_usd": month_usd},
        "capped": over_cap(c, settings, tz, now)?, "upstreams": names, "by_model": by_model,
        "daily": days.into_iter().map(|(d, (n, s))| json!({"day": d, "calls": n, "cost_usd": round6(s)})).collect::<Vec<_>>(),
        "pricing": names.iter().map(|n| (n.to_string(), rates.map_or_else(|| json!({"source": "agent.toml"}), |r| r.view(n))))
            .collect::<serde_json::Map<_, _>>(),
    }))
}

#[derive(Clone)]
struct Meter {
    s: Arc<Shared>,
    rates: Option<Arc<AzureRates>>,
    http: reqwest::Client,
}

fn error(status: StatusCode, code: &str, message: &str) -> Response {
    (status, axum::Json(json!({"error": {"code": code, "message": message}}))).into_response()
}

/// Python's repr of a name, as the error messages show it.
fn repr(name: &str) -> String {
    format!("'{}'", name.replace('\\', "\\\\").replace('\'', "\\'"))
}

/// The meter's listener: `/llm` for its numbers, `/llm/<upstream>/<path>` forwarded.
pub fn app(s: Arc<Shared>, rates: Option<Arc<AzureRates>>) -> Router {
    let http = reqwest::Client::builder().connect_timeout(Duration::from_secs(10)).timeout(Duration::from_secs(600))
        .build().expect("an HTTP client");
    Router::new()
        .route("/llm", get(|State(m): State<Meter>| async move {
            let now = Utc::now();
            match meter_view(&m.s.db.lock(), &m.s.config.llm_meter, &m.s.config.timezone, now, m.rates.as_deref()) {
                Ok(v) => axum::Json(v).into_response(),
                Err(e) => error(StatusCode::INTERNAL_SERVER_ERROR, "error", &format!("{e:#}")),
            }
        }))
        .route("/llm/{upstream}/{*tail}", any(forward))
        .layer(DefaultBodyLimit::max(64 * 1024 * 1024))
        .with_state(Meter { s, rates, http })
}

/// Refuse when a cap is spent, recording one event per day it happens.
fn refuse_if_capped(s: &Shared) -> Result<Option<Response>> {
    let now = Utc::now();
    let c = s.db.lock();
    let Some(why) = over_cap(&c, &s.config.llm_meter, &s.config.timezone, now)? else { return Ok(None) };
    let day = period_starts(&s.config.timezone, now).0;
    if get_setting(&c, CAPPED_ON)?.as_deref() != Some(&day) {
        set_setting(&c, CAPPED_ON, &day)?;
        append_event(&c, "llm_cap_reached", None, None, Some(&json!({"why": why})))?;
    }
    Ok(Some(error(StatusCode::TOO_MANY_REQUESTS, "cost_cap_reached",
                  &format!("Model calls are paused: {why}. An admin can raise the cap on the dashboard."))))
}

async fn forward(State(m): State<Meter>, Path((name, tail)): Path<(String, String)>, method: Method, headers: HeaderMap,
                 RawQuery(query): RawQuery, body: Bytes) -> Response {
    let s = &m.s;
    let Some(upstream) = s.config.llm_meter.upstreams.get(&name) else {
        return error(StatusCode::NOT_FOUND, "unknown_upstream", &format!("no upstream named {} in [llm_meter]", repr(&name)));
    };
    let key = s.secrets.get(&upstream.key).cloned().unwrap_or_default();
    if key.is_empty() {
        return error(StatusCode::SERVICE_UNAVAILABLE, "no_key",
                     &format!("the control plane has no {} secret for the {} upstream", upstream.key, repr(&name)));
    }
    if get_setting(&s.db.lock(), crate::queue::PAUSED).ok().flatten().as_deref() == Some("1") {
        return error(StatusCode::SERVICE_UNAVAILABLE, "agent_stopped",
                     "The agent is stopped, so model calls from its apps are paused too.");
    }
    match refuse_if_capped(s) {
        Ok(Some(refusal)) => return refusal,
        Ok(None) => {}
        Err(e) => return error(StatusCode::INTERNAL_SERVER_ERROR, "error", &format!("{e:#}")),
    }
    let (body, model) = prepare_body(&body, &tail);
    let model = if model.is_empty() { name.clone() } else { model };
    let price = price_of(&s.config.llm_meter, m.rates.as_deref(), &name, &model);
    let mut out = reqwest::header::HeaderMap::new();
    for (k, v) in &headers {
        if !REQUEST_DROP.contains(&k.as_str()) {
            out.append(k.clone(), v.clone());
        }
    }
    let value = if upstream.header.eq_ignore_ascii_case("authorization") { format!("Bearer {key}") } else { key };
    if let (Ok(k), Ok(v)) = (HeaderName::try_from(upstream.header.as_str()), HeaderValue::try_from(value)) {
        out.insert(k, v);
    }
    let url = format!("{}/{tail}{}", upstream.base_url, query.map(|q| format!("?{q}")).unwrap_or_default());
    let answer = match m.http.request(method, url).headers(out).body(body).send().await {
        Ok(a) => a,
        Err(e) => {
            let _ = record(&s.db.lock(), &name, &model, 502, None, &price);
            return error(StatusCode::BAD_GATEWAY, "upstream_unreachable", &format!("{name}: {e}"));
        }
    };
    let status = answer.status();
    let mut reply = HeaderMap::new();
    for (k, v) in answer.headers() {
        if !RESPONSE_DROP.contains(&k.as_str()) {
            reply.append(k.clone(), v.clone());
        }
    }
    let streaming = answer.headers().get("content-type").and_then(|v| v.to_str().ok())
        .is_some_and(|t| t.starts_with("text/event-stream"));
    if !streaming {
        let data = answer.bytes().await.unwrap_or_default();
        let usage = serde_json::from_slice::<Value>(&data).ok().as_ref().and_then(usage_of);
        let _ = record(&s.db.lock(), &name, &model, status.as_u16(), usage, &price);
        return (status, reply, data).into_response();
    }
    let tally = Tally { s: m.s.clone(), upstream: name, model, status: status.as_u16(), price, scan: SseUsage::default(), done: false };
    let chunks = futures_util::stream::unfold((answer, tally), |(mut answer, mut tally)| async move {
        match answer.chunk().await {
            Ok(Some(chunk)) => {
                tally.scan.feed(&chunk);
                Some((Ok::<Bytes, std::io::Error>(chunk), (answer, tally)))
            }
            _ => {
                tally.finish();
                None
            }
        }
    });
    (status, reply, Body::from_stream(chunks)).into_response()
}

/// A streamed answer's count: recorded when the stream ends, or when the app hangs up mid-stream (dropped).
struct Tally {
    s: Arc<Shared>,
    upstream: String,
    model: String,
    status: u16,
    price: LlmPrice,
    scan: SseUsage,
    done: bool,
}

impl Tally {
    fn finish(&mut self) {
        if std::mem::replace(&mut self.done, true) {
            return;
        }
        self.scan.close();
        if let Err(e) = record(&self.s.db.lock(), &self.upstream, &self.model, self.status, self.scan.usage, &self.price) {
            tracing::error!("recording a metered call failed: {e:#}");
        }
    }
}

impl Drop for Tally {
    fn drop(&mut self) {
        self.finish();
    }
}
