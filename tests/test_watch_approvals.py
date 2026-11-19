import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent.watch import approvals, cli
from agent.watch.approvals import fired, parse_decision, poll, post_proposals, ready
from agent.watch.slack import SlackError
from agent.watch.state import State

T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)
REPO = "acme/widgets"
APPROVER, OTHER = "U00000001", "U00000009"
PARENT = "1700000000.000100"


class FakeGh:
    """The plain `gh`, answering from an in-memory repo: `labels` holds every issue's labels, `closed` the closed
    ones. Removing a label an issue doesn't have raises, as a stand-in for "that must never fail a run"; any call
    whose first two words are in `fail` raises too."""

    def __init__(self, labels: dict[int, set[str]] | None = None, closed=(), runs=None, jobs=None, fail=()):
        self.labels = {n: set(ls) for n, ls in (labels or {}).items()}
        self.closed = set(closed)
        self.runs = runs or []
        self.jobs = jobs or {}
        self.fail = {tuple(f.split()) for f in fail}
        self.calls: list[list[str]] = []
        self.comments: list[tuple[int, str]] = []

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if tuple(args[:2]) in self.fail:
            raise RuntimeError("gh failed (exit 1)")
        if args[:2] == ["issue", "list"]:
            assert args[2:] == ["--state", "open", "--limit", "1000", "--json", "number,labels"]
            return json.dumps([{"number": n, "labels": [{"name": name} for name in sorted(ls)]}
                               for n, ls in sorted(self.labels.items()) if n not in self.closed])
        if args[:2] == ["issue", "edit"]:
            labels = self.labels[int(args[2])]
            for flag, value in zip(args[3::2], args[4::2]):
                if flag == "--add-label":
                    labels.add(value)
                else:
                    assert flag == "--remove-label"
                    if value not in labels:
                        raise RuntimeError("removing a label the issue lacks")
                    labels.discard(value)
            return ""
        if args[:2] == ["issue", "comment"]:
            assert args[3] == "--body"
            self.comments.append((int(args[2]), args[4]))
            return ""
        if args[:2] == ["issue", "view"]:
            assert args[3:] == ["--json", "state,labels"]
            n = int(args[2])
            return json.dumps({"state": "CLOSED" if n in self.closed else "OPEN",
                               "labels": [{"name": name} for name in sorted(self.labels.get(n, ()))]})
        if args[:2] == ["run", "list"]:
            assert args[2:5] == ["--workflow", "improve-jensen.yml", "--status"]
            assert args[6:] == ["--json", "databaseId,status", "--limit", "100"]
            return json.dumps([run for run in self.runs if run["status"] == args[5]])
        if args[:2] == ["run", "view"]:
            assert args[3:] == ["--json", "jobs"]
            return json.dumps({"jobs": self.jobs[int(args[2])]})
        raise AssertionError(f"unexpected gh call: {args}")

    def edits(self) -> list[list[str]]:
        return [call for call in self.calls if call[:2] == ["issue", "edit"]]


class FakeSlack:
    """Slack with canned threads (`threads[ts]` replies, `reacts[ts]` reactions); posts are recorded and get
    increasing timestamps. Reading a thread in `broken` raises `SlackError("ratelimited")`."""

    def __init__(self, threads=None, reacts=None, broken=(), post_error=None, post_errors=()):
        self.threads = threads or {}
        self.reacts = reacts or {}
        self.broken = set(broken)
        self.post_error = post_error
        self.post_errors = list(post_errors)   # raised by the next posts, one each, before `post_error`
        self.posts: list[tuple[str, str, str | None]] = []
        self.reads: list[str] = []
        self.channels: list[tuple[str, Path | None]] = []
        self._next = 100

    def channel_id(self, name, cache=None):
        self.channels.append((name, cache))
        return "C9"

    def post(self, channel, text, thread_ts=None):
        if self.post_errors:
            raise SlackError(self.post_errors.pop(0))
        if self.post_error:
            raise SlackError(self.post_error)
        self.posts.append((channel, text, thread_ts))
        self._next += 100
        return f"1700000000.{self._next:06d}"

    def replies(self, channel, ts):
        self.reads.append(ts)
        if ts in self.broken:
            raise SlackError("ratelimited")
        return list(self.threads.get(ts, []))

    def reactions(self, channel, ts):
        if ts in self.broken:
            raise SlackError("ratelimited")
        return list(self.reacts.get(ts, []))

    def thread_posts(self, ts=PARENT) -> list[str]:
        return [text for _, text, thread in self.posts if thread == ts]


def _record(root: Path, items: list[tuple[int, str]], ts=PARENT, notified=(), posted="2026-11-19T09:00:00Z",
            **extra) -> Path:
    folder = root / "proposals"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{ts}.json"
    path.write_text(json.dumps({"channel": "C9", "ts": ts, "posted": posted,
                                "items": [{"n": i, "issue": issue, "change": change}
                                          for i, (issue, change) in enumerate(items, 1)],
                                "notified": list(notified), **extra}))
    return path


def _reply(text, user=APPROVER, ts="1700000001.000000", **extra):
    return {"type": "message", "user": user, "text": text, "ts": ts, **extra}


# -- post_proposals --


def test_post_proposals_posts_one_numbered_message_and_labels_each_issue(tmp_path):
    gh = FakeGh({31: set(), 40: {"jensen-assessment"}})
    slack = FakeSlack()
    items = [{"issue": 31, "change": "Retry flaky tool calls"}, {"issue": 40, "change": "Shorter replies to <b> & co"}]

    assert post_proposals(slack, gh, tmp_path, "C9", items, REPO, T0) == 2

    assert slack.posts == [("C9", "*improve-jensen wants to change Jensen*\n"
                                  "1. Retry flaky tool calls (<https://github.com/acme/widgets/issues/31|#31>)\n"
                                  "2. Shorter replies to &lt;b&gt; &amp; co "
                                  "(<https://github.com/acme/widgets/issues/40|#40>)\n"
                                  "Reply here: `approve` ships them all, `approve 1 3` ships some, "
                                  "`reject 2` drops one.", None)]
    record = json.loads((tmp_path / "proposals" / "1700000000.000200.json").read_text())
    assert record == {"channel": "C9", "ts": "1700000000.000200", "posted": "2026-11-19T10:00:00Z",
                      "items": [{"n": 1, "issue": 31, "change": "Retry flaky tool calls"},
                                {"n": 2, "issue": 40, "change": "Shorter replies to <b> & co"}],
                      "notified": []}
    assert gh.edits() == [["issue", "edit", "31", "--add-label", "proposed"],
                          ["issue", "edit", "40", "--add-label", "proposed"]]
    assert gh.comments == [(31, "Proposed in Slack: Retry flaky tool calls"),
                           (40, "Proposed in Slack: Shorter replies to <b> & co")]
    assert [p.name for p in (tmp_path / "proposals").iterdir()] == ["1700000000.000200.json"]


