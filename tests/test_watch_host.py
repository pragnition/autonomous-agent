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

    # The return code is in the message too: stderr alone can be empty.
    calls3, run3 = _fake_run([_cp(returncode=1, stderr="boom")])
    host3 = Host(run=run3, live=tmp_path)
    with pytest.raises(RuntimeError, match="boom") as excinfo:
        host3.core_python(mod, [])
    assert "1" in str(excinfo.value)


# -- transcripts --


def test_transcripts_skips_missing(tmp_path):
    dest = tmp_path / "out"
    calls, run = _fake_run([_cp(stdout=b"line1\n"),
                            _cp(returncode=1, stderr=b"cat: No such file or directory")])
    host = Host(run=run, live=tmp_path)

    result = host.transcripts(["W-ok", "W-missing"], dest)

    assert set(result) == {"W-ok"}
    assert result["W-ok"].read_bytes() == b"line1\n"
    assert not (dest / "W-missing.jsonl").exists()
    assert calls[0][0] == ["docker", "exec", "agent-runner", "cat", "/var/agent/transcripts/W-ok.jsonl"]
    assert calls[1][0] == ["docker", "exec", "agent-runner", "cat", "/var/agent/transcripts/W-missing.jsonl"]
    # Captured as bytes, never decoded by `run`'s own kwargs.
    assert "text" not in calls[0][1]


def test_transcripts_raises_when_container_unreachable(tmp_path):
    calls, run = _fake_run([_cp(returncode=1, stderr=b"Error: No such container: agent-runner")])
    host = Host(run=run, live=tmp_path)

    with pytest.raises(RuntimeError, match="No such container"):
        host.transcripts(["W-1"], tmp_path / "out")


def test_transcripts_writes_bytes_even_with_a_split_multibyte_char(tmp_path):
    # A transcript caught mid-write can end on a truncated UTF-8 sequence; writing raw bytes must never decode it.
    dest = tmp_path / "out"
    calls, run = _fake_run([_cp(stdout=b"hello \xe2\x98")])  # a truncated 3-byte UTF-8 character
    host = Host(run=run, live=tmp_path)

    result = host.transcripts(["W-1"], dest)

    assert result["W-1"].read_bytes() == b"hello \xe2\x98"


# -- jensen_gh --


def test_jensen_gh_token_only_in_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "leaked-login-token")
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
    # GITHUB_TOKEN never rides along: gh must not have a second identity to fall back to.
    assert "GITHUB_TOKEN" not in kwargs["env"]


def test_jensen_gh_raises_when_token_missing(tmp_path):
    secrets_dir = tmp_path / "data" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "agent.env").write_text("OTHER=x\n")  # no GH_TOKEN at all

    calls, run = _fake_run([])
    host = Host(run=run, live=tmp_path)

    with pytest.raises(RuntimeError, match="GH_TOKEN missing"):
        host.jensen_gh(["pr", "list"])
    assert calls == []  # never falls through to running gh as whoever is logged in


def test_jensen_gh_raises_when_agent_env_missing(tmp_path):
    calls, run = _fake_run([])
    host = Host(run=run, live=tmp_path)

    with pytest.raises(RuntimeError, match="GH_TOKEN missing"):
        host.jensen_gh(["pr", "list"])
    assert calls == []


# -- secret_values --


def test_secret_values_handles_quotes_export_and_inline_comments(tmp_path):
    secrets_dir = tmp_path / "data" / "secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "core.env").write_text(
        'export GH_TOKEN="tok with space" # a trailing comment\n'
        "OTHER='single quoted'\n"
        "BARE=plain # another comment\n"
    )
    host = Host(run=lambda *a, **k: None, live=tmp_path)

    values = host.secret_values()

    assert "tok with space" in values
    assert "single quoted" in values
    assert "plain" in values
    # Never the raw, still-quoted or still-commented line: scrub matches the bare value only.
    assert not any('"' in v or "'" in v or "#" in v for v in values)


def test_secret_values_include_product_secrets_and_the_runner_token(tmp_path):
    # Jensen's products keep their own secrets in data/secrets/projects/*.env, and the runner's token is a bare
    # file: any of them can turn up in a transcript.
    (tmp_path / "data" / "secrets" / "projects").mkdir(parents=True)
    (tmp_path / "data" / "secrets" / "agent.env").write_text("GH_TOKEN=agent-token-0001\n")
    (tmp_path / "data" / "secrets" / "projects" / "sample.env").write_text("DB_PASSWORD=product-secret-0002\n")
    (tmp_path / "data" / "runner").mkdir(parents=True)
    (tmp_path / "data" / "runner" / "runner-token").write_text("runner-token-value-0003\n")
    host = Host(run=lambda *a, **k: None, live=tmp_path)

    values = host.secret_values()

    assert {"agent-token-0001", "product-secret-0002", "runner-token-value-0003"} <= set(values)


def _secrets_tree(tmp_path):
    (tmp_path / "data" / "secrets" / "projects").mkdir(parents=True)
    (tmp_path / "data" / "secrets" / "agent.env").write_text("GH_TOKEN=agent-token-0001\n")
    (tmp_path / "data" / "secrets" / "projects" / "sample.env").write_text("DB_PASSWORD=product-secret-0002\n")
    (tmp_path / "data" / "runner").mkdir(parents=True)
    token = tmp_path / "data" / "runner" / "runner-token"
    token.write_text("runner-token-value-0003\n")
    return token


def _unreadable(monkeypatch, *paths):
    """Make `paths` raise PermissionError on read, the way a 0600 file owned by another uid does -- whatever uid
    the tests run as (root can read a 0600 file, so a chmod alone wouldn't prove anything there)."""
    real_read_text = Path.read_text
    blocked = {Path(p) for p in paths}

    def read_text(self, *args, **kwargs):
        if self in blocked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


def test_secret_values_reads_an_unreadable_runner_token_as_its_owner(tmp_path, monkeypatch):
    # Core writes the runner token 0600 for the agent container's own uid; the watch on the host may not be that uid.
    token = _secrets_tree(tmp_path)
    _unreadable(monkeypatch, token)
    calls, run = _fake_run([_cp(stdout="runner-token-value-0003\n")])
    host = Host(run=run, live=tmp_path)

    values = host.secret_values()

    assert {"agent-token-0001", "product-secret-0002", "runner-token-value-0003"} <= set(values)
    assert calls[0][0] == ["docker", "exec", "agent-runner", "cat", "/run/agent/runner-token"]


def test_secret_values_never_raises_when_a_source_cannot_be_read(tmp_path, monkeypatch, capsys):
    token = _secrets_tree(tmp_path)
    _unreadable(monkeypatch, token, tmp_path / "data" / "secrets" / "agent.env")
    calls, run = _fake_run([_cp(returncode=1, stderr="Error: No such container: agent-runner")])
    host = Host(run=run, live=tmp_path)

    values = host.secret_values()

    assert values == ["product-secret-0002"]
    err = capsys.readouterr().err
    assert "data/runner/runner-token" in err and "data/secrets/agent.env" in err
    assert "PermissionError" in err
    assert "No such container" not in err and "Permission denied" not in err and str(tmp_path) not in err


def test_secret_values_missing_folder(tmp_path):
    host = Host(run=lambda *a, **k: None, live=tmp_path)
    assert host.secret_values() == []
