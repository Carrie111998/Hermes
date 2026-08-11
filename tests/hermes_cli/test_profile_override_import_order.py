"""Guard the real precondition behind ``_apply_profile_override()``.

``hermes_cli/main.py`` imports ~39 ``hermes_cli.subcommands.*`` parser builders
at module level *before* it calls ``_apply_profile_override()``.  That is safe
today only because those modules are pure ``argparse`` wiring: their sole
repo-owned module-level import is ``hermes_cli.subcommands._shared`` (itself
``argparse``-only), and every handler-side dependency is imported lazily inside
the ``cmd_*`` bodies.  So nothing that resolves ``HERMES_HOME`` is imported
before the override runs, and no module-level constant can snapshot the
pre-override value.

That property is a convention, not a language guarantee.  A single new
top-level ``from hermes_cli.config import ...`` in any subcommand module would
pull the ``HERMES_HOME``-resolving layer in ahead of the override and silently
re-open the profile-isolation bug the override exists to prevent.  These tests
pin the convention so the breakage surfaces as a test failure instead of as
misrouted profile state.

Static analysis on purpose: importing the modules to inspect them would itself
perturb the import order under test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "hermes_cli" / "main.py"
SUBCOMMANDS_DIR = REPO_ROOT / "hermes_cli" / "subcommands"

# The only repo-owned module a parser builder may import at module level.
ALLOWED_SUBCOMMAND_IMPORT = "hermes_cli.subcommands._shared"

# Top-level names main.py may import before the override call.  ``hermes_cli``
# is admitted only as ``hermes_cli.subcommands.*`` (asserted separately).
ALLOWED_PRE_OVERRIDE_ROOTS = {"hermes_bootstrap", "hermes_cli"}


def _is_repo_owned(module: str) -> bool:
    """True if ``module``'s top-level name is a package/module in this repo."""
    top = module.split(".", 1)[0]
    return (REPO_ROOT / top / "__init__.py").is_file() or (
        REPO_ROOT / f"{top}.py"
    ).is_file()


def _module_level_imports(tree: ast.Module, *, package: str) -> list[tuple[str, int]]:
    """Return ``(dotted_module, lineno)`` for each top-level import statement."""
    out: list[tuple[str, int]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            if node.level:  # relative import -> resolve against the package
                out.append((f"{package}.{node.module}" if node.module else package,
                            node.lineno))
            elif node.module:
                out.append((node.module, node.lineno))
    return out


def _pre_override_imports() -> list[tuple[str, int]]:
    """Module-level imports in main.py that execute before the override call."""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))

    call_lineno = None
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_apply_profile_override"
        ):
            call_lineno = node.lineno
            break

    assert call_lineno is not None, (
        "No module-level `_apply_profile_override()` call found in main.py. "
        "If the call moved or was renamed, update this guard -- do not delete it."
    )

    return [
        (mod, lineno)
        for mod, lineno in _module_level_imports(tree, package="hermes_cli")
        if lineno < call_lineno
    ]


def test_pre_override_imports_are_only_subcommand_parser_builders():
    """Only import-light parser builders may load before the override runs."""
    offenders = []
    for module, lineno in _pre_override_imports():
        if not _is_repo_owned(module):
            continue  # stdlib / third-party never reads HERMES_HOME
        top = module.split(".", 1)[0]
        if top not in ALLOWED_PRE_OVERRIDE_ROOTS:
            offenders.append((module, lineno))
        elif top == "hermes_cli" and not module.startswith("hermes_cli.subcommands"):
            offenders.append((module, lineno))

    assert not offenders, (
        "main.py imports repo module(s) before `_apply_profile_override()`:\n  "
        + "\n  ".join(f"line {ln}: {m}" for m, ln in sorted(offenders, key=lambda x: x[1]))
        + "\n\nAnything imported here loads before HERMES_HOME is resolved, so any "
        "module-level constant it computes snapshots the pre-profile value. Either "
        "import it lazily inside the handler, or move the override call above it."
    )


@pytest.mark.parametrize(
    "path",
    sorted(p for p in SUBCOMMANDS_DIR.glob("*.py") if p.name != "__init__.py"),
    ids=lambda p: p.stem,
)
def test_subcommand_modules_import_nothing_heavy_at_module_level(path: Path):
    """Parser builders must defer every repo-owned import into the handler.

    This is what keeps the pre-override import closure free of
    ``HERMES_HOME``-resolving code, even though the closure is ~39 modules wide.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = [
        (mod, lineno)
        for mod, lineno in _module_level_imports(tree, package="hermes_cli.subcommands")
        if _is_repo_owned(mod) and mod != ALLOWED_SUBCOMMAND_IMPORT
    ]

    assert not offenders, (
        f"{path.name} imports repo module(s) at module level:\n  "
        + "\n  ".join(f"line {ln}: {m}" for m, ln in sorted(offenders, key=lambda x: x[1]))
        + f"\n\nEvery {SUBCOMMANDS_DIR.name} module is imported by main.py *before* "
        "`_apply_profile_override()` resolves HERMES_HOME. Move this import inside "
        f"the function that needs it. Only {ALLOWED_SUBCOMMAND_IMPORT} (argparse-only) "
        "is allowed here."
    )
