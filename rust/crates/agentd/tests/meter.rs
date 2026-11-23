//! The LLM meter and Azure-billed prices: pure rules against outputs recorded from core
//! (tests/fixtures/dashboard/make_meter.py), forwarding against a fake upstream.

mod common;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use agentd::azure_rates::{self, AzureRates};
use agentd::config::{LlmMeterSettings, LlmPrice, LlmUpstream};
use agentd::meter::{self, SseUsage, Tokens};
use common::*;
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(&std::fs::read_to_string("tests/fixtures/dashboard/meter.json").unwrap()).unwrap()
}

fn price(v: &Value) -> LlmPrice {
    LlmPrice { input: v["input"].as_f64().unwrap(), cached_input: v["cached_input"].as_f64().unwrap(),
               output: v["output"].as_f64().unwrap(), cache_write: v["cache_write"].as_f64(),
               addon: v["addon"].as_f64().unwrap() }
}

fn price_json(p: &LlmPrice) -> Value {
    json!({"input": p.input, "cached_input": p.cached_input, "output": p.output, "cache_write": p.cache_write,
           "addon": p.addon})
}

fn tokens_json(t: Option<Tokens>) -> Value {
    t.map_or(Value::Null, |t| json!([t.input, t.cached, t.output, t.cache_write]))
}

fn settings(e: &Value) -> LlmMeterSettings {
    let prices: BTreeMap<String, LlmPrice> = e["prices"].as_object().unwrap().iter().map(|(k, v)| (k.clone(), price(v))).collect();
    LlmMeterSettings { port: 8087, daily_usd: 10.0, monthly_usd: 100.0, prices, upstreams: [
        ("foundry".to_string(), LlmUpstream { base_url: "https://team-ai.openai.azure.com/openai/v1".into(),
                                              key: "FOUNDRY_KEY".into(), header: "api-key".into() }),
        ("other".to_string(), LlmUpstream { base_url: "https://api.example.com/v1".into(), key: "OTHER_KEY".into(),
                                            header: "Authorization".into() })].into() }
}

