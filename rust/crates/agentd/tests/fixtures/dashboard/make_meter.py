"""The meter's pure rules and its dashboard view, from Python, for agentd's stage 4 tests. Run from
rust/crates/agentd with PYTHONPATH=<repo>/src; writes meter.json."""
import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path

from agent import db
from agent.config import LlmMeterSettings, LlmPrice, LlmUpstream
from agent.core import azure_rates as az, llm_meter as m

HERE = Path("tests/fixtures/dashboard")
NOW = datetime(2026, 11, 20, 10, 0, 0, tzinfo=UTC)
TZ = "Asia/Singapore"
out = {"now": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), "tz": TZ}
price = lambda p: dataclasses.asdict(p)  # noqa: E731

prices = {"gpt-5.6-terra": LlmPrice(2.0, 0.2, 12.0), "gpt-5.6": LlmPrice(1.0, 0.1, 8.0, 1.5),
          "default": LlmPrice(5.0, 0.5, 30.0)}
out["prices"] = {k: price(v) for k, v in prices.items()}
out["price_for"] = [{"model": x, "with_default": price(m.price_for(prices, x)),
                     "without": price(m.price_for({k: v for k, v in prices.items() if k != "default"}, x))}
                    for x in ("gpt-5.6-terra", "team-gpt-5.6-terra", "gpt-5.6-mini", "o4", "")]
answers = [
    {"usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 30}}},
    {"usage": {"input_tokens": 50, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 10,
                                                                               "cache_write_tokens": 7}}},
    {"type": "response.completed", "response": {"usage": {"input_tokens": 9, "output_tokens": 1}}},
    {"usage": {"prompt_tokens": 12, "total_tokens": 12}},
    {"usage": {"prompt_tokens": None, "completion_tokens": 3, "prompt_tokens_details": "x"}},
    {"usage": "nope"}, {"no": "usage"}, [1], "text",
]
out["usage_of"] = [{"answer": a, "out": (lambda u: None if u is None else list(u))(m.usage_of(a))} for a in answers]
out["cost"] = [{"price": price(p), "args": args, "out": m.cost(p, *args)} for p, args in (
    (LlmPrice(2.0, 0.2, 12.0), [1000, 300, 50, 0]), (LlmPrice(2.0, 0.2, 12.0, 2.5, 0.35), [1000, 300, 50, 100]),
    (LlmPrice(2.0, 0.2, 12.0), [100, 500, 10, 50]), (LlmPrice(1, 1, 1), [10, 2, 3, 20]))]
bodies = [(b'{"model": "gpt-5.6-terra", "stream": true, "messages": []}', "chat/completions"),
          (b'{"model": "m", "stream": true, "stream_options": {"include_usage": true}}', "chat/completions/"),
          (b'{"model": "m", "stream": true, "stream_options": {"x": 1}}', "v1/chat/completions"),
          (b'{"model": "m", "stream": true}', "responses"), (b'{"model": 5}', "embeddings"), (b"[1]", "x"),
          (b"not json", "x"), (b"", "x")]
out["prepare_body"] = [{"body": b.decode(), "path": p, "out": [r[0].decode(), r[1]]}
                       for b, p in bodies for r in [m.prepare_body(b, p)]]
stream = (b'data: {"choices": []}\n\ndata: {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}\n\n'
          b'data: {"usage": {"prompt_tokens": 8, "completion_tokens": 3}}\ndata: [DONE]\n\n')
scan_cases = []
for cut in (1, 7, 40, 64, len(stream)):
    scan = m.SseUsage()
    for i in range(0, len(stream), cut):
        scan.feed(stream[i:i + cut])
    scan.close()
    scan_cases.append({"cut": cut, "out": list(scan.usage) if scan.usage else None})
tail = m.SseUsage()
tail.feed(b'data: {"usage": {"input_tokens": 4, "output_tokens": 1}}')
tail.close()
out["sse"] = {"stream": stream.decode(), "cases": scan_cases, "no_newline_at_end": list(tail.usage)}
out["account_name"] = [[u, az.account_name(u)] for u in (
    "https://team-ai.openai.azure.com/openai/v1", "https://X1.services.ai.azure.com/models",
    "https://a.cognitiveservices.azure.com", "https://api.openai.com/v1", "https://bad_name.openai.azure.com", "")]
