"""Session-activity tracking for ``AIAgent``

Extracted from ``run_agent.py`` as part of the god-file decomposition
campaign (Phase 3 mechanical mixin lifts).  Behavior-neutral: every method
is lifted verbatim from ``AIAgent``; ``self.*``/``cls.*`` calls resolve
unchanged via the MRO (class attributes referenced through ``cls.`` stay on
``AIAgent``).  The module-level ``logger`` keeps the original logger name
(``"run_agent"``) so log records are unchanged.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from agent.session_activity import ActivityProvenance

logger = logging.getLogger("run_agent")


class ActivityMixin:
    def _touch_activity(
        self,
        desc: str,
        *,
        provenance: Optional[ActivityProvenance] = None,
        force_persist: bool = False,
    ) -> None:
        """Update the last-activity timestamp and description (thread-safe).

        Also bridges to the kanban board's heartbeat fields when this
        process is a dispatcher-spawned worker (HERMES_KANBAN_TASK set),
        so the dispatcher watchdog doesn't reclaim an actively-running
        worker as stale (#31752). Bridge is rate-limited (60s) and
        best-effort — it never raises into the agent loop.

        Separately, rate-limits a durable SessionDB activity projection
        (``last_activity_at`` + bounded description/provenance) so
        CLI/Gateway consumers share one observation source (#72016 / #72039).

        ``provenance`` defaults to ``unknown`` (the ordinary agent activity
        clock). Named values are for special writers (e.g. compression);
        ordinary call sites should leave the default.

        ``force_persist`` bypasses the 60s SessionDB rate limit so a
        terminal stamp (e.g. compression completed) is not dropped.
        """
        from agent.session_activity import (
            bound_activity_description,
            normalize_activity_provenance,
            reset_session_activity_persist_window,
        )

        self._last_activity_ts = time.time()
        self._last_activity_desc = bound_activity_description(desc)
        self._last_activity_provenance = normalize_activity_provenance(provenance)
        if os.environ.get("HERMES_KANBAN_TASK"):
            try:
                from tools.kanban_tools import (
                    heartbeat_current_worker_from_env,
                    inject_new_comments_from_env,
                )
                heartbeat_current_worker_from_env()
                # Fold any new operator notes into the running turn (OUT-OF-BAND
                # steer) so the user can talk to a live task without a restart.
                inject_new_comments_from_env(self)
            except Exception:
                # Never let the bridge break the agent loop.  The function
                # already swallows exceptions internally; this outer guard
                # covers import-time failures (kanban_tools unavailable,
                # etc.) on niche deployment surfaces.
                pass
        if force_persist:
            reset_session_activity_persist_window(self)
        self._persist_session_activity_if_due()

    def _persist_session_activity_if_due(self) -> None:
        """Best-effort durable activity heartbeat for SessionDB consumers.

        Cadence is pinned by SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS
        (>=30s per session, config-independent — see agent/session_activity.py).
        The write rides the standard SessionDB ``_execute_write`` patience
        path via ``touch_session_activity``. Fail-open: a failed heartbeat
        write must NEVER raise into the agent loop (swallow + debug-log).
        """
        session_id = getattr(self, "session_id", None)
        session_db = getattr(self, "_session_db", None)
        if not session_id or session_db is None:
            return
        touch = getattr(session_db, "touch_session_activity", None)
        if not callable(touch):
            return
        from agent.session_activity import (
            SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS,
            normalize_activity_provenance,
        )

        now_mono = time.monotonic()
        last_mono = getattr(self, "_session_activity_last_persist_mono", 0.0)
        if (now_mono - last_mono) < SESSION_ACTIVITY_HEARTBEAT_MIN_INTERVAL_SECONDS:
            return
        self._session_activity_last_persist_mono = now_mono
        try:
            touch(
                session_id,
                getattr(self, "_last_activity_ts", None),
                description=getattr(self, "_last_activity_desc", None),
                provenance=normalize_activity_provenance(
                    getattr(self, "_last_activity_provenance", None)
                ),
            )
        except Exception:
            # Never let durable heartbeat I/O break the agent loop. The
            # heartbeat is an observation-only projection; the next due
            # window retries naturally.
            logger.debug(
                "session activity heartbeat write failed (ignored)",
                exc_info=True,
            )

    def _reset_activity_labels_after_turn(self) -> None:
        """Drop mid-turn activity labels once the turn is no longer running.

        Keeps ``_last_activity_ts`` so idle/watchdog clocks stay continuous
        across interrupt-recursive turns (#15654) and between turns. Clears
        description + provenance so idle cached agents / SessionDB listings
        do not keep advertising the last mid-turn stamp (e.g. compression
        or tool execution) after the turn ended (#72039).
        """
        from agent.session_activity import ActivityProvenance

        self._last_activity_desc = ""
        self._last_activity_provenance = ActivityProvenance.UNKNOWN
        session_id = getattr(self, "session_id", None)
        session_db = getattr(self, "_session_db", None)
        if not session_id or session_db is None:
            return
        clear = getattr(session_db, "clear_session_activity_labels", None)
        if not callable(clear):
            return
        try:
            clear(session_id)
        except Exception:
            # Never let durable cleanup I/O break turn teardown.
            pass

    def get_activity_summary(self) -> dict:
        """Return a snapshot of the agent's current activity for diagnostics.

        Exposes the shared activity observation contract
        (``last_activity_at`` / ``last_activity_description`` /
        ``last_activity_provenance``) plus short aliases
        (``last_activity_ts`` / ``last_activity_desc`` / …) for existing
        gateway and delegate readers.
        """
        from agent.session_activity import (
            ActivityProvenance,
            build_activity_snapshot,
        )

        provenance = getattr(self, "_last_activity_provenance", None)
        if provenance is None:
            provenance = ActivityProvenance.UNKNOWN
        return build_activity_snapshot(
            last_activity_at=getattr(self, "_last_activity_ts", None),
            last_activity_description=getattr(self, "_last_activity_desc", None) or "",
            last_activity_provenance=provenance,
            extra={
            "current_tool": self._current_tool,
            "api_call_count": self._api_call_count,
            "max_iterations": self.max_iterations,
            "budget_used": self.iteration_budget.used,
            "budget_max": self.iteration_budget.max_total,
            },
        )

