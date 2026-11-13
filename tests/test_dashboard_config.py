import httpx
import pytest

from agent.config import ConfigError
from agent.proxy import ProxyClient, ProxyUnavailable
from test_config import MIN_AGENT, agent

WITH_TAILSCALE = MIN_AGENT.replace('role = "admin"', 'role = "admin"\ntailscale = "ann@x.com"')


def test_person_tailscale_optional(tmp_path):
    assert agent(tmp_path).people["ann"].tailscale == ""


def test_person_by_tailscale_case_insensitive(tmp_path):
    cfg = agent(tmp_path, WITH_TAILSCALE)
    assert cfg.person_by_tailscale("Ann@X.com").key == "ann"
    assert cfg.person_by_tailscale(None) is None
    assert cfg.person_by_tailscale("") is None
    assert cfg.person_by_tailscale("bob@x.com") is None


def test_person_without_tailscale_never_matches_empty(tmp_path):
    assert agent(tmp_path).person_by_tailscale("") is None


def test_dashboard_defaults(tmp_path):
    d = agent(tmp_path).dashboard
    assert d.port == 8086
    assert d.trusted_peers == ("172.30.0.1",)
    assert d.agent_origins == ("agent-core", "agent-sandbox")


def test_dashboard_overrides(tmp_path):
    d = agent(tmp_path, MIN_AGENT + '\n[dashboard]\nport = 9000\ntrusted_peers = ["10.0.0.1"]\n'
                                    'agent_origins = ["bot"]\n').dashboard
    assert (d.port, d.trusted_peers, d.agent_origins) == (9000, ("10.0.0.1",), ("bot",))


def test_dashboard_bad_port(tmp_path):
    with pytest.raises(ConfigError, match="port"):
        agent(tmp_path, MIN_AGENT + '\n[dashboard]\nport = "x"\n')


def test_tailscale_must_be_string(tmp_path):
    with pytest.raises(ConfigError, match="tailscale"):
        agent(tmp_path, MIN_AGENT.replace('role = "admin"', 'role = "admin"\ntailscale = 3'))


def client(handler):
    return ProxyClient("http://relay:28090", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_raw_usage_returns_json():
    data = {"maple": {"status": "allowed"}, "_all": {"pace": {}}}
    assert await client(lambda req: httpx.Response(200, json=data)).raw_usage() == data


async def test_perf_returns_rows():
    seen = []

    def handler(req):
        seen.append(req.url.params.get("hours"))
        return httpx.Response(200, json={"window": 300, "rows": [{"t": 1, "end": 2}]})

    assert await client(handler).perf(24) == [{"t": 1, "end": 2}]
    assert seen == ["24"]


async def test_perf_unreachable_raises():
    def handler(req):
        raise httpx.ConnectError("down")

    with pytest.raises(ProxyUnavailable):
        await client(handler).perf(24)
