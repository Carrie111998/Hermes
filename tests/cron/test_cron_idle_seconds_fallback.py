"""The cron inactivity watchdog must not read "cannot measure" as "fully active".

`_cron_idle_seconds` previously lived inline in `run_job` as `_idle_secs = 0.0` followed by a
`try/except Exception: pass`. Every path that could not obtain a reading therefore produced 0.0 —
indistinguishable from a perfectly active agent — so the configured inactivity limit was never
evaluated for as long as the agent could not answer.

The case that matters is an agent still inside its own construction: `get_activity_summary` may not
exist yet, or its activity clock may be unset, exactly while the job is stuck. The reaper then only
fires once the agent becomes answerable, which can be far past the limit.

These tests call the helper directly rather than re-implementing the polling loop, so they fail if
the production behaviour regresses.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cron.scheduler import _cron_idle_seconds  # noqa: E402


class _Reporting:
    def __init__(self, idle):
        self._idle = idle

    def get_activity_summary(self):
        return {"seconds_since_activity": self._idle}


class _Raising:
    def get_activity_summary(self):
        raise RuntimeError("agent not ready")


class _Constructing:
    """No `get_activity_summary` at all — the shape an agent has mid-construction."""


class _Malformed:
    def get_activity_summary(self):
        return {"seconds_since_activity": None}


class _NotADict:
    def get_activity_summary(self):
        return "still working"


def test_reported_idleness_is_used_unchanged():
    assert _cron_idle_seconds(_Reporting(12.5), time.time() - 999) == 12.5


def test_zero_idleness_is_still_zero():
    """An agent that is genuinely active must not be aged from dispatch."""
    assert _cron_idle_seconds(_Reporting(0.0), time.time() - 999) == 0.0


def test_agent_without_the_method_ages_from_dispatch():
    assert _cron_idle_seconds(_Constructing(), time.time() - 300) >= 299


def test_raising_agent_ages_from_dispatch():
    assert _cron_idle_seconds(_Raising(), time.time() - 300) >= 299


def test_malformed_summary_ages_from_dispatch():
    assert _cron_idle_seconds(_Malformed(), time.time() - 300) >= 299
    assert _cron_idle_seconds(_NotADict(), time.time() - 300) >= 299


def test_unmeasurable_agent_crosses_the_limit_it_used_to_ignore():
    """The regression itself: 0.0 could never reach the limit, so nothing ever fired."""
    limit = 600.0
    dispatched_at = time.time() - 5610  # a stuck job, well past the limit
    assert _cron_idle_seconds(_Raising(), dispatched_at) >= limit
    assert _cron_idle_seconds(_Constructing(), dispatched_at) >= limit


def test_fallback_is_never_negative():
    """A clock adjustment must not produce a negative idleness that disables the watchdog."""
    assert _cron_idle_seconds(_Raising(), time.time() + 60) == 0.0
