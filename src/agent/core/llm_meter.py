"""The model-call meter: the apps the agent runs and tests (a project's own LLM features) call their model provider
through core instead of directly. core adds the real key, reads the token usage from each answer, prices it, and
refuses calls once the day's or the month's cap is spent, so a test loop can't run up a bill.

The token counts are Foundry's own (the usage report on each answer). Their price is what Azure bills on the
upstream's resource when core can read it (core/azure_rates.py), else agent.toml's [llm_meter.prices].

Apps point their OpenAI-compatible base URL at http://172.30.0.10:<port>/llm/<upstream> (core's fixed address on
the stack network; apps in the agent's own Docker can't resolve `core`) with any placeholder key. The caps
start from agent.toml's [llm_meter] and can be changed at runtime (the dashboard calls set_caps); days and months
follow the config's timezone. stop_all pauses these calls too.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple
from zoneinfo import ZoneInfo

import httpx
from aiohttp import web

from agent import db, events
from agent.config import LlmMeterSettings, LlmPrice

if TYPE_CHECKING:
    from agent.core.azure_rates import AzureRates

log = logging.getLogger(__name__)
CAP_KEYS = {"daily": "llm_meter.daily_usd", "monthly": "llm_meter.monthly_usd"}
CAPPED_ON = "llm_meter.capped_on"
# Not forwarded: hop-by-hop headers, the app's placeholder key, and compression (answers are read for their usage)
REQUEST_DROP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
                "proxy-authorization", "proxy-connection", "api-key", "authorization", "accept-encoding"}
RESPONSE_DROP = {"content-length", "content-encoding", "transfer-encoding", "connection", "keep-alive"}


def _ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def period_starts(tz: str, now: datetime) -> tuple[str, str]:
    """The start of today and of this month in the config's timezone, as UTC timestamps like the table's."""
    day = now.astimezone(ZoneInfo(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    return _ts(day), _ts(day.replace(day=1))


def caps(conn: sqlite3.Connection, settings: LlmMeterSettings) -> dict:
    daily, monthly = db.get_setting(conn, CAP_KEYS["daily"]), db.get_setting(conn, CAP_KEYS["monthly"])
    return {"daily_usd": float(daily) if daily is not None else settings.daily_usd,
            "monthly_usd": float(monthly) if monthly is not None else settings.monthly_usd,
            "changed_at_runtime": daily is not None or monthly is not None}


def set_caps(conn: sqlite3.Connection, *, actor: str, daily_usd: float | None = None,
             monthly_usd: float | None = None) -> dict:
    """Change the caps at runtime (the dashboard). They outlast restarts and config edits until changed again."""
    changes = {}
    for kind, value in (("daily", daily_usd), ("monthly", monthly_usd)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{kind}_usd must be a non-negative number of US dollars")
        changes[f"{kind}_usd"] = float(value)
    for key, value in changes.items():
        db.set_setting(conn, CAP_KEYS[key.removesuffix("_usd")], str(value))
    if changes:
        events.append(conn, "llm_caps_changed", task_id=None, actor=actor, data=changes)
    return changes


def spend(conn: sqlite3.Connection, tz: str, now: datetime | None = None) -> dict:
    day, month = period_starts(tz, now or datetime.now(UTC))
    row = conn.execute("SELECT COALESCE(SUM(CASE WHEN at >= ? THEN cost_usd END), 0) AS today, "
                       "COALESCE(SUM(cost_usd), 0) AS month FROM llm_usage WHERE at >= ?", (day, month)).fetchone()
    return {"today_usd": round(row["today"], 6), "month_usd": round(row["month"], 6)}


def over_cap(conn: sqlite3.Connection, settings: LlmMeterSettings, tz: str, now: datetime | None = None) -> str | None:
    limit, spent = caps(conn, settings), spend(conn, tz, now)
    if spent["today_usd"] >= limit["daily_usd"]:
        return f"today's cap of ${limit['daily_usd']:.2f} is spent (${spent['today_usd']:.2f})"
    if spent["month_usd"] >= limit["monthly_usd"]:
        return f"this month's cap of ${limit['monthly_usd']:.2f} is spent (${spent['month_usd']:.2f})"
    return None


def price_for(prices: dict[str, LlmPrice], model: str) -> LlmPrice:
    """The price listed for the model, else for the longest listed name inside it (a deployment named
    "team-gpt-5.6-terra" gets "gpt-5.6-terra"), else the default."""
    if model in prices:
        return prices[model]
    inside = [name for name in prices if name != "default" and name in model]
    if inside:
        return prices[max(inside, key=len)]
    return prices.get("default", LlmPrice(0.0, 0.0, 0.0))


class Usage(NamedTuple):
    input: int                     # every input token, including cached and cache-written ones
    cached: int
    output: int
    cache_write: int = 0


def usage_of(answer) -> Usage | None:
    """The tokens an OpenAI-style answer or stream event reports: chat completions, responses or embeddings."""
    if not isinstance(answer, dict):
        return None
    usage = answer.get("usage")
    if not isinstance(usage, dict) and isinstance(answer.get("response"), dict):
        usage = answer["response"].get("usage")  # the responses API's response.completed event
    if not isinstance(usage, dict):
        return None
    tokens_in = usage.get("prompt_tokens", usage.get("input_tokens")) or 0
    tokens_out = usage.get("completion_tokens", usage.get("output_tokens")) or 0
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    return Usage(int(tokens_in), int(details.get("cached_tokens") or 0), int(tokens_out),
                 int(details.get("cache_write_tokens") or 0))


def cost(price: LlmPrice, tokens_in: int, cached: int, tokens_out: int, cache_write: int = 0) -> float:
    cached = min(cached, tokens_in)
    cache_write = min(cache_write, tokens_in - cached)
    fresh = tokens_in - cached - cache_write
    write_price = price.input if price.cache_write is None else price.cache_write
    return (fresh * price.input + cached * price.cached_input + cache_write * write_price + tokens_out * price.output
            + (fresh + cache_write + tokens_out) * price.addon) / 1_000_000


def price_of(settings: LlmMeterSettings, rates: AzureRates | None, upstream: str, model: str) -> LlmPrice:
    """Azure's billed rates for the model on the upstream's resource when core has them, else agent.toml's price."""
    listed = price_for(settings.prices, model)
    return rates.price(upstream, model, listed) if rates is not None else listed