def test_post_proposals_marks_learned_items_and_credits_them_in_slack_only(tmp_path):
    gh = FakeGh({31: set(), 40: {"learned"}, 52: {"learned"}})
    slack = FakeSlack()
    items = [{"issue": 31, "change": "Retry flaky tool calls"},
             {"issue": 40, "change": "Make the <x> skill a default", "kind": "learned", "credit": "Some <Person> & co"},
             {"issue": 52, "change": "Plan before a long build", "kind": "learned"}]

    assert post_proposals(slack, gh, tmp_path, "C9", items, REPO, T0) == 3

    assert slack.posts[0][1].splitlines()[1:4] == [
        "1. Retry flaky tool calls (<https://github.com/acme/widgets/issues/31|#31>)",
        "2. Learned: Make the &lt;x&gt; skill a default (from Some &lt;Person&gt; &amp; co) "
        "(<https://github.com/acme/widgets/issues/40|#40>)",
        "3. Learned: Plan before a long build (<https://github.com/acme/widgets/issues/52|#52>)",
    ]
    assert gh.comments == [(31, "Proposed in Slack: Retry flaky tool calls"),
                           (40, "Proposed in Slack: Make the <x> skill a default"),
                           (52, "Proposed in Slack: Plan before a long build")]
    assert not any("Person" in " ".join(call) for call in gh.calls)
    record = json.loads((tmp_path / "proposals" / "1700000000.000200.json").read_text())
    assert record["items"] == [
        {"n": 1, "issue": 31, "change": "Retry flaky tool calls"},
        {"n": 2, "issue": 40, "change": "Make the <x> skill a default", "kind": "learned",
         "credit": "Some <Person> & co"},
        {"n": 3, "issue": 52, "change": "Plan before a long build", "kind": "learned"}]


def test_a_credit_on_an_item_that_isnt_learned_is_not_shown(tmp_path):
    slack = FakeSlack()
    post_proposals(slack, FakeGh({31: set()}), tmp_path, "C9", [{"issue": 31, "change": "a", "credit": "Someone"}],
                   REPO, T0)
    assert slack.posts[0][1].splitlines()[1] == "1. a (<https://github.com/acme/widgets/issues/31|#31>)"


@pytest.mark.parametrize("label", ["proposed", "approved", "rejected", "fixing", "stuck", "needs-person"])
def test_post_proposals_drops_issues_already_in_the_flow(tmp_path, label):
    gh = FakeGh({31: {label}, 40: {"project"}})
    slack = FakeSlack()
    items = [{"issue": 31, "change": "first"}, {"issue": 40, "change": "second"}]

    assert post_proposals(slack, gh, tmp_path, "C9", items, REPO, T0) == 1
    assert slack.posts[0][1].splitlines()[1] == "1. second (<https://github.com/acme/widgets/issues/40|#40>)"
    assert gh.edits() == [["issue", "edit", "40", "--add-label", "proposed"]]


def test_post_proposals_with_nothing_left_posts_nothing(tmp_path):
    gh = FakeGh({31: {"stuck"}, 40: set()}, closed={40})
    slack = FakeSlack()

    assert post_proposals(slack, gh, tmp_path, "C9", [{"issue": 31, "change": "a"}, {"issue": 40, "change": "b"},
                                                     {"issue": 77, "change": "no such issue"}], REPO, T0) == 0
    assert slack.posts == []
    assert gh.calls == [["issue", "list", "--state", "open", "--limit", "1000", "--json", "number,labels"]]
    assert not (tmp_path / "proposals").exists()


def test_post_proposals_slack_failure_labels_nothing(tmp_path):
    gh = FakeGh({31: set()})
    with pytest.raises(SlackError):
        post_proposals(FakeSlack(post_error="not_in_channel"), gh, tmp_path, "C9", [{"issue": 31, "change": "a"}],
                       REPO, T0)
    assert gh.edits() == [] and gh.comments == []


# -- parse_decision --


@pytest.mark.parametrize("text, decision", [
    ("approve", ("approve", None)),
    ("Approve 1, 3", ("approve", {1, 3})),
    ("approve 1-3", ("approve", {1, 2, 3})),
    ("approve 1 3", ("approve", {1, 3})),
    ("APPROVED #2", ("approve", {2})),
    ("<@U00000002> yes", ("approve", None)),
    ("<@U00000002|bot>, ok 2", ("approve", {2})),
    ("okay", ("approve", None)),
    ("lgtm all", ("approve", None)),
    ("approve all.", ("approve", None)),
    ("ship it", ("approve", None)),
    ("Ship it!", ("approve", None)),
    ("yes.", ("approve", None)),
    ("go", ("approve", None)),
    ("approve 1 and 3", ("approve", {1, 3})),
    ("approve 1 & 3", ("approve", {1, 3})),
    ("approve 1 &amp; 3", ("approve", {1, 3})),   # how Slack sends `&`
    ("approve #1, #3.", ("approve", {1, 3})),
    ("approve 1,3", ("approve", {1, 3})),
    ("approve 1–3", ("approve", {1, 2, 3})),
    ("approve 1 — 3", ("approve", {1, 2, 3})),
    ("approve 1 to 3!", ("approve", {1, 2, 3})),
    ("ok, 2", ("approve", {2})),
    ("reject 2", ("reject", {2})),
    ("no", ("reject", None)),
    ("skip 3", ("reject", {3})),
    ("drop 1, 2", ("reject", {1, 2})),
    ("approve 1, 3; reject 2", None),
    ("ok, I will check #2 tomorrow", None),
    ("ship it after 2pm", None),
    ("Ship it! 1", None),
    ("go ahead with 1 and 3", None),
    ("nope, not now", None),
    ("don't ship 2", None),
    ("approve: 1", None),
    ("approve 1 and all", None),
    ("approve :tada:", None),
    ("approve 1 3 please", None),
    ("don’t", ("reject", None)),
    ("dont", ("reject", None)),
    ("rejected 3-1", ("reject", {1, 2, 3})),
    ("why 2?", None),
    ("nothing", None),
    ("good idea", None),
    ("going to look later", None),
    ("okay so what does 2 do?", None),
    ("approve all but 2", None),
    ("approve 1, 3 except 2", None),
    ("approves", None),
    ("", None),
    ("<@U00000002>", None),
    ("I approve", None),
])
def test_parse_decision(text, decision):
    assert parse_decision(text) == decision


# -- poll: decisions --


def test_poll_an_approvers_approve_ships_every_item(tmp_path):
    _record(tmp_path, [(31, "Retry flaky tool calls"), (40, "Shorter replies")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed", "jensen-assessment"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve")]})

    out = poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)

    assert gh.labels == {31: {"approved"}, 40: {"approved", "jensen-assessment"}}
    assert ["issue", "edit", "31", "--add-label", "approved", "--remove-label", "proposed"] in gh.edits()
    assert gh.comments == [(31, "Approved in Slack."), (40, "Approved in Slack.")]
    assert slack.thread_posts() == ["Approved 1, 2: queued to ship."]
    assert out == {"fix": 31, "project": None}