#[test]
fn pricing_and_parsing_match_python() {
    let e = fixture();
    let prices = settings(&e).prices;
    let without: BTreeMap<String, LlmPrice> = prices.iter().filter(|(k, _)| *k != "default").map(|(k, v)| (k.clone(), v.clone())).collect();
    for case in e["price_for"].as_array().unwrap() {
        let model = case["model"].as_str().unwrap();
        assert_json(&price_json(&meter::price_for(&prices, model)), &case["with_default"], model);
        assert_json(&price_json(&meter::price_for(&without, model)), &case["without"], model);
    }
    for case in e["usage_of"].as_array().unwrap() {
        assert_eq!(tokens_json(meter::usage_of(&case["answer"])), case["out"], "{}", case["answer"]);
    }
    for case in e["cost"].as_array().unwrap() {
        let a: Vec<i64> = serde_json::from_value(case["args"].clone()).unwrap();
        let got = meter::cost(&price(&case["price"]), Tokens { input: a[0], cached: a[1], output: a[2], cache_write: a[3] });
        assert!((got - case["out"].as_f64().unwrap()).abs() < 1e-12, "{case}: {got}");
    }
    for case in e["prepare_body"].as_array().unwrap() {
        let (body, model) = meter::prepare_body(case["body"].as_str().unwrap().as_bytes(), case["path"].as_str().unwrap());
        assert_eq!(json!([String::from_utf8(body).unwrap(), model]), case["out"], "{case}");
    }
    let stream = e["sse"]["stream"].as_str().unwrap().as_bytes();
    for case in e["sse"]["cases"].as_array().unwrap() {
        let mut scan = SseUsage::default();
        for chunk in stream.chunks(case["cut"].as_u64().unwrap() as usize) {
            scan.feed(chunk);
        }
        scan.close();
        assert_eq!(tokens_json(scan.usage), case["out"], "cut {}", case["cut"]);
    }
    let mut tail = SseUsage::default();
    tail.feed(br#"data: {"usage": {"input_tokens": 4, "output_tokens": 1}}"#);
    tail.close();
    assert_eq!(tokens_json(tail.usage), e["sse"]["no_newline_at_end"]);
    for case in e["account_name"].as_array().unwrap() {
        assert_eq!(json!(azure_rates::account_name(case[0].as_str().unwrap())), case[1], "{}", case[0]);
    }
    let models = e["parse_rates"]["models"].as_array().unwrap().iter().map(|m| m.as_str().unwrap().to_string()).collect();
    let (rates, addon) = azure_rates::parse_rates(e["parse_rates"]["rows"].as_array().unwrap(), &models);
    assert_json(&Value::Object(rates), &e["parse_rates"]["rates"], "rates");
    assert!((addon - e["parse_rates"]["addon"].as_f64().unwrap()).abs() < 1e-9);

    let rates = AzureRates::with_state(e["azure_state"].as_object().unwrap().clone(), None);
    for case in e["azure_price"].as_array().unwrap() {
        let (upstream, model) = (case["upstream"].as_str().unwrap(), case["model"].as_str().unwrap());
        assert_json(&price_json(&rates.price(upstream, model, meter::price_for(&prices, model))), &case["out"], model);
    }
    for (upstream, want) in e["azure_view"].as_object().unwrap() {
        assert_json(&rates.view(upstream), want, upstream);
    }
}

#[test]
fn meter_view_matches_python() {
    let e = fixture();
    let (tz, now) = (e["tz"].as_str().unwrap(), e["now"].as_str().unwrap().parse().unwrap());
    let c = rusqlite::Connection::open_in_memory().unwrap();
    agentd::db::create_schema(&c).unwrap();
    for r in e["usage_rows"].as_array().unwrap() {
        c.execute("INSERT INTO llm_usage(at, upstream, model, status, input_tokens, cached_tokens, output_tokens, cost_usd, \
                   cache_write_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  rusqlite::params![r[0].as_str(), r[1].as_str(), r[2].as_str(), r[3].as_i64(), r[4].as_i64(), r[5].as_i64(),
                                    r[6].as_i64(), r[7].as_f64(), r[8].as_i64()]).unwrap();
    }
    let s = settings(&e);
    assert_eq!(json!(meter::period_starts(tz, now)), e["period_starts"]);
    assert_json(&meter::meter_view(&c, &s, tz, now, None).unwrap(), &e["view_default"], "default");
    agentd::db::set_setting(&c, "llm_meter.daily_usd", "0.3").unwrap();
    assert_json(&meter::meter_view(&c, &s, tz, now, None).unwrap(), &e["view_capped_today"], "today");
    agentd::db::set_setting(&c, "llm_meter.daily_usd", "50").unwrap();
    agentd::db::set_setting(&c, "llm_meter.monthly_usd", "4").unwrap();
    assert_json(&meter::meter_view(&c, &s, tz, now, None).unwrap(), &e["view_capped_month"], "month");
    let rates = AzureRates::with_state(e["azure_state"].as_object().unwrap().clone(), None);
    assert_json(&meter::meter_view(&c, &s, tz, now, Some(&rates)).unwrap(), &e["view_with_rates"], "with rates");
}

