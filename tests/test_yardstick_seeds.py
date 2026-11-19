"""The five seed yardstick tasks: each `check` must fail on the untouched fixture and pass once
the reference solution is applied, every task is mechanical (no rubric), and no prompt leaks a
product or people name."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from test_watch_cli import _WORD_BOUNDARY_TERMS  # reuse the public-repo name list

REPO_ROOT = Path(__file__).resolve().parent.parent
YARDSTICK_DIR = REPO_ROOT / "deploy" / "yardstick"
SOLUTIONS_DIR = REPO_ROOT / "tests" / "yardstick_solutions"

TASK_IDS = ["setup-facts", "authz-self-grant", "tests-that-can-fail", "background-result", "split-change"]


def _prepare_run_dir(task_id: str, tmp_path: Path) -> Path:
    """Copy the fixture, build its git repo and drop in `hidden/` and `check` the way the runner
    would after a real turn."""
    task_dir = YARDSTICK_DIR / task_id
    run_dir = tmp_path / task_id
    shutil.copytree(task_dir / "fixture", run_dir)

    build = subprocess.run(["sh", "build.sh"], cwd=run_dir, capture_output=True, text=True)
    assert build.returncode == 0, f"{task_id}: build.sh failed: {build.stdout}\n{build.stderr}"
    assert not (run_dir / "build.sh").exists(), f"{task_id}: build.sh must delete itself"
    assert (run_dir / ".git").is_dir(), f"{task_id}: build.sh must leave a git repo behind"

    hidden_src = task_dir / "hidden"
    if hidden_src.exists():
        shutil.copytree(hidden_src, run_dir / "hidden")

    check_src = task_dir / "check"
    check_dst = run_dir / "check"
    shutil.copy(check_src, check_dst)
    check_dst.chmod(check_src.stat().st_mode)

    return run_dir


def _run_check(run_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["./check"], cwd=run_dir, capture_output=True, text=True)


def _apply_solution(task_id: str, run_dir: Path) -> None:
    """Overlay the reference solution onto a freshly-built run dir. `split-change` instead runs
    the small script that makes its two commits."""
    sol_dir = SOLUTIONS_DIR / task_id
    if task_id == "split-change":
        apply = subprocess.run(["sh", str(sol_dir / "apply.sh")], cwd=run_dir,
                                capture_output=True, text=True)
        assert apply.returncode == 0, f"{task_id}: apply.sh failed: {apply.stdout}\n{apply.stderr}"
        return

    for path in sol_dir.rglob("*"):
        if path.is_dir():
            continue
        dest = run_dir / path.relative_to(sol_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(path, dest)


# -- every seed's check fails on the untouched fixture, and passes on the reference solution --


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_check_fails_on_untouched_fixture(task_id, tmp_path):
    run_dir = _prepare_run_dir(task_id, tmp_path)
    result = _run_check(run_dir)
    assert result.returncode != 0, (
        f"{task_id}: check must fail on the untouched fixture\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


@pytest.mark.parametrize("task_id", TASK_IDS)
def test_check_passes_on_reference_solution(task_id, tmp_path):
    run_dir = _prepare_run_dir(task_id, tmp_path)
    _apply_solution(task_id, run_dir)
    result = _run_check(run_dir)
    assert result.returncode == 0, (
        f"{task_id}: check must pass on the reference solution\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


# -- background-result's checksum is deterministic: slow_build.sh (fast mode) and check must agree --


def test_background_result_slow_build_matches_check(tmp_path):
    run_dir = _prepare_run_dir("background-result", tmp_path)
    env = dict(os.environ, YARDSTICK_FAST="1")
    build = subprocess.run(["sh", "slow_build.sh"], cwd=run_dir, capture_output=True, text=True, env=env)
    assert build.returncode == 0, f"slow_build.sh failed: {build.stdout}\n{build.stderr}"

    checksum = (run_dir / "out" / "checksum.txt").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{64}", checksum), f"not a sha256 hex digest: {checksum!r}"

    (run_dir / "result.md").write_text(f"The checksum is {checksum}.\n")
    result = _run_check(run_dir)
    assert result.returncode == 0, f"check disagreed with slow_build.sh: {result.stdout}\n{result.stderr}"


# -- the seeds are mechanical and don't leak a product or people name --


def test_seeds_are_mechanical():
    for task_id in TASK_IDS:
        task_dir = YARDSTICK_DIR / task_id
        toml_path = task_dir / "task.toml"
        assert toml_path.exists(), f"{task_id}: missing task.toml"
        data = tomllib.loads(toml_path.read_text())

        assert isinstance(data.get("prompt"), str) and data["prompt"].strip(), f"{task_id}: bad prompt"
        assert isinstance(data.get("time_limit_min"), int) and data["time_limit_min"] > 0, (
            f"{task_id}: bad time_limit_min"
        )
        assert "rubric" not in data, f"{task_id}: seeds are mechanical, no rubric"

        check_path = task_dir / "check"
        assert check_path.exists(), f"{task_id}: missing check"
        mode = check_path.stat().st_mode
        assert mode & stat.S_IXUSR, f"{task_id}: check must be executable"

        fixture_dir = task_dir / "fixture"
        build_path = fixture_dir / "build.sh"
        assert build_path.exists(), f"{task_id}: missing fixture/build.sh"
        assert build_path.stat().st_mode & stat.S_IXUSR, f"{task_id}: build.sh must be executable"


def test_seed_prompts_are_general():
    for task_id in TASK_IDS:
        data = tomllib.loads((YARDSTICK_DIR / task_id / "task.toml").read_text())
        haystack = data["prompt"].lower()
        for term in _WORD_BOUNDARY_TERMS:
            assert not re.search(rf"\b{re.escape(term)}\b", haystack), f"{task_id}: prompt leaks {term!r}"


# -- tests-that-can-fail states the contract its hidden tests check, and its fixture agrees with it --


def test_tests_that_can_fail_states_the_contract_its_hidden_tests_check():
    task_dir = YARDSTICK_DIR / "tests-that-can-fail"
    prompt = " ".join(tomllib.loads((task_dir / "task.toml").read_text())["prompt"].split())
    hidden = (task_dir / "hidden" / "test_inventory_hidden.py").read_text()
    # every rejection the hidden tests check raises ValueError, and the prompt names it for each one
    assert "assertRaises(ValueError)" in hidden and "KeyError" not in hidden
    assert "below zero raises ValueError" in prompt and "unknown item raises ValueError" in prompt
    assert "returns the stock that is left" in prompt
    # the existing code Jensen starts from raises the same exception, so nothing nudges it towards another
    for path in (task_dir / "fixture" / "inventory.py", task_dir / "hidden" / "mutant.py"):
        assert "KeyError" not in path.read_text(), path
