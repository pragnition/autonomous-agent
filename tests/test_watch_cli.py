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
    """A host for `measure` wiring: an empty-but-valid core collection, no Jensen PRs, and a canned reply to
    the plain `gh issue list` call that fetches open `fix-deployed` issues."""

    def __init__(self, open_deployed_cp):
        self.open_deployed_cp = open_deployed_cp
        self.run_calls: list[list[str]] = []

    def core_python(self, module_file, args):
        return json.dumps({"items": [], "conversations": {}, "projects": [], "pool": "normal", "claimed": 0})

    def secret_values(self):
        return []

    def jensen_gh(self, args, *, check=True, capture=True):
        if args[:2] == ["api", "user"]:
            return _cp(stdout="jensen-bot\n")
        if args[:2] == ["search", "prs"]:
            return _cp(stdout="[]")
        raise AssertionError(f"unexpected jensen_gh call: {args}")

    def run(self, args, **kwargs):
        self.run_calls.append(list(args))
        return self.open_deployed_cp


def test_measure_writes_github_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    deployed = [{"number": 7, "title": "Some deployed fix"}]
    host = _MeasureHost(_cp(stdout=json.dumps(deployed)))
    state = State(tmp_path / "state")
    output_file = tmp_path / "out.txt"

    rc = main(["measure", "--github-output", str(output_file)], host=host, state=state, now=T0)

    assert rc == 0
    kv = _kv(output_file.read_text())
    assert kv["has_work"] == "true"
    assert kv["skip_reason"] == ""
    assert kv["digest"]
    assert Path(kv["digest"]).exists()

    # The open fix-deployed issues are fetched with the plain `gh` (the job's own token via `host.run`), never
    # through Jensen's token.
    assert host.run_calls == [["gh", "issue", "list", "-R", "acme/widgets", "--label", "fix-deployed",
                               "--state", "open", "--json", "number,title"]]


def test_measure_warns_and_skips_when_repository_unset(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    host = _MeasureHost(_cp(stdout="[]"))
    state = State(tmp_path / "state")

    rc = main(["measure"], host=host, state=state, now=T0)

    assert rc == 0
    assert host.run_calls == []
    captured = capsys.readouterr()
    assert "GITHUB_REPOSITORY" in captured.err

    # No --github-output: the same lines print to stdout instead.
    kv = _kv(captured.out)
    assert kv["has_work"] == "false"
    assert kv["skip_reason"] == "quiet"
    assert kv["digest"] == ""


def test_measure_warns_and_skips_when_gh_call_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/widgets")
    host = _MeasureHost(_cp(returncode=1, stderr="gh: not authenticated"))
    state = State(tmp_path / "state")

    rc = main(["measure"], host=host, state=state, now=T0)

    assert rc == 0
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    kv = _kv(captured.out)
    assert kv["has_work"] == "false"
    assert kv["skip_reason"] == "quiet"


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
    assert kv["fix"] == ""
    assert kv["has_reviews"] == "false"
    assert kv["has_fix"] == "false"
    assert "reviews" not in kv    # plan never outputs raw PR URLs -- see review-target
    assert json.loads((state_dir / "runs" / "42-reviews.json").read_text()) == []


def test_plan_outputs_real_plan_to_stdout(tmp_path, capsys):
    state_dir = tmp_path / "state"
    state = State(state_dir)
    (state_dir / "plan.json").write_text(json.dumps({
        "reviews": [{"url": "https://github.com/acme/widgets/pull/1", "conversation": "C1"}], "fix": 9,
    }))

    rc = main(["plan", "--run-id", "77"], host=object(), state=state, now=T0)

    assert rc == 0
    kv = _kv(capsys.readouterr().out)
    assert json.loads(kv["review_indices"]) == [0]
    assert kv["fix"] == "9"
    assert kv["has_reviews"] == "true"
    assert kv["has_fix"] == "true"
    assert "reviews" not in kv
    saved = json.loads((state_dir / "runs" / "77-reviews.json").read_text())
    assert saved == [{"url": "https://github.com/acme/widgets/pull/1", "conversation": "C1"}]


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
                              "fixing", "fix-deployed", "stuck", "project"}
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
