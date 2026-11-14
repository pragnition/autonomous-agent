import json
from datetime import UTC, datetime

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent import db
from agent.config import ConfigError, LlmMeterSettings, LlmPrice, LlmUpstream, load_agent_config
from agent.core import llm_meter
from agent.core.llm_meter import SseUsage, make_meter_app, meter_view, prepare_body, price_for, set_caps, spend, usage_of

TZ = "Asia/Singapore"
PRICES = {"gpt-5.6-terra": LlmPrice(2.0, 0.2, 12.0), "default": LlmPrice(5.0, 0.5, 30.0)}


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def settings(**kw) -> LlmMeterSettings:
    return LlmMeterSettings(daily_usd=kw.get("daily", 10.0), monthly_usd=kw.get("monthly", 100.0),
                            upstreams={"dev": LlmUpstream("https://dev.example.azure.com/openai/v1", "DEV_KEY")},
                            prices=PRICES)


def add_usage(conn, at: str, cost: float) -> None:
    conn.execute("INSERT INTO llm_usage(at, upstream, model, status, cost_usd) VALUES (?, 'dev', 'm', 200, ?)",
                 (at, cost))


# config

def write_config(tmp_path, meter: str):
    path = tmp_path / "agent.toml"
    path.write_text('name = "agent"\n[people.ann]\nslack = "U1"\nlinear = "ann@example.com"\n' + meter)
    return path


def test_config_reads_upstreams_prices_and_caps(tmp_path):
    cfg = load_agent_config(write_config(tmp_path, """
[llm_meter]
daily_usd = 5
monthly_usd = 50.5
[llm_meter.upstreams.team-dev]
base_url = "https://x.openai.azure.com/openai/v1/"
key = "TEAM_DEV_KEY"
[llm_meter.prices]
"gpt-5.6-terra" = [2.0, 0.2, 12]
default = [5, 0.5, 30]
"""))
    meter = cfg.llm_meter
    assert (meter.daily_usd, meter.monthly_usd, meter.port) == (5.0, 50.5, 8087)
    assert meter.upstreams["team-dev"] == LlmUpstream("https://x.openai.azure.com/openai/v1", "TEAM_DEV_KEY")
    assert meter.prices["gpt-5.6-terra"] == LlmPrice(2.0, 0.2, 12.0)


def test_config_without_meter_has_no_upstreams(tmp_path):
    assert load_agent_config(write_config(tmp_path, "")).llm_meter.upstreams == {}


def test_config_problems(tmp_path):
    with pytest.raises(ConfigError) as err:
        load_agent_config(write_config(tmp_path, """
[llm_meter]
daily_usd = -1
[llm_meter.upstreams.Dev_1]
base_url = "http://plain"
[llm_meter.prices]
x = [1, 2]
"""))
    text = "\n".join(err.value.problems)
    for expected in ("daily_usd", "lowercase", "https://", "'key' is required", "[input, cached input, output]",
                     "'default' price"):
        assert expected in text


# pricing and usage

def test_price_for_exact_then_longest_part_then_default():
    prices = {**PRICES, "gpt-5.6": LlmPrice(1, 1, 1)}
    assert price_for(prices, "gpt-5.6-terra") == PRICES["gpt-5.6-terra"]
    assert price_for(prices, "team-dev-gpt-5.6-terra") == PRICES["gpt-5.6-terra"]
    assert price_for(prices, "kimi") == PRICES["default"]


def test_usage_of_each_api_shape():
    assert usage_of({"usage": {"prompt_tokens": 100, "completion_tokens": 7,
                               "prompt_tokens_details": {"cached_tokens": 40}}}) == (100, 40, 7)
    assert usage_of({"type": "response.completed", "response": {"usage": {
        "input_tokens": 9, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 0}}}}) == (9, 0, 3)
    assert usage_of({"usage": {"prompt_tokens": 12, "total_tokens": 12}}) == (12, 0, 0)
    assert usage_of({"choices": []}) is None
    assert usage_of({"usage": None}) is None


def test_cost_charges_cached_tokens_at_the_cached_price():
    price = PRICES["gpt-5.6-terra"]
    assert llm_meter.cost(price, 1_000_000, 400_000, 100_000) == pytest.approx(0.6 * 2 + 0.4 * 0.2 + 0.1 * 12)


def test_sse_usage_survives_chunks_split_mid_line():
    stream = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
              b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
    scan = SseUsage()
    for i in range(0, len(stream), 7):
        scan.feed(stream[i:i + 7])
    scan.close()
    assert scan.usage == (5, 0, 2)


def test_prepare_body_asks_streams_for_usage_and_finds_the_model():
    body, model = prepare_body(json.dumps({"model": "m1", "stream": True}).encode(), "chat/completions")
    assert model == "m1" and json.loads(body)["stream_options"] == {"include_usage": True}
    plain = json.dumps({"model": "m2"}).encode()
    assert prepare_body(plain, "chat/completions") == (plain, "m2")
    assert prepare_body(b"not json", "embeddings") == (b"not json", "")


# caps and spend

def test_spend_follows_the_configured_timezone(conn):
    now = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)          # 11:00 in Singapore
    add_usage(conn, "2026-11-12T15:59:59Z", 1.0)            # 23:59:59 yesterday in Singapore
    add_usage(conn, "2026-11-12T16:00:00Z", 2.0)            # midnight today in Singapore
    add_usage(conn, "2026-10-31T15:59:59Z", 4.0)            # last month in Singapore
    assert spend(conn, TZ, now) == {"today_usd": 2.0, "month_usd": 3.0}


