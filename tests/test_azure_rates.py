import json
from datetime import UTC, datetime, timedelta

import pytest

from agent import db
from agent.config import CliRules, LlmMeterSettings, LlmPrice, LlmUpstream
from agent.core import llm_meter
from agent.core.azure_rates import RATES_KEY, AzureRates, account_name, parse_rates

TZ = "Asia/Singapore"
LISTED = LlmPrice(2.0, 0.2, 12.0)
RESOURCE = ("/subscriptions/sub-1/resourceGroups/RG_DEV/providers/Microsoft.CognitiveServices/accounts/"
            "team-dev-models")
COLUMNS = ["Cost", "UsageQuantity", "Meter", "UnitOfMeasure", "MeterSubcategory", "Currency"]
# The shape Cost Management returns for a Foundry resource
ROWS = [
    [36.33, 18.872, "5.6 terra ShortCo Cd Wr Std Gl 1M Tokens", "1M", "Azure OpenAI GPT5", "USD"],
    [13.54, 21977.759, "Standard Tokens", "1K", "Microsoft Defender for AI Services", "USD"],
    [9.945, 1.0763, "5.6 terra ShortCo Opt Std Gl 1M Tokens", "1M", "Azure OpenAI GPT5", "USD"],
    [4.065, 26.3968, "5.6 terra ShortCo Cd Inp Std Gl 1M Tokens", "1M", "Azure OpenAI GPT5", "USD"],
    [4.02, 0.373, "Search Transactions", "1K", "Grounding with Bing", "USD"],
    [2.731, 1.7735, "5.6 terra ShortCo Inp Std Gl 1M Tokens", "1M", "Azure OpenAI GPT5", "USD"],
    [9.0, 1.0, "5.6 terra LongCo Inp Std Gl 1M Tokens", "1M", "Azure OpenAI GPT5", "USD"],
    [0.046, 0.597, "6-luna ShortCo Inp Std Gl 1M Tokens", "1M", "Azure OpenAI GPT6", "USD"],
    [0.984, 10652.377, "Embed v4 Txt Glbl Tokens", "1K", "Cohere Models", "USD"],
]
DEPLOYMENTS = {"value": [
    {"name": "team-dev-gpt-5.6-terra", "properties": {"model": {"name": "gpt-5.6-terra"}}},
    {"name": "team-dev-gpt-6-luna", "properties": {"model": {"name": "gpt-6-luna"}}},
    {"name": "cohere-embed-v4", "properties": {"model": {"name": "embed-v-4-0"}}},
]}


def test_account_name_from_azure_hosts_only():
    assert account_name("https://team-dev-models.openai.azure.com/openai/v1") == "team-dev-models"
    assert account_name("https://Team-Dev.services.ai.azure.com/models") == "team-dev"
    assert account_name("https://x.cognitiveservices.azure.com/") == "x"
    assert account_name("https://api.openai.com/v1") is None
    assert account_name("https://openai.azure.com.evil.example/v1") is None


def test_parse_rates_is_cost_over_quantity_per_kind_with_the_defender_addon():
    rates, addon = parse_rates([dict(zip(COLUMNS, r)) for r in ROWS], {"gpt-5.6-terra", "gpt-6-luna", "embed-v-4-0"})
    terra = rates["gpt-5.6-terra"]
    assert terra["input"] == pytest.approx(2.731 / 1.7735)                      # the short-context rate, not LongCo
    assert terra["cached_input"] == pytest.approx(4.065 / 26.3968)
    assert terra["cache_write"] == pytest.approx(36.33 / 18.872)
    assert terra["output"] == pytest.approx(9.945 / 1.0763)
    assert rates["gpt-6-luna"] == {"input": pytest.approx(0.046 / 0.597)}
    assert addon == pytest.approx(13.54 / 21977.759 * 1000)                      # per 1K tokens, as per 1M
    assert "embed-v-4-0" not in rates                                           # its meter doesn't name the model


def az_double(calls: list, *, fail: str | None = None):
    """A stand-in for `az rest`, answering each URL the way Azure does."""
    class Proc:
        def __init__(self, out: dict | None, err: str = ""):
            self.out, self.err, self.returncode = out, err, 0 if out is not None else 1

        async def communicate(self):
            return (json.dumps(self.out).encode() if self.out is not None else b""), self.err.encode()

    async def exec_fn(*args, **kw):
        calls.append((args, kw))
        url = args[args.index("--url") + 1]
        if fail and fail in url:
            return Proc(None, "WARNING: noise\nERROR: AADSTS70043: The refresh token has expired")
        if "ResourceGraph" in url:
            assert "name =~ 'team-dev-models'" in json.loads(args[args.index("--body") + 1])["query"]
            return Proc({"data": [{"id": RESOURCE}]})
        if url.endswith("/deployments?api-version=2024-10-01"):
            assert url.startswith(f"https://management.azure.com{RESOURCE}")
            return Proc(DEPLOYMENTS)
        assert "/subscriptions/sub-1/providers/Microsoft.CostManagement/query" in url
        query = json.loads(args[args.index("--body") + 1])
        assert query["dataset"]["filter"]["dimensions"]["values"] == [RESOURCE]
        return Proc({"properties": {"columns": [{"name": c} for c in COLUMNS], "rows": ROWS}})
    return exec_fn


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "agent.db")
    db.create_schema(c)
    return c


