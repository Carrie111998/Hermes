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
  help an install whose databases are already in WAL mode. This is also a hard
  error. It began as a warning on the reasoning that the risk is probabilistic
  and refusing to start would strand a user mid-work; that reasoning did not
  survive contact with this install. ``hermes-agent/venv`` is Python 3.11.15 —
  *inside* ``requires-python``, so the Python check waves it through — linking
  SQLite 3.50.4, against ~10 of 11 databases already in WAL. Warning-only left
  a live corruption path on a runtime the guard itself called supported, and
  three launchers still pointed at that venv.

Both checks are overridable, because a guard that cannot be bypassed becomes
the outage — but the two overrides are kept distinct. Silencing a message
(``HERMES_SUPPRESS_SQLITE_WARNING``) must never be the same gesture as
accepting a corruption risk (``HERMES_ALLOW_VULNERABLE_SQLITE``); the former
used to short-circuit the check before it ran. An accepted risk always prints.

stdlib-only and dependency-free so this can run before anything heavier is
imported.
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
# Deliberate acceptance of a real corruption risk (e.g. rolling back to the
# 3.11 venv). Distinct from the cosmetic suppressor on purpose: one silences
# a message, the other assumes liability, and conflating them is what let a
# single env var clear the check entirely.
ALLOW_VULNERABLE_SQLITE_ENV = "HERMES_ALLOW_VULNERABLE_SQLITE"


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


def _sqlite_vulnerable() -> Tuple[Optional[bool], str]:
    """(is_vulnerable, version). ``None`` means the probe itself failed."""
    try:
        import sqlite3

        from hermes_state import is_sqlite_wal_reset_vulnerable

        return bool(is_sqlite_wal_reset_vulnerable()), sqlite3.sqlite_version
    except Exception:
        return None, ""


def check_sqlite(*, stream=None) -> bool:
    """Return True when it is safe to proceed on this SQLite build.

    Vulnerable SQLite is FATAL, not advisory.  The Python half of this guard
    passes ``hermes-agent/venv`` — 3.11.15 is inside ``requires-python`` — while
    that same interpreter links SQLite 3.50.4 and ~10 of the profile's 11
    databases are already in WAL.  Upstream's mitigation ("refuse WAL on new
    databases") cannot retroactively protect them, so a guard that observes the
    exact precondition for corruption and then returns control to the caller is
    documentation, not a guard.

    ``SUPPRESS_SQLITE_WARNING_ENV`` is cosmetic only: it silences the advisory
    emitted when the version cannot be determined, and deliberately CANNOT clear
    a real vulnerability.  It previously returned True before probing at all, so
    a single env var turned a corruption risk into a clean start.  Accepting the
    risk now requires ``ALLOW_VULNERABLE_SQLITE_ENV``, which is loud and is never
    silenced — an accepted risk that prints nothing is indistinguishable from no
    risk, which is how the emergency bypass in this system became routine.
    """
    stream = stream or sys.stderr
    vulnerable, version = _sqlite_vulnerable()

    if vulnerable is False:
        return True

    if vulnerable is None:
        # Unknown, not proven safe. Never block on a broken probe — bricking the
        # CLI because an import failed is its own outage — but never pass in
        # silence either.
        if not _truthy(os.environ.get(SUPPRESS_SQLITE_WARNING_ENV)):
            print("\n[hermes] WARNING: could not determine the linked SQLite "
                  "version; WAL-reset exposure is UNVERIFIED.", file=stream)
            print(f"[hermes] interpreter: {sys.executable}", file=stream)
            print(f"[hermes] Silence with {SUPPRESS_SQLITE_WARNING_ENV}=1.\n",
                  file=stream)
        return True

    if _truthy(os.environ.get(ALLOW_VULNERABLE_SQLITE_ENV)):
        # The rollback path (e.g. deliberately running the 3.11 venv) stays
        # open, but never quietly.
        print(f"\n[hermes] OVERRIDE: proceeding on SQLite {version}, which has "
              "the WAL-reset bug.", file=stream)
        print(f"[hermes] {ALLOW_VULNERABLE_SQLITE_ENV} is set — existing WAL "
              "databases opened by this interpreter may be corrupted.",
              file=stream)
        print(f"[hermes] interpreter: {sys.executable}\n", file=stream)
        return True

    print(f"\n[hermes] ERROR: SQLite {version} has the WAL-reset bug "
          "(https://sqlite.org/wal.html#walresetbug).", file=stream)
    print("[hermes] Existing WAL databases opened by this interpreter are at "
          "risk; refusing WAL on new databases does not protect them.",
          file=stream)
    print(f"[hermes] interpreter: {sys.executable}", file=stream)
    for hint in _supported_interpreter_hints():
        print(f"[hermes] try: {hint}", file=stream)
    print(f"[hermes] To accept this risk deliberately, set "
          f"{ALLOW_VULNERABLE_SQLITE_ENV}=1 ({SUPPRESS_SQLITE_WARNING_ENV} will "
          "not clear it).\n", file=stream)
    return False


def enforce(*, exit_on_failure: bool = True, stream=None) -> bool:
    """Run every runtime check. Returns True when the runtime is usable.

    Called once from the CLI entry point before anything opens a database.
    """
    # Both halves gate. Evaluated eagerly (not short-circuited) so a run on an
    # unsupported interpreter still reports its SQLite exposure in the same
    # breath — the operator needs both facts to pick a working interpreter.
    python_ok = check_python(stream=stream)
    sqlite_ok = check_sqlite(stream=stream)
    ok = python_ok and sqlite_ok
    if not ok and exit_on_failure:
        raise SystemExit(1)
    return ok


__all__ = [
    "MIN_PYTHON",
    "MAX_PYTHON_EXCLUSIVE",
    "ALLOW_UNSUPPORTED_PYTHON_ENV",
    "SUPPRESS_SQLITE_WARNING_ENV",
    "ALLOW_VULNERABLE_SQLITE_ENV",
    "python_supported",
    "check_python",
    "check_sqlite",
    "enforce",
]
