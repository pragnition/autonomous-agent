"""What Azure actually bills per token on each metered upstream's Foundry resource, so the meter prices the agent's
calls at those rates instead of agent.toml's list prices.

Azure keeps no per-caller record on a shared resource: its metrics split by deployment, not by who called. So the
agent's tokens are Foundry's own usage report on each answer the meter forwards, and only the rate comes from here:
for each billing meter on the upstream's resource, Cost Management's cost over its quantity for the last 30 days.
That carries the discount, the cache-write price and per-token add-ons such as Defender for AI, as the bill does.
core reads them every few hours with the admin's az login ([cli.azure]) and re-prices this month's calls. Until a
read works, and for models Azure hasn't billed on the resource yet, the meter uses agent.toml's prices.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from agent import db
from agent.config import CliRules, LlmMeterSettings, LlmPrice
from agent.core import llm_meter

log = logging.getLogger(__name__)
RATES_KEY = "llm_meter.azure_rates"
ARM = "https://management.azure.com"
AZURE_HOSTS = (".openai.azure.com", ".services.ai.azure.com", ".cognitiveservices.azure.com")
UNITS = {"1": 1, "1K": 1_000, "1M": 1_000_000}
# How a billing meter names each kind of token, after the model and context ("5.6 terra ShortCo Cd Wr Std Gl ...")
KINDS = (("cdwr", "cache_write"), ("cdinp", "cached_input"), ("inp", "input"), ("opt", "output"))
WINDOW_DAYS = 30
AZ_TIMEOUT_S = 120


class AzureError(Exception):
    pass


def account_name(base_url: str) -> str | None:
    """The Azure resource an upstream's URL belongs to: https://<name>.openai.azure.com/... gives <name>."""
    host = (urlparse(base_url).hostname or "").lower()
    name = host.split(".", 1)[0]
    return name if host.endswith(AZURE_HOSTS) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) else None


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _family(model: str) -> str:
    """How billing meters spell a model: gpt-5.6-terra as "5.6 terra", gpt-6-luna as "6-luna"."""
    return _alnum(model).removeprefix("gpt")


def parse_rates(rows: list[dict], models: set[str]) -> tuple[dict[str, dict[str, float]], float]:
    """Cost Management rows (Cost, UsageQuantity, Meter, UnitOfMeasure, MeterSubcategory) as USD per million
    tokens of each kind for each model, and the per-token add-ons in USD per million."""
    totals: dict[tuple[str, str], list[float]] = {}
    addon = [0.0, 0.0]
    for row in rows:
        per, quantity = UNITS.get(row.get("UnitOfMeasure", "")), row.get("UsageQuantity") or 0
        if per is None or quantity <= 0:
            continue
        spent, tokens = row.get("Cost") or 0.0, quantity * per
        if "defender" in (row.get("MeterSubcategory") or "").lower():
            addon[0], addon[1] = addon[0] + spent, addon[1] + tokens
            continue
        meter = _alnum(row.get("Meter") or "")
        named = [m for m in models if _family(m) and meter.startswith(_family(m))]
        if not named:
            continue
        model = max(named, key=lambda m: len(_family(m)))
        rest = meter[len(_family(model)):]
        if rest.startswith("longco"):   # the meter prices every call at the short-context rate
            continue
        rest = rest.removeprefix("shortco")
        kind = next((k for prefix, k in KINDS if rest.startswith(prefix)), None)
        if kind is not None:
            total = totals.setdefault((model, kind), [0.0, 0.0])
            total[0], total[1] = total[0] + spent, total[1] + tokens
    rates: dict[str, dict[str, float]] = {}
    for (model, kind), (spent, tokens) in totals.items():
        rates.setdefault(model, {})[kind] = round(spent / tokens * 1_000_000, 6)
    return rates, round(addon[0] / addon[1] * 1_000_000, 6) if addon[1] else 0.0