def meter() -> LlmMeterSettings:
    return LlmMeterSettings(upstreams={
        "team-dev": LlmUpstream("https://team-dev-models.openai.azure.com/openai/v1", "KEY"),
        "elsewhere": LlmUpstream("https://api.example.com/v1", "OTHER")},
        prices={"gpt-5.6-terra": LISTED, "default": LlmPrice(5.0, 0.5, 30.0)})


def rates_for(conn, calls, **kw) -> AzureRates:
    rules = CliRules(command="az", env={"AZURE_CONFIG_DIR": "/data/cli/azure"})
    return AzureRates(conn, meter(), timezone=TZ, rules=rules, exec_fn=az_double(calls, **kw), run_as=1001)


async def test_refresh_prices_deployments_at_the_billed_rates_and_reprices_the_month(conn):
    this_month = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("INSERT INTO llm_usage(at, upstream, model, status, input_tokens, cached_tokens, output_tokens, "
                 "cache_write_tokens, cost_usd) VALUES (?, 'team-dev', 'team-dev-gpt-5.6-terra', 200, 1000000, "
                 "400000, 200000, 500000, 99)", (this_month,))
    calls = []
    rates = rates_for(conn, calls)
    await rates.refresh()

    args, kw = calls[0]
    assert args[:2] == ("az", "rest") and args[-2:] == ("--output", "json")
    assert kw["user"] == 1001 and kw["env"]["AZURE_CONFIG_DIR"] == "/data/cli/azure" and kw["env"]["HOME"] == "/tmp"
    assert len(calls) == 3                                                       # the other upstream isn't Azure

    price = rates.price("team-dev", "team-dev-gpt-5.6-terra", LISTED)
    assert (price.input, price.cache_write) == (pytest.approx(2.731 / 1.7735), pytest.approx(36.33 / 18.872))
    assert price.addon == pytest.approx(0.616, abs=0.001)
    assert rates.price("elsewhere", "gpt-5.6-terra", LISTED) == LISTED
    luna = rates.price("team-dev", "team-dev-gpt-6-luna", LISTED)                # Azure billed only its input so far
    assert (luna.input, luna.output) == (pytest.approx(0.046 / 0.597), LISTED.output)

    expected = llm_meter.cost(price, 1_000_000, 400_000, 200_000, 500_000)
    assert conn.execute("SELECT cost_usd FROM llm_usage").fetchone()[0] == pytest.approx(expected)
    view = rates.view("team-dev")
    assert view["source"] == "azure" and view["resource"] == "team-dev-models" and view["error"] is None
    assert AzureRates(conn, meter(), timezone=TZ, rules=CliRules("az")).price(
        "team-dev", "team-dev-gpt-5.6-terra", LISTED) == price                   # kept across restarts


async def test_a_fresh_read_is_kept_for_6_hours(conn):
    now = datetime.now(UTC)
    await rates_for(conn, []).refresh(now)
    calls = []
    await rates_for(conn, calls).refresh(now + timedelta(hours=5))
    assert calls == []
    await rates_for(conn, calls).refresh(now + timedelta(hours=7))
    assert len(calls) == 3


async def test_a_failed_read_keeps_the_last_rates_says_why_and_is_retried_next_check(conn):
    now = datetime.now(UTC)
    await rates_for(conn, []).refresh(now)
    before = rates_for(conn, []).price("team-dev", "team-dev-gpt-5.6-terra", LISTED)
    rates = rates_for(conn, [], fail="CostManagement")
    await rates.refresh(now + timedelta(hours=7))
    assert rates.price("team-dev", "team-dev-gpt-5.6-terra", LISTED) == before
    assert "AADSTS70043" in rates.view("team-dev")["error"]
    calls = []
    retry = rates_for(conn, calls)
    await retry.refresh(now + timedelta(hours=7, minutes=30))
    assert len(calls) == 3 and retry.view("team-dev")["error"] is None


async def test_without_any_read_the_meter_uses_agent_toml(conn):
    rates = rates_for(conn, [], fail="ResourceGraph")
    await rates.refresh()
    assert rates.price("team-dev", "team-dev-gpt-5.6-terra", LISTED) == LISTED
    view = rates.view("team-dev")
    assert view["source"] == "agent.toml" and "refresh token has expired" in view["error"]
    assert "team-dev" in json.loads(db.get_setting(conn, RATES_KEY))