def test_poll_releases_a_proposal_nobody_decided_within_14_days(tmp_path):
    _record(tmp_path, [(31, "Retry flaky tool calls"), (40, "Shorter replies"), (41, "Fewer clones")])
    gh = FakeGh({31: {"proposed"}, 40: {"rejected"}, 41: {"approved"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve")]})

    out = poll(slack, gh, tmp_path, {APPROVER}, REPO, T0 + timedelta(days=14, hours=2))

    assert slack.reads == [] and slack.posts == []  # an old thread is never read again
    assert gh.labels == {31: set(), 40: {"rejected"}, 41: {"approved"}}
    assert gh.comments == [(31, "No decision in Slack within 14 days; the watch can propose it again.")]
    assert out == {"fix": 41, "project": None}
    assert json.loads((tmp_path / "proposals" / f"{PARENT}.json").read_text())["released"] is True


def test_poll_a_released_proposal_is_never_released_again(tmp_path):
    path = _record(tmp_path, [(31, "Retry flaky tool calls")])
    gh = FakeGh({31: {"proposed"}})
    later = T0 + timedelta(days=15)
    poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, later)
    assert gh.labels == {31: set()} and json.loads(path.read_text())["released"] is True

    # The watch proposes it again: the old proposal stays out of it.
    _record(tmp_path, [(31, "Retry flaky tool calls")], ts="1701000000.000100", posted="2026-12-04T09:00:00Z")
    gh.labels[31].add("proposed")
    edits = len(gh.edits())
    poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, later)
    assert gh.labels == {31: {"proposed"}} and len(gh.edits()) == edits


def test_poll_an_expired_proposal_never_touches_a_newer_proposal_of_the_same_issue(tmp_path):
    old = _record(tmp_path, [(31, "old wording"), (40, "b")], ts="1600000000.000100", posted="2026-10-01T09:00:00Z")
    _record(tmp_path, [(31, "new wording")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve")]})

    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)

    assert gh.labels == {31: {"approved"}, 40: set()}
    assert ["issue", "edit", "31", "--remove-label", "proposed"] not in gh.edits()
    assert json.loads(old.read_text())["released"] is True
    assert slack.reads == [PARENT]


def test_poll_skips_a_released_proposal(tmp_path):
    _record(tmp_path, [(31, "a")], released=True)
    _record(tmp_path, [(31, "a again")], ts="1700000500.000100")
    gh = FakeGh({31: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve")]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert slack.reads == ["1700000500.000100"]
    assert gh.labels == {31: {"proposed"}}


def test_poll_retries_a_release_that_failed(tmp_path):
    path = _record(tmp_path, [(31, "a")], posted="2026-10-01T09:00:00Z")
    gh = FakeGh({31: {"proposed"}}, fail=["issue edit"])
    poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, T0)
    assert "released" not in json.loads(path.read_text())

    gh.fail = set()
    poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: set()}
    assert json.loads(path.read_text())["released"] is True


def test_poll_releases_a_proposed_issue_no_live_proposal_names(tmp_path):
    # The state folder lost its record, the file is broken, or only a released proposal names it: nothing could
    # ever decide the issue.
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals" / "1700000300.000100.json").write_text("{not json")
    _record(tmp_path, [(40, "b")])
    _record(tmp_path, [(52, "c")], ts="1600000000.000100", released=True)
    gh = FakeGh({31: {"proposed", "jensen-assessment"}, 40: {"proposed"}, 52: {"proposed"}, 60: {"rejected"}})

    poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, T0)

    assert gh.labels == {31: {"jensen-assessment"}, 40: {"proposed"}, 52: set(), 60: {"rejected"}}
    assert gh.comments == [(31, "No decision in Slack within 14 days; the watch can propose it again."),
                           (52, "No decision in Slack within 14 days; the watch can propose it again.")]


def test_poll_still_reads_a_proposal_just_under_14_days_old(tmp_path):
    _record(tmp_path, [(31, "Retry flaky tool calls")])
    gh = FakeGh({31: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve")]})

    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0 + timedelta(days=13, hours=23))

    assert gh.labels == {31: {"approved"}}


def test_poll_ignores_non_approvers_bots_and_system_messages(tmp_path):
    _record(tmp_path, [(31, "a")])
    gh = FakeGh({31: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [
        _reply("approve", user=OTHER),
        _reply("approve", bot_id="B1"),
        _reply("approve", subtype="channel_join"),
        _reply("why 1?"),
    ]})

    assert poll(slack, gh, tmp_path, {APPROVER}, REPO, T0) == {"fix": None, "project": None}
    assert gh.labels == {31: {"proposed"}}
    assert gh.edits() == [] and gh.comments == [] and slack.posts == []


def test_poll_counts_an_approvers_reply_also_sent_to_the_channel(tmp_path):
    _record(tmp_path, [(31, "a")])
    gh = FakeGh({31: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve", subtype="thread_broadcast")]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"approved"}}


def test_poll_with_no_approvers_approves_nothing(tmp_path):
    _record(tmp_path, [(31, "a")])
    gh = FakeGh({31: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve")]},
                      reacts={PARENT: [{"name": "white_check_mark", "users": [APPROVER]}]})
    poll(slack, gh, tmp_path, set(), REPO, T0)
    assert gh.labels == {31: {"proposed"}}


@pytest.mark.parametrize("name", ["white_check_mark", "ballot_box_with_check"])
def test_poll_an_approvers_check_reaction_approves_all(tmp_path, name):
    _record(tmp_path, [(31, "a"), (40, "b")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}})
    slack = FakeSlack(reacts={PARENT: [{"name": name, "users": [OTHER, APPROVER]}]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"approved"}, 40: {"approved"}}


@pytest.mark.parametrize("name", ["+1", "+1::skin-tone-3", "heavy_check_mark", "eyes"])
def test_poll_a_thumbs_up_or_other_reaction_approves_nothing(tmp_path, name):
    # A thumbs-up often just means "seen".
    _record(tmp_path, [(31, "a")])
    gh = FakeGh({31: {"proposed"}})
    poll(FakeSlack(reacts={PARENT: [{"name": name, "users": [APPROVER]}]}), gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"proposed"}}


def test_poll_a_check_reaction_never_flips_a_rejected_item(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b")])
    gh = FakeGh({31: {"proposed"}, 40: {"rejected"}})
    slack = FakeSlack(reacts={PARENT: [{"name": "white_check_mark", "users": [APPROVER]}]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"approved"}, 40: {"rejected"}}
    assert slack.thread_posts() == ["Approved 1: queued to ship."]


def test_poll_a_bare_approve_never_flips_a_rejected_item(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b")])
    gh = FakeGh({31: {"proposed"}, 40: {"rejected"}})
    poll(FakeSlack(threads={PARENT: [_reply("approve all")]}), gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"approved"}, 40: {"rejected"}}


def test_poll_a_bare_decision_only_touches_items_still_proposed(tmp_path):
    # In the thread's own order: once an item is decided by number, a bare word no longer reaches it.
    _record(tmp_path, [(31, "a"), (40, "b"), (52, "c")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}, 52: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("reject 2", ts="1700000001.000000"),
                                        _reply("approve 3", ts="1700000002.000000"),
                                        _reply("approve", ts="1700000003.000000"),
                                        _reply("no", ts="1700000004.000000")]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"approved"}, 40: {"rejected"}, 52: {"approved"}}


@pytest.mark.parametrize("name", ["x", "-1", "no_entry", "negative_squared_cross_mark"])
def test_poll_an_approvers_cross_reaction_rejects_all(tmp_path, name):
    _record(tmp_path, [(31, "a"), (40, "b")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}})
    slack = FakeSlack(reacts={PARENT: [{"name": name, "users": [APPROVER]}]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"rejected"}, 40: {"rejected"}}
    assert gh.comments == [(31, "Not approved in Slack."), (40, "Not approved in Slack.")]
    assert slack.thread_posts() == ["Dropped 1, 2."]


def test_poll_ignores_a_non_approvers_reaction(tmp_path):
    _record(tmp_path, [(31, "a")])
    gh = FakeGh({31: {"proposed"}})
    slack = FakeSlack(reacts={PARENT: [{"name": "white_check_mark", "users": [OTHER]}]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"proposed"}}


def test_poll_a_later_reply_overrides_a_reaction(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b"), (52, "c")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}, 52: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("reject 2")]},
                      reacts={PARENT: [{"name": "white_check_mark", "users": [APPROVER]}]})

    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)

    assert gh.labels == {31: {"approved"}, 40: {"rejected"}, 52: {"approved"}}
    assert slack.thread_posts() == ["Approved 1, 3: queued to ship. Dropped 2."]


def test_poll_replies_apply_in_ts_order(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve 2", ts="1700000002.000000"),
                                        _reply("no", ts="1700000001.000000")]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"rejected"}, 40: {"approved"}}