/// A fake model provider: answers chat completions with usage, streams when asked, and records what it got.
async fn upstream(seen: Arc<Mutex<Vec<Value>>>) -> String {
    use axum::response::IntoResponse;
    let app = axum::Router::new().fallback(move |method: axum::http::Method, uri: axum::http::Uri,
                                               headers: axum::http::HeaderMap, body: axum::body::Bytes| {
        let seen = seen.clone();
        async move {
            let body: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
            let h = |k: &str| headers.get(k).map(|v| v.to_str().unwrap().to_string());
            seen.lock().unwrap().push(json!({"method": method.as_str(), "uri": uri.to_string(), "body": body,
                                             "api-key": h("api-key"), "authorization": h("authorization"),
                                             "x-custom": h("x-custom"), "accept-encoding": h("accept-encoding")}));
            if body["stream"] == true {
                let hang = body["hang"] == true;
                let chunks = futures_util::stream::unfold(0, move |i| async move {
                    let text = match i {
                        0 => "data: {\"choices\": [{\"delta\": {\"content\": \"hi\"}}]}\n\n",
                        1 => "data: {\"choices\": [], \"usage\": {\"prompt_tokens\": 1000, \"completion_tokens\": 50}}\n\n",
                        2 if hang => { tokio::time::sleep(Duration::from_secs(30)).await; "data: [DONE]\n\n" }
                        2 => "data: [DONE]\n\n",
                        _ => return None,
                    };
                    Some((Ok::<_, std::io::Error>(axum::body::Bytes::from(text)), i + 1))
                });
                return ([("content-type", "text/event-stream"), ("x-upstream", "yes")], axum::body::Body::from_stream(chunks))
                    .into_response();
            }
            (axum::http::StatusCode::OK, [("x-upstream", "yes")],
             axum::Json(json!({"id": "c1", "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                                                     "prompt_tokens_details": {"cached_tokens": 200}}}))).into_response()
        }
    });
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await });
    url
}

struct Rig {
    _dir: tempfile::TempDir,
    s: Arc<agentd::shared::Shared>,
    url: String,
    seen: Arc<Mutex<Vec<Value>>>,
}

async fn rig() -> Rig {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let up = upstream(seen.clone()).await;
    let (dir, slack) = (tempfile::tempdir().unwrap(), FakeSlack::start().await);
    let mut s = shared_plain(dir.path(), &slack, &fake_proxy(usage(0.2)).await);
    s.config.llm_meter.upstreams = [("foundry".to_string(), LlmUpstream { base_url: format!("{up}/openai/v1"),
                                                                           key: "FOUNDRY_KEY".into(), header: "api-key".into() }),
                                     ("nokey".to_string(), LlmUpstream { base_url: up.clone(), key: "MISSING_KEY".into(),
                                                                         header: "Authorization".into() })].into();
    s.config.llm_meter.prices = [("default".to_string(), LlmPrice { input: 2.0, cached_input: 0.2, output: 12.0,
                                                                     cache_write: None, addon: 0.0 })].into();
    s.secrets.insert("FOUNDRY_KEY".into(), "real-foundry-key-123".into());
    let s = Arc::new(s);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let app = meter::app(s.clone(), None);
    tokio::spawn(async move { axum::serve(listener, app).await });
    Rig { _dir: dir, s, url, seen }
}

fn rows(s: &agentd::shared::Shared) -> Vec<Value> {
    s.db.lock().prepare("SELECT upstream, model, status, input_tokens, cached_tokens, output_tokens, cost_usd FROM llm_usage ORDER BY id")
        .unwrap().query_map([], agentd::db::row_json).unwrap().map(Result::unwrap).collect()
}

#[tokio::test]
async fn forwards_with_key_and_records() {
    let r = rig().await;
    let resp = reqwest::Client::new().post(format!("{}/llm/foundry/chat/completions?api-version=2025-01", r.url))
        .header("api-key", "placeholder").header("authorization", "Bearer placeholder").header("x-custom", "kept")
        .json(&json!({"model": "gpt-5.6-terra", "messages": []})).send().await.unwrap();
    assert_eq!(resp.status(), 200);
    assert_eq!(resp.headers()["x-upstream"], "yes");
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["id"], "c1");
    let got = r.seen.lock().unwrap()[0].clone();
    assert_eq!(got["uri"], "/openai/v1/chat/completions?api-version=2025-01");
    assert_eq!((&got["api-key"], &got["authorization"], &got["x-custom"]),
               (&json!("real-foundry-key-123"), &Value::Null, &json!("kept")), "the real key, the app's placeholders gone");
    let rows = rows(&r.s);
    assert_eq!(rows.len(), 1);
    assert_eq!((&rows[0]["upstream"], &rows[0]["model"], &rows[0]["status"]), (&json!("foundry"), &json!("gpt-5.6-terra"), &json!(200)));
    let want = (800.0 * 2.0 + 200.0 * 0.2 + 50.0 * 12.0) / 1e6;
    assert!((rows[0]["cost_usd"].as_f64().unwrap() - want).abs() < 1e-12);

    let unknown = reqwest::get(format!("{}/llm/nope/chat/completions", r.url)).await.unwrap();
    assert_eq!(unknown.status(), 404);
    assert_eq!(unknown.json::<Value>().await.unwrap()["error"]["code"], "unknown_upstream");
    let nokey = reqwest::Client::new().post(format!("{}/llm/nokey/x", r.url)).send().await.unwrap();
    assert_eq!(nokey.status(), 503);
    assert_eq!(nokey.json::<Value>().await.unwrap()["error"]["code"], "no_key");
    let view: Value = reqwest::get(format!("{}/llm", r.url)).await.unwrap().json().await.unwrap();
    assert_eq!(view["upstreams"], json!(["foundry", "nokey"]));
}

#[tokio::test]
async fn streaming_usage_recorded() {
    let r = rig().await;
    let resp = reqwest::Client::new().post(format!("{}/llm/foundry/chat/completions", r.url))
        .json(&json!({"model": "m", "stream": true})).send().await.unwrap();
    assert_eq!(resp.headers()["content-type"], "text/event-stream");
    let text = resp.text().await.unwrap();
    assert!(text.ends_with("data: [DONE]\n\n"), "{text}");
    assert_eq!(r.seen.lock().unwrap()[0]["body"]["stream_options"], json!({"include_usage": true}));
    let rows = rows(&r.s);
    assert_eq!((&rows[0]["input_tokens"], &rows[0]["output_tokens"]), (&json!(1000), &json!(50)));
}

#[tokio::test]
async fn stream_hangup_still_recorded() {
    let r = rig().await;
    let mut resp = reqwest::Client::new().post(format!("{}/llm/foundry/chat/completions", r.url))
        .json(&json!({"model": "m", "stream": true, "hang": true})).send().await.unwrap();
    let mut seen = String::new();
    while !seen.contains("usage") {
        seen.push_str(&String::from_utf8_lossy(&resp.chunk().await.unwrap().unwrap()));
    }
    drop(resp); // the app hangs up before the stream ends
    for _ in 0..100 {
        if !rows(&r.s).is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(20)).await;
    }
    let rows = rows(&r.s);
    assert_eq!(rows.len(), 1, "recorded once, when the app went away");
    assert_eq!(rows[0]["input_tokens"], 1000);
}

