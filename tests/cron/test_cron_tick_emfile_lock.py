"""#87644: FD exhaustion during tick lock acquisition must be visible.

Previously an ``OSError`` (EMFILE/ENFILE) while opening the tick lock file
was treated as lock contention: logged at debug level and returning 0. The
provider loop read that as a clean tick (ok=True), clearing any error marker
-- leaving the cron scheduler silently stalled for days while the gateway
heartbeat kept reporting healthy. The tick must surface FD exhaustion so the
failure is visible to ``hermes cron status`` / health checks and retries on
the next tick once descriptors free up.
"""

from __future__ import annotations

import builtins
import errno

import pytest

from cron.scheduler import tick


def test_tick_lock_emfile_raises_instead_of_silent_zero(monkeypatch):
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if "cron" in str(path) and "lock" in str(path):
            raise OSError(errno.EMFILE, "Too many open files")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    with pytest.raises(OSError) as exc:
        tick(verbose=False)
    assert exc.value.errno == errno.EMFILE


def test_tick_lock_contention_still_returns_zero(monkeypatch):
    """Non-EMFILE lock failures keep the old semantics (skip, not raise)."""
    real_open = builtins.open

    def fake_open(path, *a, **kw):
        if "cron" in str(path) and "lock" in str(path):
            raise OSError(errno.EACCES, "Permission denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(builtins, "open", fake_open)
    assert tick(verbose=False) == 0
