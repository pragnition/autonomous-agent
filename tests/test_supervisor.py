import pytest

from agent.supervisor import Watchdog


@pytest.fixture
def wd():
    alerts, restarts, health = [], [], {"core": True}

    async def alert(text):
        alerts.append(text)

    async def restart(name):
        restarts.append(name)

    async def check(url):
        return health["core"]

    return Watchdog({"core": "http://core:8080/health"}, alert=alert, restart=restart, check=check), \
        alerts, restarts, health


async def tick(w, n):
    for _ in range(n):
        await w.tick()


async def test_no_alert_before_threshold(wd):
    w, alerts, restarts, health = wd
    health["core"] = False
    await tick(w, 2)
    assert alerts == [] and restarts == []


async def test_alert_and_restart_at_threshold(wd):
    w, alerts, restarts, health = wd
    health["core"] = False
    await tick(w, 3)
    assert alerts == ["core is down (3 failed health checks); restarting it once."] and restarts == ["core"]


async def test_no_repeat_alert_while_down(wd):
    w, alerts, restarts, health = wd
    health["core"] = False
    await tick(w, 10)
    assert len(alerts) == 1 and restarts == ["core"]


async def test_recovery_alert(wd):
    w, alerts, restarts, health = wd
    health["core"] = False
    await tick(w, 3)
    health["core"] = True
    await tick(w, 2)
    assert alerts[-1] == "core is back up." and len(alerts) == 2


async def test_brief_blip_never_alerts(wd):
    w, alerts, _, health = wd
    health["core"] = False
    await tick(w, 2)
    health["core"] = True
    await tick(w, 1)
    health["core"] = False
    await tick(w, 2)
    assert alerts == []
