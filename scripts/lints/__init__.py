"""Project-wide lint registry.

A *lint* is a named repo-hygiene check with two explicit axes:

- ``severity`` — ``blocking`` findings fail the PR gate; ``advisory``
  findings only surface as warnings in the CI review comment.
- ``autofix`` — the lint ships a ``fix()`` that produces deterministic
  edits, applied by the autofix bot on push to main.

The severity policy (one rule, generalized from how eslint already works
in this repo):

1. blocking + not fixable        -> fails the PR gate
2. blocking + fixable            -> advisory at PR time; the autofix bot
                                    fixes it on merge to main
3. advisory                      -> warning, never fails anything
4. ``network=True``              -> always advisory on PRs (registry/API
                                    blips must never block a merge); the
                                    enforcement point is the autofix pass
                                    on main

Lints live as modules in this directory, one per file, each exposing a
top-level ``LINT``. Discovery is automatic — there is no per-lint CI
wiring to forget (the class of bug where a checker script exists but no
workflow runs it).

Run them with ``python scripts/lints/run.py`` (see ``--help``).
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]

_SEVERITIES = ("blocking", "advisory")

# Modules in this directory that are not lints.
_NON_LINT_MODULES = {"__init__.py", "run.py"}


@dataclass(frozen=True)
class Finding:
    """One lint violation.

    ``fixable`` is per-finding: a lint with ``autofix=True`` may still
    emit findings its fixer cannot resolve (those gate like unfixable
    ones when the lint is blocking).
    """

    lint_id: str
    message: str
    path: str = ""  # repo-relative; empty for repo-wide findings
    line: int | None = None
    fixable: bool = False

    def location(self) -> str:
        if not self.path:
            return "(repo)"
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass(frozen=True)
class Lint:
    """A registered lint.

    ``check`` returns the current findings; ``fix`` (when ``autofix``)
    edits files in place and returns the repo-relative paths it changed.
    ``fix_touches`` declares the path globs a fix may modify — the
    autofix workflow's patch guard is derived from the union of these,
    so a fixer that writes outside its declared globs is rejected before
    anything reaches the bot branch.
    """

    id: str
    description: str
    severity: str
    autofix: bool
    check: Callable[[], list[Finding]]
    fix: Callable[[], list[str]] | None = None
    network: bool = False
    fix_touches: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITIES:
            raise ValueError(
                f"lint {self.id!r}: severity must be one of {_SEVERITIES}, "
                f"got {self.severity!r}"
            )
        if self.autofix and self.fix is None:
            raise ValueError(f"lint {self.id!r}: autofix=True requires a fix callable")
        if self.autofix and not self.fix_touches:
            raise ValueError(
                f"lint {self.id!r}: autofix=True requires fix_touches globs "
                "(the autofix patch guard is derived from them)"
            )
        if not self.autofix and self.fix is not None:
            raise ValueError(f"lint {self.id!r}: fix provided but autofix=False")


def _ensure_importable() -> None:
    """Make ``import lints`` work regardless of how the caller started.

    Lint modules import their base types as ``from lints import ...``,
    so the ``scripts/`` directory must be on ``sys.path`` before any
    lint module executes.
    """
    scripts_dir = str(Path(__file__).resolve().parents[1])
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


def discover(directory: Path | None = None) -> list[Lint]:
    """Load every lint module and return the registered lints.

    A module in the lint directory that fails to import or does not
    define ``LINT`` is a hard error, never a skip — a lint that silently
    drops out of the run is exactly the failure mode this registry
    exists to prevent.
    """
    _ensure_importable()
    directory = Path(directory) if directory else Path(__file__).parent

    lints: list[Lint] = []
    for path in sorted(directory.glob("*.py")):
        if path.name in _NON_LINT_MODULES or path.name.startswith("_"):
            continue
        module_name = f"_hermes_lint_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load lint module {path}")
        module = importlib.util.module_from_spec(spec)
        # Register before exec: @dataclass resolves string annotations by
        # looking the module up in sys.modules — an unregistered module
        # crashes _is_type with AttributeError on None.
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        lint = getattr(module, "LINT", None)
        if lint is None:
            raise RuntimeError(f"lint module {path} defines no top-level LINT")
        if not isinstance(lint, Lint):
            raise RuntimeError(f"lint module {path}: LINT is not a Lint instance")
        lints.append(lint)

    seen: dict[str, int] = {}
    for lint in lints:
        seen[lint.id] = seen.get(lint.id, 0) + 1
    dupes = [lint_id for lint_id, n in seen.items() if n > 1]
    if dupes:
        raise RuntimeError(f"duplicate lint ids: {dupes}")
    return lints