#[tokio::test]
async fn caps_and_stop_refuse() {
    let r = rig().await;
    let call = || reqwest::Client::new().post(format!("{}/llm/foundry/chat/completions", r.url)).json(&json!({"model": "m"})).send();
    meter::set_caps(&r.s.db.lock(), "sam", Some(0.0), None).unwrap();
    for _ in 0..2 {
        let resp = call().await.unwrap();
        assert_eq!(resp.status(), 429);
        let err = resp.json::<Value>().await.unwrap()["error"].clone();
        assert_eq!(err["code"], "cost_cap_reached");
        assert_eq!(err["message"], "Model calls are paused: today's cap of $0.00 is spent ($0.00). An admin can raise the cap on the dashboard.");
    }
    let reached: i64 = r.s.db.lock().query_row("SELECT COUNT(*) FROM events WHERE type = 'llm_cap_reached'", [], |x| x.get(0)).unwrap();
    assert_eq!(reached, 1, "one event per day the cap is hit");
    let changed: String = r.s.db.lock().query_row("SELECT data FROM events WHERE type = 'llm_caps_changed'", [], |x| x.get(0)).unwrap();
    assert_eq!(changed, r#"{"daily_usd": 0.0}"#);
    assert!(meter::set_caps(&r.s.db.lock(), "sam", Some(-1.0), None).is_err());

    meter::set_caps(&r.s.db.lock(), "sam", Some(5.0), None).unwrap();
    agentd::db::set_setting(&r.s.db.lock(), "paused", "1").unwrap();
    let resp = call().await.unwrap();
    assert_eq!(resp.status(), 503);
    assert_eq!(resp.json::<Value>().await.unwrap()["error"]["code"], "agent_stopped");
    assert!(r.seen.lock().unwrap().is_empty(), "nothing reached the provider");
}

#[tokio::test]
async fn refresh_reads_billed_rates_and_reprices_month() {
    let dir = tempfile::tempdir().unwrap();
    // a stand-in `az`: answers the three reads by what it is asked
    let az = dir.path().join("az");
    std::fs::write(&az, r#"#!/bin/sh
case "$*" in
  *ResourceGraph*) echo '{"data": [{"id": "/subscriptions/s1/resourceGroups/g/providers/x/accounts/team-ai"}]}' ;;
  *deployments*) echo '{"value": [{"name": "team-terra", "properties": {"model": {"name": "gpt-5.6-terra"}}}]}' ;;
  *CostManagement*) echo '{"properties": {"columns": [{"name": "Cost"}, {"name": "UsageQuantity"}, {"name": "Meter"}, {"name": "UnitOfMeasure"}, {"name": "MeterSubcategory"}], "rows": [[4.0, 1000, "5.6 terra Inp", "1K", "x"], [12.0, 1, "5.6 terra Opt", "1M", "x"]]}}' ;;
  *) echo "unexpected: $*" >&2; exit 3 ;;
