"""Fail fast when Hermes is started on a runtime it does not support.

Two independent hazards, deliberately given different severities:

* **Python version** — ``pyproject.toml`` declares ``requires-python =
  ">=3.11,<3.14"``. Nothing in the codebase enforced it, so ``hermes`` launched
  happily on 3.14 and only failed later, deep inside a private-stdlib mirror
  (``concurrent.futures.thread._worker`` changed shape in 3.14, breaking every
  ``delegate_task``). An unsupported interpreter is a hard error: the failure
  modes are silent and arbitrary, and the user has a supported venv available.

* **SQLite WAL-reset bug** — a vulnerable library can corrupt a WAL database.
  Upstream's mitigation is to refuse WAL on *fresh* databases, which does not
  help an install whose databases are already in WAL mode. That is a warning
  rather than a hard error: it is real but probabilistic, refusing to start
  would strand a user mid-work, and the safe interpreter may be one command
  away. The point is that it can no longer happen *silently* from any entry
  point, not only from ``hermes doctor``.

Both checks are overridable, because a guard that cannot be bypassed becomes
the outage. stdlib-only and dependency-free so this can run before anything
heavier is imported.
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

# Mirrors ``requires-python`` in pyproject.toml. Duplicated deliberately:
# resolving pyproject at runtime is unreliable for an installed package, and a
# stale constant is caught by test_runtime_guard.py, which parses pyproject and
# asserts these two values still agree.
MIN_PYTHON: Tuple[int, int] = (3, 11)
MAX_PYTHON_EXCLUSIVE: Tuple[int, int] = (3, 14)

ALLOW_UNSUPPORTED_PYTHON_ENV = "HERMES_ALLOW_UNSUPPORTED_PYTHON"
SUPPRESS_SQLITE_WARNING_ENV = "HERMES_SUPPRESS_SQLITE_WARNING"


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def python_supported(version: Optional[Tuple[int, ...]] = None) -> bool:
    """True when *version* falls inside the declared supported range."""
    info = tuple(version or sys.version_info[:2])[:2]
    return MIN_PYTHON <= info < MAX_PYTHON_EXCLUSIVE


def _range_text() -> str:
    return (f">={MIN_PYTHON[0]}.{MIN_PYTHON[1]},"
            f"<{MAX_PYTHON_EXCLUSIVE[0]}.{MAX_PYTHON_EXCLUSIVE[1]}")


def _supported_interpreter_hints() -> List[str]:
    """Point at a real supported interpreter when one is discoverable.

    Derived from this module's own location (``<repo>/hermes_cli/``) rather
    than from ``get_hermes_home()``: the guard runs before config is loaded, so
    anything that reads config can raise here and silently swallow the single
    most actionable line of the error. The repo root is knowable from __file__
    with no configuration at all.

    Best-effort: a missing hint must never turn a clear error into a confusing
    one, so every probe is guarded and only existing executables are reported.
    """
    hints: List[str] = []
    exe_name = "hermes.exe" if os.name == "nt" else "hermes"
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    try:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
    except Exception:
        return hints
    for venv in ("venv313", "venv", ".venv"):
        try:
            candidate = repo_root / venv / bin_dir / exe_name
            if candidate.exists() and candidate.parent.parent.name != _current_venv_name():
                hints.append(str(candidate))
        except Exception:
            continue
    return sorted(set(hints))


def _current_venv_name() -> str:
    """Name of the venv this interpreter lives in, or '' when not in one.

    Used to avoid recommending the very interpreter that just failed.
    """
    try:
        from pathlib import Path

        prefix = Path(sys.prefix)
        return prefix.name if prefix != Path(sys.base_prefix) else ""
    except Exception:
        return ""


def check_python(*, stream=None) -> bool:
    """Return True when the interpreter is supported.

    Prints an actionable error and returns False otherwise, unless the override
    env var is set (then it warns and returns True).
    """
    stream = stream or sys.stderr
    if python_supported():
        return True

    running = ".".join(str(p) for p in sys.version_info[:3])
    override = _truthy(os.environ.get(ALLOW_UNSUPPORTED_PYTHON_ENV))
    label = "WARNING" if override else "ERROR"
    print(f"\n[hermes] {label}: unsupported Python {running}", file=stream)
    print(f"[hermes] hermes-agent supports {_range_text()} (pyproject requires-python).",
          file=stream)
    print(f"[hermes] interpreter: {sys.executable}", file=stream)
    for hint in _supported_interpreter_hints():
        print(f"[hermes] supported interpreter available: {hint}", file=stream)
    if override:
        print(f"[hermes] {ALLOW_UNSUPPORTED_PYTHON_ENV} is set — continuing anyway. "
              "Failures here are not supported.", file=stream)
        return True
    print(f"[hermes] Re-run with a supported interpreter, or set "
          f"{ALLOW_UNSUPPORTED_PYTHON_ENV}=1 to override.\n", file=stream)
    return False


def check_sqlite(*, stream=None) -> bool:
    """Return True when the linked SQLite is free of the WAL-reset bug.

    Never fatal — warns and returns False so callers can still proceed.
    """
    stream = stream or sys.stderr
    if _truthy(os.environ.get(SUPPRESS_SQLITE_WARNING_ENV)):
        return True
    try:
        import sqlite3

        from hermes_state import is_sqlite_wal_reset_vulnerable

        if not is_sqlite_wal_reset_vulnerable():
            return True
        version = sqlite3.sqlite_version
    except Exception:
        # A probe failure must never block startup.
        return True

    print(f"\n[hermes] WARNING: SQLite {version} has the WAL-reset bug "
          "(https://sqlite.org/wal.html#walresetbug).", file=stream)
    print("[hermes] Existing WAL databases opened by this interpreter are at risk; "
          "refusing WAL on new databases does not protect them.", file=stream)
    print(f"[hermes] interpreter: {sys.executable}", file=stream)
    for hint in _supported_interpreter_hints():
        print(f"[hermes] try: {hint}", file=stream)
    print(f"[hermes] Silence with {SUPPRESS_SQLITE_WARNING_ENV}=1.\n", file=stream)
    return False


def enforce(*, exit_on_failure: bool = True, stream=None) -> bool:
    """Run every runtime check. Returns True when the runtime is usable.

    Called once from the CLI entry point before anything opens a database.
    """
    ok = check_python(stream=stream)
    check_sqlite(stream=stream)          # advisory: never gates startup
    if not ok and exit_on_failure:
        raise SystemExit(1)
    return ok


__all__ = [
    "MIN_PYTHON",
    "MAX_PYTHON_EXCLUSIVE",
    "ALLOW_UNSUPPORTED_PYTHON_ENV",
    "SUPPRESS_SQLITE_WARNING_ENV",
    "python_supported",
    "check_python",
    "check_sqlite",
    "enforce",
]
