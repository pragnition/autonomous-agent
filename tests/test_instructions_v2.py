"""Anchor tests for improve-jensen's instruction files and docs: the assessment, improvement projects, the yardstick
gate and Jensen's notes. The files are improve-jensen's behaviour (loaded with `--append-system-prompt-file`), so
these pin the rules and the exact commands each one has to carry."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from agent.watch.cli import _build_parser

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

# The workflow runs a gate after the job that started it ends; nothing dispatches one (the job token can't).
GATE_HANDOFF = "`gate start`; the workflow runs the gate after your job ends"

# The one rule for which deploy a merge belongs to, the same in the charter and the watch.
DEPLOY_OF_A_MERGE = (
    'the first `"result": "ok"` record', "whose `to` contains the merge",
    "git merge-base --is-ancestor <merge sha> <to>",
)


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _watch_commands() -> tuple[set[str], dict[str, set[str]]]:
    """The watch CLI's subcommands and, for those that have them (`yardstick`, `gate`), their own subcommands, read
    from the real parser at test time. `gh` is handled before argparse (`cli.main`), so it's added by hand."""
    top = _subcommands(_build_parser())
    nested = {name: set(_subcommands(sub)) for name, sub in top.items() if _subcommands(sub)}
    return set(top) | {"gh"}, nested


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _flat(path: str) -> str:
    """The file with every run of whitespace as one space, so an anchor matches across a wrapped line."""
    return " ".join(_read(path).split())


def _assert_anchors(path: str, anchors: tuple[str, ...]) -> None:
    text = _flat(path)
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
        "project-mark", "--status active", "--status done", "just deploy", GATE_HANDOFF, ".github/improve-jensen.md",
        "6 hours",
    ))


def test_charter_gate_notes_and_change_log():
    _assert_anchors(CHARTER, (
        "gate start", "changes-add", "feedback-reply", GATE_HANDOFF, "--kind repo", "--kind host", "gating",
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
    """Every `jensen_watch.py <sub>` (and `<sub> <subsub>` for yardstick and gate, also when written bare as
    `` `gate status` ``) must be one the CLI's parser has. Only names are checked, not flags."""
    top, nested = _watch_commands()
    assert {"yardstick", "gate"} <= set(nested)
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW, DEPLOY):
        text = _read(path)
        found = re.findall(r"jensen_watch\.py\"?\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?", text)
        assert found, path
        for sub, second in found:
            assert sub in top, (path, sub)
            if sub in nested:
                assert second in nested[sub], (path, sub, second)
        for sub, second in re.findall(r"`(gate|yardstick) ([a-z][a-z-]*)", text):
            assert second in nested[sub], (path, sub, second)


def test_no_instruction_dispatches_a_gate():
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW, DEPLOY, DESIGN):
        text = _read(path)
        assert "target=gate" not in text and "target` `gate`" not in text, path
    for path in (PROJECT, CHARTER, WATCH):
        assert "gh workflow run" not in _read(path), path
        _assert_anchors(path, (GATE_HANDOFF,))


def test_the_failed_gate_reads_the_last_gate():
    section = _flat(CHARTER).split("## The failed gate", 1)[1].split(" ## ", 1)[0]
    assert "jensen_watch.py gate status --last" in section
    assert "The workflow has already ended the gate and removed `gating`" in section
    assert "gate status --last" in _read(".github/workflows/improve-jensen.yml")


def test_one_rule_for_the_deploy_a_merge_belongs_to():
    _assert_anchors(CHARTER, DEPLOY_OF_A_MERGE)
    _assert_anchors(WATCH, DEPLOY_OF_A_MERGE)
    assert "the newest `\"result\": \"ok\"` record" not in _flat(CHARTER)


def test_watch_follows_every_pending_deploy_and_runs_its_live_test_first():
    section = _flat(WATCH).split("## Pending deploys", 1)[1].split(" ## ", 1)[0]
    for anchor in ("'\"deploy pending\" in:comments'", "a respond run's", "a stage of an improvement project",
                   "live behaviour test", "Run it first", "Deploy check failed at <merge sha>: picked to revert.",
                   "start no gate", "close a respond run's issue", GATE_HANDOFF):
        assert anchor in section, anchor
    assert section.index("Run it first") < section.index(GATE_HANDOFF)
    _assert_anchors(CHARTER, ("The PR body names the test's file",))


def test_watch_never_closes_ranked_or_followed_issues_as_stale():
    _assert_anchors(WATCH, ("Never close one labelled `priority`, `project` or `fix-deployed` this way",))


def test_finished_and_blocked_projects():
    _assert_anchors(PROJECT, (
        "gh issue edit <N> --remove-label project", "An issue with `fix-deployed` stays open",
        "doesn't dispatch the project again while it has `needs-person` or `stuck`",
    ))
    _assert_anchors(ASSESS, (
        "none of `fix-deployed`, `gating`, `stuck` or `needs-person`",
        "While its issue has `stuck` or `needs-person` it waits for a person: `{\"project\": null}`",
    ))


def test_staged_projects_wait_for_their_gate():
    _assert_anchors(PROJECT, (
        "**A stage that isn't the last** is still gated", "Stage <k> of <n>.", "without `fix-deployed` either way",
        "**The last stage**, or a project in one PR, gets `fix-deployed`", "jensen_watch.py gate status --last",
        "go on only once that stage is deployed and its gate has ended with result `passed`",
        "leave progress and end the run", "After 4 hours, start no new build step",
    ))
    assert "After 5 hours" not in _flat(PROJECT)


def test_assessment_records_evidence_on_the_issues_it_files():
    _assert_anchors(ASSESS, (
        "python scripts/jensen_watch.py note <N> <ref> \"<line>\"", "`yardstick/<task id>`", "`feedback/<name>`",
        "Set its `Seen:` line to the total the last `note` printed",
    ))


def test_assessment_grows_the_yardstick_without_duplicates():
    _assert_anchors(ASSESS, (
        'ls "$STATE/yardstick/tasks/" "$STATE/yardstick/retired/"', "`task.toml` for its prompt",
        "the yardstick task that covers it: its id, or `none`", "At most 2 new tasks per assessment",
        "pick a new id only for a genuinely different failure",
    ))
    assert "pick another." not in _flat(ASSESS)


def test_assessment_window_starts_when_it_starts_reading():
    _assert_anchors(ASSESS, (
        "date -u +%Y-%m-%dT%H:%M:%SZ", "-newermt \"<the previous Assessed: time>\"",
        "`Assessed: <the time you noted before reading>`",
    ))
    assert '-newer "$STATE/assessment.md"' not in _flat(ASSESS)


def test_priority_is_exactly_the_top_three():
    _assert_anchors(ASSESS, (
        "**`priority` on exactly the top 3 fix-scale items**", "keep the issue that already has `priority`",
        "so exactly those three have it afterwards",
    ))


def test_issue_queries_split_open_and_recently_closed():
    text = _flat(ASSESS)
    assert "--state all" not in text
    assert "gh issue list --label jensen-watch --state open" in text
    assert "gh issue list --label jensen-watch --state closed --search \"closed:>=" in text


def test_deploy_doc_assess_skip_rule():
    _assert_anchors(DEPLOY, ("An assessment with nothing new since the last one, and no project waiting, starts no "
                             "Claude run",))


def test_slack_stays_limited_to_the_admin_dm():
    # Only the charter describes the one Slack DM; the assessment and project runs never post there.
    for path in (ASSESS, PROJECT):
        text = _read(path)
        assert "chat.postMessage" not in text, path
        assert "Post nothing in Slack" in text, path
