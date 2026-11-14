import httpx
import pytest

from agent.proxy import PLACEHOLDER_TOKEN, ProxyClient, ProxyUnavailable

USAGE = {
    "maple": {"5h": [0.2, 1], "7d": [0.4, 2], "status": "allowed", "active": 1, "verified": True, "rank": 1},
    "cedar": {"5h": [0.9, 1], "7d": [0.8, 2], "status": "rejected", "active": 0, "verified": True},
    "_all": {"pace": {"state": "behind", "delta": -0.1}},
}


def client(handler):
    return ProxyClient("http://relay:28090", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_usage_parses_accounts_and_ranks():
    usage = await client(lambda req: httpx.Response(200, json=USAGE)).usage()
    assert set(usage.accounts) == {"maple", "cedar"}
    assert usage.has_capacity() is True
    assert usage.status_of("maple") == "allowed"
    assert usage.status_of("nope") is None
    assert usage.accounts["maple"].rank == 1 and usage.accounts["cedar"].rank is None
    assert usage.accounts["maple"].verified is True


async def test_no_rank_means_no_capacity():
    data = {k: {kk: vv for kk, vv in v.items() if kk != "rank"} for k, v in USAGE.items()}
    usage = await client(lambda req: httpx.Response(200, json=data)).usage()
    assert usage.has_capacity() is False


async def test_whoami_found_and_unpinned():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        sid = req.url.params["sid"]
        return httpx.Response(200, json={"account": "cedar"}) if sid == "a" else httpx.Response(404, json={})

    c = client(handler)
    assert await c.whoami("a") == "cedar"
    assert await c.whoami("b") is None
    assert seen[0] == "http://relay:28090/proxy/whoami?sid=a"


async def test_unreachable_raises():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(ProxyUnavailable):
        await client(handler).usage()


async def test_server_error_raises():
    with pytest.raises(ProxyUnavailable):
        await client(lambda req: httpx.Response(503, text="usage not ready")).usage()


def test_placeholder_token_constant():
    assert PLACEHOLDER_TOKEN == "sk-ant-oat01-container-via-host-proxy"