def test_poll_approved_is_final(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b"), (52, "c")])
    gh = FakeGh({31: {"approved"}, 40: {"proposed"}, 52: {"proposed"}}, closed={52})
    slack = FakeSlack(threads={PARENT: [_reply("approve 1"), _reply("reject 1-3", ts="1700000002.000000")]})

    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)

    assert gh.labels == {31: {"approved"}, 40: {"rejected"}, 52: {"proposed"}}
    assert slack.thread_posts() == ["Dropped 2."]


def test_poll_rejected_can_later_be_approved_and_repeats_nothing(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve 1"), _reply("reject 2", ts="1700000002.000000")]})

    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"approved"}, 40: {"rejected"}}
    assert slack.thread_posts() == ["Approved 1: queued to ship. Dropped 2."]

    # Nothing new: no label, comment or thread reply.
    calls, posts = len(gh.calls), len(slack.posts)
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert [c for c in gh.calls[calls:] if c[:2] != ["issue", "list"] and c[0] != "run"] == []
    assert len(slack.posts) == posts

    slack.threads[PARENT].append(_reply("ok 2", ts="1700000003.000000"))
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"approved"}, 40: {"approved"}}
    assert gh.edits()[-1] == ["issue", "edit", "40", "--add-label", "approved", "--remove-label", "rejected"]
    assert slack.thread_posts()[-1] == "Approved 2: queued to ship."


def test_poll_numbers_outside_the_proposal_change_nothing(tmp_path):
    _record(tmp_path, [(31, "a")])
    gh = FakeGh({31: {"proposed"}})
    poll(FakeSlack(threads={PARENT: [_reply("approve 4")]}), gh, tmp_path, {APPROVER}, REPO, T0)
    assert gh.labels == {31: {"proposed"}}


def test_poll_skips_records_with_nothing_left_to_decide(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "b")])
    gh = FakeGh({31: {"approved"}, 40: {"stuck"}})
    slack = FakeSlack(threads={PARENT: [_reply("reject")]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)
    assert slack.reads == [] and slack.posts == []


def test_poll_a_slack_error_on_one_record_leaves_the_others_going(tmp_path, capsys):
    other = "1700000500.000100"
    _record(tmp_path, [(31, "a")])
    _record(tmp_path, [(40, "b")], ts=other)
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}})
    slack = FakeSlack(threads={other: [_reply("approve")]}, broken={PARENT})

    out = poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)

    assert gh.labels == {31: {"proposed"}, 40: {"approved"}}
    assert out["fix"] == 40
    err = capsys.readouterr().err
    assert "ratelimited" in err


def test_poll_reactions_that_cannot_be_read_leave_the_replies_deciding(tmp_path, capsys):
    # Reactions only decide what no reply does, so going without them never approves anything wrongly.
    _record(tmp_path, [(31, "a"), (40, "b")])
    gh = FakeGh({31: {"proposed"}, 40: {"proposed"}})

    class NoReactions(FakeSlack):
        def reactions(self, channel, ts):
            raise SlackError("missing_scope")

    slack = NoReactions(threads={PARENT: [_reply("approve 2")]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)

    assert gh.labels == {31: {"proposed"}, 40: {"approved"}}
    assert "missing_scope" in capsys.readouterr().err


def test_poll_a_gh_failure_on_one_item_leaves_the_others_going(tmp_path, capsys):
    _record(tmp_path, [(31, "a"), (40, "b")])

    class Flaky(FakeGh):
        def __call__(self, args):
            if args[:3] == ["issue", "edit", "31"]:
                self.calls.append(list(args))
                raise RuntimeError("gh issue edit failed (exit 1)")
            return super().__call__(args)

    gh = Flaky({31: {"proposed"}, 40: {"proposed"}})
    slack = FakeSlack(threads={PARENT: [_reply("approve")]})
    poll(slack, gh, tmp_path, {APPROVER}, REPO, T0)

    assert gh.labels == {31: {"proposed"}, 40: {"approved"}}
    assert slack.thread_posts() == ["Approved 2: queued to ship."]
    assert "RuntimeError" in capsys.readouterr().err


def test_poll_a_failed_issue_list_raises(tmp_path):
    _record(tmp_path, [(31, "a")])
    with pytest.raises(RuntimeError):
        poll(FakeSlack(), FakeGh({31: {"proposed"}}, fail=["issue list"]), tmp_path, {APPROVER}, REPO, T0)


def test_poll_with_no_records_reads_no_slack(tmp_path):
    gh = FakeGh({31: {"approved"}})
    slack = FakeSlack()
    assert poll(slack, gh, tmp_path, {APPROVER}, REPO, T0) == {"fix": 31, "project": None}
    assert slack.reads == [] and slack.posts == []


def test_poll_ignores_broken_records(tmp_path):
    (tmp_path / "proposals").mkdir()
    (tmp_path / "proposals" / "1.000000.json").write_text("{not json")
    (tmp_path / "proposals" / "2.000000.json").write_text(json.dumps({"items": "nope"}))
    assert poll(FakeSlack(), FakeGh({}), tmp_path, {APPROVER}, REPO, T0) == {"fix": None, "project": None}


# -- poll: what to fire --


def test_poll_picks_the_lowest_eligible_fix_and_project(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "500")
    gh = FakeGh({
        12: {"approved", "fixing"}, 15: {"approved", "stuck"}, 18: {"approved", "needs-person"},
        20: {"approved", "project", "stuck"}, 22: {"approved", "project", "needs-person"},
        24: {"approved", "project", "fixing"}, 25: {"approved"}, 26: {"approved", "project"},
        30: {"approved"}, 7: {"proposed"}, 8: {"rejected"}, 9: set(),
    }, runs=[{"databaseId": 500, "status": "in_progress"}, {"databaseId": 499, "status": "completed"}])

    assert poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, T0) == {"fix": 25, "project": 24}
    # Neither our own run nor a completed one is looked into.
    assert [c for c in gh.calls if c[:2] == ["run", "view"]] == []