class SseUsage:
    """The last usage object in a server-sent event stream, fed in chunks that may split lines anywhere."""

    def __init__(self):
        self.buffer, self.usage = b"", None

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        *lines, self.buffer = self.buffer.split(b"\n")
        for line in lines:
            self._line(line)

    def close(self) -> None:
        self._line(self.buffer)
        self.buffer = b""

    def _line(self, line: bytes) -> None:
        line = line.strip()
        if not line.startswith(b"data:") or b'"usage"' not in line:
            return
        try:
            found = usage_of(json.loads(line[5:]))
        except ValueError:
            return
        if found is not None:
            self.usage = found


def prepare_body(body: bytes, path: str) -> tuple[bytes, str]:
    """The body to send and the model it names. A streaming chat completion is asked to report its usage in the
    last chunk, or the meter couldn't count it."""
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return body, ""
    if not isinstance(data, dict):
        return body, ""
    model = data["model"] if isinstance(data.get("model"), str) else ""
    if path.rstrip("/").endswith("chat/completions") and data.get("stream") is True:
        options = data.get("stream_options") if isinstance(data.get("stream_options"), dict) else {}
        if not options.get("include_usage"):
            data["stream_options"] = {**options, "include_usage": True}
            body = json.dumps(data).encode()
    return body, model


def record(conn: sqlite3.Connection, *, upstream: str, model: str, status: int, usage: Usage | None,
           price: LlmPrice) -> float:
    tokens = usage or Usage(0, 0, 0)
    spent = cost(price, *tokens)
    conn.execute("INSERT INTO llm_usage(at, upstream, model, status, input_tokens, cached_tokens, output_tokens, "
                 "cache_write_tokens, cost_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (db.utcnow(), upstream, model, status, tokens.input, tokens.cached, tokens.output, tokens.cache_write,
                  spent))
    if usage is None and status < 400:
        log.warning("%s answered %s for %s without a usage report; the call is counted as free", upstream, status, model)
    return spent


def reprice(conn: sqlite3.Connection, settings: LlmMeterSettings, rates: AzureRates | None, tz: str,
            now: datetime | None = None) -> None:
    """Price this month's calls again at the current rates, so the month's spend is all at what Azure bills."""
    _, month = period_starts(tz, now or datetime.now(UTC))
    rows = conn.execute("SELECT id, upstream, model, input_tokens, cached_tokens, output_tokens, cache_write_tokens "
                        "FROM llm_usage WHERE at >= ?", (month,)).fetchall()
    for r in rows:
        spent = cost(price_of(settings, rates, r["upstream"], r["model"]), r["input_tokens"], r["cached_tokens"],
                     r["output_tokens"], r["cache_write_tokens"])
        conn.execute("UPDATE llm_usage SET cost_usd = ? WHERE id = ?", (spent, r["id"]))


