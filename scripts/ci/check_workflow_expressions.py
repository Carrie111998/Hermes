#!/usr/bin/env python3
"""Reject workflow files GitHub cannot parse.

A syntax break in ``.github/workflows/ci.yaml`` is invisible to the pull
request that introduces it.  When GitHub cannot parse a workflow file it
never creates a run for it, so the ``CI`` check does not turn red — it simply
stops existing, and a required-check gate has nothing to block on.  Every
*other* pull request then inherits the broken file from ``main`` and fails at
parse time.  That is exactly how ``24f5a60`` ("fix: re-disable e2e") shipped
an unclosed ``${{`` and took the whole repository's CI down (#100748).

Thirty-one of the thirty-two workflow files are ``workflow_call`` targets that
``ci.yaml`` invokes, so none of them can police it.  This check therefore runs
from a workflow that owns its own trigger and survives ``ci.yaml`` being
unparseable.

Two failure classes are covered, both of which produce the same silence:

* the file is not valid YAML;
* a ``${{`` expression is never closed by a ``}}`` inside its own YAML scalar,
  which is the unit GitHub's expression parser works on.  Scoping to the
  scalar — rather than to the line or the whole file — is what makes a legal
  multi-line expression in a folded scalar pass while ``'true' })}`` fails.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml

_WORKFLOW_SUFFIXES = (".yml", ".yaml")
_OPEN = "${{"
_CLOSE = "}}"


@dataclass(frozen=True)
class Problem:
    """One rejected workflow file location."""

    path: Path
    line: int
    message: str


def iter_workflow_files(root: Path) -> list[Path]:
    """Return the workflow files under ``root``, sorted by path."""
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    return sorted(
        path
        for path in workflows.iterdir()
        if path.is_file() and path.suffix in _WORKFLOW_SUFFIXES
    )


def _iter_scalars(node: yaml.Node | None) -> Iterator[yaml.ScalarNode]:
    if isinstance(node, yaml.ScalarNode):
        yield node
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            yield from _iter_scalars(child)
    elif isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            yield from _iter_scalars(key)
            yield from _iter_scalars(value)


def check_source(path: Path, text: str) -> list[Problem]:
    """Return every parse problem in one workflow file's source."""
    try:
        root_node = yaml.compose(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = mark.line + 1 if mark is not None else 1
        reason = getattr(exc, "problem", None) or str(exc).splitlines()[0]
        return [Problem(path, line, f"not valid YAML: {reason}")]

    problems: list[Problem] = []
    for scalar in _iter_scalars(root_node):
        value = scalar.value
        cursor = 0
        while True:
            start = value.find(_OPEN, cursor)
            if start < 0:
                break
            end = value.find(_CLOSE, start + len(_OPEN))
            if end < 0:
                line = scalar.start_mark.line + 1 + value.count("\n", 0, start)
                problems.append(
                    Problem(
                        path,
                        line,
                        "the expression is not closed: an unescaped ${{ "
                        "sequence was found, but the closing }} sequence was "
                        "not found",
                    )
                )
                break
            cursor = end + len(_CLOSE)
    return problems


def check_workflows(root: Path) -> list[Problem]:
    """Return every parse problem across the checkout's workflow files."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")

    problems: list[Problem] = []
    for path in iter_workflow_files(root):
        problems.extend(
            check_source(path.relative_to(root), path.read_text(encoding="utf-8"))
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject workflow files GitHub cannot parse."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root to inspect (default: current directory)",
    )
    args = parser.parse_args(argv)

    try:
        problems = check_workflows(args.root)
    except ValueError as exc:
        parser.error(str(exc))

    if not problems:
        count = len(iter_workflow_files(args.root.resolve()))
        print(f"All {count} workflow files parse.")
        return 0

    for problem in problems:
        posix = problem.path.as_posix()
        print(
            f"::error file={posix},line={problem.line}::{problem.message}",
            file=sys.stdout,
        )
        print(f"  {posix}:{problem.line}: {problem.message}")
    print(
        "GitHub creates no run at all for an unparseable workflow, so this "
        "would not have shown up as a failing check on this pull request."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
