from pathlib import Path

import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from agent.runner.base import RunSpec
from agent.runner.policy import decide, make_can_use_tool


@pytest.fixture
def dirs(tmp_path):
    mirror, wt, outside = tmp_path / "mirror", tmp_path / "wt", tmp_path / "outside"
    for d in (mirror, wt, outside):
        d.mkdir()
    (outside / "secret.txt").write_text("x")
    return mirror, wt, outside


def chat(tool, args, mirror, cwd=None):
    return decide("chat", tool, args, cwd=cwd or mirror, read_paths=[mirror], write_paths=[])


def builder(tool, args, wt):
    return decide("builder", tool, args, cwd=wt, read_paths=[], write_paths=[wt])


def test_chat_read_inside_mirror_allowed(dirs):
    mirror, _, _ = dirs
    assert chat("Read", {"file_path": str(mirror / "README.md")}, mirror)[0] is True


def test_read_secrets_denied(dirs):
    mirror, _, _ = dirs
    assert chat("Read", {"file_path": "/data/secrets/core.env"}, mirror)[0] is False


def test_dotdot_escape_denied(dirs):
    mirror, _, _ = dirs
    assert chat("Read", {"file_path": f"{mirror}/../outside/secret.txt"}, mirror)[0] is False


def test_symlink_escape_denied(dirs):
    mirror, _, outside = dirs
    (mirror / "link").symlink_to(outside)
    assert chat("Read", {"file_path": str(mirror / "link" / "secret.txt")}, mirror)[0] is False


def test_relative_path_resolved_against_cwd(dirs):
    _, wt, _ = dirs
    assert builder("Read", {"file_path": "src/x.py"}, wt)[0] is True


def test_chat_bash_denied(dirs):
    mirror, _, _ = dirs
    assert chat("Bash", {"command": "ls"}, mirror)[0] is False


def test_chat_write_denied_even_inside(dirs):
    mirror, _, _ = dirs
    ok, _ = decide("chat", "Write", {"file_path": str(mirror / "a")}, cwd=mirror,
                   read_paths=[mirror], write_paths=[mirror])
    assert ok is False


@pytest.mark.parametrize("cmd", ["git push origin HEAD", "cd x && git  push", "git remote -v",
                                 "gh pr merge 1", "ls; gh api /user", "cat /data/secrets/worker.env",
                                 "cat /proc/1/environ", "printenv", "env"])
def test_builder_dangerous_bash_denied(dirs, cmd):
    _, wt, _ = dirs
    assert builder("Bash", {"command": cmd}, wt)[0] is False


@pytest.mark.parametrize("cmd", ["python -m pytest -q", "git commit -am 'x'", "git status", "npm test",
                                 "env FOO=1 python x.py"])
def test_builder_ordinary_bash_allowed(dirs, cmd):
    _, wt, _ = dirs
    assert builder("Bash", {"command": cmd}, wt)[0] is True


def test_builder_write_outside_worktree_denied(dirs):
    _, wt, outside = dirs
    assert builder("Write", {"file_path": str(outside / "x")}, wt)[0] is False


def test_builder_edit_inside_worktree_allowed(dirs):
    _, wt, _ = dirs
    assert builder("Edit", {"file_path": str(wt / "a.py")}, wt)[0] is True


def test_grep_without_path_uses_cwd(dirs):
    mirror, _, outside = dirs
    assert chat("Grep", {"pattern": "x"}, mirror)[0] is True
    assert chat("Grep", {"pattern": "x"}, mirror, cwd=outside)[0] is False


def test_mcp_tools_allowed(dirs):
    mirror, _, _ = dirs
    assert chat("mcp__agent__recall", {"query": "x"}, mirror)[0] is True


def test_unknown_tool_denied(dirs):
    mirror, _, _ = dirs
    ok, reason = chat("WebFetch", {"url": "https://example.com"}, mirror)
    assert ok is False and "not allowed" in reason


async def test_callback_returns_sdk_types(dirs):
    _, wt, _ = dirs
    spec = RunSpec(run_id="R", role="builder", prompt="p", cwd=wt, model="m", system_prompt="s",
                   max_turns=1, timeout_s=1, write_paths=[wt])
    cb = make_can_use_tool(spec)
    ctx = ToolPermissionContext()
    deny = await cb("Bash", {"command": "git push"}, ctx)
    allow = await cb("Read", {"file_path": str(wt / "a")}, ctx)
    assert isinstance(deny, PermissionResultDeny) and deny.message
    assert isinstance(allow, PermissionResultAllow)
