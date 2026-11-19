import io
import json
import urllib.error
import urllib.parse

import pytest

from agent.watch.slack import Slack, SlackError, load_approvers, load_token

TOKEN = "xoxb-test-0000-secret"


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """Stands in for `urllib.request.urlopen`: answers each Slack method from its own queue of canned replies (a
    dict becomes the JSON body, bytes are sent as they are, an exception is raised) and records every request as
    `(method, params, headers)`."""

    def __init__(self, answers: dict[str, list]):
        self.answers = {method: list(replies) for method, replies in answers.items()}
        self.requests: list[tuple[str, dict, dict]] = []
        self.urls: list[str] = []

    def __call__(self, request, timeout=None):
        self.urls.append(request.full_url)
        method = request.full_url.rsplit("/", 1)[1]
        params = dict(urllib.parse.parse_qsl(request.data.decode()))
        self.requests.append((method, params, {k.lower(): v for k, v in request.header_items()}))
        answer = self.answers[method].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Response(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.requests]


def _channel(name, cid, member=True, private=False):
    return {"id": cid, "name": name, "is_member": member, "is_private": private}


# -- calls --


def test_every_call_sends_the_token_only_in_the_authorization_header():
    api = FakeApi({"chat.postMessage": [{"ok": True, "ts": "1.000100"}]})
    Slack(TOKEN, urlopen=api).post("C1", "hello")
    method, params, headers = api.requests[0]
    assert api.urls == ["https://slack.com/api/chat.postMessage"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(params) and TOKEN not in api.urls[0]


def test_post_sends_text_without_unfurls_and_returns_the_ts():
    api = FakeApi({"chat.postMessage": [{"ok": True, "ts": "1.000100"}, {"ok": True, "ts": "2.000200"}]})
    slack = Slack(TOKEN, urlopen=api)

    assert slack.post("C1", "hello") == "1.000100"
    assert slack.post("C1", "in thread", thread_ts="1.000100") == "2.000200"

    first, second = api.requests[0][1], api.requests[1][1]
    assert first == {"channel": "C1", "text": "hello", "unfurl_links": "false", "unfurl_media": "false"}
    assert second["thread_ts"] == "1.000100" and second["text"] == "in thread"


def test_post_without_a_ts_in_the_reply_is_an_error():
    api = FakeApi({"chat.postMessage": [{"ok": True}]})
    with pytest.raises(SlackError):
        Slack(TOKEN, urlopen=api).post("C1", "hello")


def test_replies_follow_the_cursor_and_leave_out_the_parent():
    parent = {"ts": "1.000100", "user": "U1", "text": "the proposal"}
    api = FakeApi({"conversations.replies": [
        {"ok": True, "messages": [parent, {"ts": "1.000200", "user": "U1", "text": "approve"}],
         "response_metadata": {"next_cursor": "page2"}},
        {"ok": True, "messages": [parent, {"ts": "1.000300", "user": "U2", "text": "reject 2"}],
         "response_metadata": {"next_cursor": ""}},
    ]})
    replies = Slack(TOKEN, urlopen=api).replies("C1", "1.000100")

    assert [r["ts"] for r in replies] == ["1.000200", "1.000300"]
    assert api.requests[0][1]["channel"] == "C1" and api.requests[0][1]["ts"] == "1.000100"
    assert "cursor" not in api.requests[0][1]
    assert api.requests[1][1]["cursor"] == "page2"


def test_reactions_ask_for_every_user_and_return_name_and_users():
    api = FakeApi({"reactions.get": [{"ok": True, "type": "message", "message": {"ts": "1.000100", "reactions": [
        {"name": "white_check_mark", "users": ["U1", "U2"], "count": 2},
        {"name": "x", "users": ["U3"], "count": 1},
    ]}}]})
    reactions = Slack(TOKEN, urlopen=api).reactions("C1", "1.000100")

    assert reactions == [{"name": "white_check_mark", "users": ["U1", "U2"]}, {"name": "x", "users": ["U3"]}]
    assert api.requests[0][1] == {"channel": "C1", "timestamp": "1.000100", "full": "true"}


def test_reactions_on_a_message_without_any():
    api = FakeApi({"reactions.get": [{"ok": True, "message": {"ts": "1.000100"}}]})
    assert Slack(TOKEN, urlopen=api).reactions("C1", "1.000100") == []


# -- channel_id --


def test_channel_id_pages_through_the_list_and_strips_the_hash():
    api = FakeApi({"conversations.list": [
        {"ok": True, "channels": [_channel("general", "C0")], "response_metadata": {"next_cursor": "next"}},
        {"ok": True, "channels": [_channel("autonomous-agent", "C9")], "response_metadata": {"next_cursor": ""}},
    ]})
    assert Slack(TOKEN, urlopen=api).channel_id("#autonomous-agent") == "C9"

    first, second = api.requests[0][1], api.requests[1][1]
    assert first == {"types": "public_channel,private_channel", "exclude_archived": "true", "limit": "1000"}
    assert second["cursor"] == "next"
    assert api.methods() == ["conversations.list", "conversations.list"]


def test_channel_id_joins_a_public_channel_it_is_not_in():
    api = FakeApi({"conversations.list": [{"ok": True, "channels": [_channel("autonomous-agent", "C9", member=False)]}],
                   "conversations.join": [{"ok": True, "channel": {"id": "C9"}}]})
    assert Slack(TOKEN, urlopen=api).channel_id("autonomous-agent") == "C9"
    assert api.methods() == ["conversations.list", "conversations.join"]
    assert api.requests[1][1] == {"channel": "C9"}


def test_channel_id_never_tries_to_join_a_private_channel():
    api = FakeApi({"conversations.list": [{"ok": True, "channels": [
        _channel("autonomous-agent", "G9", member=False, private=True)]}]})
    assert Slack(TOKEN, urlopen=api).channel_id("autonomous-agent") == "G9"
    assert api.methods() == ["conversations.list"]


def test_channel_id_missing_channel_is_channel_not_found():
    api = FakeApi({"conversations.list": [{"ok": True, "channels": [_channel("general", "C0")]}]})
    with pytest.raises(SlackError) as caught:
        Slack(TOKEN, urlopen=api).channel_id("autonomous-agent")
    assert str(caught.value) == "channel_not_found"


def test_channel_id_caches_and_reuses_the_cache(tmp_path):
    cache = tmp_path / "slack-channel.json"
    api = FakeApi({"conversations.list": [{"ok": True, "channels": [_channel("autonomous-agent", "C9")]}]})
    slack = Slack(TOKEN, urlopen=api)

    assert slack.channel_id("autonomous-agent", cache=cache) == "C9"
    assert json.loads(cache.read_text()) == {"name": "autonomous-agent", "id": "C9"}
    assert slack.channel_id("#autonomous-agent", cache=cache) == "C9"
    assert api.methods() == ["conversations.list"]
    assert list(tmp_path.iterdir()) == [cache]   # written atomically: no temp file left behind


def test_channel_id_ignores_a_cache_for_another_channel_or_a_broken_one(tmp_path):
    cache = tmp_path / "slack-channel.json"
    api = FakeApi({"conversations.list": [{"ok": True, "channels": [_channel("autonomous-agent", "C9")]},
                                          {"ok": True, "channels": [_channel("autonomous-agent", "C9")]}]})
    slack = Slack(TOKEN, urlopen=api)

    cache.write_text(json.dumps({"name": "general", "id": "C0"}))
    assert slack.channel_id("autonomous-agent", cache=cache) == "C9"
    cache.write_text("{not json")
    assert slack.channel_id("autonomous-agent", cache=cache) == "C9"
    assert json.loads(cache.read_text()) == {"name": "autonomous-agent", "id": "C9"}
    assert api.methods() == ["conversations.list", "conversations.list"]


# -- errors never carry the token --


def _http_error(code):
    return urllib.error.HTTPError(f"https://slack.com/api/chat.postMessage?token={TOKEN}", code, f"oops {TOKEN}",
                                  {}, io.BytesIO(f'{{"token": "{TOKEN}"}}'.encode()))


@pytest.mark.parametrize("answer, message", [
    ({"ok": False, "error": "not_in_channel"}, "not_in_channel"),
    ({"ok": False, "error": f"weird {TOKEN}"}, "unknown_error"),
    ({"ok": False}, "unknown_error"),
    (_http_error(429), "http_429"),
    (_http_error(500), "http_500"),
    (b"<html>gateway timeout</html>", "bad_json"),
    (b"[1, 2]", "bad_json"),
    (urllib.error.URLError(f"no route for {TOKEN}"), "URLError"),
    (TimeoutError(f"timed out {TOKEN}"), "TimeoutError"),
    (ValueError(f"Invalid header value b'Bearer {TOKEN}'"), "ValueError"),
])
def test_slack_error_is_the_code_or_http_status_never_the_token(answer, message):
    api = FakeApi({"chat.postMessage": [answer]})
    with pytest.raises(SlackError) as caught:
        Slack(TOKEN, urlopen=api).post("C1", "hello")
    assert str(caught.value) == message
    assert TOKEN not in str(caught.value) and TOKEN not in repr(caught.value)
    # Nothing underneath it shows in a traceback either.
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_the_client_never_shows_its_token_in_repr():
    assert TOKEN not in repr(Slack(TOKEN))


# -- load_token / load_approvers --


def _live(tmp_path, env: str | None = None, toml: str | None = None):
    if env is not None:
        (tmp_path / "data" / "secrets").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "secrets" / "agent.env").write_text(env)
    if toml is not None:
        (tmp_path / "data" / "config").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "config" / "agent.toml").write_text(toml)
    return tmp_path


