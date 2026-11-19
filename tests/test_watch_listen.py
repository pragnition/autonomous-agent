import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.watch import cli, listen
from agent.watch.approvals import answer_threads
from agent.watch.slack import SlackError
from test_watch_approvals import APPROVER, OTHER, PARENT, REPO, FakeGh, FakeSlack, _record, _reply

T0 = datetime(2026, 11, 19, 10, 5, 0, tzinfo=UTC)
PEOPLE = {APPROVER: "first"}
MSG = "1700000001.000000"
FALLBACK = "Sorry, I couldn't read that just now. Try again, or reply `approve 1 3` / `reject 2`."


# -- the clock --


class Dispatch:
    def __init__(self, fail=()):
        self.sent: list[str] = []
        self.fail = set(fail)

    def __call__(self, event_type):
        if event_type in self.fail:
            raise subprocess.CalledProcessError(1, ["gh"])
        self.sent.append(event_type)


def _clock(root: Path) -> dict:
    return json.loads((root / "clock.json").read_text())


@pytest.mark.parametrize("now, watch, assess, approvals", [
    ("10:05:00", "09:17", "09:55", "10:00"),
    ("10:17:00", "10:17", "09:55", "10:10"),
    ("10:54:59", "10:17", "09:55", "10:50"),
    ("10:55:00", "10:17", "10:55", "10:50"),
    ("00:16:59", "23:17", "23:55", "00:10"),
])
def test_latest_slots(now, watch, assess, approvals):
    at = datetime.fromisoformat(f"2026-11-19T{now}+00:00")
    slots = {kind: slot.strftime("%H:%M") for kind, slot in listen.latest_slots(at).items()}
    assert slots == {"improve-jensen-watch": watch, "improve-jensen-assess": assess,
                     "improve-jensen-approvals": approvals}


def test_first_start_records_the_latest_slots_without_sending(tmp_path):
    dispatch = Dispatch()
    listen.tick(dispatch, tmp_path, T0)
    assert dispatch.sent == []
    assert _clock(tmp_path) == {"improve-jensen-watch": "2026-11-19T09:17:00Z",
                                "improve-jensen-assess": "2026-11-19T09:55:00Z",
                                "improve-jensen-approvals": "2026-11-19T10:00:00Z"}


def test_each_slot_is_sent_once(tmp_path):
    dispatch = Dispatch()
    listen.tick(dispatch, tmp_path, T0)
    for minute, second in ((10, 0), (10, 15), (12, 30), (17, 0), (17, 45)):
        listen.tick(dispatch, tmp_path, T0.replace(minute=minute, second=second))
    assert dispatch.sent == ["improve-jensen-approvals", "improve-jensen-watch"]


def test_catch_up_after_downtime_sends_the_latest_slot_once(tmp_path):
    dispatch = Dispatch()
    listen.tick(dispatch, tmp_path, T0)
    later = T0 + timedelta(hours=3, minutes=15)   # 13:20
    listen.tick(dispatch, tmp_path, later)
    listen.tick(dispatch, tmp_path, later + timedelta(seconds=15))
    assert sorted(dispatch.sent) == ["improve-jensen-approvals", "improve-jensen-assess", "improve-jensen-watch"]
    assert _clock(tmp_path) == {"improve-jensen-watch": "2026-11-19T13:17:00Z",
                                "improve-jensen-assess": "2026-11-19T12:55:00Z",
                                "improve-jensen-approvals": "2026-11-19T13:20:00Z"}


def test_a_failed_send_is_tried_again_next_tick(tmp_path, capsys):
    dispatch = Dispatch(fail={"improve-jensen-approvals"})
    listen.tick(dispatch, tmp_path, T0)
    listen.tick(dispatch, tmp_path, T0.replace(minute=10))
    assert dispatch.sent == [] and _clock(tmp_path)["improve-jensen-approvals"] == "2026-11-19T10:00:00Z"
    assert "CalledProcessError" in capsys.readouterr().err
    dispatch.fail = set()
    listen.tick(dispatch, tmp_path, T0.replace(minute=10, second=15))
    assert dispatch.sent == ["improve-jensen-approvals"]


# -- the threads --


class ListenGh(FakeGh):
    """`FakeGh` that also answers `gh issue view N --json title,body` with a long body."""

    def __call__(self, args):
        if args[:2] == ["issue", "view"] and args[3:] == ["--json", "title,body"]:
            self.calls.append(list(args))
            return json.dumps({"title": f"Title {args[2]}", "body": "x" * 1400 + "y" * 600})
        return super().__call__(args)