esac
"#).unwrap();
    std::fs::set_permissions(&az, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
    let slack = FakeSlack::start().await;
    let mut s = shared_plain(dir.path(), &slack, &fake_proxy(usage(0.2)).await);
    s.config.llm_meter.upstreams = [("foundry".to_string(), LlmUpstream {
        base_url: "https://team-ai.openai.azure.com/openai/v1".into(), key: "K".into(), header: "api-key".into() })].into();
    let rules = agentd::config::CliRules { command: az.to_string_lossy().into(), env: Default::default(), login: None,
                                           login_check: None };
    let now: chrono::DateTime<chrono::Utc> = "2026-11-20T10:00:00Z".parse().unwrap();
    {
        let c = s.db.lock();
        for at in ["2026-11-20T09:00:00Z", "2026-10-01T00:00:00Z"] {
            c.execute("INSERT INTO llm_usage(at, upstream, model, status, input_tokens, output_tokens, cost_usd) \
                       VALUES (?, 'foundry', 'team-terra', 200, 1000, 100, 99)", [at]).unwrap();
        }
    }
    let rates = AzureRates::load(&s.db.lock(), Some(rules)).unwrap();
    rates.refresh(&s, now).await.unwrap();
    let view = rates.view("foundry");
    assert_eq!((&view["source"], &view["resource"]), (&json!("azure"), &json!("team-ai")));
    assert_eq!(view["rates"]["gpt-5.6-terra"], json!({"input": 4.0, "output": 12.0}));
    let costs: Vec<f64> = s.db.lock().prepare("SELECT cost_usd FROM llm_usage ORDER BY at DESC").unwrap()
        .query_map([], |x| x.get(0)).unwrap().map(Result::unwrap).collect();
    assert!((costs[0] - (1000.0 * 4.0 + 100.0 * 12.0) / 1e6).abs() < 1e-12, "this month repriced: {costs:?}");
    assert_eq!(costs[1], 99.0, "an earlier month keeps its price");
    let saved = agentd::db::get_setting(&s.db.lock(), "llm_meter.azure_rates").unwrap().unwrap();
    assert!(saved.contains("team-terra"));
    assert_eq!(AzureRates::load(&s.db.lock(), None).unwrap().view("foundry")["source"], "azure", "kept across restarts");

    std::fs::write(&az, "#!/bin/sh\necho 'the login has expired' >&2\nexit 1\n").unwrap();
    rates.refresh(&s, now + chrono::Duration::hours(7)).await.unwrap();
    let view = rates.view("foundry");
    assert_eq!(view["source"], "azure", "a failed read keeps the last rates");
    assert_eq!(view["error"], "AzureError: the login has expired");
}
