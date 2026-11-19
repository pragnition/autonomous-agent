import base64
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from agent.watch.cli import main
from agent.watch.state import State

T0 = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)

SEED_PATH = Path(__file__).resolve().parent.parent / "deploy" / "improve-jensen-seed.json"


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _kv(text: str) -> dict:
    return dict(line.split("=", 1) for line in text.splitlines() if line)


# -- measure --


class _MeasureHost:
    """A host for `measure` wiring: an empty-but-valid core collection, no Jensen PRs."""

    def core_python(self, module_file, args):
        return json.dumps({"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0})

    def secret_values(self):
        return []

    def runner_python(self, module_file, args):
        return "[]"

    def jensen_gh(self, args, *, check=True, capture=True):
        if args[:2] == ["api", "user"]:
            return _cp(stdout="jensen-bot\n")
        if args[:2] == ["search", "prs"]:
            return _cp(stdout="[]")
        raise AssertionError(f"unexpected jensen_gh call: {args}")


def test_measure_writes_github_outputs(tmp_path):
    # A newest failed-deploy record is a simple way to force `has_work` without faking a whole core collection.
    state = State(tmp_path / "state")
    state.log_deploy({"ts": "2026-11-19T09:00:00Z", "from": "a" * 40, "to": "b" * 40, "services": [],
                      "result": "check_failed: compose up exited 1"})
    host = _MeasureHost()
    output_file = tmp_path / "out.txt"

    rc = main(["measure", "--github-output", str(output_file)], host=host, state=state, now=T0)

    assert rc == 0
    kv = _kv(output_file.read_text())
    assert kv["has_work"] == "true"
    assert kv["skip_reason"] == ""
    assert kv["digest"]
    assert Path(kv["digest"]).exists()


def test_measure_skips_a_quiet_hour(tmp_path, capsys):
    host = _MeasureHost()
    state = State(tmp_path / "state")

    rc = main(["measure"], host=host, state=state, now=T0)

    assert rc == 0
    # No --github-output: the same lines print to stdout instead.
    kv = _kv(capsys.readouterr().out)
    assert kv["has_work"] == "false"
    assert kv["skip_reason"] == "quiet"
    assert kv["digest"] == ""


# -- commit --


def test_commit_delegates_to_state(tmp_path):
    state = State(tmp_path / "state")
    state.window(T0)

    rc = main(["commit"], host=object(), state=state, now=T0)

    assert rc == 0
    raw = json.loads((tmp_path / "state" / "state.json").read_text())
    assert raw["committed_end"] == "2026-11-19T10:00:00Z"


# -- count / note --


def test_count_and_note_print(tmp_path, capsys):
    state = State(tmp_path / "state")
    state.append_metrics([{"ts": "2026-11-18T01:00:00Z", "kind": "turn", "lost_background": 1}])

    rc = main(["count", "lost_background", "--since", "2026-11-18T00:00:00Z"], host=object(), state=state, now=T0)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == state.count("lost_background", datetime(2026, 11, 18, tzinfo=UTC))

    rc_unknown = main(["count", "not_a_signal", "--since", "2026-11-18T00:00:00Z"],
                      host=object(), state=state, now=T0)
    assert rc_unknown == 2
    err = capsys.readouterr().err
    assert "not_a_signal" in err

    rc_note1 = main(["note", "42", "ref-1", "saw it again"], host=object(), state=state, now=T0)
    assert rc_note1 == 0
    assert capsys.readouterr().out.strip() == "1"

    rc_note2 = main(["note", "42", "ref-2", "and again"], host=object(), state=state, now=T0)
    assert rc_note2 == 0
    assert capsys.readouterr().out.strip() == "2"


# -- plan --


def test_plan_outputs_fallback(tmp_path):
    state_dir = tmp_path / "state"
    state = State(state_dir)
    (state_dir / "plan.json").write_text("{not valid json")
    output_file = tmp_path / "out.txt"

    rc = main(["plan", "--github-output", str(output_file), "--run-id", "42"], host=object(), state=state, now=T0)

    assert rc == 0
    kv = _kv(output_file.read_text())
    assert kv["review_indices"] == "[]"
    assert kv["has_reviews"] == "false"
    assert "reviews" not in kv    # plan never outputs raw PR URLs -- see review-target
    assert json.loads((state_dir / "runs" / "42-reviews.json").read_text()) == []


def test_plan_outputs_real_plan_to_stdout(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state = State(state_dir)
    (state_dir / "plan.json").write_text(json.dumps({
        "reviews": [{"url": "https://github.com/acme/widgets/pull/1", "conversation": "C1"}],
        "propose": [{"issue": 9, "change": "stop doing the thing"}],
    }))

    rc = main(["plan", "--run-id", "77"], host=object(), state=state, now=T0)

    assert rc == 0
    kv = _kv(capsys.readouterr().out)
    assert json.loads(kv["review_indices"]) == [0]
    assert kv["has_reviews"] == "true"
    assert "reviews" not in kv
    assert "propose" not in kv  # plan never outputs the propose list -- that's `propose --from watch`'s job
    saved = json.loads((state_dir / "runs" / "77-reviews.json").read_text())
    assert saved == [{"url": "https://github.com/acme/widgets/pull/1", "conversation": "C1"}]


def test_plan_drops_prs_an_earlier_run_already_reviewed(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state = State(state_dir)
    reviewed, fresh = "https://github.com/acme/widgets/pull/1", "https://github.com/acme/widgets/pull/2"
    state.write_run_reviews("41", [{"url": reviewed, "conversation": "C1"}])
    (state_dir / "plan.json").write_text(json.dumps({"reviews": [
        {"url": reviewed, "conversation": "C1"}, {"url": fresh, "conversation": None},
        {"url": fresh, "conversation": None}]}))  # listed twice: still one review

    rc = main(["plan", "--run-id", "42"], host=object(), state=state, now=T0)

    assert rc == 0
    assert json.loads(_kv(capsys.readouterr().out)["review_indices"]) == [0]
    assert json.loads((state_dir / "runs" / "42-reviews.json").read_text()) == [{"url": fresh, "conversation": None}]

    # Run 42 attempted again (a re-run keeps its run id): its own earlier file doesn't count as "already reviewed".
    rc = main(["plan", "--run-id", "42"], host=object(), state=state, now=T0)
    assert rc == 0
    assert json.loads(_kv(capsys.readouterr().out)["review_indices"]) == [0]


def test_measure_with_work_clears_last_hours_plan(tmp_path, capsys):
    # Nothing else consumes plan.json: if Claude's run this hour succeeds without writing one, `plan` must not
    # dispatch last hour's reviews and proposals again.
    state_dir = tmp_path / "state"
    state = State(state_dir)
    (state_dir / "plan.json").write_text(json.dumps(
        {"reviews": [{"url": "https://github.com/acme/widgets/pull/1", "conversation": None}],
         "propose": [{"issue": 9, "change": "stop doing the thing"}]}))
    state.log_deploy({"ts": "2026-11-19T09:00:00Z", "from": "a" * 40, "to": "b" * 40, "services": [],
                      "result": "check_failed: compose up exited 1"})
    host = _MeasureHost()

    assert main(["measure"], host=host, state=state, now=T0) == 0
    assert _kv(capsys.readouterr().out)["has_work"] == "true"

    assert not (state_dir / "plan.json").exists()
    assert main(["plan", "--run-id", "43"], host=object(), state=state, now=T0) == 0
    kv = _kv(capsys.readouterr().out)
    assert kv["review_indices"] == "[]"


def test_plan_run_id_defaults_to_the_github_run_id_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    state_dir = tmp_path / "state"
    state = State(state_dir)
    (state_dir / "plan.json").write_text(json.dumps({"reviews": [{"url": "https://github.com/x/y/pull/1"}]}))

    rc = main(["plan"], host=object(), state=state, now=T0)

    assert rc == 0
    assert (state_dir / "runs" / "555-reviews.json").exists()


# -- review-target --


def test_review_target_prints_url_and_conversation(tmp_path, capsys):
    state = State(tmp_path / "state")
    state.write_run_reviews("42", [{"url": "https://github.com/acme/widgets/pull/1", "conversation": "C1"},
                                   {"url": "https://github.com/acme/widgets/pull/2", "conversation": None}])

    rc = main(["review-target", "--run", "42", "--index", "0"], host=object(), state=state, now=T0)
    assert rc == 0
    assert capsys.readouterr().out == "url=https://github.com/acme/widgets/pull/1\nconversation=C1\n"

    rc2 = main(["review-target", "--run", "42", "--index", "1"], host=object(), state=state, now=T0)
    assert rc2 == 0
    assert capsys.readouterr().out == "url=https://github.com/acme/widgets/pull/2\nconversation=none\n"


def test_review_target_missing_file_or_index_exits_2(tmp_path, capsys):
    state = State(tmp_path / "state")

    rc = main(["review-target", "--run", "no-such-run", "--index", "0"], host=object(), state=state, now=T0)
    assert rc == 2
    assert "no-such-run" in capsys.readouterr().err

    state.write_run_reviews("42", [{"url": "https://github.com/acme/widgets/pull/1", "conversation": None}])
    rc2 = main(["review-target", "--run", "42", "--index", "5"], host=object(), state=state, now=T0)
    assert rc2 == 2
    assert "42" in capsys.readouterr().err


# -- feedback-reply / changes-add --


class _FeedbackHost:
    """Records every `runner_python` call `feedback-reply`/`changes-add` makes, decoding the base64'd payload
    back out so a test can check exactly what crossed the `docker exec` boundary. `fail` lets a test make the
    call raise, the way `Host.runner_python` does on a non-zero `docker exec` exit."""

    def __init__(self, fail: Exception | None = None):
        self.calls: list[tuple] = []
        self._fail = fail

    def runner_python(self, module_file, args):
        self.calls.append((module_file, list(args)))
        if self._fail:
            raise self._fail
        return "ok\n"


def _flag(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_feedback_reply_appends_status(tmp_path):
    host = _FeedbackHost()

    rc = main(["feedback-reply", "2026-11-19-thing.md", "fixed in the next deploy"],
              host=host, state=State(tmp_path), now=T0)

    assert rc == 0
    [(module_file, args)] = host.calls
    assert module_file == "runner_feedback.py"
    assert args[0] == "reply"
    # `--name=<name>`, one token: a name starting with `-` must not be read as another option.
    assert "--name=2026-11-19-thing.md" in args
    assert not any(a == "--name" for a in args)
    assert _flag(args, "--now") == "2026-11-19T10:00:00Z"
    assert base64.b64decode(_flag(args, "--status-b64")).decode() == "fixed in the next deploy"


def test_feedback_reply_rejects_bad_names_without_touching_the_runner(tmp_path, capsys):
    host = _FeedbackHost()

    for bad_name in ("../x.md", "a/b.md", "x.txt"):
        rc = main(["feedback-reply", bad_name, "status"], host=host, state=State(tmp_path), now=T0)
        assert rc == 2
        assert "bad note name" in capsys.readouterr().err

    assert host.calls == []  # a bad name never reaches docker exec at all


def test_feedback_reply_runner_failure_prints_only_the_exception_type(tmp_path, capsys):
    host = _FeedbackHost(fail=RuntimeError("docker exec returned 1: Traceback .../home/someone/secret"))

    rc = main(["feedback-reply", "2026-11-19-thing.md", "status"], host=host, state=State(tmp_path), now=T0)

    assert rc == 1
    err = capsys.readouterr().err
    assert "feedback-reply failed (RuntimeError)" in err
    assert "/home/someone/secret" not in err and "Traceback" not in err


def test_changes_add_prepends_a_changelog_entry(tmp_path):
    host = _FeedbackHost()

    rc = main(["changes-add", "jensen ran out of disk space"], host=host, state=State(tmp_path), now=T0)

    assert rc == 0
    [(module_file, args)] = host.calls
    assert module_file == "runner_feedback.py"
    assert args[0] == "change"
    assert _flag(args, "--now") == "2026-11-19T10:00:00Z"
    assert base64.b64decode(_flag(args, "--text-b64")).decode() == "jensen ran out of disk space"


def test_changes_add_runner_failure_prints_only_the_exception_type(tmp_path, capsys):
    host = _FeedbackHost(fail=RuntimeError("docker exec returned 1: Traceback .../home/someone/secret"))

    rc = main(["changes-add", "jensen ran out of disk space"], host=host, state=State(tmp_path), now=T0)

    assert rc == 1
    err = capsys.readouterr().err
    assert "changes-add failed (RuntimeError)" in err
    assert "/home/someone/secret" not in err and "Traceback" not in err


# -- gh passthrough --


def test_gh_subcommand_passes_flags_through_and_returns_code(tmp_path):
    calls = []

    class _GhHost:
        def jensen_gh(self, args, *, check, capture):
            calls.append((list(args), check, capture))
            return _cp(returncode=3)

    rc = main(["gh", "issue", "list", "--json", "number,title"], host=_GhHost(), state=State(tmp_path), now=T0)

    assert rc == 3
    assert calls == [(["issue", "list", "--json", "number,title"], False, False)]


def test_gh_subcommand_reports_missing_token(tmp_path, capsys):
    class _GhHost:
        def jensen_gh(self, args, *, check, capture):
            raise RuntimeError("GH_TOKEN missing from /x/agent.env")

    rc = main(["gh", "pr", "list"], host=_GhHost(), state=State(tmp_path), now=T0)

    assert rc == 1
    assert "GH_TOKEN missing" in capsys.readouterr().err


# -- seed --


class _SeedHost:
    """Records every `gh` call `seed` makes through the plain `run`, and answers `issue list --search` based on
    which titles are already open."""

    def __init__(self, existing_titles=()):
        self.existing_titles = set(existing_titles)
        self.calls: list[list[str]] = []

    def run(self, args, **kwargs):
        self.calls.append(list(args))
        if args[1:3] == ["label", "create"]:
            return _cp(returncode=0)
        if args[1:3] == ["issue", "list"]:
            search = args[args.index("--search") + 1]
            title = search[: -len(" in:title")]
            found = [{"title": title}] if title in self.existing_titles else []
            return _cp(stdout=json.dumps(found))
        if args[1:3] == ["issue", "create"]:
            return _cp(returncode=0, stdout="https://github.com/acme/widgets/issues/99\n")
        raise AssertionError(f"unexpected call: {args}")


def test_seed_skips_existing_titles_and_creates_labels(tmp_path):
    seed_file = tmp_path / "seed.json"
    entries = [
        {"title": "Old problem", "labels": ["jensen-watch", "works"], "body": "body-old"},
        {"title": "New problem", "labels": ["jensen-watch", "people", "needs-person"], "body": "body-new"},
    ]
    seed_file.write_text(json.dumps(entries))
    host = _SeedHost(existing_titles={"Old problem"})

    rc = main(["seed", str(seed_file), "--repo", "acme/widgets"], host=host, state=State(tmp_path / "state"))

    assert rc == 0

    label_calls = [c for c in host.calls if c[1:3] == ["label", "create"]]
    created_labels = {c[3] for c in label_calls}
    assert created_labels == {"jensen-watch", "works", "people", "needs-person",
                              "fixing", "stuck", "project",
                              "proposed", "approved", "rejected", "jensen-assessment"}
    for c in label_calls:
        assert c[4] == "--force"
        assert c[5:7] == ["-R", "acme/widgets"]

    list_calls = [c for c in host.calls if c[1:3] == ["issue", "list"]]
    assert len(list_calls) == 2

    create_calls = [c for c in host.calls if c[1:3] == ["issue", "create"]]
    assert len(create_calls) == 1
    (create_args,) = create_calls
    assert "New problem" in create_args
    assert "body-new" in create_args
    assert "Old problem" not in create_args


def test_seed_uses_repo_env_and_plain_gh_never_jensen_token(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    seed_file = tmp_path / "seed.json"
    seed_file.write_text(json.dumps([{"title": "One problem", "labels": ["jensen-watch", "ships"], "body": "b"}]))

    class _NoJensenTokenHost(_SeedHost):
        def jensen_gh(self, *a, **k):
            raise AssertionError("seed must never use Jensen's token")

    host = _NoJensenTokenHost()

    rc = main(["seed", str(seed_file)], host=host, state=State(tmp_path / "state"))

    assert rc == 0
    assert any(c[1:3] == ["issue", "create"] and "acme/widgets" in c for c in host.calls)


# -- the seed file itself (public-repo constraint) --

_AREAS = {"ships", "works", "long-runs", "people"}
_HEADINGS = ("## What goes wrong", "## Why it matters", "## Evidence", "## Where the fix belongs")
_WORD_BOUNDARY_TERMS = ("rosterrolo", "nyp", "chilliclaw", "haojun", "zhongwei", "kunming", "parthib", "ivan",
                        "carl", "chingyen")
_SUBSTRING_TERMS = ("/backend/", ".js:")


def test_seed_file_is_general():
    entries = json.loads(SEED_PATH.read_text())
    assert entries, "the seed file must not be empty"

    project_count = 0
    for entry in entries:
        labels = set(entry["labels"])
        assert "jensen-watch" in labels, entry["title"]
        assert len(labels & _AREAS) == 1, f"{entry['title']!r} must carry exactly one area label"
        if "project" in labels:
            project_count += 1

        body = entry["body"]
        for heading in _HEADINGS:
            assert heading in body, f"{entry['title']!r} is missing {heading!r}"

        haystack = f"{entry['title']}\n{body}".lower()
        for term in _WORD_BOUNDARY_TERMS:
            assert not re.search(rf"\b{re.escape(term)}\b", haystack), f"{entry['title']!r} leaks {term!r}"
        for term in _SUBSTRING_TERMS:
            assert term not in haystack, f"{entry['title']!r} leaks {term!r}"

    assert project_count == 2, "exactly the two runner rewrites carry `project`"


_VAGUE = ("stays low", "well under", "near zero", "leaves room", "close to zero", "stays at zero")
# The fields `count` prints (see `agent.watch.state.summarize`).
_COUNT_FIELD = re.compile(r"`(?:total|turns|n|median|p90|max|prs|merged_without_passing_ci|ci_unknown|median_lines|"
                          r"median_open_to_merge_s|coauthor_trailers)(?:=\d+)?`")


def test_seed_measured_issues_are_fixed_at_a_number_count_prints():
    # A `jensen-watch` issue with a signal is judged by `count`'s own output, so its "Fixed when" names a field of
    # that output and the number it has to reach -- never a word like "low" that each watch would read its own way.
    for entry in json.loads(SEED_PATH.read_text()):
        evidence = entry["body"].split("## Evidence", 1)[1].split("## Where the fix belongs", 1)[0]
        fixed_when = evidence.split("Fixed when", 1)[1]
        for phrase in _VAGUE:
            assert phrase not in fixed_when, (entry["title"], phrase)
        if evidence.lstrip().startswith("Signal:"):
            assert _COUNT_FIELD.search(fixed_when) and re.search(r"\d", fixed_when), entry["title"]
