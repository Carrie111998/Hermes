"""Throwaway HERMES_HOME lifecycle for the stress scripts.

Every script in this directory builds its own HERMES_HOME with
``tempfile.mkdtemp()`` so runs cannot see each other's sqlite state. None
of them used to remove it, so each run left a directory behind forever:
by 2026-08-17 ``%TEMP%`` held ~2100 of them, 2000 from
``test_property_fuzzing.py`` alone (it mkdtemps once per *sequence*, 500
per run). This module is the one place that decides when such a directory
dies.

The contract:

* a run that **passed** deletes its home — these are disposable sqlite
  scratch dirs, nothing in them outlives the assertions;
* a run that **failed** keeps it, and prints the path, because the
  ``kanban.db`` inside is usually the only way to work out *why*. This
  mirrors what the 2026-07-22 temp-leak sweep chose for the pytest side
  (``tmp_path_retention_policy = "failed"``);
* ``HERMES_STRESS_KEEP_TEMP=1`` keeps everything, for when you want the
  passing runs' state too.

Failure arrives in two shapes here, which is why marking is explicit:
most scripts raise or ``sys.exit(1)``, and :func:`temp_home` catches that
by itself; ``test_property_fuzzing.py`` and
``test_concurrency_parent_gate.py`` instead record a violation and carry
on returning a non-zero code later, so they call
:func:`keep_for_debugging` at the point the violation is detected.

Imported as a sibling (``from _temphome import ...``): Python puts the
script's own directory on ``sys.path[0]`` when you run
``python tests/stress/<script>.py``, which is the documented way to run
these and the way ``test_stress_entrypoints.py`` runs them. The leading
underscore keeps it out of that module's ``test_*.py`` discovery glob.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from typing import Iterator

# Paths explicitly marked to survive cleanup. A module-level set is enough:
# each of these scripts is a single-run program, and the mark is always set
# and read inside one process.
_KEEP: set[str] = set()

_KEEP_ENV = "HERMES_STRESS_KEEP_TEMP"


def keep_for_debugging(home: str) -> None:
    """Mark ``home`` to survive cleanup because a check failed.

    For scripts whose failure signal is a return value rather than an
    exception -- :func:`temp_home` cannot see those, so they say so here.
    """
    _KEEP.add(os.path.abspath(home))


def _keep_everything() -> bool:
    return os.environ.get(_KEEP_ENV, "").strip().lower() not in ("", "0", "false", "no")


def cleanup_home(home: str, *, failed: bool = False) -> None:
    """Remove a throwaway HERMES_HOME, unless it is wanted for debugging.

    Close every sqlite connection into ``home`` *before* calling this. On
    Windows an open handle keeps ``kanban.db`` locked and ``rmtree`` cannot
    unlink it -- the exact failure mode the 2026-07-22 sweep hit -- and
    with ``ignore_errors`` that loses the directory silently, which is how
    a leak like this one goes unnoticed in the first place. So this reports
    a survivor instead of swallowing it.
    """
    home = os.path.abspath(home)
    if failed or home in _KEEP:
        print(f"  [temp] kept for debugging: {home}")
        return
    if _keep_everything():
        print(f"  [temp] kept ({_KEEP_ENV} set): {home}")
        return

    shutil.rmtree(home, ignore_errors=True)
    if os.path.isdir(home):
        # A worker that has not fully exited can still hold the db open for
        # a moment after we think it is done. One retry clears that; a
        # directory that survives both is worth saying out loud.
        time.sleep(0.25)
        shutil.rmtree(home, ignore_errors=True)
        if os.path.isdir(home):
            print(f"  [temp] WARNING: could not remove {home} (open handle?)")


@contextmanager
def temp_home(prefix: str) -> Iterator[str]:
    """Yield a fresh mkdtemp HERMES_HOME that cleans itself up on exit.

    Kept if the body raises -- including the ``SystemExit`` from
    ``sys.exit(1)``, which is how most of these scripts report failure --
    or if the body called :func:`keep_for_debugging` on the yielded path.
    """
    home = tempfile.mkdtemp(prefix=prefix)
    failed = False
    try:
        yield home
    except BaseException:
        failed = True
        raise
    finally:
        cleanup_home(home, failed=failed)
