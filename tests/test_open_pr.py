import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("open_pr", ROOT / "scripts" / "open_pr.py")
open_pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(open_pr)


def _fake(existing):
    calls = []

    def call(method, path, token, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return existing
        if path.endswith("/pulls"):
            return {"number": 12, "html_url": "https://github.com/o/r/pull/12"}
        return []

    return calls, call


def test_creates_pr_and_labels(monkeypatch):
    calls, call = _fake([])
    monkeypatch.setattr(open_pr, "_call", call)
    url = open_pr.open_pr("o/r", "t", "improve-jensen/x", "main", "T", "Closes #10", ["improve-jensen"])
    assert url == "https://github.com/o/r/pull/12"
    assert calls[0] == ("GET", "/repos/o/r/pulls?state=open&head=o:improve-jensen/x", None)
    assert calls[1] == (
        "POST",
        "/repos/o/r/pulls",
        {"title": "T", "body": "Closes #10", "head": "improve-jensen/x", "base": "main"},
    )
    assert calls[2] == ("POST", "/repos/o/r/issues/12/labels", {"labels": ["improve-jensen"]})


def test_reuses_open_pr(monkeypatch):
    calls, call = _fake([{"number": 9, "html_url": "https://github.com/o/r/pull/9"}])
    monkeypatch.setattr(open_pr, "_call", call)
    assert open_pr.open_pr("o/r", "t", "b", "main", "T", "B", []) == "https://github.com/o/r/pull/9"
    assert [c[0] for c in calls] == ["GET"]


def test_charter_says_to_open_the_pr():
    charter = (ROOT / ".github" / "improve-jensen.md").read_text()
    assert "scripts/open_pr.py" in charter and "Closes #" in charter
