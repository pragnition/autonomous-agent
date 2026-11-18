import subprocess
from pathlib import Path

import pytest

from agent.watch import host as host_module
from agent.watch.host import Host


def _fake_run(results):
    """A fake `run` that pops one `CompletedProcess` per call, in order, and records every call's args/kwargs."""
    calls = []
    queue = list(results)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return queue.pop(0)

    return calls, run


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# -- core_python --


def test_core_python_pipes_file_and_raises_on_error(tmp_path):
    mod = tmp_path / "mod.py"
    mod.write_text("print('hi')\n")

    calls, run = _fake_run([_cp(stdout="ok\n")])
    host = Host(run=run, live=tmp_path)
    out = host.core_python(mod, ["--since", "x"])

    assert out == "ok\n"
    args, kwargs = calls[0]
    assert args == ["docker", "exec", "-i", "agent-core", "python", "-", "--since", "x"]
    assert kwargs["input"] == "print('hi')\n"

    # A bare name resolves relative to src/agent/watch, where core_collect.py/core_ask.py actually live.
    calls2, run2 = _fake_run([_cp()])
    host2 = Host(run=run2, live=tmp_path)
    host2.core_python("state.py", [])
    expected_text = (Path(host_module.__file__).parent / "state.py").read_text()
    assert calls2[0][1]["input"] == expected_text

    calls3, run3 = _fake_run([_cp(returncode=1, stderr="boom")])
    host3 = Host(run=run3, live=tmp_path)
    with pytest.raises(RuntimeError, match="boom"):
        host3.core_python(mod, [])


# -- transcripts --


def test_transcripts_skips_missing(tmp_path):
    dest = tmp_path / "out"
    calls, run = _fake_run([_cp(stdout="line1\n"), _cp(returncode=1, stderr="cat: No such file or directory")])
    host = Host(run=run, live=tmp_path)

    result = host.transcripts(["W-ok", "W-missing"], dest)

    assert set(result) == {"W-ok"}
    assert result["W-ok"].read_text() == "line1\n"
    assert not (dest / "W-missing.jsonl").exists()
    assert calls[0][0] == ["docker", "exec", "agent-runner", "cat", "/var/agent/transcripts/W-ok.jsonl"]
    assert calls[1][0] == ["docker", "exec", "agent-runner", "cat", "/var/agent/transcripts/W-missing.jsonl"]


# -- jensen_gh --


def test_jensen_gh_token_only_in_env(tmp_path):
    secrets_dir = tmp_path / "data" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "agent.env").write_text("GH_TOKEN=tok123\n# a comment\nOTHER=x\n")

    calls, run = _fake_run([_cp(stdout="[]")])
    host = Host(run=run, live=tmp_path)
    host.jensen_gh(["pr", "list"])

    args, kwargs = calls[0]
    assert "tok123" not in args
    assert args == ["gh", "pr", "list"]
    assert kwargs["env"]["GH_TOKEN"] == "tok123"