@pytest.mark.parametrize("job, out", [
    ({"name": "fix", "status": "queued"}, {"fix": None, "project": 26}),
    ({"name": "fix", "status": "in_progress"}, {"fix": None, "project": 26}),
    ({"name": "project", "status": "in_progress"}, {"fix": 25, "project": None}),
    ({"name": "fix", "status": "completed"}, {"fix": 25, "project": 26}),
    ({"name": "fix-up", "status": "in_progress"}, {"fix": 25, "project": 26}),
    ({"name": "watch", "status": "in_progress"}, {"fix": 25, "project": 26}),
])
def test_poll_waits_for_a_fix_or_project_job_in_another_run(tmp_path, monkeypatch, job, out):
    monkeypatch.setenv("GITHUB_RUN_ID", "500")
    gh = FakeGh({25: {"approved"}, 26: {"approved", "project"}},
                runs=[{"databaseId": 480, "status": "in_progress"}, {"databaseId": 500, "status": "in_progress"}],
                jobs={480: [{"name": "approvals", "status": "completed"}, job]})
    assert poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, T0) == out
    assert [c for c in gh.calls if c[:2] == ["run", "view"]] == [["run", "view", "480", "--json", "jobs"]]


@pytest.mark.parametrize("fail", ["run list", "run view"])
def test_poll_fires_nothing_when_the_running_check_fails(tmp_path, monkeypatch, fail):
    monkeypatch.setenv("GITHUB_RUN_ID", "500")
    gh = FakeGh({25: {"approved"}, 26: {"approved", "project"}}, runs=[{"databaseId": 480, "status": "queued"}],
                jobs={480: []}, fail=[fail])
    assert poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, T0) == {"fix": None, "project": None}


def test_poll_finds_unfinished_runs_by_status_however_old(tmp_path, monkeypatch):
    # A six-hour project run is long out of the newest 30 runs while it's still going.
    monkeypatch.setenv("GITHUB_RUN_ID", "500")
    runs = [{"databaseId": 900 - i, "status": "completed"} for i in range(60)]
    runs += [{"databaseId": 10, "status": "in_progress"}, {"databaseId": 11, "status": "waiting"},
             {"databaseId": 500, "status": "in_progress"}]
    gh = FakeGh({25: {"approved"}, 26: {"approved", "project"}}, runs=runs,
                jobs={10: [{"name": "project", "status": "in_progress"}],
                      11: [{"name": "fix", "status": "waiting"}]})

    assert poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, T0) == {"fix": None, "project": None}
    assert [c[5] for c in gh.calls if c[:2] == ["run", "list"]] == [
        "in_progress", "queued", "waiting", "requested"]
    assert [c[2] for c in gh.calls if c[:2] == ["run", "view"]] == ["10", "11"]


def test_poll_with_nothing_approved_never_asks_about_runs(tmp_path):
    gh = FakeGh({25: {"proposed"}})
    assert poll(FakeSlack(), gh, tmp_path, {APPROVER}, REPO, T0) == {"fix": None, "project": None}
    assert [c for c in gh.calls if c[0] == "run"] == []


def test_poll_fires_what_it_just_approved(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    _record(tmp_path, [(40, "b"), (33, "project-sized")])
    gh = FakeGh({40: {"proposed"}, 33: {"proposed", "project"}})
    out = poll(FakeSlack(threads={PARENT: [_reply("approve")]}), gh, tmp_path, {APPROVER}, REPO, T0)
    assert out == {"fix": 40, "project": 33}


# -- fired --


def test_fired_closed_issue_reports_shipped_in_its_thread(tmp_path):
    _record(tmp_path, [(31, "Retry flaky tool calls."), (40, "b")])
    gh = FakeGh({31: {"approved"}}, closed={31})
    slack = FakeSlack()

    fired(slack, gh, tmp_path, 31, "fix", REPO, T0)

    assert slack.posts == [("C9", "Shipped 1: Retry flaky tool calls.", PARENT)]
    assert gh.edits() == [] and gh.comments == []


def test_fired_open_fix_is_marked_stuck(tmp_path):
    _record(tmp_path, [(31, "a"), (40, "Shorter <replies>")])
    gh = FakeGh({40: {"approved", "jensen-assessment"}})
    slack = FakeSlack()

    fired(slack, gh, tmp_path, 40, "fix", REPO, T0)

    assert gh.labels[40] == {"stuck", "jensen-assessment"}
    assert gh.edits() == [["issue", "edit", "40", "--remove-label", "approved", "--add-label", "stuck"]]
    assert gh.comments == [(40, "The approved change didn't ship; a person needs to look.")]
    assert slack.posts == [("C9", "Couldn't ship 2: Shorter &lt;replies&gt;. It needs a person (#40).", PARENT)]


def test_fired_open_fix_without_approved_still_gets_stuck(tmp_path):
    gh = FakeGh({40: set()})
    slack = FakeSlack()
    fired(slack, gh, tmp_path, 40, "fix", REPO, T0)
    assert gh.labels[40] == {"stuck"}
    assert gh.comments == [(40, "The approved change didn't ship; a person needs to look.")]
    assert slack.posts == []   # no proposal thread to answer in


def test_fired_closed_issue_without_a_proposal_posts_nothing(tmp_path):
    slack = FakeSlack()
    fired(slack, FakeGh({31: set()}, closed={31}), tmp_path, 31, "fix", REPO, T0)
    assert slack.posts == []


def test_fired_open_project_still_going_does_nothing(tmp_path):
    _record(tmp_path, [(33, "project-sized")])
    gh = FakeGh({33: {"approved", "project"}})
    slack = FakeSlack()
    fired(slack, gh, tmp_path, 33, "project", REPO, T0)
    assert gh.edits() == [] and gh.comments == [] and slack.posts == []


@pytest.mark.parametrize("label", ["stuck", "needs-person"])
def test_fired_open_project_needing_a_person_says_so_once(tmp_path, label):
    path = _record(tmp_path, [(31, "a"), (33, "project-sized")])
    gh = FakeGh({33: {"approved", "project", label}})
    slack = FakeSlack()

    fired(slack, gh, tmp_path, 33, "project", REPO, T0)
    fired(slack, gh, tmp_path, 33, "project", REPO, T0)

    assert slack.posts == [("C9", "2 needs a person (#33).", PARENT)]
    assert json.loads(path.read_text())["notified"] == [33]
    assert gh.edits() == [] and gh.comments == []
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]


