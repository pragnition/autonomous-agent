import pytest

from aiohttp.test_utils import TestClient, TestServer

from agent.supervisor import (CONTROL, Heartbeat, Watchdog, deferrals, health_app, health_targets,
                              restartable)


@pytest.fixture
def wd():
    alerts, restarts, health = [], [], {"agentd": True}

    async def alert(text):
        alerts.append(text)

    async def restart(name):
        restarts.append(name)

    async def check(url):
        return health["agentd"]

    return Watchdog({"agentd": "http://control:8080/health"}, alert=alert, restart=restart, check=check), \
        alerts, restarts, health


async def tick(w, n):
    for _ in range(n):
        await w.tick()


async def test_no_alert_before_threshold(wd):
    w, alerts, restarts, health = wd
    health["agentd"] = False
    await tick(w, 2)
    assert alerts == [] and restarts == []


async def test_alert_and_restart_at_threshold(wd):
    w, alerts, restarts, health = wd
    health["agentd"] = False
    await tick(w, 3)
    assert alerts == ["agentd is down (3 failed health checks); restarting it once."] and restarts == ["agentd"]


async def test_no_repeat_alert_while_down(wd):
    w, alerts, restarts, health = wd
    health["agentd"] = False
    await tick(w, 10)
    assert len(alerts) == 1 and restarts == ["agentd"]


async def test_recovery_alert(wd):
    w, alerts, restarts, health = wd
    health["agentd"] = False
    await tick(w, 3)
    health["agentd"] = True
    await tick(w, 2)
    assert alerts[-1] == "agentd is back up." and len(alerts) == 2


async def test_brief_blip_never_alerts(wd):
    w, alerts, _, health = wd
    health["agentd"] = False
    await tick(w, 2)
    health["agentd"] = True
    await tick(w, 1)
    health["agentd"] = False
    await tick(w, 2)
    assert alerts == []


# Two supervisors: each watches the control plane and its peer; the standby leaves the control plane to the primary
# while the primary is up.

PRIMARY_ENV = {"SUPERVISOR_ROLE": "primary", "PEER_HEALTH": "http://supervisor-standby:8090/health",
               "PEER_CONTAINER": "agent-supervisor-standby"}
STANDBY_ENV = {"SUPERVISOR_ROLE": "standby", "PEER_HEALTH": "http://supervisor:8090/health",
               "PEER_CONTAINER": "agent-supervisor"}


def target_of(url):
    return "peer" if "supervisor" in url else CONTROL


def pair(environ, clock=None):
    alerts, restarts, health = [], [], {CONTROL: True, "peer": True}

    async def alert(text):
        alerts.append(text)

    async def restart(name):
        restarts.append(name)

    async def check(url):
        return health[target_of(url)]

    targets = health_targets(environ)
    return Watchdog(targets, alert=alert, restart=restart, check=check, defer_to=deferrals(environ, targets),
                    **({"clock": clock} if clock else {})), alerts, restarts, health


def test_targets_and_containers():
    # one supervisor alone still works
    assert health_targets({}) == {CONTROL: "http://control:8080/health"}
    assert restartable({}) == {CONTROL: "agent-control"}
    assert deferrals({}, health_targets({})) == {}
    assert health_targets(STANDBY_ENV)["peer"] == "http://supervisor:8090/health"
    assert restartable(STANDBY_ENV) == {CONTROL: "agent-control", "peer": "agent-supervisor"}
    assert deferrals(PRIMARY_ENV, health_targets(PRIMARY_ENV)) == {}
    assert deferrals(STANDBY_ENV, health_targets(STANDBY_ENV)) == {CONTROL: "peer"}


async def test_each_restarts_its_peer():
    for env in (PRIMARY_ENV, STANDBY_ENV):
        w, alerts, restarts, health = pair(env)
        health["peer"] = False
        await tick(w, 3)
        assert restarts == ["peer"] and len(alerts) == 1


async def test_primary_acts_on_the_control_plane_and_standby_stays_quiet():
    primary, p_alerts, p_restarts, p_health = pair(PRIMARY_ENV)
    standby, s_alerts, s_restarts, s_health = pair(STANDBY_ENV)
    p_health[CONTROL] = s_health[CONTROL] = False
    await tick(primary, 5)
    await tick(standby, 5)
    assert p_restarts == [CONTROL] and len(p_alerts) == 1
    assert s_restarts == [] and s_alerts == []


async def test_standby_takes_the_control_plane_over_when_primary_is_down():
    w, alerts, restarts, health = pair(STANDBY_ENV)
    health[CONTROL] = health["peer"] = False
    await tick(w, 3)
    assert restarts == ["peer", CONTROL]  # the peer is checked first, so the control plane isn't deferred on the same tick


async def test_standby_takes_the_control_plane_over_when_primary_dies_later():
    w, alerts, restarts, health = pair(STANDBY_ENV)
    health[CONTROL] = False
    await tick(w, 4)
    assert restarts == []
    health["peer"] = False
    await tick(w, 3)
    assert restarts == ["peer", CONTROL]


async def test_second_outage_within_30_minutes_alerts_without_restarting():
    now = [0.0]
    w, alerts, restarts, health = pair(PRIMARY_ENV, clock=lambda: now[0])
    health[CONTROL] = False
    await tick(w, 3)
    health[CONTROL] = True
    await tick(w, 1)
    now[0] = 20 * 60
    health[CONTROL] = False
    await tick(w, 5)
    assert restarts == [CONTROL]
    assert alerts[-1].startswith("control plane is down again (3 failed health checks), less than 30 minutes after")
    assert sum("down" in a for a in alerts) == 2  # one alert per outage, not one per tick


async def test_outage_after_30_minutes_gets_another_restart():
    now = [0.0]
    w, alerts, restarts, health = pair(PRIMARY_ENV, clock=lambda: now[0])
    health[CONTROL] = False
    await tick(w, 3)
    health[CONTROL] = True
    await tick(w, 1)
    now[0] = 31 * 60
    health[CONTROL] = False
    await tick(w, 3)
    assert restarts == [CONTROL, CONTROL]


def test_heartbeat_goes_stale():
    now = [0.0]
    hb = Heartbeat(max_age_s=180, clock=lambda: now[0])
    assert hb.healthy()
    now[0] = 181
    assert not hb.healthy()
    hb.beat()
    assert hb.healthy()


async def test_health_endpoint_reports_a_stalled_loop():
    now = [0.0]
    hb = Heartbeat(max_age_s=180, clock=lambda: now[0])
    async with TestClient(TestServer(health_app(hb))) as client:
        assert (await client.get("/health")).status == 200
        now[0] = 500
        assert (await client.get("/health")).status == 503
