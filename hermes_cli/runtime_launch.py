"""Import-safe helpers for launching detached Hermes Python processes.

Some remote launchers start Hermes with a base Python interpreter and inject
the install venv's site-packages into ``sys.path`` before importing the CLI.
That makes ``sys.executable`` and the process environment incomplete proxies
for the runtime that is actually serving requests.  Detached children must
prefer the checkout's venv launcher and carry any non-interpreter import roots
forward explicitly.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from hermes_constants import venv_python_path


def _normalised_absolute(path: str | os.PathLike[str]) -> str | None:
    """Return a lexical absolute path, rejecting empty and relative entries.

    ``Path.resolve()`` is intentionally not used here.  A POSIX venv's
    ``bin/python`` is commonly a symlink to the base interpreter; resolving
    that symlink before launch loses the adjacent ``pyvenv.cfg`` and therefore
    loses the venv itself.
    """
    raw = os.fspath(path)
    if not raw or not os.path.isabs(raw):
        return None
    return os.path.normpath(raw)


def resolve_project_python(
    project_root: str | os.PathLike[str],
    *,
    current_executable: str | os.PathLike[str] | None = None,
) -> str:
    """Return the canonical checkout venv Python, or the current interpreter.

    Managed installs use ``venv``; ``.venv`` remains a developer-compatible
    fallback.  Both platform layouts are probed so tests and copied installs
    remain portable.  Candidate paths are returned without resolving symlinks
    because invoking the venv path itself is what activates a POSIX venv.
    """
    current = os.fspath(current_executable or sys.executable)
    root = Path(project_root)
    platform_order = (True, False) if os.name == "nt" else (False, True)

    for venv_name in ("venv", ".venv"):
        for windows in platform_order:
            candidate = venv_python_path(root / venv_name, windows=windows)
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue

            chosen = os.path.abspath(os.fspath(candidate))
            if os.path.normcase(os.path.abspath(current)) == os.path.normcase(chosen):
                return current
            return chosen

    return current


def _is_within(path: str, root: str) -> bool:
    """Lexically test containment without following symlinks or touching I/O."""
    try:
        return os.path.commonpath((os.path.normcase(path), os.path.normcase(root))) == os.path.normcase(root)
    except (OSError, ValueError):
        return False


def detached_python_env(
    base_env: Mapping[str, str] | None = None,
    *,
    runtime_paths: Iterable[str | os.PathLike[str]] | None = None,
    interpreter_prefixes: Iterable[str | os.PathLike[str]] | None = None,
) -> dict[str, str]:
    """Copy an environment and preserve parent-only Python import roots.

    Python does not inherit mutations to ``sys.path``.  Add absolute paths
    that sit outside the running interpreter's normal prefixes to
    ``PYTHONPATH`` while preserving the caller's existing value verbatim.
    Empty and relative runtime entries are rejected so the detached child's
    working directory cannot unexpectedly become a new import authority.
    """
    env = dict(os.environ if base_env is None else base_env)
    paths = sys.path if runtime_paths is None else runtime_paths
    prefixes = (
        (sys.prefix, sys.base_prefix)
        if interpreter_prefixes is None
        else interpreter_prefixes
    )

    normalised_prefixes = [
        value
        for prefix in prefixes
        if (value := _normalised_absolute(prefix)) is not None
    ]
    injected: list[str] = []
    seen: set[str] = set()

    for entry in paths:
        value = _normalised_absolute(entry)
        if value is None or any(_is_within(value, prefix) for prefix in normalised_prefixes):
            continue
        key = os.path.normcase(value)
        if key in seen:
            continue
        seen.add(key)
        injected.append(value)

    existing = env.get("PYTHONPATH", "")
    if existing:
        if injected:
            env["PYTHONPATH"] = os.pathsep.join(injected) + os.pathsep + existing
    elif injected:
        env["PYTHONPATH"] = os.pathsep.join(injected)

    return env