def test_fired_a_project_failing_twice_in_a_row_needs_a_person(tmp_path):
    path = _record(tmp_path, [(31, "a"), (33, "project-sized")])
    gh = FakeGh({33: {"approved", "project"}})
    slack = FakeSlack()

    fired(slack, gh, tmp_path, 33, "project", REPO, T0, outcome="failure")
    assert gh.edits() == [] and gh.comments == [] and slack.posts == []

    fired(slack, gh, tmp_path, 33, "project", REPO, T0, outcome="cancelled")
    assert gh.labels[33] == {"project", "stuck"}
    assert gh.edits() == [["issue", "edit", "33", "--remove-label", "approved", "--add-label", "stuck"]]
    assert gh.comments == [(33, "The approved project's last 2 runs failed; a person needs to look.")]
    assert slack.posts == [("C9", "2 needs a person (#33).", PARENT)]
    assert json.loads(path.read_text())["notified"] == [33]

    # Once a person puts it back, it counts from nothing again.
    gh.labels[33] = {"approved", "project"}
    fired(slack, gh, tmp_path, 33, "project", REPO, T0, outcome="failure")
    assert gh.labels[33] == {"approved", "project"}


def test_fired_a_successful_project_run_resets_the_count(tmp_path):
    gh = FakeGh({33: {"approved", "project"}})
    for outcome in ("failure", "success", "failure"):
        fired(FakeSlack(), gh, tmp_path, 33, "project", REPO, T0, outcome=outcome)
    assert gh.labels[33] == {"approved", "project"} and gh.edits() == []


def test_fired_counts_failures_per_project(tmp_path):
    gh = FakeGh({33: {"approved", "project"}, 34: {"approved", "project"}})
    fired(FakeSlack(), gh, tmp_path, 33, "project", REPO, T0, outcome="failure")
    fired(FakeSlack(), gh, tmp_path, 34, "project", REPO, T0, outcome="failure")
    assert gh.edits() == []


def test_fired_without_an_outcome_counts_nothing(tmp_path):
    gh = FakeGh({33: {"approved", "project"}})
    for _ in range(3):
        fired(FakeSlack(), gh, tmp_path, 33, "project", REPO, T0)
    assert gh.edits() == []


def test_fired_a_fix_ignores_the_outcome(tmp_path):
    gh = FakeGh({40: {"approved"}}, closed={40})
    fired(FakeSlack(), gh, tmp_path, 40, "fix", REPO, T0, outcome="failure")
    assert gh.edits() == [] and not (tmp_path / "project-failures.json").exists()


def test_fired_closed_project_reports_shipped(tmp_path):
    _record(tmp_path, [(33, "project-sized")])
    slack = FakeSlack()
    fired(slack, FakeGh({33: {"approved", "project"}}, closed={33}), tmp_path, 33, "project", REPO, T0)
    assert slack.posts == [("C9", "Shipped 1: project-sized.", PARENT)]


def test_fired_without_slack_still_marks_github(tmp_path):
    _record(tmp_path, [(40, "b")])
    gh = FakeGh({40: {"approved"}})
    fired(None, gh, tmp_path, 40, "fix", REPO, T0)
    assert gh.labels[40] == {"stuck"}


def test_fired_uses_the_newest_proposal_for_the_issue(tmp_path):
    _record(tmp_path, [(40, "old wording")], ts="1600000000.000100")
    _record(tmp_path, [(31, "a"), (40, "new wording")])
    slack = FakeSlack()
    fired(slack, FakeGh({40: set()}, closed={40}), tmp_path, 40, "fix", REPO, T0)
    assert slack.posts == [("C9", "Shipped 2: new wording.", PARENT)]


# -- ready --


@pytest.mark.parametrize("labels, closed, kind, go", [
    ({"approved"}, False, "fix", True),
    ({"approved", "jensen-assessment"}, False, "fix", True),
    ({"approved"}, True, "fix", False),
    ({"proposed"}, False, "fix", False),
    ({"approved", "stuck"}, False, "fix", False),
    ({"approved", "needs-person"}, False, "fix", False),
    ({"approved", "project"}, False, "fix", False),
    ({"approved", "project"}, False, "project", True),
    ({"approved", "project", "stuck"}, False, "project", False),
    (set(), False, "project", False),
])
def test_ready_only_for_an_open_approved_issue_nobody_has_to_look_at(labels, closed, kind, go):
    gh = FakeGh({40: labels}, closed={40} if closed else ())
    assert ready(gh, 40, kind) is go


# -- CLI --


class _CliHost:
    """A host whose `run` answers `gh` from a `FakeGh`, checking each call carries `-R <repo>` last."""

    def __init__(self, live: Path, gh: FakeGh, pool="normal"):
        self.live = live
        self.gh = gh
        self.pool = pool   # core's usage-watch pool; `None` makes reading it fail
        self.kwargs: list[dict] = []

    def core_python(self, module_file, args, **kwargs):
        if self.pool is None:
            raise RuntimeError("docker exec returned 1")
        return json.dumps({"pool": self.pool})

    def run(self, args, **kwargs):
        assert args[0] == "gh" and args[-2:] == ["-R", REPO]
        self.kwargs.append(kwargs)
        try:
            stdout = self.gh(args[1:-2])
        except RuntimeError:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 401: Bad credentials /home/someone/.config")
        return subprocess.CompletedProcess(args, 0, stdout, "")


class _PlanState(State):
    """A state whose plans already carry `propose` lists, whatever shape `State.plan` has on this branch."""

    def __init__(self, root, plan=None, assess=None):
        super().__init__(root)
        self._plan, self._assess = plan or {}, assess or {}

    def plan(self, run_id=None):
        return self._plan

    def assess_plan(self):
        return self._assess


def _live(tmp_path, token=True, people=True) -> Path:
    live = tmp_path / "live"
    (live / "data" / "secrets").mkdir(parents=True)
    (live / "data" / "config").mkdir(parents=True)
    if token:
        (live / "data" / "secrets" / "agent.env").write_text("SLACK_BOT_TOKEN=xoxb-test-secret\n")
    if people:
        (live / "data" / "config" / "agent.toml").write_text(f'[people.first]\nslack = "{APPROVER}"\n')
    return live


