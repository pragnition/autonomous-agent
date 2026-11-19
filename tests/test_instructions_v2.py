"""improve-jensen's instruction files are its behaviour (loaded with `--append-system-prompt-file`). Three things
about them are checked mechanically: every watch CLI command they name exists, each one carries the public-repo
rule word for word, and none dispatches a gate itself."""

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


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _read(path: str) -> str:
    return (ROOT / path).read_text()


def test_instructions_name_only_real_watch_subcommands():
    """Every `jensen_watch.py <sub>` (and `<sub> <subsub>` for yardstick and gate, also when written bare as
    `` `gate status` ``) must be one the CLI's parser has. Only names are checked, not flags. `gh` is handled
    before argparse (`cli.main`), so it's added by hand."""
    top = _subcommands(_build_parser())
    nested = {name: set(_subcommands(sub)) for name, sub in top.items() if _subcommands(sub)}
    names = set(top) | {"gh"}
    assert {"yardstick", "gate"} <= set(nested)
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW, DEPLOY):
        text = _read(path)
        found = re.findall(r"jensen_watch\.py\"?\s+([a-z][a-z-]*)(?:\s+([a-z][a-z-]*))?", text)
        assert found, path
        for sub, second in found:
            assert sub in names, (path, sub)
            if sub in nested:
                assert second in nested[sub], (path, sub, second)
        for sub, second in re.findall(r"`(gate|yardstick) ([a-z][a-z-]*)", text):
            assert second in nested[sub], (path, sub, second)


def test_every_instruction_file_carries_the_public_repo_rule_verbatim():
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW):
        assert PUBLIC_REPO_RULE in _read(path), path


def test_no_instruction_dispatches_a_gate():
    """The workflow runs a gate after the job that started it ends; nothing dispatches one (the job token can't)."""
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW, DEPLOY, DESIGN):
        assert "target=gate" not in _read(path), path
    for path in (ASSESS, PROJECT, CHARTER, WATCH, REVIEW):
        assert "gh workflow run" not in _read(path), path
