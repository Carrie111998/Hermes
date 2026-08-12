"""Load-tolerant deadlines for tests that bound child processes.

Why this exists
---------------
Several tests spawn a child (sometimes a grandchild: a runner that itself
runs pytest) and bound it with ``subprocess.run(..., timeout=N)`` or
``Popen.communicate(timeout=N)``. Those bounds are safety nets — they exist
so a wedged child cannot hang the whole session — but they were written as
hardcoded wall-clock constants sized for an idle box.

On a loaded host the same child takes several times longer purely from CPU
and memory contention, the bound trips, and the test fails with
``subprocess.TimeoutExpired``. That failure is indistinguishable from a real
regression: the failure count tracks wall-clock time, not the code under
test. (Measured 2026-08-12: the same two files produced 1, 2, 5 or 11
failures depending only on how many files shared the run.)

Two rules
---------
1. A deadline that is *the thing being asserted* — "the lock wait really does
   give up", "the runner really does kill a hung file" — stays small and
   explicit and is NOT scaled. Scaling it would weaken the assertion.
2. A deadline that is *incidental* — a safety net around a spawn whose
   expected duration is well under the bound — gets a generous base and is
   multiplied by :func:`timeout_scale`.

``HERMES_TEST_TIMEOUT_SCALE`` is the multiplier for rule 2. It defaults to
``1.0`` (so CI and clean checkouts are unchanged) and is raised on hosts
that routinely run under memory/CPU pressure, e.g. ``4``. Values below 1 are
clamped away: shortening a safety net is never the intent, and a stray
``0.1`` would recreate exactly the flakiness this module removes.
"""

from __future__ import annotations

import math
import os

SCALE_ENV_VAR = "HERMES_TEST_TIMEOUT_SCALE"


def timeout_scale() -> float:
    """Return the configured multiplier, never below ``1.0``.

    The environment is read on every call (not cached at import) so a test
    can ``monkeypatch.setenv`` it, and so a scale exported after collection
    still applies.
    """
    raw = os.environ.get(SCALE_ENV_VAR)
    if raw is None:
        return 1.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if not math.isfinite(value):
        return 1.0
    return max(1.0, value)


def scaled(seconds: float) -> float:
    """Scale an incidental safety-net deadline for the current host load."""
    return seconds * timeout_scale()