@pytest.fixture
def fake_slack(monkeypatch):
    """Every `Slack(token)` the CLI builds is this one fake; `tokens` records what it was built with."""
    slack = FakeSlack()
    slack.tokens = []

    def build(token):
        slack.tokens.append(token)
        return slack

    monkeypatch.setattr(cli, "Slack", build)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.delenv("IMPROVE_JENSEN_SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    return slack


def _kv(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if line)


def test_cli_propose_from_watch_posts_and_prints_the_count(tmp_path, fake_slack, capsys):
    gh = FakeGh({31: set(), 40: {"stuck"}})
    host = _CliHost(_live(tmp_path), gh)
    state = _PlanState(tmp_path / "state", plan={"reviews": [], "propose": [
        {"issue": 31, "change": "Retry flaky tool calls"}, {"issue": 40, "change": "b"}, {"issue": "x"}]})

    assert cli.main(["propose", "--from", "watch"], host=host, state=state, now=T0) == 0

    assert capsys.readouterr().out == "proposed=1\n"
    assert fake_slack.tokens == ["xoxb-test-secret"]
    assert fake_slack.channels == [("autonomous-agent", state.root / "slack-channel.json")]
    assert fake_slack.posts[0][0] == "C9"
    assert (state.root / "proposals" / "1700000000.000200.json").exists()
    assert gh.labels[31] == {"proposed"}
    assert all(kwargs == {"capture_output": True, "text": True} for kwargs in host.kwargs)


def test_cli_propose_from_assess_clears_the_assess_plan(tmp_path, fake_slack, monkeypatch, capsys):
    monkeypatch.setenv("IMPROVE_JENSEN_SLACK_CHANNEL", "#elsewhere")
    gh = FakeGh({33: {"project"}})
    state = _PlanState(tmp_path / "state", assess={"propose": [{"issue": 33, "change": "project-sized"}]})
    (state.root / "assess-plan.json").write_text("{}")

    assert cli.main(["propose", "--from", "assess"], host=_CliHost(_live(tmp_path), gh), state=state, now=T0) == 0

    assert capsys.readouterr().out == "proposed=1\n"
    assert not (state.root / "assess-plan.json").exists()
    assert fake_slack.channels[0][0] == "#elsewhere"
    assert gh.labels[33] == {"project", "proposed"}


def test_cli_propose_reads_a_plan_without_proposals_as_none(tmp_path, fake_slack, capsys):
    state = State(tmp_path / "state")
    (state.root / "plan.json").write_text(json.dumps({"reviews": [], "fix": 3}))
    gh = FakeGh({3: set()})
    assert cli.main(["propose", "--from", "watch"], host=_CliHost(_live(tmp_path), gh), state=state, now=T0) == 0
    assert capsys.readouterr().out == "proposed=0\n"
    assert fake_slack.posts == [] and gh.calls == []


def test_cli_propose_slack_error_is_one_line_and_exit_1(tmp_path, fake_slack, capsys):
    fake_slack.post_error = "not_in_channel"
    state = _PlanState(tmp_path / "state", plan={"propose": [{"issue": 31, "change": "a"}]})
    rc = cli.main(["propose", "--from", "watch"], host=_CliHost(_live(tmp_path), FakeGh({31: set()})), state=state,
                  now=T0)
    assert rc == 1
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "not_in_channel" in err


def test_cli_propose_without_a_token_exits_1_without_showing_secrets(tmp_path, fake_slack, capsys):
    state = _PlanState(tmp_path / "state", plan={"propose": [{"issue": 31, "change": "a"}]})
    rc = cli.main(["propose", "--from", "watch"], host=_CliHost(_live(tmp_path, token=False), FakeGh({31: set()})),
                  state=state, now=T0)
    assert rc == 1
    assert "no_token" in capsys.readouterr().err


def test_cli_propose_gh_failure_shows_no_gh_output(tmp_path, fake_slack, capsys):
    state = _PlanState(tmp_path / "state", plan={"propose": [{"issue": 31, "change": "a"}]})
    gh = FakeGh({31: set()}, fail=["issue list"])
    assert cli.main(["propose", "--from", "watch"], host=_CliHost(_live(tmp_path), gh), state=state, now=T0) == 1
    err = capsys.readouterr().err
    assert "exit 1" in err and "Bad credentials" not in err and "/home/someone" not in err


def test_cli_approvals_writes_github_outputs(tmp_path, fake_slack):
    state = State(tmp_path / "state")
    _record(state.root, [(25, "a"), (26, "b")])
    fake_slack.threads[PARENT] = [_reply("approve")]
    gh = FakeGh({25: {"proposed"}, 26: {"proposed", "project"}})
    output = tmp_path / "out.txt"
    output.write_text("earlier=1\n")

    rc = cli.main(["approvals", "--github-output", str(output)], host=_CliHost(_live(tmp_path), gh), state=state,
                  now=T0)

    assert rc == 0
    assert output.read_text() == "earlier=1\nfix=25\nhas_fix=true\nproject=26\nhas_project=true\n"


def test_cli_approvals_with_nothing_to_fire(tmp_path, fake_slack, capsys):
    rc = cli.main(["approvals"], host=_CliHost(_live(tmp_path), FakeGh({})), state=State(tmp_path / "state"),
                  now=T0)
    assert rc == 0
    assert _kv(capsys.readouterr().out) == {"fix": "", "has_fix": "false", "project": "", "has_project": "false"}


@pytest.mark.parametrize("token, fail", [(False, ()), (True, ["issue list"])])
def test_cli_approvals_errors_emit_empty_outputs_and_exit_0(tmp_path, fake_slack, capsys, token, fail):
    gh = FakeGh({25: {"approved"}}, fail=fail)
    output = tmp_path / "out.txt"
    rc = cli.main(["approvals", "--github-output", str(output)], host=_CliHost(_live(tmp_path, token=token), gh),
                  state=State(tmp_path / "state"), now=T0)
    assert rc == 0
    assert _kv(output.read_text()) == {"fix": "", "has_fix": "false", "project": "", "has_project": "false"}
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "xoxb" not in err and "Bad credentials" not in err


def test_cli_fired_reports_in_the_thread(tmp_path, fake_slack):
    state = State(tmp_path / "state")
    _record(state.root, [(40, "b")])
    gh = FakeGh({40: {"approved"}})
    assert cli.main(["fired", "--issue", "40", "--kind", "fix"], host=_CliHost(_live(tmp_path), gh), state=state,
                    now=T0) == 0
    assert gh.labels[40] == {"stuck"}
    assert fake_slack.posts == [("C9", "Couldn't ship 1: b. It needs a person (#40).", PARENT)]


def test_cli_fired_slack_error_still_exits_0(tmp_path, fake_slack, capsys):
    fake_slack.post_error = "ratelimited"
    state = State(tmp_path / "state")
    _record(state.root, [(40, "b")])
    gh = FakeGh({40: {"approved"}})
    assert cli.main(["fired", "--issue", "40", "--kind", "fix"], host=_CliHost(_live(tmp_path), gh), state=state,
                    now=T0) == 0
    assert "ratelimited" in capsys.readouterr().err
    assert gh.labels[40] == {"stuck"}


def test_cli_fired_without_a_token_still_marks_github(tmp_path, fake_slack, capsys):
    state = State(tmp_path / "state")
    _record(state.root, [(40, "b")])
    gh = FakeGh({40: {"approved"}})
    assert cli.main(["fired", "--issue", "40", "--kind", "fix"],
                    host=_CliHost(_live(tmp_path, token=False), gh), state=state, now=T0) == 0
    assert "no_token" in capsys.readouterr().err
    assert fake_slack.tokens == [] and fake_slack.posts == []
    assert gh.labels[40] == {"stuck"}


def test_cli_fired_gh_failure_exits_1(tmp_path, fake_slack, capsys):
    gh = FakeGh({40: {"approved"}}, fail=["issue view"])
    assert cli.main(["fired", "--issue", "40", "--kind", "fix"], host=_CliHost(_live(tmp_path), gh),
                    state=State(tmp_path / "state"), now=T0) == 1
    assert "Bad credentials" not in capsys.readouterr().err


def test_cli_without_a_repository_is_a_usage_error(tmp_path, fake_slack, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY")
    host = _CliHost(_live(tmp_path), FakeGh({}))
    state = _PlanState(tmp_path / "state", plan={"propose": [{"issue": 31, "change": "a"}]})
    assert cli.main(["propose", "--from", "watch"], host=host, state=state, now=T0) == 2
    assert cli.main(["fired", "--issue", "4", "--kind", "fix"], host=host, state=state, now=T0) == 2
    output = tmp_path / "out.txt"
    assert cli.main(["approvals", "--github-output", str(output)], host=host, state=state, now=T0) == 2
    assert _kv(output.read_text()) == {"fix": "", "has_fix": "false", "project": "", "has_project": "false"}
    assert "GITHUB_REPOSITORY" in capsys.readouterr().err


def test_cli_propose_resolves_the_channel_again_when_it_went_stale(tmp_path, fake_slack, capsys):
    fake_slack.post_errors = ["channel_not_found"]
    state = _PlanState(tmp_path / "state", plan={"propose": [{"issue": 31, "change": "a"}]})
    (state.root / "slack-channel.json").write_text(json.dumps({"name": "autonomous-agent", "id": "C1"}))
    gh = FakeGh({31: set()})

    assert cli.main(["propose", "--from", "watch"], host=_CliHost(_live(tmp_path), gh), state=state, now=T0) == 0

    assert capsys.readouterr().out == "proposed=1\n"
    assert len(fake_slack.channels) == 2
    assert not (state.root / "slack-channel.json").exists()   # dropped; this fake resolves without caching
    assert gh.labels[31] == {"proposed"}


@pytest.mark.parametrize("code", ["is_archived", "not_in_channel"])
def test_cli_propose_retries_a_stale_channel_once_only(tmp_path, fake_slack, capsys, code):
    fake_slack.post_errors = [code, code]
    state = _PlanState(tmp_path / "state", plan={"propose": [{"issue": 31, "change": "a"}]})
    rc = cli.main(["propose", "--from", "watch"], host=_CliHost(_live(tmp_path), FakeGh({31: set()})), state=state,
                  now=T0)
    assert rc == 1
    assert len(fake_slack.channels) == 2
    assert code in capsys.readouterr().err


def test_cli_propose_does_not_retry_other_slack_errors(tmp_path, fake_slack):
    fake_slack.post_errors = ["ratelimited"]
    state = _PlanState(tmp_path / "state", plan={"propose": [{"issue": 31, "change": "a"}]})
    rc = cli.main(["propose", "--from", "watch"], host=_CliHost(_live(tmp_path), FakeGh({31: set()})), state=state,
                  now=T0)
    assert rc == 1
    assert len(fake_slack.channels) == 1


def test_cli_approvals_while_the_pool_holds_decides_but_fires_nothing(tmp_path, fake_slack, capsys):
    state = State(tmp_path / "state")
    _record(state.root, [(25, "a")])
    fake_slack.threads[PARENT] = [_reply("approve")]
    gh = FakeGh({25: {"proposed"}, 30: {"approved"}})
    host = _CliHost(_live(tmp_path), gh, pool="hold_scheduled")

    assert cli.main(["approvals"], host=host, state=state, now=T0) == 0

    assert gh.labels[25] == {"approved"}
    assert fake_slack.thread_posts() == ["Approved 1: queued to ship."]
    assert _kv(capsys.readouterr().out) == {"fix": "", "has_fix": "false", "project": "", "has_project": "false"}
    assert [c for c in gh.calls if c[0] == "run"] == []


def test_cli_approvals_fires_when_the_pool_cannot_be_read(tmp_path, fake_slack, capsys):
    gh = FakeGh({25: {"approved"}})
    host = _CliHost(_live(tmp_path), gh, pool=None)
    assert cli.main(["approvals"], host=host, state=State(tmp_path / "state"), now=T0) == 0
    assert _kv(capsys.readouterr().out)["fix"] == "25"


def test_cli_ready_writes_go(tmp_path, fake_slack):
    output = tmp_path / "out.txt"
    host = _CliHost(_live(tmp_path), FakeGh({40: {"approved"}, 33: {"approved", "project", "stuck"}}))
    assert cli.main(["ready", "--issue", "40", "--kind", "fix", "--github-output", str(output)], host=host,
                    state=State(tmp_path / "state"), now=T0) == 0
    assert cli.main(["ready", "--issue", "33", "--kind", "project", "--github-output", str(output)], host=host,
                    state=State(tmp_path / "state"), now=T0) == 0
    assert output.read_text() == "go=true\ngo=false\n"


def test_cli_ready_gh_failure_is_no_go(tmp_path, fake_slack, capsys):
    output = tmp_path / "out.txt"
    host = _CliHost(_live(tmp_path), FakeGh({40: {"approved"}}, fail=["issue view"]))
    assert cli.main(["ready", "--issue", "40", "--kind", "fix", "--github-output", str(output)], host=host,
                    state=State(tmp_path / "state"), now=T0) == 0
    assert output.read_text() == "go=false\n"
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1 and "Bad credentials" not in err


def test_cli_fired_passes_the_claude_steps_outcome_on(tmp_path, fake_slack):
    state = State(tmp_path / "state")
    _record(state.root, [(33, "project-sized")])
    gh = FakeGh({33: {"approved", "project"}})
    host = _CliHost(_live(tmp_path), gh)
    for _ in range(2):
        assert cli.main(["fired", "--issue", "33", "--kind", "project", "--outcome", "failure"], host=host,
                        state=state, now=T0) == 0
    assert gh.labels[33] == {"project", "stuck"}
    assert fake_slack.posts == [("C9", "1 needs a person (#33).", PARENT)]