class ListenSlack(FakeSlack):
    def __init__(self, react_error=None, **kwargs):
        super().__init__(**kwargs)
        self.reacted: list[tuple[str, str, str]] = []
        self.react_error = react_error

    def react(self, channel, ts, name):
        if self.react_error:
            raise SlackError(self.react_error)
        self.reacted.append((channel, ts, name))


class Claude:
    """Answers each ask with the next of `answers` (an exception is raised, a dict is sent as JSON); records each
    `(system, prompt)`."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[tuple[str, str]] = []

    def __call__(self, system, prompt):
        self.asked.append((system, prompt))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)

    def prompt(self, i=0) -> dict:
        return json.loads(self.asked[i][1])


def _answer(root, slack, gh, claude, dispatch=None, now=T0):
    dispatch = dispatch if dispatch is not None else Dispatch()
    answer_threads(slack, gh, claude, dispatch, root, PEOPLE, now)
    return dispatch


def _saved(root) -> dict:
    return json.loads((root / "proposals" / f"{PARENT}.json").read_text())


def test_a_reply_gets_eyes_an_answer_and_its_decisions_stored(tmp_path):
    _record(tmp_path, [(31, "Retry flaky tool calls"), (40, "Shorter replies")])
    gh = ListenGh({31: {"proposed"}, 40: {"proposed", "project"}})
    slack = ListenSlack(threads={PARENT: [
        {"type": "message", "bot_id": "B1", "text": "earlier answer", "ts": "1700000000.500000"},
        _reply("go ahead with the first, only for new projects", ts=MSG)]})
    claude = Claude({"decisions": [{"n": 1, "verdict": "approve", "note": "Only for new projects"}],
                     "reply": "Approved 1 <!channel>; it starts shipping within minutes."})

    dispatch = _answer(tmp_path, slack, gh, claude)

    assert slack.reacted == [("C9", MSG, "eyes")]
    assert slack.thread_posts() == ["Approved 1 &lt;!channel&gt;; it starts shipping within minutes."]
    record = _saved(tmp_path)
    assert record["decisions"] == {"1": {"verdict": "approve", "note": "Only for new projects", "ts": MSG,
                                         "by": APPROVER}}
    assert record["handled"] == [MSG]
    assert dispatch.sent == ["improve-jensen-approvals"]

    system, _ = claude.asked[0]
    assert "JSON" in system and "public" in system
    prompt = claude.prompt()
    assert prompt["items"] == [
        {"n": 1, "change": "Retry flaky tool calls", "kind": "fix", "issue": 31, "state": "open",
         "title": "Title 31", "body": "x" * 1400 + "y" * 100},
        {"n": 2, "change": "Shorter replies", "kind": "project", "issue": 40, "state": "open",
         "title": "Title 40", "body": "x" * 1400 + "y" * 100}]
    assert prompt["thread"] == [{"from": "improve-jensen", "text": "earlier answer"}]
    assert prompt["new_message"] == {"from": "first", "text": "go ahead with the first, only for new projects"}

    # Handled: the next loop leaves it alone.
    _answer(tmp_path, slack, gh, claude)
    assert len(claude.asked) == 1 and len(slack.reacted) == 1 and len(slack.posts) == 1


def test_a_question_is_answered_with_no_decision(tmp_path):
    _record(tmp_path, [(31, "a")])
    slack = ListenSlack(threads={PARENT: [_reply("what does 1 change?", ts=MSG)]})
    dispatch = _answer(tmp_path, slack, ListenGh({31: {"proposed"}}), Claude({"decisions": [], "reply": "It ..."}))
    assert slack.thread_posts() == ["It ..."]
    assert _saved(tmp_path)["handled"] == [MSG] and _saved(tmp_path).get("decisions", {}) == {}
    assert dispatch.sent == []


def test_only_approvers_messages_are_answered(tmp_path):
    _record(tmp_path, [(31, "a")])
    slack = ListenSlack(threads={PARENT: [
        _reply("approve", user=OTHER), _reply("approve", bot_id="B1"), _reply("approve", subtype="channel_join")]})
    claude = Claude()
    _answer(tmp_path, slack, ListenGh({31: {"proposed"}}), claude)
    assert claude.asked == [] and slack.reacted == [] and slack.posts == []
    assert "handled" not in _saved(tmp_path)


def test_a_reply_also_sent_to_the_channel_counts(tmp_path):
    _record(tmp_path, [(31, "a")])
    slack = ListenSlack(threads={PARENT: [_reply("yes", ts=MSG, subtype="thread_broadcast")]})
    _answer(tmp_path, slack, ListenGh({31: {"proposed"}}),
            Claude({"decisions": [{"n": 1, "verdict": "approve"}], "reply": "ok"}))
    assert _saved(tmp_path)["decisions"]["1"]["verdict"] == "approve"


@pytest.mark.parametrize("failures", [
    [RuntimeError("claude exited 1"), RuntimeError("claude exited 1")],
    [subprocess.TimeoutExpired(["claude"], 90), "not json"],
    ['{"decisions": []}', '["reply"]'],
])
def test_claude_failing_twice_falls_back_to_the_plain_words(tmp_path, capsys, failures):
    _record(tmp_path, [(31, "a")])
    slack = ListenSlack(threads={PARENT: [_reply("approve the secret thing", ts=MSG)]})
    claude = Claude(*failures)

    dispatch = _answer(tmp_path, slack, ListenGh({31: {"proposed"}}), claude)

    assert len(claude.asked) == 2
    assert slack.thread_posts() == [FALLBACK]
    assert _saved(tmp_path)["handled"] == [MSG] and _saved(tmp_path).get("decisions", {}) == {}
    assert dispatch.sent == []
    assert "secret thing" not in capsys.readouterr().err


def test_claude_failing_once_is_retried(tmp_path):
    _record(tmp_path, [(31, "a")])
    slack = ListenSlack(threads={PARENT: [_reply("approve", ts=MSG)]})
    claude = Claude(RuntimeError("claude exited 1"),
                    '```json\n{"decisions": [{"n": 1, "verdict": "approve"}], "reply": "Done."}\n```')
    _answer(tmp_path, slack, ListenGh({31: {"proposed"}}), claude)
    assert slack.thread_posts() == ["Done."]
    assert _saved(tmp_path)["decisions"]["1"]["verdict"] == "approve"


def test_malformed_and_disallowed_decisions_are_dropped(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b"), (52, "c"), (60, "d"), (70, "e")])
    gh = ListenGh({31: {"approved"}, 40: {"proposed"}, 60: {"stuck"}, 70: {"rejected"}}, closed={52})
    slack = ListenSlack(threads={PARENT: [_reply("do things", ts=MSG)]})
    claude = Claude({"decisions": [
        {"n": 1, "verdict": "reject"},          # approved is final
        {"n": 3, "verdict": "approve"},         # closed
        {"n": 4, "verdict": "approve"},         # stuck
        {"n": 9, "verdict": "approve"},         # no such item
        {"n": "2", "verdict": "approve"}, {"n": True, "verdict": "approve"}, {"n": 2, "verdict": "maybe"},
        {"n": 2.0, "verdict": "approve"}, "approve 2",
        {"n": 5, "verdict": "approve", "note": 7},
    ], "reply": "Done."})

    dispatch = _answer(tmp_path, slack, gh, claude)

    assert [item["state"] for item in claude.prompt()["items"]] == ["approved", "open", "closed", "stuck",
                                                                    "rejected"]
    assert _saved(tmp_path)["decisions"] == {"5": {"verdict": "approve", "note": "", "ts": MSG, "by": APPROVER}}
    assert dispatch.sent == ["improve-jensen-approvals"]
    assert slack.thread_posts() == ["Done."]


def test_a_decision_not_yet_applied_counts_as_the_items_state(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b")], handled=[MSG],
            decisions={"1": {"verdict": "approve", "note": "", "ts": MSG, "by": APPROVER},
                       "2": {"verdict": "reject", "note": "", "ts": MSG, "by": APPROVER}})
    slack = ListenSlack(threads={PARENT: [_reply("approve 1", ts=MSG),
                                          _reply("actually drop 1, and take 2", ts="1700000002.000000")]})
    claude = Claude({"decisions": [{"n": 1, "verdict": "reject"}, {"n": 2, "verdict": "approve"}], "reply": "ok"})

    _answer(tmp_path, slack, ListenGh({31: {"proposed"}, 40: {"proposed"}}), claude)

    assert [item["state"] for item in claude.prompt()["items"]] == ["approved", "rejected"]
    assert claude.prompt()["thread"] == [{"from": "first", "text": "approve 1"}]
    decisions = _saved(tmp_path)["decisions"]
    assert decisions["1"]["verdict"] == "approve" and decisions["2"]["verdict"] == "approve"
    assert _saved(tmp_path)["handled"] == [MSG, "1700000002.000000"]


def test_old_and_released_proposals_are_not_read(tmp_path):
    _record(tmp_path, [(31, "a")], posted="2026-11-01T09:00:00Z")
    _record(tmp_path, [(40, "b")], ts="1700000500.000100", released=True)
    slack = ListenSlack(threads={PARENT: [_reply("approve", ts=MSG)]})
    _answer(tmp_path, slack, ListenGh({31: {"proposed"}, 40: {"proposed"}}), Claude())
    assert slack.reads == []


def test_slack_and_gh_failures_are_logged_and_the_message_waits(tmp_path, capsys):
    _record(tmp_path, [(31, "a")])
    slack = ListenSlack(react_error="ratelimited", threads={PARENT: [_reply("approve please", ts=MSG)]})
    claude = Claude()
    _answer(tmp_path, slack, ListenGh({31: {"proposed"}}, fail=["issue list"]), claude)
    assert claude.asked == [] and "handled" not in _saved(tmp_path)
    err = capsys.readouterr().err
    assert "ratelimited" in err and "RuntimeError" in err and "please" not in err


# -- the loop --


class LoopHost:
    """A host whose `git rev-parse HEAD` answers each of `heads` in turn and whose `jensen_gh` records the
    dispatches it sends."""

    def __init__(self, live: Path, heads):
        self.live = live
        self.heads = list(heads)
        self.gh: list[list[str]] = []

    def run(self, args, **kwargs):
        assert args == ["git", "rev-parse", "HEAD"] and kwargs["cwd"] == self.live
        return subprocess.CompletedProcess(args, 0, self.heads.pop(0) + "\n", "")

    def jensen_gh(self, args, **kwargs):
        self.gh.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")


def test_the_loop_exits_when_the_checkout_moves(tmp_path, capsys):
    live = tmp_path / "live"
    (live / "data" / "secrets").mkdir(parents=True)
    host = LoopHost(live, ["a" * 40, "a" * 40, "a" * 40, "b" * 40])
    times = iter([T0, T0.replace(minute=10, second=5), T0.replace(minute=10, second=20)])
    slept = []

    rc = listen.run(host, tmp_path / "state", REPO, ask=Claude(), clock=lambda: next(times), sleep=slept.append)

    assert rc == 0 and slept == [15, 15, 15]
    assert host.gh == [["api", "-X", "POST", f"repos/{REPO}/dispatches", "-f", "event_type=improve-jensen-approvals"]]
    assert "no_token" in capsys.readouterr().err   # Slack without a token is logged, and the clock goes on


def test_cli_listen_runs_the_loop_on_the_default_repo(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setattr(listen, "run", lambda host, root, repo: seen.update(root=root, repo=repo) or 0)
    state = cli.State(tmp_path / "state")
    assert cli.main(["listen"], host=object(), state=state) == 0
    assert seen == {"root": state.root, "repo": "pragnition/autonomous-agent"}


# -- Claude through the CLI --


def test_claude_runs_the_cli_through_the_proxy_with_no_tools(monkeypatch):
    calls = []

    class Host:
        def run(self, args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, json.dumps({"type": "result", "result": "hi"}), "")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-matter")
    assert listen.claude(Host())("the system", "the prompt") == "hi"
    args, kwargs = calls[0]
    assert args == [str(Path.home() / ".local" / "bin" / "claude"), "-p", "--output-format", "json",
                    "--model", "claude-sonnet-5", "--system-prompt", "the system", "--tools", "",
                    "--max-turns", "1"]
    assert kwargs["input"] == "the prompt" and kwargs["timeout"] == 90
    assert kwargs["env"]["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:18092"
    assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-container-via-host-proxy"


@pytest.mark.parametrize("returncode, stdout", [(1, ""), (0, "not json"), (0, json.dumps({"is_error": True,
                                                                                            "result": "x"}))])
def test_claude_failures_raise(returncode, stdout):
    class Host:
        def run(self, args, **kwargs):
            return subprocess.CompletedProcess(args, returncode, stdout, "")

    with pytest.raises(Exception):
        listen.claude(Host())("s", "p")
