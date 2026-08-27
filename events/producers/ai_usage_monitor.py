"""Resident AI-usage collection, run inside the gateway instead of by a task.

WHY THIS EXISTS. ``AIUsageCollector`` is a Windows scheduled task that cold-starts
a fresh interpreter per sample. At the task's BelowNormal priority the import
prefix ALONE was measured at 344.24s against a 340s usable window -- so the run
could die before ``collect()`` was ever reached, writing nothing at all: no
snapshot, no diagnostics, just a stale ai-tokens.json. Cadence PT5M -> PT15M
(2026-08-26) cut the exposure from 288 to 96 cold starts/day but did not remove
the class. Holding the interpreter resident does: imports are paid once per
gateway lifetime and cached in ``sys.modules``, and the same warm process keeps
httpx's SSL context (13.28s import + 4.22s first Client, both one-time).

WHY NOT A CRON JOB. Hermes cron script jobs shell out -- ``cron/scheduler.py``
builds ``argv = [python_exe, str(path)]`` and runs it through
``run_text_capture``. That is a cold interpreter start per fire, i.e. exactly
the thing being removed. The in-gateway monitors driven from the subscriber
poll loop (health, resource pressure, code drift, partial backlog) are the
mechanism that actually stays warm.

WHY check() DOES NOT DO THE WORK. That poll loop is shared and sequential: every
monitor's ``check()`` runs inline on the one thread that also services lag
checks, health checks and mailbox scans. A collector run is 40-60s of network
plus a CDP browser probe. Running it inline would stall the bus for a minute
every interval. So ``check()`` is a clock comparison that hands off to a daemon
worker and returns; results are reaped on a later tick. At most one run is ever
in flight -- a slow run causes a SKIP, never a pile-up, which is the same
guarantee ``MultipleInstancesPolicy=IgnoreNew`` gave at the task boundary.

SHADOW MODE. Default is ``shadow``: the resident run writes to a SEPARATE path
and the scheduled task keeps owning the real ai-tokens.json, so the two can be
diffed over a day before anything is cut over. ``off`` disables it entirely.
Only ``on`` writes the production snapshot, and only then should the scheduled
task be disabled -- two writers would otherwise fight over one file.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Matches the task cadence this replaces (PT15M, set 2026-08-26).
DEFAULT_INTERVAL_SECONDS = 900.0

# A run that overruns this is abandoned by the reaper for diagnostics purposes
# only -- the worker is a daemon thread and cannot be killed. The value is
# deliberately far above the 40-60s steady state and above collect()'s own 90s
# cooperative budget, so it fires only on a genuinely wedged run.
DEFAULT_RUN_TIMEOUT_SECONDS = 300.0

_MODE_OFF = "off"
_MODE_SHADOW = "shadow"
_MODE_ON = "on"
_VALID_MODES = (_MODE_OFF, _MODE_SHADOW, _MODE_ON)

# Shadow output sits beside the production snapshot so a diff is one command.
SHADOW_FILENAME = "ai-tokens-resident.json"
PRODUCTION_FILENAME = "ai-tokens.json"


def resolve_mode(env: Optional[dict] = None) -> str:
    """Read the mode from the environment, defaulting to the safe one.

    An unrecognised value degrades to ``shadow`` rather than ``on``: a typo in
    a .env must never silently promote the resident collector into owning the
    production snapshot while the scheduled task also writes it.
    """
    source = env if env is not None else os.environ
    raw = (source.get("HERMES_AI_USAGE_RESIDENT") or "").strip().lower()
    if not raw:
        return _MODE_SHADOW
    if raw not in _VALID_MODES:
        logger.warning(
            "HERMES_AI_USAGE_RESIDENT=%r is not one of %s; using %s",
            raw, _VALID_MODES, _MODE_SHADOW,
        )
        return _MODE_SHADOW
    return raw


def _default_runner(out_path: Path) -> dict:
    """Run one collection into ``out_path``. Mirrors ai_usage.__main__.main().

    Imported lazily and INSIDE the worker thread: the whole point is that the
    first call pays the import and every later call finds it in sys.modules,
    but the gateway's own startup must not pay it at all if the mode is off.
    """
    import json

    from agent.account_usage import fetch_account_usage
    from ai_usage.collector import _default_warmup, collect, write_atomic

    prev = None
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            prev = None

    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    # state.db lives at the ~/.hermes ROOT, never profile-scoped (see CLAUDE.md).
    db = os.environ.get("HERMES_STATE_DB") or os.path.join(home, ".hermes", "state.db")

    data = collect(
        db_path=db,
        prev=prev,
        fetch_usage=fetch_account_usage,
        warmup=_default_warmup,
    )
    write_atomic(out_path, data)
    return data


class AIUsageCollectorMonitor:
    """Runs the AI-usage collector on an interval, inside the gateway.

    ``check()`` is called from the subscriber poll loop and must stay cheap and
    non-raising. Clock and runner are injectable so the scheduling logic tests
    without sleeps, threads-that-do-real-work, or network.
    """

    def __init__(
        self,
        *,
        mode: Optional[str] = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        run_timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS,
        runner: Optional[Callable[[Path], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        home: Optional[str] = None,
        start_immediately: bool = False,
    ):
        self.mode = mode if mode is not None else resolve_mode()
        self.interval_seconds = interval_seconds
        self.run_timeout_seconds = run_timeout_seconds
        self._runner = runner or _default_runner
        self._clock = clock or time.monotonic
        self._home = home or os.environ.get("USERPROFILE") or os.path.expanduser("~")

        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._started_at: Optional[float] = None
        # None means "never run"; start_immediately makes the first tick due.
        self._last_finished_at: Optional[float] = None
        if not start_immediately:
            # Defensive: this runs during gateway startup, where an exception
            # would take down the whole registration rather than one monitor.
            # A clock that cannot be read degrades to "due on the first tick",
            # which check() then handles under its own try/except.
            try:
                self._last_finished_at = self._clock()
            except Exception:
                logger.exception(
                    "Clock unreadable while constructing the AI usage monitor; "
                    "first check() will treat a run as due"
                )

        # Diagnostics, read by get_status() -- never used for control flow.
        self.runs_completed = 0
        self.runs_failed = 0
        self.runs_skipped_in_flight = 0
        self.last_error: Optional[str] = None
        self.last_duration_seconds: Optional[float] = None
        self.last_provider_count: Optional[int] = None

    @property
    def out_path(self) -> Path:
        name = PRODUCTION_FILENAME if self.mode == _MODE_ON else SHADOW_FILENAME
        return Path(self._home) / "architecture-map" / name

    @property
    def enabled(self) -> bool:
        return self.mode != _MODE_OFF

    def _due(self, now: float) -> bool:
        if self._last_finished_at is None:
            return True
        return (now - self._last_finished_at) >= self.interval_seconds

    def check(self) -> None:
        """Start a run if one is due and none is in flight. Never raises."""
        try:
            if not self.enabled:
                return
            now = self._clock()
            with self._lock:
                if self._worker is not None:
                    if self._worker.is_alive():
                        # A run is still going. Count it as a skip only once it
                        # has also become due again, so a normal long run is
                        # not reported as a skipped one every single tick.
                        if self._due(now):
                            self.runs_skipped_in_flight += 1
                            if (
                                self._started_at is not None
                                and now - self._started_at > self.run_timeout_seconds
                            ):
                                logger.warning(
                                    "AI usage collector run has exceeded %.0fs "
                                    "(started %.0fs ago) and is still in flight",
                                    self.run_timeout_seconds,
                                    now - self._started_at,
                                )
                        return
                    # Finished since the last tick; reap it.
                    self._worker = None
                    self._started_at = None
                if not self._due(now):
                    return
                self._started_at = now
                worker = threading.Thread(
                    target=self._run,
                    name="ai-usage-collector",
                    daemon=True,
                )
                self._worker = worker
            worker.start()
        except Exception:
            logger.exception("AI usage collector check failed")

    def _run(self) -> None:
        """Worker body. Owns all the slow work; never raises to the caller."""
        started = self._clock()
        try:
            out = self.out_path
            out.parent.mkdir(parents=True, exist_ok=True)
            data = self._runner(out)
            self.runs_completed += 1
            self.last_error = None
            try:
                providers = data.get("providers") if isinstance(data, dict) else None
                self.last_provider_count = len(providers) if providers is not None else None
            except Exception:
                self.last_provider_count = None
        except Exception as exc:
            self.runs_failed += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("AI usage collector run failed")
        finally:
            finished = self._clock()
            self.last_duration_seconds = finished - started
            # Interval is measured from COMPLETION, matching the task semantics
            # this replaces (last_run_at is a completion stamp) and preventing a
            # slow run from being immediately re-due the moment it lands.
            self._last_finished_at = finished

    def get_status(self) -> dict:
        with self._lock:
            in_flight = self._worker is not None and self._worker.is_alive()
        return {
            "mode": self.mode,
            "enabled": self.enabled,
            "out_path": str(self.out_path),
            "interval_seconds": self.interval_seconds,
            "in_flight": in_flight,
            "runs_completed": self.runs_completed,
            "runs_failed": self.runs_failed,
            "runs_skipped_in_flight": self.runs_skipped_in_flight,
            "last_duration_seconds": self.last_duration_seconds,
            "last_provider_count": self.last_provider_count,
            "last_error": self.last_error,
        }
