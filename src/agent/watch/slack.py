"""Slack for improve-jensen: the few Web API calls its approval flow makes as Jensen's own bot (find the channel,
post, read a thread's replies and a message's reactions), plus where the bot's token and the approvers come from.

Stdlib only (`urllib`): the watch's workflow steps install nothing but this package, and `slack_sdk` is core's
dependency, not the watch's. `Slack` takes its `urlopen` as a constructor argument so tests can answer calls
without the network.

The token travels only in the `Authorization` header and is never printed, logged or put in an exception: a
failed call raises `SlackError` whose message is Slack's own error code (`channel_not_found`), `http_<status>`,
`bad_json`, or the name of the exception type a transport failure raised -- never its text, which for a header
urllib refuses can quote the header itself. Every `SlackError` is raised `from None`, so a traceback shows no
underlying exception either.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from agent.secrets import load_secrets

_API = "https://slack.com/api/"
_TIMEOUT = 30
# A Slack error code is lowercase words joined by `_`; anything else is reported as `unknown_error`.
_ERROR_CODE = re.compile(r"[a-z0-9_]{1,64}")
# Cursor pagination stops here even if Slack keeps handing back cursors.
_MAX_PAGES = 50


class SlackError(Exception):
    """A Slack call that failed. The message is Slack's error code, `http_<status>`, `bad_json`, or a transport
    exception's type name -- never the token."""


class Slack:
    """Slack's Web API as the bot whose `token` this is, through `urlopen` (`urllib.request.urlopen` by default;
    tests inject a fake). Each method returns parsed JSON and raises `SlackError` on `ok: false`, an HTTP error,
    a transport failure or a body that isn't a JSON object."""

    def __init__(self, token: str, *, urlopen: Callable = urllib.request.urlopen):
        self._token = token
        self._urlopen = urlopen

    def __repr__(self) -> str:
        return "Slack(<token hidden>)"

    def _call(self, method: str, **params: str | None) -> dict:
        body = urllib.parse.urlencode({key: value for key, value in params.items() if value is not None}).encode()
        request = urllib.request.Request(_API + method, data=body, headers={
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        })
        try:
            with self._urlopen(request, timeout=_TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise SlackError(f"http_{exc.code}") from None
        except Exception as exc:
            raise SlackError(type(exc).__name__) from None
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise SlackError("bad_json") from None
        if not isinstance(data, dict):
            raise SlackError("bad_json")
        if data.get("ok") is not True:
            error = data.get("error")
            raise SlackError(error if isinstance(error, str) and _ERROR_CODE.fullmatch(error) else "unknown_error")
        return data

    def _pages(self, method: str, key: str, **params: str | None) -> list[dict]:
        """Every `key` entry across `method`'s cursor-paginated replies."""
        out: list[dict] = []
        cursor = None
        for _ in range(_MAX_PAGES):
            data = self._call(method, **params, cursor=cursor)
            out.extend(entry for entry in data.get(key) or [] if isinstance(entry, dict))
            cursor = (data.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        return out

    def channel_id(self, name: str, cache: Path | None = None) -> str:
        """The id of the channel called `name` (a leading `#` stripped), joining it first when it's a public
        channel the bot isn't in (a private one has to invite the bot). With `cache`, the answer is kept there as
        `{"name", "id"}` and reused while it's for the same name. `SlackError("channel_not_found")` when the bot
        can't see a channel by that name."""
        name = name.removeprefix("#")
        if cache is not None:
            cached = _read_cache(Path(cache))
            if cached.get("name") == name and isinstance(cached.get("id"), str) and cached["id"]:
                return cached["id"]
        channels = self._pages("conversations.list", "channels", types="public_channel,private_channel",
                               exclude_archived="true", limit="1000")
        found = next((c for c in channels if c.get("name") == name and isinstance(c.get("id"), str)), None)
        if found is None:
            raise SlackError("channel_not_found")
        if not found.get("is_member") and not found.get("is_private"):
            self._call("conversations.join", channel=found["id"])
        if cache is not None:
            _atomic_write(Path(cache), json.dumps({"name": name, "id": found["id"]}))
        return found["id"]

    def post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        """Post `text` to `channel` (in `thread_ts`'s thread when given), links never unfurled; the new message's
        `ts`."""
        data = self._call("chat.postMessage", channel=channel, text=text, thread_ts=thread_ts,
                          unfurl_links="false", unfurl_media="false")
        ts = data.get("ts")
        if not isinstance(ts, str) or not ts:
            raise SlackError("bad_response")
        return ts

    def replies(self, channel: str, ts: str) -> list[dict]:
        """Every reply in the thread under `ts`, oldest first, without the parent message itself."""
        messages = self._pages("conversations.replies", "messages", channel=channel, ts=ts, limit="200")
        return [message for message in messages if message.get("ts") != ts]

    def reactions(self, channel: str, ts: str) -> list[dict]:
        """The reactions on the message at `ts`, each `{"name", "users"}` with every user who reacted."""
        data = self._call("reactions.get", channel=channel, timestamp=ts, full="true")
        message = data.get("message") if isinstance(data.get("message"), dict) else {}
        return [{"name": r["name"], "users": [u for u in r.get("users") or [] if isinstance(u, str)]}
                for r in message.get("reactions") or [] if isinstance(r, dict) and isinstance(r.get("name"), str)]


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_token(live: Path) -> str:
    """Jensen's Slack bot token (`SLACK_BOT_TOKEN` in `<live>/data/secrets/agent.env`), the bot that posts the
    proposals. `SlackError("no_token")` when it's missing, empty or unreadable."""
    try:
        token = load_secrets(Path(live) / "data" / "secrets" / "agent.env").get("SLACK_BOT_TOKEN", "").strip()
    except OSError:
        token = ""
    if not token:
        raise SlackError("no_token")
    return token


def load_approvers(live: Path) -> set[str]:
    """The Slack user id of every `[people.*]` entry in `<live>/data/config/agent.toml`: the people whose replies
    and reactions count. An empty set when the file can't be read or parsed, so nothing is approved."""
    try:
        with open(Path(live) / "data" / "config" / "agent.toml", "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    people = data.get("people")
    if not isinstance(people, dict):
        return set()
    return {person["slack"].strip() for person in people.values()
            if isinstance(person, dict) and isinstance(person.get("slack"), str) and person["slack"].strip()}
