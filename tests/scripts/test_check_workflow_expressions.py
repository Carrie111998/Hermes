"""Behavioral tests for the workflow-parse CI guard."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_workflow_expressions.py"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

from check_workflow_expressions import (  # noqa: E402
    check_source,
    check_workflows,
)

# The exact guard `24f5a60` ("fix: re-disable e2e") shipped. The trailing
# `})}` is one brace short, which left the `${{` open and took every CI run
# in the repository down at parse time (#100748).
BROKEN_GUARD = """\
name: CI
on:
  pull_request:
jobs:
  e2e-desktop:
    needs: detect
    if: ${{ false && (needs.detect.outputs.python_prod == 'true' || needs.detect.outputs.frontend == 'true' })}
    uses: ./.github/workflows/e2e-desktop.yml
"""

FIXED_GUARD = BROKEN_GUARD.replace("'true' })}", "'true') }}")


def _write_workflow(root: Path, name: str, text: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(text, encoding="utf-8")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_unclosed_expression_is_reported_at_its_own_line():
    problems = check_source(Path("ci.yaml"), BROKEN_GUARD)

    assert len(problems) == 1
    # GitHub reports "(Line: 126, Col: 9)" for this scalar in the real file;
    # here the same guard sits on line 7 of the fixture.
    assert problems[0].line == 7
    assert "the expression is not closed" in problems[0].message


def test_closing_the_expression_makes_the_file_pass():
    assert check_source(Path("ci.yaml"), FIXED_GUARD) == []


def test_multi_line_expression_in_a_folded_scalar_is_not_flagged():
    # Legal GitHub syntax: the expression spans lines inside one YAML scalar.
    # A line-by-line scan would reject this, which is why the check is scoped
    # to the scalar the expression actually lives in.
    source = """\
name: CI
on:
  push:
jobs:
  build:
    if: >-
      ${{ github.event_name == 'push'
          && github.ref == 'refs/heads/main' }}
    runs-on: ubuntu-latest
"""

    assert check_source(Path("ci.yaml"), source) == []


def test_escaped_braces_inside_format_are_not_flagged():
    source = """\
name: CI
on:
  push:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo "${{ format('{{0}} done', github.sha) }}"
"""

    assert check_source(Path("ci.yaml"), source) == []


def test_invalid_yaml_is_reported():
    problems = check_source(Path("ci.yaml"), "name: CI\non:\n  push:\n   - [unclosed\n")

    assert len(problems) == 1
    assert "not valid YAML" in problems[0].message


def test_directory_without_workflows_is_clean(tmp_path):
    assert check_workflows(tmp_path) == []


def test_cli_rejects_a_broken_workflow(tmp_path):
    _write_workflow(tmp_path, "ci.yaml", BROKEN_GUARD)

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "::error file=.github/workflows/ci.yaml,line=7::" in result.stdout


def test_cli_accepts_a_fixed_workflow(tmp_path):
    _write_workflow(tmp_path, "ci.yaml", FIXED_GUARD)

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "workflow files parse" in result.stdout


def test_every_workflow_in_this_repository_parses():
    problems = check_workflows(REPO_ROOT)

    assert problems == [], "\n".join(
        f"{problem.path.as_posix()}:{problem.line}: {problem.message}"
        for problem in problems
    )
