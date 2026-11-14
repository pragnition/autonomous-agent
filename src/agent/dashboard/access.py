"""Who may use the dashboard.

Tailscale Serve is the only way in: it reaches core's dashboard port from the host, so requests arrive from the
Docker gateway, and it names the person in Tailscale-User-Login. The headers are trusted only from those peers; the
agent container reaches core from its own address and gets 403, so it can't pose as a person. POSTs also need a
header a cross-site form can't send.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from aiohttp import web

from agent.config import AgentConfig, Person


@dataclass(frozen=True)
class Viewer:
    login: str
    name: str
    person: Person | None

    @property
    def actor(self) -> str:
        return self.person.key if self.person else f"tailscale:{self.login}"

    @property
    def can_resume(self) -> bool:
        return self.person is not None and self.person.role == "admin"


VIEWER = web.RequestKey("viewer", Viewer)


def viewer(request: web.Request) -> Viewer:
    return request[VIEWER]


def _forbidden(why: str) -> web.Response:
    return web.json_response({"error": why}, status=403)


def access_middleware(config: AgentConfig):
    trusted = set(config.dashboard.trusted_peers)

    @web.middleware
    async def middleware(request: web.Request, handler):
        if request.remote not in trusted:
            return _forbidden("not from the tailnet")
        if request.path.startswith("/api/"):
            login = request.headers.get("Tailscale-User-Login", "").strip()
            if not login:
                return _forbidden("no Tailscale identity")
            if request.method not in ("GET", "HEAD"):
                if request.headers.get("X-Dashboard") != "1":
                    return _forbidden("missing X-Dashboard header")
                origin = request.headers.get("Origin")
                hosts = {request.host, request.headers.get("X-Forwarded-Host")}
                if origin and urlsplit(origin).netloc not in hosts:
                    return _forbidden("cross-site request")
            name = request.headers.get("Tailscale-User-Name", "").strip() or login
            request[VIEWER] = Viewer(login, name, config.person_by_tailscale(login))
        return await handler(request)

    return middleware
