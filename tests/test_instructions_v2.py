"""Anchor tests for improve-jensen's instruction files and docs: the assessment, improvement projects, the yardstick
gate and Jensen's notes. The files are improve-jensen's behaviour (loaded with `--append-system-prompt-file`), so
these pin the rules and the exact commands each one has to carry."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent

ASSESS = ".github/improve-jensen-assess.md"
PROJECT = ".github/improve-jensen-project.md"
CHARTER = ".github/improve-jensen.md"
WATCH = ".github/improve-jensen-watch.md"
REVIEW = ".github/improve-jensen-review.md"
DEPLOY = "deploy/improve-jensen.md"
DESIGN = "docs/design.md"

PUBLIC_REPO_RULE = """\
- **This repo is public.** improve-jensen's issues, PRs, commits and workflow logs never hold:
  - secrets;
  - conversation text;
  - people's names;
  - details of products, customers or their code (file paths, vulnerabilities).

  The evidence stays on the host."""

GATE_DISPATCH = "gh workflow run improve-jensen.yml -f mode=yardstick -f target=gate"

# The watch CLI's subcommands, as the parallel tasks build them. An instruction that names any other one would send
# improve-jensen after a command that doesn't exist.
KNOWN_SUBCOMMANDS = {
    "measure", "commit", "count", "note", "plan", "review-target", "seed", "gh", "feedback-reply", "changes-add",
    "yardstick", "gate", "project-mark", "assess-check", "assess-plan", "assess-commit",
}


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _assert_anchors(path: str, anchors: tuple[str, ...]) -> None:
    text = _read(path)
    for anchor in anchors:
        assert anchor in text, (path, anchor)


def test_assessment_instructions():
    _assert_anchors(ASSESS, (
        "assessment.md", "jensen-assessment", "priority", "yardstick add", "assess-plan.json",
        "yardstick score", "jensen_watch.py count", "evidence from Jensen's own work", "10 items", "5 days",
        "project-active.json", "gh issue pin", "build.sh", "never copies of product code",
        "Post nothing in Slack",
    ))


def test_project_instructions():
    _assert_anchors(PROJECT, (
        "improve-jensen/project-", "gate start", "docs/improve-jensen/projects/", "-progress.md", "Design note:",
        "project-mark", "--status active", "--status done", "just deploy", GATE_DISPATCH, ".github/improve-jensen.md",
        "6 hours",
    ))


def test_charter_gate_notes_and_change_log():
    _assert_anchors(CHARTER, (
        "gate start", "changes-add", "feedback-reply", GATE_DISPATCH, "--kind repo", "--kind host", "gating",
        "## The failed gate", "gate status", "yardstick score", "Attempt failed: the yardstick gate failed",
    ))


def test_watch_priority_gating_and_notes():
    _assert_anchors(WATCH, (
        "gating", "priority", "gate status", "Jensen reports", "feedback-reply", "feedback/<name>",
        "general terms", "visible harm",
    ))


def test_deploy_doc_switch_on_steps():
    _assert_anchors(DEPLOY, (
        "five", "work-4", "gating", "priority", "jensen-assessment", "yardstick install deploy/yardstick",
        "mode=yardstick", "mode `assess`", "assessment.md", "docker exec agent-runner mkdir -p /var/agent/feedback",
        "retired/",
    ))


def test_design_doc_covers_the_engine_v2():
    section = _read(DESIGN).split("## improve-jensen", 1)[1].split("\n## ", 1)[0]
    for anchor in ("assess", "yardstick", "project", "/var/agent/feedback", "CHANGES.md", "five self-hosted runners",
                   ".github/improve-jensen-assess.md", ".github/improve-jensen-project.md"):
        assert anchor in section, anchor


def test_every_instruction_file_carries_the_public_repo_rule_verbatim():
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW):
        assert PUBLIC_REPO_RULE in _read(path), path


def test_instructions_name_only_real_watch_subcommands():
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW, DEPLOY):
        for sub in re.findall(r"jensen_watch\.py\"? ([a-z][a-z-]*)", _read(path)):
            assert sub in KNOWN_SUBCOMMANDS, (path, sub)


def test_slack_stays_limited_to_the_admin_dm():
    # Only the charter describes the one Slack DM; the assessment and project runs never post there.
    for path in (ASSESS, PROJECT):
        text = _read(path)
        assert "chat.postMessage" not in text, path
        assert "Post nothing in Slack" in text, path
