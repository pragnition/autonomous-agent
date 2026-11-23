"""scripts/ask_jensen.py: a turn handed to Jensen from the host goes through agentd's own `ask`."""

import importlib.util
import io
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent


def load():
    spec = importlib.util.spec_from_file_location("ask_jensen", ROOT / "scripts" / "ask_jensen.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_asks_through_agentd(monkeypatch, capsys):
    module = load()
    calls = []

    class FakeHost:
        def run(self, args, **kwargs):
            calls.append((args, kwargs.get("input")))
            return subprocess.CompletedProcess(args, 0, "W-1234\n", "")

    monkeypatch.setattr(module.Host, "default", classmethod(lambda cls: FakeHost()))
    monkeypatch.setattr(module.sys, "stdin", io.StringIO("look at this"))
    assert module.main(["--new", "--text-file", "-"]) == 0
    assert module.main(["--conversation", "V-abc", "--text-file", "-"]) == 0
    (new, text), (existing, _) = calls
    assert new[:6] == ["docker", "exec", "-i", "agent-control", "agentd", "ask"] and text == "look at this"
    for part in (["--text-file", "-"], ["--source", "improve-jensen"], ["--origin", "improve-jensen review"],
                 ["--new-prefix", "V-review-"]):
        assert any(new[i:i + 2] == part for i in range(len(new))), part
    assert any(existing[i:i + 2] == ["--conversation", "V-abc"] for i in range(len(existing)))
    assert capsys.readouterr().out.split() == ["W-1234", "W-1234"]
