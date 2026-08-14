"""No test module may define the same name twice in one scope.

Python keeps only the last definition, so a duplicate silently deletes
the first one. When the shadowed name is a test, its coverage disappears
with no error and no skip; when it is an ``autouse`` fixture, the whole
module quietly switches to the newer one's isolation rules. Both have
happened here (see the original module docstring in git history:
``tests/test_no_shadowed_test_definitions.py``).

Migrated from that test to the lint registry: same AST scan, but
per-file ``::error`` annotations instead of one aggregated assert.
"""

from __future__ import annotations

import ast
from pathlib import Path

from lints import REPO_ROOT, Finding, Lint

TESTS_ROOT = REPO_ROOT / "tests"

# Decorators that legitimately repeat a name in one scope.
REDEFINING_DECORATORS = ("overload", "setter", "getter", "deleter", "register")

# Throwaway callbacks conventionally named `_` are not shadowing bugs.
ALLOWED_REPEATS = {"_"}


def _decorator_names(node: ast.AST) -> list[str]:
    out = []
    for dec in getattr(node, "decorator_list", []):
        try:
            out.append(ast.unparse(dec))
        except Exception:  # pragma: no cover - defensive
            pass
    return out


def duplicates_in(body, scope: str) -> list[tuple[int, str]]:
    """``(lineno, message)`` for every shadowing definition in a scope."""
    seen: dict[str, int] = {}
    problems: list[tuple[int, str]] = []
    for node in body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in ALLOWED_REPEATS:
            continue
        if any(
            marker in dec
            for dec in _decorator_names(node)
            for marker in REDEFINING_DECORATORS
        ):
            seen[node.name] = node.lineno
            continue
        if node.name in seen:
            problems.append(
                (
                    node.lineno,
                    f"{scope}.{node.name}() shadows the definition at line "
                    f"{seen[node.name]} — Python keeps only the last one, so "
                    "the earlier test/fixture silently disappears",
                )
            )
        seen[node.name] = node.lineno
    return problems


def scan_file(path: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
        return []
    problems = duplicates_in(tree.body, "<module>")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            problems += duplicates_in(node.body, node.name)
    return problems


def check() -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, message in scan_file(path):
            findings.append(
                Finding(
                    lint_id="no-shadowed-test-definitions",
                    path=rel,
                    line=lineno,
                    message=message,
                )
            )
    return findings


LINT = Lint(
    id="no-shadowed-test-definitions",
    description=(
        "duplicate def names in a test module scope silently delete the "
        "earlier test or fixture."
    ),
    severity="blocking",
    autofix=False,
    check=check,
)