class AzureRates:
    def __init__(self, conn: sqlite3.Connection, settings: LlmMeterSettings, *, timezone: str, rules: CliRules,
                 exec_fn=asyncio.create_subprocess_exec, run_as: int | None = None):
        """rules: the [cli.azure] command and env; run_as: the uid that owns the az login (the agent's)."""
        self.conn, self.settings, self.timezone, self.rules = conn, settings, timezone, rules
        self.exec_fn, self.run_as = exec_fn, run_as
        self.state: dict[str, dict] = json.loads(db.get_setting(conn, RATES_KEY) or "{}")

    def price(self, upstream: str, model: str, listed: LlmPrice) -> LlmPrice:
        info = self.state.get(upstream) or {}
        if "rates" not in info:
            return listed
        billed = info["rates"].get(info["deployments"].get(model, model), {})
        return LlmPrice(billed.get("input", listed.input), billed.get("cached_input", listed.cached_input),
                        billed.get("output", listed.output),
                        billed.get("cache_write", listed.cache_write), info["addon"])

    def view(self, upstream: str) -> dict:
        info = self.state.get(upstream) or {}
        if "rates" not in info:
            return {"source": "agent.toml", "error": info.get("error")}
        return {"source": "azure", "resource": info["resource"].rsplit("/", 1)[-1], "as_of": info["as_of"],
                "window_days": WINDOW_DAYS, "rates": info["rates"], "addon": info["addon"],
                "error": info.get("error")}

    async def refresh(self) -> None:
        """Read the billed rates for every upstream on an Azure resource, then re-price this month's calls."""
        for name, upstream in self.settings.upstreams.items():
            account = account_name(upstream.base_url)
            if account is None:
                continue
            try:
                self.state[name] = await self._read(account)
            except (AzureError, KeyError, TypeError, ValueError) as e:
                log.warning("couldn't read Azure's billed rates for %s: %s", name, e)
                self.state[name] = {**self.state.get(name, {}), "error": f"{type(e).__name__}: {e}"[:300]}
        db.set_setting(self.conn, RATES_KEY, json.dumps(self.state))
        llm_meter.reprice(self.conn, self.settings, self, self.timezone)

    async def _read(self, account: str) -> dict:
        found = await self._az("rest", "--method", "post",
                               "--url", f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01",
                               "--body", json.dumps({"query": "resources | where type =~ "
                                                     "'microsoft.cognitiveservices/accounts' and name =~ "
                                                     f"'{account}' | project id"}))
        ids = [r["id"] for r in found.get("data", [])]
        if len(ids) != 1:
            raise AzureError(f"the az login sees {len(ids)} Azure AI resources named {account}")
        resource = ids[0]
        listed = await self._az("rest", "--url", f"{ARM}{resource}/deployments?api-version=2024-10-01")
        deployments = {d["name"]: d["properties"]["model"]["name"] for d in listed.get("value", [])}
        now = datetime.now(UTC)
        query = {"type": "ActualCost", "timeframe": "Custom",
                 "timePeriod": {"from": (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT00:00:00Z"),
                                "to": now.strftime("%Y-%m-%dT%H:%M:%SZ")},
                 "dataset": {"granularity": "None",
                             "aggregation": {"Cost": {"name": "Cost", "function": "Sum"},
                                             "UsageQuantity": {"name": "UsageQuantity", "function": "Sum"}},
                             "grouping": [{"type": "Dimension", "name": n}
                                          for n in ("Meter", "UnitOfMeasure", "MeterSubcategory")],
                             "filter": {"dimensions": {"name": "ResourceId", "operator": "In",
                                                       "values": [resource]}}}}
        subscription = resource.split("/")[2]
        billed = await self._az("rest", "--method", "post", "--url", f"{ARM}/subscriptions/{subscription}/providers/"
                                "Microsoft.CostManagement/query?api-version=2023-11-01", "--body", json.dumps(query))
        columns = [c["name"] for c in billed["properties"]["columns"]]
        rates, addon = parse_rates([dict(zip(columns, r)) for r in billed["properties"]["rows"]],
                                   set(deployments.values()))
        return {"resource": resource, "deployments": deployments, "rates": rates, "addon": addon,
                "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ")}

    async def _az(self, *args: str) -> dict:
        as_agent = {"HOME": "/tmp", "USER": "agent", "LOGNAME": "agent"} if self.run_as is not None else {}
        proc = await self.exec_fn(self.rules.command, *args, "--output", "json",
                                  env={**os.environ, **self.rules.env, **as_agent}, stdin=asyncio.subprocess.DEVNULL,
                                  stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                  **({"user": self.run_as} if self.run_as is not None else {}))
        try:
            out, err = await asyncio.wait_for(proc.communicate(), AZ_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            raise AzureError(f"az {args[0]} took over {AZ_TIMEOUT_S}s") from None
        if proc.returncode != 0:
            lines = [line for line in err.decode(errors="replace").splitlines() if line.strip()]
            raise AzureError(lines[-1] if lines else f"az {args[0]} exited {proc.returncode}")
        return json.loads(out or b"{}")