def meter_view(conn: sqlite3.Connection, settings: LlmMeterSettings, tz: str, now: datetime | None = None,
               rates: AzureRates | None = None) -> dict:
    """What the dashboard shows: caps, spend, this month by model, spend per day for 30 days, and where the prices
    come from."""
    now = now or datetime.now(UTC)
    _, month = period_starts(tz, now)
    by_model = [dict(r) for r in conn.execute(
        "SELECT upstream, model, COUNT(*) AS calls, SUM(status >= 400) AS errors, SUM(input_tokens) AS input_tokens, "
        "SUM(cached_tokens) AS cached_tokens, SUM(cache_write_tokens) AS cache_write_tokens, "
        "SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd "
        "FROM llm_usage WHERE at >= ? GROUP BY upstream, model ORDER BY cost_usd DESC", (month,))]
    zone, days = ZoneInfo(tz), {}
    for row in conn.execute("SELECT at, cost_usd FROM llm_usage WHERE at >= ?", (_ts(now - timedelta(days=30)),)):
        day = datetime.strptime(row["at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).astimezone(zone).date()
        calls, spent = days.get(day.isoformat(), (0, 0.0))
        days[day.isoformat()] = (calls + 1, spent + row["cost_usd"])
    return {"caps": caps(conn, settings), "spend": spend(conn, tz, now),
            "capped": over_cap(conn, settings, tz, now), "upstreams": sorted(settings.upstreams),
            "by_model": by_model,
            "daily": [{"day": d, "calls": c, "cost_usd": round(s, 6)} for d, (c, s) in sorted(days.items())],
            "pricing": {name: rates.view(name) if rates is not None else {"source": "agent.toml"}
                        for name in sorted(settings.upstreams)}}


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def make_meter_app(conn: sqlite3.Connection, settings: LlmMeterSettings, *, secrets: dict[str, str], timezone: str,
                   http: httpx.AsyncClient | None = None, rates: AzureRates | None = None) -> web.Application:
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))

    def refuse_if_capped() -> web.Response | None:
        why = over_cap(conn, settings, timezone)
        if why is None:
            return None
        day = period_starts(timezone, datetime.now(UTC))[0]
        if db.get_setting(conn, CAPPED_ON) != day:  # one event per day the cap is hit
            db.set_setting(conn, CAPPED_ON, day)
            events.append(conn, "llm_cap_reached", task_id=None, actor=None, data={"why": why})
        return _error(429, "cost_cap_reached", f"Model calls are paused: {why}. An admin can raise the cap on the "
                                               "dashboard.")

    async def forward(request: web.Request) -> web.StreamResponse:
        name, tail = request.match_info["upstream"], request.match_info["tail"]
        upstream = settings.upstreams.get(name)
        if upstream is None:
            return _error(404, "unknown_upstream", f"no upstream named {name!r} in [llm_meter]")
        key = secrets.get(upstream.key, "")
        if not key:
            return _error(503, "no_key", f"core has no {upstream.key} secret for the {name!r} upstream")
        if db.get_setting(conn, "paused", "0") == "1":
            return _error(503, "agent_stopped", "The agent is stopped, so model calls from its apps are paused too.")
        if (refusal := refuse_if_capped()) is not None:
            return refusal
        body, model = prepare_body(await request.read(), tail)
        model = model or name
        price = price_of(settings, rates, name, model)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in REQUEST_DROP}
        headers[upstream.header] = f"Bearer {key}" if upstream.header == "Authorization" else key
        outgoing = client.build_request(request.method, f"{upstream.base_url}/{tail}",
                                        params=list(request.query.items()), headers=headers, content=body)
        try:
            answer = await client.send(outgoing, stream=True)
        except httpx.HTTPError as e:
            record(conn, upstream=name, model=model, status=502, usage=None, price=price)
            return _error(502, "upstream_unreachable", f"{name}: {type(e).__name__}: {e}")
        reply_headers = {k: v for k, v in answer.headers.items() if k.lower() not in RESPONSE_DROP}
        try:
            if not answer.headers.get("content-type", "").startswith("text/event-stream"):
                data = await answer.aread()
                try:
                    usage = usage_of(json.loads(data))
                except ValueError:
                    usage = None
                record(conn, upstream=name, model=model, status=answer.status_code, usage=usage, price=price)
                return web.Response(status=answer.status_code, body=data, headers=reply_headers)
            reply = web.StreamResponse(status=answer.status_code, headers=reply_headers)
            await reply.prepare(request)
            scan = SseUsage()
            try:
                async for chunk in answer.aiter_bytes():
                    scan.feed(chunk)
                    await reply.write(chunk)
            finally:  # count what was used even when the app hangs up mid-stream
                scan.close()
                record(conn, upstream=name, model=model, status=answer.status_code, usage=scan.usage, price=price)
            await reply.write_eof()
            return reply
        finally:
            await answer.aclose()

    async def status(request: web.Request) -> web.Response:
        return web.json_response(meter_view(conn, settings, timezone, rates=rates))

    async def close_client(app: web.Application) -> None:
        if http is None:
            await client.aclose()

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_get("/llm", status)
    app.router.add_route("*", "/llm/{upstream}/{tail:.*}", forward)
    app.on_cleanup.append(close_client)
    return app
