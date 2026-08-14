#!/usr/bin/env python3
"""Run the project-wide lints registered under ``scripts/lints/``.

Modes:

  python scripts/lints/run.py                 # check; exit 1 on gating findings
  python scripts/lints/run.py --fix           # run fixers, print changed files
  python scripts/lints/run.py --list          # list registered lints
  python scripts/lints/run.py --print-fix-globs   # union of fix_touches globs

Gating: a finding fails the run only when its lint is ``blocking`` AND
the finding is not fixable AND the lint does not need the network.
Fixable findings are advisory at PR time because the autofix bot
resolves them on merge to main; network lints are advisory because a
registry blip must never block a merge.

Output options:

  --format text            human-readable (default)
  --format github          adds ::error/::warning workflow annotations
  --format review-status   JSON array in the review_status contract
                           consumed by scripts/ci/assemble_review_comment.py
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lints import REPO_ROOT, Finding, Lint, discover  # noqa: E402

# The `source` string for review_status entries. The comment assembler
# excludes the matching job from its synthesized error list, keyed by
# normalized-substring match against the workflow job name (see
# scripts/ci/emit_review_status.py for the rules).
REVIEW_SOURCE = "project-lints"


def gates(lint: Lint, finding: Finding) -> bool:
    """True when this finding should fail the check run."""
    if lint.severity != "blocking":
        return False
    if lint.network:
        return False
    if lint.autofix and finding.fixable:
        return False
    return True


def run_checks(lints: list[Lint]) -> tuple[list[tuple[Lint, Finding]], list[str]]:
    """Run every lint's check. Returns (results, crashed_lint_errors).

    A lint that raises is reported as a gating failure of the run itself
    rather than aborting the remaining lints — one broken lint must not
    hide every other lint's findings.
    """
    results: list[tuple[Lint, Finding]] = []
    errors: list[str] = []
    for lint in lints:
        try:
            for finding in lint.check():
                results.append((lint, finding))
        except Exception as exc:  # noqa: BLE001 — isolate lint crashes
            errors.append(f"{lint.id}: check crashed: {exc!r}")
    return results, errors


def run_fixes(
    lints: list[Lint],
    dirty_paths_fn: Callable[[], set[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """Run every fixer. Returns (changed_paths, errors).

    Each fixer is bounded by ITS OWN ``fix_touches`` globs — never the
    union across lints. The working tree is snapshotted around every
    fixer, so changes are attributed to the lint that made them even
    when the fixer under-reports its path list: lint A touching a file
    that only lint B's globs allow is still an error.
    """
    if dirty_paths_fn is None:
        dirty_paths_fn = _git_dirty_paths
    snapshot: Callable[[], set[str]] = dirty_paths_fn
    changed: list[str] = []
    errors: list[str] = []
    # Pre-existing working-tree changes (dev edits, npm churn) are not
    # any fixer's doing — exclude them from attribution.
    baseline = snapshot()
    for lint in lints:
        if not lint.autofix or lint.fix is None:
            continue
        try:
            reported = lint.fix()
        except Exception as exc:  # noqa: BLE001 — isolate fixer crashes
            errors.append(f"{lint.id}: fix crashed: {exc!r}")
            continue
        now_dirty = dirty_paths_fn()
        touched = set(reported) | (now_dirty - baseline)
        for path in sorted(touched):
            if not any(fnmatch.fnmatch(path, glob) for glob in lint.fix_touches):
                errors.append(
                    f"{lint.id}: fix modified {path!r} outside its declared "
                    f"fix_touches globs {list(lint.fix_touches)}"
                )
            changed.append(path)
        # Advance the baseline so the next fixer only answers for its
        # own changes.
        baseline = now_dirty
    return changed, errors


def _git_dirty_paths() -> set[str]:
    """Working-tree paths with any change, including untracked files
    (a fixer that *creates* a file must answer for it too)."""
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    paths: set[str] = set()
    for line in out.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        # Rename entries are "old -> new"; the new path is the change.
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip().strip('"')
        paths.add(path)
    return paths


def to_review_status(
    results: list[tuple[Lint, Finding]], errors: list[str]
) -> list[dict]:
    """Render findings in the review_status contract."""
    entries: list[dict] = []
    for lint, finding in results:
        gating = gates(lint, finding)
        will_autofix = lint.autofix and finding.fixable
        summary = f"`{finding.location()}` — {finding.message}"
        if will_autofix:
            summary += " (auto-fixed on merge to main)"
        entries.append(
            {
                "kind": "action_required" if gating else "warning",
                "title": f"lint: {lint.id}",
                "summary": summary,
                "how_to_fix": (
                    f"Run `python scripts/lints/run.py --only {lint.id} --fix`"
                    if will_autofix
                    else f"See `python scripts/lints/run.py --list` for {lint.id}."
                ),
            }
        )
    for err in errors:
        entries.append(
            {
                "kind": "action_required",
                "title": "lint runner error",
                "summary": err,
            }
        )
    if not entries:
        return []
    return [{"source": REVIEW_SOURCE, "results": entries}]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run the project-wide lints registered under scripts/lints/."
    )
    parser.add_argument("--fix", action="store_true", help="run fixers in place")
    parser.add_argument("--only", action="append", help="run only these lint ids")
    parser.add_argument(
        "--skip-network", action="store_true", help="skip lints that need the network"
    )
    parser.add_argument("--list", action="store_true", help="list lints and exit")
    parser.add_argument(
        "--print-fix-globs",
        action="store_true",
        help="print the union of fix_touches globs (one per line) and exit",
    )
    parser.add_argument(
        "--format",
        choices=("text", "github", "review-status"),
        default="text",
    )
    parser.add_argument(
        "--review-status-out",
        type=Path,
        help="also write review_status JSON to this file (any --format)",
    )
    args = parser.parse_args(argv)

    lints = discover()

    if args.only:
        known = {lint.id for lint in lints}
        unknown = [lint_id for lint_id in args.only if lint_id not in known]
        if unknown:
            print(f"unknown lint id(s): {unknown}; known: {sorted(known)}", file=sys.stderr)
            return 2
        lints = [lint for lint in lints if lint.id in args.only]

    if args.skip_network:
        lints = [lint for lint in lints if not lint.network]

    if args.list:
        for lint in lints:
            flags = [lint.severity]
            if lint.autofix:
                flags.append("autofix")
            if lint.network:
                flags.append("network")
            print(f"{lint.id:32} [{', '.join(flags)}] {lint.description}")
        return 0

    if args.print_fix_globs:
        globs: list[str] = []
        for lint in lints:
            for glob in lint.fix_touches:
                if glob not in globs:
                    globs.append(glob)
        for glob in globs:
            print(glob)
        return 0

    if args.fix:
        # run_fixes snapshots the working tree around each fixer and
        # validates every change against that lint's OWN fix_touches
        # globs — under-reported paths and cross-lint writes (lint A
        # touching a file only lint B's globs allow) both error.
        changed, errors = run_fixes(lints)
        for path in changed:
            print(f"fixed: {path}")
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1 if errors else 0

    results, errors = run_checks(lints)

    review_status = to_review_status(results, errors)
    if args.review_status_out:
        args.review_status_out.write_text(
            json.dumps(review_status), encoding="utf-8"
        )

    if args.format == "review-status":
        print(json.dumps(review_status))
    else:
        for lint, finding in results:
            gating = gates(lint, finding)
            level = "error" if gating else "warning"
            note = ""
            if lint.autofix and finding.fixable:
                note = " [auto-fixed on merge]"
            if args.format == "github":
                loc = ""
                if finding.path:
                    loc = f" file={finding.path}"
                    if finding.line:
                        loc += f",line={finding.line}"
                print(f"::{level}{loc}::[{lint.id}] {finding.message}{note}")
            print(f"{level}: [{lint.id}] {finding.location()}: {finding.message}{note}")
        for err in errors:
            if args.format == "github":
                print(f"::error::{err}")
            print(f"error: {err}", file=sys.stderr)

    gating_count = sum(1 for lint, finding in results if gates(lint, finding))
    advisory_count = len(results) - gating_count
    print(
        f"\n{len(lints)} lint(s) ran: {gating_count} blocking finding(s), "
        f"{advisory_count} advisory, {len(errors)} runner error(s)."
    )
    return 1 if (gating_count or errors) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