def test_runtime_caps_override_config_and_are_logged(conn):
    meter = settings()
    assert llm_meter.caps(conn, meter)["daily_usd"] == 10.0
    assert set_caps(conn, actor="ann", daily_usd=3) == {"daily_usd": 3.0}
    assert llm_meter.caps(conn, meter) == {"daily_usd": 3.0, "monthly_usd": 100.0, "changed_at_runtime": True}
    row = conn.execute("SELECT actor, data FROM events WHERE type = 'llm_caps_changed'").fetchone()
    assert row["actor"] == "ann" and json.loads(row["data"]) == {"daily_usd": 3.0}
    with pytest.raises(ValueError):
        set_caps(conn, actor="ann", monthly_usd=-5)


def test_meter_view(conn):
    now = datetime(2026, 11, 13, 3, 0, tzinfo=UTC)
    add_usage(conn, "2026-11-13T01:00:00Z", 0.5)
    add_usage(conn, "2026-11-10T01:00:00Z", 0.25)
    view = meter_view(conn, settings(), TZ, now)
    assert view["spend"] == {"today_usd": 0.5, "month_usd": 0.75}
    assert view["capped"] is None and view["upstreams"] == ["dev"]
    assert view["by_model"][0]["calls"] == 2
    assert view["daily"] == [{"day": "2026-11-10", "calls": 1, "cost_usd": 0.25},
                             {"day": "2026-11-13", "calls": 1, "cost_usd": 0.5}]


# the proxy

def chat_answer(stream: bool) -> httpx.Response:
    if stream:
        body = (b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":100}}\n\ndata: [DONE]\n\n')
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
    return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}],
                                     "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})


@pytest.fixture
def upstream_calls():
    return []


@pytest.fixture
async def client(conn, upstream_calls):
    def handler(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(request)
        if request.url.path.endswith("/broken"):
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return chat_answer(json.loads(request.content).get("stream", False))

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app = make_meter_app(conn, settings(), secrets={"DEV_KEY": "real-key"}, timezone=TZ, http=http)
    async with TestClient(TestServer(app)) as c:
        yield c
    await http.aclose()


async def test_forwards_with_the_real_key_and_records_the_cost(client, conn, upstream_calls):
    resp = await client.post("/llm/dev/chat/completions?api-version=x",
                             json={"model": "team-gpt-5.6-terra", "messages": []},
                             headers={"api-key": "placeholder", "Authorization": "Bearer placeholder"})
    assert resp.status == 200 and (await resp.json())["choices"][0]["message"]["content"] == "hello"
    sent = upstream_calls[0]
    assert str(sent.url) == "https://dev.example.azure.com/openai/v1/chat/completions?api-version=x"
    assert sent.headers["api-key"] == "real-key" and "authorization" not in sent.headers
    row = conn.execute("SELECT * FROM llm_usage").fetchone()
    assert (row["model"], row["input_tokens"], row["output_tokens"]) == ("team-gpt-5.6-terra", 1000, 100)
    assert row["cost_usd"] == pytest.approx((1000 * 2 + 100 * 12) / 1_000_000)


async def test_streams_through_and_counts_the_last_chunk(client, conn, upstream_calls):
    resp = await client.post("/llm/dev/chat/completions", json={"model": "kimi", "stream": True})
    text = await resp.text()
    assert resp.status == 200 and "hello" in text and text.rstrip().endswith("[DONE]")
    assert json.loads(upstream_calls[0].content)["stream_options"] == {"include_usage": True}
    row = conn.execute("SELECT * FROM llm_usage").fetchone()
    assert row["cost_usd"] == pytest.approx((1000 * 5 + 100 * 30) / 1_000_000)


async def test_upstream_errors_pass_through_and_are_recorded(client, conn):
    resp = await client.post("/llm/dev/broken", json={"model": "m"})
    assert resp.status == 500 and (await resp.json())["error"]["message"] == "boom"
    assert conn.execute("SELECT status FROM llm_usage").fetchone()["status"] == 500


async def test_refuses_once_the_cap_is_spent_and_logs_it_once(client, conn, upstream_calls):
    set_caps(conn, actor="ann", daily_usd=1)
    add_usage(conn, db.utcnow(), 1.5)
    for _ in range(2):
        resp = await client.post("/llm/dev/chat/completions", json={"model": "m"})
        assert resp.status == 429
        assert (await resp.json())["error"]["code"] == "cost_cap_reached"
    assert upstream_calls == []
    assert conn.execute("SELECT COUNT(*) FROM events WHERE type = 'llm_cap_reached'").fetchone()[0] == 1


async def test_paused_unknown_and_keyless(client, conn):
    assert (await client.post("/llm/nope/chat/completions", json={})).status == 404
    db.set_setting(conn, "paused", "1")
    resp = await client.post("/llm/dev/chat/completions", json={})
    assert resp.status == 503 and (await resp.json())["error"]["code"] == "agent_stopped"


async def test_missing_key_is_a_clear_error(conn):
    app = make_meter_app(conn, settings(), secrets={}, timezone=TZ, http=httpx.AsyncClient())
    async with TestClient(TestServer(app)) as c:
        resp = await c.post("/llm/dev/chat/completions", json={})
        assert resp.status == 503 and "DEV_KEY" in (await resp.json())["error"]["message"]