rows = [
    {"Cost": 2.0, "UsageQuantity": 1000, "Meter": "5.6 terra ShortCo Inp Std Gl", "UnitOfMeasure": "1K",
     "MeterSubcategory": "Azure OpenAI"},
    {"Cost": 0.4, "UsageQuantity": 2000, "Meter": "5.6 terra ShortCo Cd Inp Std Gl", "UnitOfMeasure": "1K",
     "MeterSubcategory": "Azure OpenAI"},
    {"Cost": 6.0, "UsageQuantity": 0.5, "Meter": "5.6 terra ShortCo Opt Std Gl", "UnitOfMeasure": "1M",
     "MeterSubcategory": "Azure OpenAI"},
    {"Cost": 1.0, "UsageQuantity": 400, "Meter": "5.6 terra ShortCo Cd Wr Std Gl", "UnitOfMeasure": "1K",
     "MeterSubcategory": "Azure OpenAI"},
    {"Cost": 9.0, "UsageQuantity": 10, "Meter": "5.6 terra LongCo Inp Std Gl", "UnitOfMeasure": "1K",
     "MeterSubcategory": "Azure OpenAI"},
    {"Cost": 0.7, "UsageQuantity": 2, "Meter": "Defender tokens", "UnitOfMeasure": "1M",
     "MeterSubcategory": "Defender for AI"},
    {"Cost": 3.0, "UsageQuantity": 1000, "Meter": "6-luna Inp Std", "UnitOfMeasure": "1K",
     "MeterSubcategory": "Azure OpenAI"},
    {"Cost": 3.0, "UsageQuantity": 1000, "Meter": "5.6 Inp Std", "UnitOfMeasure": "1K", "MeterSubcategory": "x"},
    {"Cost": 1.0, "UsageQuantity": 0, "Meter": "5.6 terra Inp", "UnitOfMeasure": "1K"},
    {"Cost": 1.0, "UsageQuantity": 5, "Meter": "5.6 terra Inp", "UnitOfMeasure": "hours"},
    {"Cost": 1.0, "UsageQuantity": 5, "Meter": "unknown thing", "UnitOfMeasure": "1"},
]
models = {"gpt-5.6-terra", "gpt-6-luna", "gpt-5.6"}
rates, addon = az.parse_rates(rows, models)
out["parse_rates"] = {"rows": rows, "models": sorted(models), "rates": rates, "addon": addon}

conn = db.connect(":memory:")
db.create_schema(conn)
usage = [("2026-11-20T09:00:00Z", "foundry", "gpt-5.6-terra", 200, 1000, 200, 300, 0.25, 0),
         ("2026-11-20T08:00:00Z", "foundry", "gpt-5.6-terra", 500, 0, 0, 0, 0.0, 0),
         ("2026-11-19T15:59:59Z", "foundry", "gpt-6-luna", 200, 5000, 0, 100, 1.5, 10),
         ("2026-11-19T16:00:00Z", "other", "o4", 200, 10, 0, 10, 0.125, 0),
         ("2026-11-02T00:00:00Z", "foundry", "gpt-5.6-terra", 200, 99, 0, 99, 3.0, 0),
         ("2026-10-31T15:00:00Z", "foundry", "gpt-5.6-terra", 200, 99, 0, 99, 40.0, 0),
         ("2026-10-10T00:00:00Z", "foundry", "gpt-5.6-terra", 200, 99, 0, 99, 7.0, 0)]
for row in usage:
    conn.execute("INSERT INTO llm_usage(at, upstream, model, status, input_tokens, cached_tokens, output_tokens, "
                 "cost_usd, cache_write_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
settings = LlmMeterSettings(daily_usd=10.0, monthly_usd=100.0, prices=prices,
                            upstreams={"foundry": LlmUpstream("https://team-ai.openai.azure.com/openai/v1", "FOUNDRY_KEY"),
                                       "other": LlmUpstream("https://api.example.com/v1", "OTHER_KEY", "Authorization")})
out["usage_rows"] = [list(r) for r in usage]
out["period_starts"] = list(m.period_starts(TZ, NOW))
out["view_default"] = m.meter_view(conn, settings, TZ, NOW)
db.set_setting(conn, "llm_meter.daily_usd", "0.3")
out["view_capped_today"] = m.meter_view(conn, settings, TZ, NOW)
db.set_setting(conn, "llm_meter.daily_usd", "50")
db.set_setting(conn, "llm_meter.monthly_usd", "4")
out["view_capped_month"] = m.meter_view(conn, settings, TZ, NOW)
state = {"foundry": {"resource": "/subscriptions/s1/resourceGroups/g/providers/x/accounts/team-ai",
                     "deployments": {"team-terra": "gpt-5.6-terra"}, "rates": rates, "addon": addon,
                     "as_of": "2026-11-20T04:00:00Z"},
         "other": {"error": "AzureError: boom"}}


class Rates(az.AzureRates):
    def __init__(self):
        self.state = state


r = Rates()
out["azure_state"] = state
out["azure_price"] = [{"upstream": u, "model": x, "out": price(r.price(u, x, m.price_for(prices, x)))}
                      for u, x in (("foundry", "team-terra"), ("foundry", "gpt-6-luna"), ("foundry", "o9"),
                                   ("other", "gpt-5.6-terra"), ("none", "gpt-5.6"))]
out["azure_view"] = {u: r.view(u) for u in ("foundry", "other", "none")}
out["view_with_rates"] = m.meter_view(conn, settings, TZ, NOW, rates=r)
(HERE / "meter.json").write_text(json.dumps(out, indent=1) + "\n")
print("ok")
