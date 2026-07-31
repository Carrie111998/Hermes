"""Windows UTF-8 bootstrap for Hermes entry points.

Python on Windows has two long-standing text-encoding footguns:

1. ``sys.stdout`` / ``sys.stderr`` are bound to the console code page
   (``cp1252`` on US-locale installs), so ``print("café")`` crashes with
   ``UnicodeEncodeError: 'charmap' codec can't encode character``.

2. Child processes spawned via ``subprocess`` don't know to use UTF-8
   unless ``PYTHONUTF8`` and/or ``PYTHONIOENCODING`` are set in their
   environment — so any Python subprocess (the execute_code sandbox,
   delegation children, linter subprocesses, etc.) inherits the same
   cp1252 defaults and hits the same UnicodeEncodeError.

This module fixes both on Windows *only* — POSIX is untouched.  It
should be imported at the very top of every Hermes entry point
(``hermes``, ``hermes-agent``, ``hermes-acp``, ``python -m gateway.run``,
``batch_runner.py``, ``cron/scheduler.py``) before any other imports
that might do file I/O or print to stdout.

What this module does on Windows:

  - Sets ``os.environ["PYTHONUTF8"] = "1"`` (PEP 540 UTF-8 mode) so
    every child process we spawn uses UTF-8 for ``open()`` and stdio.
  - Sets ``os.environ["PYTHONIOENCODING"] = "utf-8"`` for belt-and-
    suspenders — some tools read this instead of / in addition to
    ``PYTHONUTF8``.
  - Reconfigures ``sys.stdout`` / ``sys.stderr`` to UTF-8 in the current
    process, using the ``reconfigure()`` API (Python 3.7+).  This fixes
    ``print("café")`` in the parent without a re-exec.

What this module does NOT do:

  - It does not re-exec Python with ``-X utf8``, so ``open()`` calls in
    the *current* process still default to locale encoding.  Those need
    an explicit ``encoding="utf-8"`` at the call site (lint rule
    ``PLW1514`` / ``PYI058``).  Ruff is the right tool for that sweep.

What this module does on POSIX:

  - Nothing.  POSIX systems are already UTF-8 by default in 99% of cases,
    and we don't want to touch ``LANG``/``LC_*`` behavior that users may
    have configured intentionally.  If someone hits a C/POSIX locale on
    Linux, they can export ``PYTHONUTF8=1`` themselves — we won't override.

Idempotent: safe to call multiple times.  ``_bootstrap_once`` guards
against double-reconfigure.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_IS_WINDOWS = sys.platform == "win32"
_bootstrap_applied = False


def _douglas_home_candidates() -> list[Path]:
    """Directories to check for an existing install, most-preferred first.

    The first entry doubles as the default target for a brand-new install
    (used unconditionally when none of the candidates exist on disk).

    Mirrors apps/desktop/electron/main.ts::resolveHermesHome() and
    apps/bootstrap-installer/src-tauri/src/paths.rs::hermes_home() — see
    douglas/README.md, section "Cadena canonica de resolucion
    Douglas/Hermes", for the single source of truth this chain implements
    in three separate runtimes (Python here, Node in Electron's main
    process, Rust in the native bootstrap installer that runs before
    Python exists).
    """
    if _IS_WINDOWS:
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        if local_appdata:
            base = Path(local_appdata)
            return [base / "douglas", base / "hermes", Path.home() / ".hermes"]
        return [Path.home() / ".douglas", Path.home() / ".hermes"]
    return [Path.home() / ".douglas", Path.home() / ".hermes"]


def _resolve_default_douglas_home() -> Path:
    candidates = _douglas_home_candidates()
    for candidate in candidates:
        try:
            if candidate.is_dir():
                return candidate
        except OSError:
            continue
    return candidates[0]


def normalize_douglas_env() -> None:
    """Normalize DOUGLAS_* environment variables into the HERMES_* names
    the rest of Hermes already reads, and resolve HERMES_HOME per the
    Douglas/Hermes compatibility chain when neither is set.

    Must run before any other Hermes module is imported (see the call at
    the bottom of this file, which runs first among this module's
    import-time side effects) — get_hermes_home() and ~200 other call
    sites across the codebase read HERMES_HOME (and other HERMES_* vars)
    directly from os.environ, so normalizing it here, once, at the very
    start of the process, makes every one of those call sites work
    correctly without modifying any of them.

    Does not interact with the profile ContextVar override
    (hermes_constants.get_hermes_home_override(), used by
    hermes_cli/profiles.py): that override is checked first and
    unconditionally wins inside get_hermes_home(), and is only ever set
    later, contextually, per profile-scoped task — never at process
    bootstrap. See douglas/README.md for the full resolution chain and
    the reasoning behind each tier.
    """
    # 1. Generic DOUGLAS_<X> -> HERMES_<X> aliasing. DOUGLAS_<X> always
    #    wins when both are present, matching the documented priority
    #    (DOUGLAS_HOME > HERMES_HOME, and likewise for every other var).
    for key, value in list(os.environ.items()):
        if key.startswith("DOUGLAS_") and value.strip():
            os.environ["HERMES_" + key[len("DOUGLAS_"):]] = value

    # 2. If HERMES_HOME is still unset after aliasing (neither DOUGLAS_HOME
    #    nor HERMES_HOME was in the environment), resolve the default
    #    directory: prefer an existing ~/.douglas install, then an
    #    existing ~/.hermes install (don't orphan prior Hermes users),
    #    else create ~/.douglas for a brand-new install.
    if not os.environ.get("HERMES_HOME", "").strip():
        os.environ["HERMES_HOME"] = str(_resolve_default_douglas_home())


def apply_windows_utf8_bootstrap() -> bool:
    """Apply the Windows UTF-8 bootstrap if we're on Windows.

    Returns True if bootstrap was applied (i.e. we're on Windows and
    haven't already done this), False otherwise.  The return value is
    advisory — callers normally don't need it, but tests may want to
    assert the path was taken.

    Idempotent: subsequent calls after the first are a no-op.
    """
    global _bootstrap_applied

    if not _IS_WINDOWS:
        return False
    if _bootstrap_applied:
        return False

    # 1. Child processes inherit these and run in UTF-8 mode.
    #    We use setdefault() rather than overwriting so the user can
    #    explicitly opt out by setting PYTHONUTF8=0 in their environment
    #    (or PYTHONIOENCODING=something-else) if they really want to.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    # 2. Reconfigure the current process's stdio to UTF-8.  Needed
    #    because os.environ changes don't retroactively rebind sys.stdout
    #    — those were bound at interpreter startup based on the console
    #    code page.  ``reconfigure`` is a TextIOWrapper method since 3.7.
    #
    #    errors="replace" means that if we ever *read* something from
    #    stdin that isn't UTF-8 (unlikely but possible with piped input
    #    from legacy tools), we'll get U+FFFD replacement chars rather
    #    than a crash.  Output is pure UTF-8.
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # Not a TextIOWrapper (could be redirected to a BytesIO in
            # tests, or a non-standard stream in some embedded cases).
            # Skip silently — the env-var fix is still in effect for
            # child processes, which is the bigger win.
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            # Already closed, or someone replaced it with something
            # non-reconfigurable.  Non-fatal.
            pass

    # stdin is reconfigured separately with errors="replace" too — input
    # from a legacy pipe shouldn't crash the process.
    stdin = getattr(sys, "stdin", None)
    if stdin is not None:
        reconfigure = getattr(stdin, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass

    _bootstrap_applied = True
    return True


def suppress_platform_ver_console() -> None:
    """Stub ``platform._syscmd_ver`` on Windows — decode-crash + flash guard.

    CPython's ``platform.win32_ver()`` (reached via ``platform.uname()`` /
    ``platform.platform()``, which the OpenAI SDK touches for its
    platform headers) shells out ``cmd /c ver``. Two failure modes:

    - **Console flash**: the ``check_output(..., shell=True)`` call has no
      ``CREATE_NO_WINDOW``, so a windowless parent (pythonw gateway, slash
      workers, kanban workers) flashes a visible console per call.
    - **UnicodeDecodeError on Python 3.11.0/3.11.1**: those micros lack
      CPython's ``encoding="locale"`` fix (added 3.11.2), so under PEP 540
      UTF-8 mode (which we enable above) the ``ver`` output — OEM code page
      bytes on localized Windows — is strict-utf-8 decoded and raises,
      crashing ``platform.platform()`` in any process that inherits
      ``PYTHONUTF8=1`` (issue #69413).

    Stubbing ``_syscmd_ver`` to return its inputs makes ``win32_ver()`` hit
    its documented fallback and read the version from
    ``sys.getwindowsversion()`` — same data, in-process, no subprocess.
    Mirrors ``hermes_cli._subprocess_compat.suppress_platform_ver_console``
    (kept there for callers that don't import bootstrap); double
    application is harmless. Lives here so EVERY entry point gets it —
    ``tui_gateway/slash_worker.py``, ``tui_gateway/entry.py``,
    ``run_agent.py``, ``batch_runner.py``, and ``cli.py`` import only
    ``hermes_bootstrap``, never ``hermes_cli.main``.
    """
    if not _IS_WINDOWS:
        return
    try:
        import platform

        if hasattr(platform, "_syscmd_ver"):
            def _quiet_syscmd_ver(system="", release="", version="",
                                  supported_platforms=("win32", "win16", "dos")):
                return system, release, version

            platform._syscmd_ver = _quiet_syscmd_ver
    except Exception:
        # Hardening only — never let it break an entry point.
        pass


def harden_import_path(src_root: str | None = None) -> None:
    """Stop a package in the current directory from shadowing Hermes modules.

    Hermes ships top-level modules with common names (``utils``, ``proxy``,
    ``ui``).  Python always seeds ``sys.path`` with the current directory, so
    launching an entry point from a project that has its own ``utils/`` package
    makes ``from utils import ...`` resolve to the *user's* package and crash
    with an ImportError before the gateway can even start.

    The current directory reaches ``sys.path`` two ways, and a complete guard
    has to handle both:

      - As the empty string ``""`` (or ``"."``) that Python inserts at
        ``sys.path[0]`` for ``-m`` / script launches.
      - As its own *absolute* path, when a venv activation or a project that
        adds itself to ``PYTHONPATH`` puts the directory there explicitly.

    We drop the relative forms outright, then force the real Hermes source root
    to the front — relocating it ahead of any absolute cwd entry rather than
    only inserting when absent, so an absolute cwd path can't keep winning.

    ``src_root`` defaults to the directory this module lives in, which is the
    repository root for every shipped entry point, so the guard is
    self-sufficient and does not depend on the spawner exporting an env var.
    """
    root = src_root or os.environ.get("HERMES_PYTHON_SRC_ROOT") or os.path.dirname(
        os.path.abspath(__file__)
    )

    sys.path[:] = [p for p in sys.path if p not in ("", ".")]

    root_abs = os.path.abspath(root)
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != root_abs]
    sys.path.insert(0, root)


def activate_durable_lazy_target() -> None:
    """Put the durable lazy-install dir on ``sys.path`` if one is configured.

    On immutable Docker images the agent venv is sealed and lazy installs
    are redirected to a writable dir on the data volume
    (``HERMES_LAZY_INSTALL_TARGET``, e.g. ``/opt/data/lazy-packages``).
    Packages installed there on a previous run must be importable on this
    run, so we activate the dir here — at the very first import, before any
    backend module imports its SDK.

    The activation appends to the END of ``sys.path`` so the core venv
    always wins name collisions (see ``tools.lazy_deps`` for the full
    security rationale). Never raises; a missing/empty target is a no-op.
    """
    if not os.environ.get("HERMES_LAZY_INSTALL_TARGET", "").strip():
        return
    try:
        from tools import lazy_deps
        lazy_deps.activate_durable_lazy_target()
    except Exception:
        # Bootstrap must never crash an entry point. If activation fails the
        # backend simply reports itself unavailable, exactly as before.
        pass


# Apply on import — entry points just need ``import hermes_bootstrap``
# (or ``from hermes_bootstrap import apply_windows_utf8_bootstrap``) at
# the very top of their module, before importing anything else.  The
# import side effect does the right thing.
#
# normalize_douglas_env() runs FIRST, before anything else in this block:
# HERMES_HOME (and every other HERMES_* var) must be normalized before any
# other Hermes module — including the UTF-8/console helpers below, which
# don't depend on it today but might in the future — gets a chance to read
# os.environ.
normalize_douglas_env()
apply_windows_utf8_bootstrap()
suppress_platform_ver_console()

# Activate the durable lazy-install target (immutable Docker images) so
# packages installed into the data volume on a previous run are importable
# this run, before any backend module imports its SDK. No-op when unset.
activate_durable_lazy_target()
