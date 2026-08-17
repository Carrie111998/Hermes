"""Locate the checkout this Hermes distribution was INSTALLED from.

``Path(__file__).parent.parent`` (``PROJECT_ROOT``) answers a different
question: which copy of the source tree the *current process* happens to be
executing. Those two answers diverge, and when they do, using the second one to
launch a long-lived process is a deployment bug.

2026-08-17, observed live: an agent session ran ``hermes gateway restart`` from
its worktree (``<checkout>/.claude/worktrees/<name>``). ``python -m
hermes_cli.main`` puts the caller's cwd on ``sys.path[0]``, and setuptools'
editable-install finder is *appended* to ``sys.meta_path`` — i.e. it sits behind
``PathFinder`` — so ``hermes_cli`` resolved from the worktree. ``PROJECT_ROOT``
became the worktree, the spawn handed the new gateway that root as its cwd and
``PYTHONPATH``, and the live gateway ran the worktree's commits (~10 behind
main) out of a directory the worktree reaper is free to delete. Nothing was
detectably wrong: a worktree is a real checkout, so the port owner, the 13
event-bus subscribers and ``/health`` all looked normal, and the documented
preflight (grep the shared checkout for the symbol the fix adds) returned a true
answer about a tree the process never imported.

The install record is the authority, in this order:

1. The editable-install finder's ``MAPPING`` (``pip install -e`` writes
   ``__editable___<dist>_finder.py`` with ``{package: absolute source dir}``).
   It cannot be shadowed by a cwd, which is exactly what makes it right here.
2. A ``sys.path`` scan that skips the cwd AND the copy we are running from —
   ``hermes_cli/main.py`` itself does ``sys.path.insert(0, PROJECT_ROOT)``, so a
   worktree is on ``sys.path`` explicitly, not only via the cwd. This is the
   non-editable (site-packages) install case.
3. Nothing — no install record. ``find_installed_package_root()`` says so with
   ``None`` so callers holding a better stable anchor (the Windows task
   script's ``cd /d``) can prefer it; ``installed_package_root()`` falls back to
   the running copy, which is all a from-source run ever had.

Deliberately import-light and un-cached: it is called at spawn time (a handful
of ``stat`` calls), and a cache would outlive the ``sys.path`` it was computed
from.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The distribution's anchor package. Its parent directory is the install root.
PACKAGE = "hermes_cli"


def _running_package_root() -> Path:
    """Where the ``hermes_cli`` executing right now was loaded from."""
    return Path(__file__).resolve().parent.parent


def _holds_package(root: Path) -> bool:
    """True when ``root`` is a directory containing the ``hermes_cli`` package."""
    try:
        return (root / PACKAGE / "__init__.py").is_file()
    except OSError:
        return False


def _path_key(path: Path) -> str | None:
    """Case-folded absolute key for identity comparison, or None if unusable."""
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return None


def _finder_mapping(finder: object) -> dict | None:
    """The ``{package: source dir}`` mapping a meta-path finder was built with.

    setuptools' generated ``_EditableFinder`` is a class whose ``find_spec``
    reads a MODULE-level ``MAPPING`` global — it is not an attribute of the
    class, so ``getattr(finder, "MAPPING")`` finds nothing. Look in the defining
    module too. (Getting this wrong is silent: resolution just falls through to
    the running copy, which is the bug being fixed.)
    """
    mapping = getattr(finder, "MAPPING", None)
    if isinstance(mapping, dict):
        return mapping
    module = sys.modules.get(getattr(finder, "__module__", "") or "")
    mapping = getattr(module, "MAPPING", None)
    return mapping if isinstance(mapping, dict) else None


def _editable_mapping_roots() -> list[Path]:
    """Install roots recorded by editable-install finders on ``sys.meta_path``."""
    roots: list[Path] = []
    for finder in list(sys.meta_path):
        mapping = _finder_mapping(finder)
        if mapping is None:
            continue
        target = mapping.get(PACKAGE)
        if isinstance(target, str) and target:
            # The mapping names the package directory; we want its parent, and
            # we keep the recorded spelling (a junctioned install should still
            # identify itself by the path it was installed under).
            roots.append(Path(target).parent)
    return roots


def _sys_path_roots(skip: set[str]) -> list[Path]:
    """``sys.path`` entries that could host an install, minus ``skip`` keys.

    ``''`` and ``.`` mean "the caller's cwd" — the hazard itself — so they are
    dropped unconditionally rather than resolved.
    """
    roots: list[Path] = []
    for entry in list(sys.path):
        if not entry or entry in {".", os.curdir}:
            continue
        candidate = Path(entry)
        key = _path_key(candidate)
        if key is None or key in skip:
            continue
        roots.append(candidate)
    return roots


def _cwd() -> Path | None:
    """``Path.cwd()`` without raising when the cwd has been deleted."""
    try:
        return Path.cwd()
    except OSError:
        return None


def find_installed_package_root() -> Path | None:
    """Return the recorded install root, or None when there is no record.

    None is meaningful: it says "this process is not running an installed copy",
    not "resolution failed".
    """
    for root in _editable_mapping_roots():
        if _holds_package(root):
            return root

    cwd = _cwd()
    skip = {
        key
        for key in (
            _path_key(cwd) if cwd is not None else None,
            _path_key(_running_package_root()),
        )
        if key is not None
    }
    for root in _sys_path_roots(skip):
        if _holds_package(root):
            return root
    return None


def installed_package_root() -> Path:
    """Return the best root to run this distribution from — never the cwd.

    Use this for anything handed to a child process (``cwd=``, ``PYTHONPATH``,
    a generated launcher script). Falls back to the running copy when there is
    no install record at all.
    """
    return find_installed_package_root() or _running_package_root()
