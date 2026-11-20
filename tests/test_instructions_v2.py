"""improve-jensen's instruction files are its behaviour (loaded with `--append-system-prompt-file`). Four things
about them are checked mechanically: every watch CLI command they name exists, each one carries the public-repo
rule word for word, none dispatches a run itself, and the proposals they write skip issues already on their way
through Slack approval."""

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
SPEC = "docs/superpowers/specs/2026-11-18-improve-jensen-watch-design.md"

PUBLIC_REPO_RULE = """\
- **This repo is public.** improve-jensen's issues, PRs, commits and workflow logs never hold:
  - secrets;
  - conversation text;
  - people's names;
  - details of products, customers or their code (file paths, vulnerabilities).

  The evidence stays on the host."""


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def _flat(path: str) -> str:
    return " ".join(_read(path).split())


def test_instructions_name_only_real_watch_subcommands():
    """Every `jensen_watch.py <sub>` must be one the CLI's parser has. Only names are checked, not flags. `gh` is
    handled before argparse (`cli.main`), so it's added by hand."""
    names = set(_subcommands(_build_parser())) | {"gh"}
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW, DEPLOY):
        found = re.findall(r"jensen_watch\.py\"?\s+([a-z][a-z-]*)", _read(path))
        assert found, path
        for sub in found:
            assert sub in names, (path, sub)


def test_every_instruction_file_carries_the_public_repo_rule_verbatim():
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW):
        assert PUBLIC_REPO_RULE in _read(path), path


def test_no_instruction_dispatches_a_run():
    """Runs start from the workflow's own triggers and the approvals job; nothing dispatches one (the job token
    can't)."""
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW, DEPLOY, DESIGN):
        assert "target=gate" not in _read(path), path
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW):
        assert "gh workflow run" not in _read(path), path


def test_nothing_measures_a_change_after_it_ships():
    """A person approves a change; beyond the deploy's own check nothing follows it up. No instruction or doc names
    the old after-deploy measuring or its labels."""
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW, DEPLOY, SPEC):
        text = _read(path).lower()
        for gone in ("yardstick", "gate start", "gate status", "gate end", "gating", "fix-deployed", "`priority`",
                     "attempt failed", "live behaviour test"):
            assert gone not in text, (path, gone)
    section = _read(DESIGN).split("## improve-jensen", 1)[1].split("\n## ", 1)[0].lower()
    for gone in ("yardstick", "gate", "fix-deployed", "priority"):
        assert gone not in section, gone


def test_proposals_are_few_fresh_and_one_per_problem():
    watch = _flat(WATCH)
    assert "You propose nothing" in watch
    assess = _flat(ASSESS)
    assert "Proposing nothing is the normal outcome." in assess
    assert "One item per problem." in assess
    assert "Folded into #<kept>." in assess
    assert ("whose issues have none of `proposed`, `approved`, `rejected`, `stuck`, `needs-person` or `fixing`") in assess
    assert "under 100 characters saying what will change in Jensen" in assess
    assert "it is also posted on the public issue" in assess


def test_the_workflow_posts_to_slack_not_the_runs():
    """The proposals and their thread replies come from the workflow's own steps; a run sends at most the charter's
    DM to the admins."""
    assert "Post nothing in Slack yourself, except the DM above for a failed deploy" in _flat(WATCH)
    assert "Post nothing in Slack yourself: the workflow posts your proposals." in _flat(ASSESS)
    assert "The Slack DM** is the only Slack message you send yourself" in _flat(CHARTER)
    assert "Post nothing in Slack." in _read(REVIEW)


def test_a_fix_ships_what_was_approved_and_closes_its_issue():
    charter = _flat(CHARTER)
    assert "Its `Proposed in Slack: <change>` comment says what was approved" in charter
    assert "nothing wider" in charter
    assert "A fix's PR body says `Closes #N`" in charter
    assert "reopen it (`gh issue reopen <N>`)" in charter


def test_the_assessment_asks_what_worked_and_credits_only_in_slack():
    """Next to what holds Jensen back, the assessment asks what worked. Its learned proposals carry a credit (a
    person's name) that only the Slack list shows; the hourly watch leaves learned issues to it."""
    assess = _flat(ASSESS)
    assert "what worked that Jensen should keep?" in assess
    assert "labelled `jensen-watch` and `learned`" in assess
    assert '"kind": "learned", "credit":' in assess
    assert "never put it in an issue, a comment or your final message" in assess
    watch = _flat(WATCH)
    assert "You propose nothing" in watch
    assert "Never close one labelled `project`, `proposed`, `approved` or `learned` this way." in watch
    charter = _flat(CHARTER)
    assert "src/agent/plugins/core/skills/<name>/SKILL.md" in charter
    assert "never in the issue, a PR or a commit" in charter
    assert "`rejected`, `learned`, `jensen-assessment`" in _flat(DEPLOY)


def test_the_assessment_checks_the_code_before_it_proposes():
    assess = _flat(ASSESS)
    assert "Before you propose: check it isn't already fixed" in assess
    assert "Propose only a PASS." in assess
    for check in ("Already handled in the code?", "Changed recently?", "Asked and answered?",
                  "Real and worth a person's time?"):
        assert check in assess
    assert "src/agent/defaults/system.md" in assess and "deploys.jsonl" in assess