def test_load_token_reads_the_bots_token_from_agent_env(tmp_path):
    live = _live(tmp_path, env=f"GH_TOKEN=ghp_other\nSLACK_BOT_TOKEN='{TOKEN}'\n")
    assert load_token(live) == TOKEN


@pytest.mark.parametrize("env", [None, "GH_TOKEN=ghp_other\n", "SLACK_BOT_TOKEN=\n"])
def test_load_token_missing_is_no_token(tmp_path, env):
    with pytest.raises(SlackError) as caught:
        load_token(_live(tmp_path, env=env))
    assert str(caught.value) == "no_token"


def test_load_approvers_is_every_persons_slack_id(tmp_path):
    live = _live(tmp_path, toml="""
name = "agent"

[people.first]
slack = "U00000001"
github = "first-example"
linear = "first@example.com"
role = "admin"

[people.second]
slack = " U00000002 "
linear = "second@example.com"

[people.third]
linear = "third@example.com"
""")
    assert load_approvers(live) == {"U00000001", "U00000002"}


@pytest.mark.parametrize("toml", [None, "this is = = not toml", "people = 3\n", "name = 'agent'\n"])
def test_load_approvers_unreadable_is_nobody(tmp_path, toml):
    assert load_approvers(_live(tmp_path, toml=toml)) == set()
