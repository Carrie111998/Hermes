"""Durable session-state guards mixin for ContextCompressor (LB6).

Session-lifecycle persistence and anti-thrash bookkeeping: fallback/ineffective
counters, proactive-prune rearm tokens, compression-failure cooldown. Composed
into ContextCompressor as a mixin base (MRO-first).

Part of #78645 + #78647.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional


PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY = "_proactive_prune_rearm_tokens"


class ContextCompressorDurableGuardsMixin:
    """Durable session-state guard helpers (extracted from the godfile)."""

    def on_session_end(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Clear all per-session compaction state at a real session boundary.

        Session end (CLI exit, gateway expiry, session-id rotation) goes
        through this method rather than ``on_session_reset()`` (/new, /reset).
        The original fix (#38788) only cleared ``_previous_summary``, but the
        same cross-session contamination risk applies to every per-session
        variable that ``on_session_reset()`` clears: stale
        ``_ineffective_compression_count`` can suppress compression in a
        subsequent live session; ``_summary_failure_cooldown_until`` can block
        summary generation; ``_last_compress_aborted`` can make callers think
        compression is still aborted; ``_last_aux_model_failure_*`` can surface
        stale error warnings; ``_last_summary_dropped_count`` /
        ``_last_summary_fallback_used`` can produce misleading user warnings.

        ``compress()`` already guards ``_previous_summary`` leakage at the
        point of use; this is defense-in-depth that resets the full per-session
        surface the moment the owning session ends.
        """
        self._previous_summary = None
        self._summary_has_user_turn = None
        self._last_summary_error = None
        self._consecutive_timeout_failures = 0
        self._last_summary_dropped_count = 0
        self._last_summary_fallback_used = False
        self._last_feasibility_skip = False
        self._last_aux_model_failure_error = None
        self._last_aux_model_failure_model = None
        self._last_compression_savings_pct = 100.0
        self._ineffective_compression_count = 0
        self._anti_thrash_recovery_deadline = 0.0
        self._prellm_skip_count = 0
        self._fallback_compression_streak = 0
        self._verify_compaction_cleared_threshold = False
        self._last_compression_made_progress = False
        self._summary_failure_cooldown_until = 0.0
        self._cooldown_persist_failed = False
        self._last_compress_aborted = False
        self._context_probed = False
        self._context_probe_persistable = False
        self.last_real_prompt_tokens = 0
        self.last_compression_rough_tokens = 0
        self.last_rough_tokens_when_real_prompt_fit = 0
        self.awaiting_real_usage_after_compression = False
        self._last_compression_telemetry = None
        self._active_compression_telemetry = None
        self._compression_telemetry_seed = None
        self._proactive_prune_rearm_tokens = 0

    def bind_session_state(self, session_db: Any = None, session_id: str = "") -> None:
        """Bind the current session row so durable cooldowns can round-trip."""
        self._session_db = session_db
        self._session_id = session_id or ""
        self._summary_failure_cooldown_until = 0.0
        self._cooldown_persist_failed = False
        self._last_summary_error = None
        self._consecutive_timeout_failures = 0
        self._fallback_compression_streak = 0
        self._ineffective_compression_count = 0
        self._prellm_skip_count = 0
        self._anti_thrash_recovery_deadline = 0.0
        self._proactive_prune_rearm_tokens = 0
        self.get_active_compression_failure_cooldown()
        self._load_fallback_compression_streak()
        self._load_ineffective_compression_count()
        self._load_proactive_prune_rearm_tokens()

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Bind session-scoped compression state for a new or resumed session."""
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        super().on_session_start(session_id, **kwargs)
        boundary_reason = kwargs.get("boundary_reason")
        old_session_id = kwargs.get("old_session_id")
        session_db = kwargs.get("session_db", getattr(self, "_session_db", None))
        previous_fallback_streak = self._fallback_compression_streak
        previous_ineffective_count = self._ineffective_compression_count
        if boundary_reason == "compression" and old_session_id:
            getter = getattr(session_db, "get_compression_fallback_streak", None)
            if callable(getter):
                try:
                    stored_streak = getter(old_session_id)
                    if isinstance(stored_streak, (int, float, str)):
                        previous_fallback_streak = max(0, int(stored_streak))
                except (TypeError, ValueError, sqlite3.Error) as exc:
                    logger.debug("compression parent fallback streak lookup failed: %s", exc)
                except Exception as exc:
                    logger.debug(
                        "compression parent fallback streak lookup failed (non-sqlite): %s",
                        exc,
                    )
            count_getter = getattr(
                session_db, "get_compression_ineffective_count", None,
            )
            if callable(count_getter):
                try:
                    stored_count = count_getter(old_session_id)
                    if isinstance(stored_count, (int, float, str)):
                        previous_ineffective_count = max(0, int(stored_count))
                except (TypeError, ValueError, sqlite3.Error) as exc:
                    logger.debug(
                        "compression parent ineffective count lookup failed: %s", exc,
                    )
                except Exception as exc:
                    logger.debug(
                        "compression parent ineffective count lookup failed (non-sqlite): %s",
                        exc,
                    )
        self.bind_session_state(session_db, session_id)
        if boundary_reason == "compression":
            # Rotation creates a fresh child row before this callback. Preserve
            # the logical conversation's streak until boundary bookkeeping
            # persists the updated value onto the child row.
            self._fallback_compression_streak = previous_fallback_streak
            # Same for the anti-thrash strike counter — but unlike the streak,
            # no later boundary bookkeeping writes it, so persist the carried
            # value onto the (fresh) child row now. Otherwise a restart between
            # rotation and the next real-usage verdict would silently disarm
            # an armed guard (#54923).
            if self._ineffective_compression_count != previous_ineffective_count:
                self._ineffective_compression_count = previous_ineffective_count
                self._persist_ineffective_compression_count()

    def _load_fallback_compression_streak(self) -> None:
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        getter = getattr(session_db, "get_compression_fallback_streak", None)
        if not session_id or not callable(getter):
            return
        try:
            stored_streak = getter(session_id)
            self._fallback_compression_streak = max(
                0,
                int(stored_streak)
                if isinstance(stored_streak, (int, float, str))
                else 0,
            )
        except (TypeError, ValueError, sqlite3.Error) as exc:
            logger.debug("compression fallback streak lookup failed: %s", exc)
        except Exception as exc:
            logger.debug("compression fallback streak lookup failed (non-sqlite): %s", exc)

    def _load_proactive_prune_rearm_tokens(self) -> None:
        """Restore the cache-boundary runway for a resumed durable session."""
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        getter = getattr(session_db, "get_session_model_config_value", None)
        if not session_id or not callable(getter):
            return
        try:
            value = getter(session_id, PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY, 0)
            self._proactive_prune_rearm_tokens = max(
                0,
                int(value) if isinstance(value, (int, float, str)) else 0,
            )
        except (TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            logger.debug("proactive prune runway lookup failed: %s", exc)
        except Exception as exc:
            logger.debug("proactive prune runway lookup failed (non-sqlite): %s", exc)

    def _clear_durable_proactive_prune_rearm(self) -> None:
        """Remove the persisted runway key without touching the transcript.

        Best-effort companion to zeroing the in-memory mirror at sites that
        void the runway (model switch): without it a restart would reload a
        runway computed under thresholds that no longer apply.
        """
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        patcher = getattr(session_db, "patch_session_model_config", None)
        if not session_id or not callable(patcher):
            return
        try:
            patcher(session_id, {PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY: None})
        except Exception as exc:
            logger.debug("proactive prune runway clear failed: %s", exc)

    def _persist_fallback_compression_streak(self) -> None:
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        setter = getattr(session_db, "set_compression_fallback_streak", None)
        if not session_id or not callable(setter):
            return
        try:
            setter(session_id, self._fallback_compression_streak)
        except sqlite3.Error as exc:
            logger.debug("compression fallback streak persist failed: %s", exc)
        except Exception as exc:
            logger.debug("compression fallback streak persist failed (non-sqlite): %s", exc)

    def _load_ineffective_compression_count(self) -> None:
        """Load the durable anti-thrash strike count for the bound session.

        A fresh compressor on a resumed session starts with
        ``compression_count == 0`` and, historically, an in-memory-only
        ineffective counter — so a guard armed (1 strike) or tripped
        (2 strikes) before a process restart silently disarmed, and a
        near-threshold session could re-compact once per restart forever
        (#54923). The counter now round-trips through the session row like
        the failure cooldown and the fallback streak.
        """
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        getter = getattr(session_db, "get_compression_ineffective_count", None)
        if not session_id or not callable(getter):
            return
        try:
            stored_count = getter(session_id)
            self._ineffective_compression_count = max(
                0,
                int(stored_count)
                if isinstance(stored_count, (int, float, str))
                else 0,
            )
        except (TypeError, ValueError, sqlite3.Error) as exc:
            logger.debug("compression ineffective count lookup failed: %s", exc)
        except Exception as exc:
            logger.debug("compression ineffective count lookup failed (non-sqlite): %s", exc)

    def _persist_ineffective_compression_count(self) -> None:
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        setter = getattr(session_db, "set_compression_ineffective_count", None)
        if not session_id or not callable(setter):
            return
        try:
            setter(session_id, self._ineffective_compression_count)
        except sqlite3.Error as exc:
            logger.debug("compression ineffective count persist failed: %s", exc)
        except Exception as exc:
            logger.debug("compression ineffective count persist failed (non-sqlite): %s", exc)

    def _record_ineffective_compression_verdict(self, count: int) -> None:
        """Set the anti-thrash strike counter, keeping the durable copy in sync.

        Persists only on change so the reset issued by every ordinary fitting
        response (already-zero -> zero) never costs a DB write.
        """
        if count == self._ineffective_compression_count:
            return
        self._ineffective_compression_count = count
        self._persist_ineffective_compression_count()

    def record_completed_compaction(
        self, *, used_fallback: bool = False, feasibility_skip: bool = False,
    ) -> None:
        """Record one completed boundary and its summary quality.

        ``feasibility_skip=True`` marks a deliberate pre-LLM skip (#60451):
        the boundary is streak-NEUTRAL for ``_fallback_compression_streak``
        (neither incremented nor reset). It still arms the real-usage
        effectiveness verdict (``_verify_compaction_cleared_threshold``) on
        purpose — a skipped-summary drop that fails to clear the threshold is
        exactly the incompressible-transcript case the ineffective-strike
        breaker exists for, and its recovery probe bounds the block.
        """
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        self._verify_compaction_cleared_threshold = True
        if feasibility_skip:
            # A deliberate pre-LLM feasibility skip (#60451) is not a
            # summary-quality verdict: it must neither extend a fallback
            # streak (two skips would otherwise latch the >= 2 breaker and
            # disable compression entirely — including the cheap deterministic
            # dropping the skip exists to reach) nor reset one (a skip proves
            # nothing about the summary model's health).
            if not self.quiet_mode:
                logger.info(
                    "Compaction completed via pre-LLM feasibility skip; "
                    "fallback_compression_streak unchanged (%d)",
                    self._fallback_compression_streak,
                )
            return
        if used_fallback:
            self._fallback_compression_streak += 1
            if not self.quiet_mode:
                logger.warning(
                    "Compaction completed with a deterministic fallback summary. "
                    "fallback_compression_streak=%d",
                    self._fallback_compression_streak,
                )
        elif self._fallback_compression_streak:
            self._fallback_compression_streak = 0
        self._persist_fallback_compression_streak()

    def get_active_compression_failure_cooldown(
        self,
        *,
        refresh: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return the live compression-failure cooldown for the bound session."""
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        if refresh:
            # Transaction rollback must distinguish an authoritative empty row
            # from a failed/unavailable durable read. The public return value
            # cannot do so because it deliberately falls back to local state.
            self._last_cooldown_refresh_was_authoritative = None
        now_mono = time.monotonic()
        local_state = None
        if self._summary_failure_cooldown_until > now_mono:
            local_state = {
                "cooldown_until": time.time() + (
                    self._summary_failure_cooldown_until - now_mono
                ),
                "remaining_seconds": self._summary_failure_cooldown_until - now_mono,
                "error": self._last_summary_error,
            }
            if not refresh:
                return local_state

        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        if not session_db or not session_id:
            return local_state

        getter = getattr(session_db, "get_compression_failure_cooldown", None)
        if getter is None:
            return local_state
        try:
            state = getter(session_id)
        except sqlite3.Error as exc:
            if refresh:
                self._last_cooldown_refresh_was_authoritative = False
            logger.debug("compression failure cooldown lookup failed: %s", exc)
            return local_state
        except Exception:
            if refresh:
                self._last_cooldown_refresh_was_authoritative = False
            return local_state
        if refresh:
            self._last_cooldown_refresh_was_authoritative = True
        if not state:
            if refresh:
                if local_state is not None and self._cooldown_persist_failed:
                    # The live local cooldown never made it to the DB (persist
                    # failed), so the empty row is not evidence that another
                    # agent cleared it. Honouring the DB here would re-enable
                    # auto-compress mid-cooldown and reopen the #11529 thrash
                    # window. Keep the local timer authoritative until it
                    # expires or a successful DB read supersedes it.
                    return local_state
                self._summary_failure_cooldown_until = 0.0
                self._last_summary_error = None
            return None

        remaining_seconds = float(state.get("remaining_seconds") or 0.0)
        if remaining_seconds <= 0:
            if refresh:
                if local_state is not None and self._cooldown_persist_failed:
                    return local_state
                self._summary_failure_cooldown_until = 0.0
                self._last_summary_error = None
            return None

        self._summary_failure_cooldown_until = now_mono + remaining_seconds
        self._last_summary_error = state.get("error")
        self._cooldown_persist_failed = False
        return {
            "cooldown_until": float(state.get("cooldown_until") or 0.0),
            "remaining_seconds": remaining_seconds,
            "error": self._last_summary_error,
        }

    def _record_compression_failure_cooldown(
        self,
        cooldown_seconds: float,
        error: Optional[str],
    ) -> None:
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        cooldown_until = time.time() + cooldown_seconds
        self._summary_failure_cooldown_until = time.monotonic() + cooldown_seconds
        self._last_summary_error = error

        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        if not session_db or not session_id:
            return

        recorder = getattr(session_db, "record_compression_failure_cooldown", None)
        if recorder is None:
            self._cooldown_persist_failed = True
            return
        try:
            recorder(session_id, cooldown_until, error)
            self._cooldown_persist_failed = False
        except sqlite3.Error as exc:
            self._cooldown_persist_failed = True
            logger.debug("compression failure cooldown persist failed: %s", exc)
        except Exception as exc:
            self._cooldown_persist_failed = True
            logger.debug("compression failure cooldown persist failed (non-sqlite): %s", exc)

    def record_timeout_failure(self, error: str) -> None:
        """Record a consecutive timeout failure using the shared cooldown ladder.

        Used by both the summary-LLM exception handler (inline at line ~3714)
        and the host-level ``compress_context`` timeout wrapper in
        ``run_compress_context_with_progress_timeout``. Avoids re-implementing
        the ladder at each call site (#62452).
        """
        _TIMEOUT_COOLDOWN_LADDER = (60, 300, 900)
        self._consecutive_timeout_failures = (
            getattr(self, "_consecutive_timeout_failures", 0) + 1
        )
        cooldown = _TIMEOUT_COOLDOWN_LADDER[
            min(self._consecutive_timeout_failures,
                len(_TIMEOUT_COOLDOWN_LADDER)) - 1
        ]
        self._record_compression_failure_cooldown(float(cooldown), error)

    def _clear_compression_failure_cooldown(self) -> None:
        # #76354 review F4: fence check BEFORE cooldown-clear. A late worker
        # whose host already timed out (and recorded a timeout cooldown) must
        # not undo that cooldown when its summary eventually succeeds. The
        # hook is installed by compress_context for the duration of the
        # fenced call; when it reports cancellation, keep the host's cooldown.
        from agent.context_compressor import logger  # noqa: E402 — round-trip seam
        cancelled_check = getattr(self, "_compression_cancelled_check", None)
        if callable(cancelled_check):
            try:
                if cancelled_check():
                    logger.info(
                        "Skipping compression cooldown clear: host already "
                        "cancelled this compression attempt"
                    )
                    return
            except Exception:
                logger.debug(
                    "compression cancellation check failed", exc_info=True
                )
        self._summary_failure_cooldown_until = 0.0
        self._last_summary_error = None
        self._consecutive_timeout_failures = 0
        self._cooldown_persist_failed = False

        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        if not session_db or not session_id:
            return

        clearer = getattr(session_db, "clear_compression_failure_cooldown", None)
        if clearer is None:
            return
        try:
            clearer(session_id)
        except sqlite3.Error as exc:
            logger.debug("compression failure cooldown clear failed: %s", exc)
        except Exception as exc:
            logger.debug("compression failure cooldown clear failed (non-sqlite): %s", exc)
