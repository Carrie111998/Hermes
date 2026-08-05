"""Session-lifecycle methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, Wave 3
mixin lifts). This mixin holds the session-state cluster: the /queue FIFO
helpers, the /goal continuation machinery, session run-generation guards, the
conversation-scope funnel (``_clear_conversation_scope`` + the
``_CONVERSATION_SCOPED_STATE`` registry), the session expiry + stall watchers,
and session env propagation (``_set_session_env`` / ``_clear_session_env``).

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. The module-level ``logger`` is
``logging.getLogger("gateway.run")`` so log records keep the exact name
(``"gateway.run"``), matching the sibling mixins' convention. run.py module
helpers/constants that stay behind (``_AGENT_PENDING_SENTINEL``, ``_float_env``,
``_STALL_NOTIFY_SEND_TIMEOUT_SECONDS``) are imported lazily inside the method
that uses them — a deferred ``from gateway.run import ...`` resolves at call
time (run.py fully loaded by then), so this module never imports
``gateway.run`` at import time -> no import cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, merge_pending_message_event
from gateway.session import SessionContext, SessionSource, build_session_context_prompt

logger = logging.getLogger("gateway.run")


class GatewaySessionMixin:
    # -------- /queue FIFO helpers --------------------------------------
    # /queue must produce one full agent turn per invocation, in FIFO
    # order, with no merging.  The adapter's _pending_messages dict is a
    # single "next-up" slot (shared with photo-burst follow-ups), so we
    # use it for the head of the queue and an overflow list for the
    # tail.  Enqueue puts new items in the slot when free, otherwise in
    # the overflow.  Promotion (called after each run's drain) moves the
    # next overflow item into the slot so the following recursion picks
    # it up.  Clearing happens on /new and /reset via
    # _handle_reset_command.

    def _enqueue_fifo(self, session_key: str, queued_event: "MessageEvent", adapter: Any) -> None:
        """Append a /queue event to the FIFO chain for a session."""
        if adapter is None:
            return
        pending_slot = getattr(adapter, "_pending_messages", None)
        if pending_slot is None:
            return
        if session_key in pending_slot:
            self._session_state(session_key).conversation.queued_events.append(
                queued_event
            )
        else:
            pending_slot[session_key] = queued_event

    def _promote_queued_event(
        self,
        session_key: str,
        adapter: Any,
        pending_event: Optional["MessageEvent"],
    ) -> Optional["MessageEvent"]:
        """Promote the next overflow item after the slot was drained.

        Called at the drain site after _dequeue_pending_event consumed
        (or failed to consume) the slot.  If there's an overflow item:
          - When pending_event is None (slot was empty), return the
            overflow head as the new pending_event.
          - When pending_event already exists (slot was populated by an
            interrupt follow-up or similar), stage the overflow head in
            the slot so the NEXT recursion picks it up.
        Returns the (possibly updated) pending_event for drain to use.
        """
        _q_state = self._peek_session_state(session_key)
        overflow = _q_state.conversation.queued_events if _q_state else None
        if not overflow:
            return pending_event
        next_queued = overflow.pop(0)
        if pending_event is None:
            return next_queued
        if adapter is not None and hasattr(adapter, "_pending_messages"):
            adapter._pending_messages[session_key] = next_queued
        else:
            # No adapter — push back so we don't silently drop the item.
            overflow.insert(0, next_queued)
        return pending_event

    def _queue_depth(self, session_key: str, *, adapter: Any = None) -> int:
        """Total pending /queue items for a session — slot + overflow."""
        _q_state = self._peek_session_state(session_key)
        depth = len(_q_state.conversation.queued_events) if _q_state else 0
        if adapter is not None and session_key in getattr(adapter, "_pending_messages", {}):
            depth += 1
        return depth

    @staticmethod
    def _is_goal_continuation_event(event_or_text: Any) -> bool:
        """Return True for synthetic /goal continuation turns.

        Goal continuations are normal queued user-role events, so pause/clear
        must distinguish them from real user /queue messages before removing or
        suppressing them.
        """
        text = getattr(event_or_text, "text", event_or_text) or ""
        return str(text).startswith("[Continuing toward your standing goal]\nGoal:")

    def _clear_goal_pending_continuations(self, session_key: str, adapter: Any) -> int:
        """Remove queued synthetic /goal continuations for one session.

        User-issued /goal pause/clear can race with a continuation already
        queued by the judge.  Remove only synthetic goal continuations while
        preserving normal /queue and user follow-up events.
        """
        removed = 0
        pending_slot = getattr(adapter, "_pending_messages", None) if adapter is not None else None
        if isinstance(pending_slot, dict):
            pending_event = pending_slot.get(session_key)
            if self._is_goal_continuation_event(pending_event):
                pending_slot.pop(session_key, None)
                removed += 1

        _q_state = self._peek_session_state(session_key)
        overflow = _q_state.conversation.queued_events if _q_state else []
        if overflow:
            kept = []
            for queued_event in overflow:
                if self._is_goal_continuation_event(queued_event):
                    removed += 1
                else:
                    kept.append(queued_event)
            _q_state.conversation.queued_events = kept
        return removed

    def _goal_still_active_for_session(self, session_id: str) -> bool:
        """Best-effort fresh DB check before running a queued continuation."""
        if not session_id:
            return False
        try:
            from hermes_cli.goals import GoalManager
            return GoalManager(session_id=session_id).is_active()
        except Exception as exc:
            logger.debug("goal continuation: active-state recheck failed: %s", exc)
            return False

    @staticmethod
    def _lookup_session_id_under_store_lock(session_store, session_key: str):
        """Sync helper run in the thread pool: read session_id under the store lock."""
        # noqa: SLF001 — intentional private access; runs off the event loop.
        with session_store._lock:  # noqa: SLF001
            session_store._ensure_loaded_locked()  # noqa: SLF001
            entry = session_store._entries.get(session_key)  # noqa: SLF001
        return getattr(entry, "session_id", None) if entry is not None else None

    # Hard cap on per-session pending follow-ups for busy_input_mode=queue
    # (and the draining/steer-fallback/subagent-demotion paths that share
    # this entry point).  Without a cap, a stuck agent + a rapid-fire user
    # could grow the overflow list unboundedly.  32 turns of queued
    # follow-ups is far beyond any realistic conversational backlog while
    # still small enough to never threaten memory.
    _BUSY_QUEUE_MAX_PENDING = 32

    def _queue_or_replace_pending_event(self, session_key: str, event: MessageEvent) -> None:
        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return
        # #28503 — Previously this called ``merge_pending_message_event``
        # with the default ``merge_text=False``, which silently OVERWROTE
        # the single pending slot when consecutive text messages arrived
        # in ``busy_input_mode: queue``. Route through the FIFO
        # infrastructure shared with ``/queue`` so each follow-up gets
        # its own turn in arrival order. Photo bursts still merge into
        # the head slot via ``merge_pending_message_event`` (album
        # semantics); everything else appends to the overflow tail.
        pending_slot = getattr(adapter, "_pending_messages", None)
        existing = pending_slot.get(session_key) if isinstance(pending_slot, dict) else None
        if existing is not None and (
            getattr(existing, "message_type", None) == MessageType.PHOTO
            or event.message_type == MessageType.PHOTO
            or bool(getattr(existing, "media_urls", None))
            or bool(getattr(event, "media_urls", None))
        ):
            # Preserve photo-burst / media-merge semantics for the head slot.
            merge_pending_message_event(
                adapter._pending_messages,
                session_key,
                event,
                merge_text=event.message_type == MessageType.TEXT,
            )
            return

        if self._queue_depth(session_key, adapter=adapter) >= self._BUSY_QUEUE_MAX_PENDING:
            logger.warning(
                "Dropping busy-mode follow-up for session %s — pending queue at cap (%d).",
                session_key,
                self._BUSY_QUEUE_MAX_PENDING,
            )
            return

        self._enqueue_fifo(session_key, event, adapter)

    async def _session_expiry_watcher(self, interval: int = 300):
        """Background task that finalizes expired sessions.

        Runs every ``interval`` seconds (default 5 min).  For each session
        whose reset policy has expired, invokes ``on_session_finalize``
        hooks, cleans up the cached AIAgent's tool resources, evicts the
        cache entry so it can be garbage-collected, and marks the session
        so it won't be finalized again.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        await asyncio.sleep(60)  # initial delay — let the gateway fully start
        _finalize_failures: dict[str, int] = {}  # session_id -> consecutive failure count
        _MAX_FINALIZE_RETRIES = 3
        while self._running:
            try:
                await self.async_session_store._ensure_loaded()
                # Collect expired sessions first, then log a single summary.
                _expired_entries = []
                for key, entry in list(self.session_store._entries.items()):
                    if entry.expiry_finalized:
                        continue
                    if not await self.async_session_store._is_session_expired(entry):
                        continue
                    _expired_entries.append((key, entry))

                if _expired_entries:
                    # Extract platform names from session keys for a compact summary.
                    # Keys look like "agent:main:telegram:dm:12345" — platform is field [2].
                    _platforms: dict[str, int] = {}
                    for _k, _e in _expired_entries:
                        _parts = _k.split(":")
                        _plat = _parts[2] if len(_parts) > 2 else "unknown"
                        _platforms[_plat] = _platforms.get(_plat, 0) + 1
                    _plat_summary = ", ".join(
                        f"{p}:{c}" for p, c in sorted(_platforms.items())
                    )
                    logger.info(
                        "Session expiry: %d sessions to finalize (%s)",
                        len(_expired_entries), _plat_summary,
                    )

                for key, entry in _expired_entries:
                    try:
                        try:
                            from hermes_cli.lifecycle import finalize_session
                            _parts = key.split(":")
                            _platform = _parts[2] if len(_parts) > 2 else ""
                            finalize_session(
                                session_id=entry.session_id,
                                platform=_platform,
                                reason="session_expired",
                            )
                        except Exception:
                            pass
                        # Shut down memory provider and close tool resources
                        # on the cached agent.  Idle agents live in
                        # _agent_cache (not _running_agents), so look there.
                        _cached_agent = None
                        _cache_lock = getattr(self, "_agent_cache_lock", None)
                        if _cache_lock is not None:
                            with _cache_lock:
                                _cached = self._agent_cache.get(key)
                                _cached_agent = _cached[0] if isinstance(_cached, tuple) else _cached if _cached else None
                        # Fall back to _running_agents in case the agent is
                        # still mid-turn when the expiry fires.
                        if _cached_agent is None:
                            _exp_state = self._peek_session_state(key)
                            _cached_agent = _exp_state.turn.agent if _exp_state else None
                        if _cached_agent and _cached_agent is not _AGENT_PENDING_SENTINEL:
                            await self._cleanup_agent_resources_off_loop(
                                _cached_agent, context="session expiry"
                            )
                        # Drop the cache entry so the AIAgent (and its LLM
                        # clients, tool schemas, memory provider refs) can
                        # be garbage-collected.  Otherwise the cache grows
                        # unbounded across the gateway's lifetime.
                        self._evict_cached_agent(key)
                        # Permanently finalizing this session — one funnel
                        # call drops every conversation-scoped dict AND the
                        # boundary security state (approvals, update
                        # prompts, slash-confirm) so the dicts don't grow
                        # unbounded across the gateway's lifetime. (Idle
                        # agent-cache eviction must NOT do this: the
                        # session is still alive and a resumed turn rebuilds
                        # its agent from these overrides. Only true session
                        # finalization, /new, and /reset clear them.) See
                        # _CONVERSATION_SCOPED_STATE.
                        self._clear_conversation_scope(
                            key, reason="expiry_finalized"
                        )
                        # Persist the finalized flag to sessions.json AND
                        # state.db (single write-path, #9006) — also drops
                        # the persisted /model override, since finalization
                        # is a conversation boundary.
                        await self.async_session_store.set_expiry_finalized(entry)
                        logger.debug(
                            "Session expiry finalized for %s",
                            entry.session_id,
                        )
                        _finalize_failures.pop(entry.session_id, None)
                    except Exception as e:
                        failures = _finalize_failures.get(entry.session_id, 0) + 1
                        _finalize_failures[entry.session_id] = failures
                        if failures >= _MAX_FINALIZE_RETRIES:
                            logger.warning(
                                "Session finalize gave up after %d attempts for %s: %s. "
                                "Marking as finalized to prevent infinite retry loop.",
                                failures, entry.session_id, e,
                            )
                            await self.async_session_store.set_expiry_finalized(
                                entry, clear_model_override=False
                            )
                            _finalize_failures.pop(entry.session_id, None)
                        else:
                            logger.debug(
                                "Session finalize failed (%d/%d) for %s: %s",
                                failures, _MAX_FINALIZE_RETRIES, entry.session_id, e,
                            )

                if _expired_entries:
                    _done = sum(
                        1 for _, e in _expired_entries if e.expiry_finalized
                    )
                    _failed = len(_expired_entries) - _done
                    if _failed:
                        logger.info(
                            "Session expiry done: %d finalized, %d pending retry",
                            _done, _failed,
                        )
                    else:
                        logger.info(
                            "Session expiry done: %d finalized", _done,
                        )

                # Sweep agents that have been idle beyond the TTL regardless
                # of session reset policy.  This catches sessions with very
                # long / "never" reset windows, whose cached AIAgents would
                # otherwise pin memory for the gateway's entire lifetime.
                try:
                    _idle_evicted = self._sweep_idle_cached_agents()
                    if _idle_evicted:
                        logger.info(
                            "Agent cache idle sweep: evicted %d agent(s)",
                            _idle_evicted,
                        )
                except Exception as _e:
                    logger.debug("Idle agent sweep failed: %s", _e)

                # Periodically prune stale SessionStore entries.  The
                # in-memory dict (and sessions.json) would otherwise grow
                # unbounded in gateways serving many rotating chats /
                # threads / users over long time windows.  Pruning is
                # invisible to users — a resumed session just gets a
                # fresh session_id, exactly as if the reset policy fired.
                _last_prune_ts = getattr(self, "_last_session_store_prune_ts", 0.0)
                _prune_interval = 3600.0  # once per hour
                if time.time() - _last_prune_ts > _prune_interval:
                    try:
                        _max_age = int(
                            getattr(self.config, "session_store_max_age_days", 0) or 0
                        )
                        if _max_age > 0:
                            _pruned = await self.async_session_store.prune_old_entries(_max_age)
                            if _pruned:
                                logger.info(
                                    "SessionStore prune: dropped %d stale entries",
                                    _pruned,
                                )
                    except Exception as _e:
                        logger.debug("SessionStore prune failed: %s", _e)
                    self._last_session_store_prune_ts = time.time()
            except Exception as e:
                logger.debug("Session expiry watcher error: %s", e)
            # Sleep in small increments so we can stop quickly
            for _ in range(interval):
                if not self._running:
                    break
                await asyncio.sleep(1)

    def _session_stall_timeout_seconds(self) -> float:
        """Return configured stall timeout (seconds); 0 disables the watchdog."""
        from gateway.run import _float_env
        return _float_env("HERMES_SESSION_STALL_TIMEOUT", 300)

    def _session_activity_for_stall(self, session_key: str) -> Optional[dict]:
        """Return the shared activity snapshot for stall progress (#72039).

        Single progress source: ``AIAgent.get_activity_summary()`` /
        ``agent.session_activity``. No turn-start or pending-inbound clocks.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        agent = (getattr(self, "_running_agents", None) or {}).get(session_key)
        if agent is None or agent is _AGENT_PENDING_SENTINEL:
            return None
        if not hasattr(agent, "get_activity_summary"):
            return None
        try:
            summary = agent.get_activity_summary()
        except Exception:
            return None
        return summary if isinstance(summary, dict) else None

    async def _check_session_stalls(self, timeout_seconds: float) -> int:
        """Scan pending inbound sessions and notify once per stall episode.

        Returns the number of notifications sent this pass (for tests).
        """
        from gateway.run import _STALL_NOTIFY_SEND_TIMEOUT_SECONDS
        from gateway.session_stall import (
            format_session_stall_notification,
            resolve_session_idle_seconds_from_activity,
            should_clear_session_stall_notification,
            should_emit_session_stall_notification,
        )

        notified_map = getattr(self, "_session_stall_notified", None)
        if notified_map is None:
            notified_map = {}
            self._session_stall_notified = notified_map

        sent = 0
        now = time.time()
        candidates: Dict[str, tuple[Any, Any]] = {}

        for adapter in self._iter_gateway_adapters():
            pending_slot = getattr(adapter, "_pending_messages", None) or {}
            for session_key, event in list(pending_slot.items()):
                if session_key and session_key not in candidates and event is not None:
                    candidates[session_key] = (adapter, event)

        for session_key, overflow in list(
            (getattr(self, "_queued_events", None) or {}).items()
        ):
            if not session_key or session_key in candidates or not overflow:
                continue
            event = overflow[0]
            source = getattr(event, "source", None)
            adapter = (
                self._adapter_for_source(source) if source is not None else None
            )
            if adapter is None:
                continue
            candidates[session_key] = (adapter, event)

        for session_key, (adapter, pending_event) in list(candidates.items()):
            has_pending = pending_event is not None
            activity = (
                self._session_activity_for_stall(session_key) if has_pending else None
            )
            idle_seconds = (
                resolve_session_idle_seconds_from_activity(activity, now=now)
                if has_pending
                else None
            )
            already = bool(notified_map.get(session_key))
            if should_clear_session_stall_notification(
                timeout_seconds=timeout_seconds,
                idle_seconds=idle_seconds,
                has_pending_inbound=has_pending,
            ):
                notified_map.pop(session_key, None)
                already = False
            if not should_emit_session_stall_notification(
                timeout_seconds=timeout_seconds,
                idle_seconds=idle_seconds,
                has_pending_inbound=has_pending,
                already_notified=already,
            ):
                continue

            if idle_seconds is None:
                continue
            mins = max(1, int(idle_seconds // 60))
            activity = activity or {}
            logger.warning(
                "Session stall detected: session=%s idle=%.0fs "
                "(timeout=%.0fs, ~%d min); pending inbound present "
                "| last_activity=%s | provenance=%s "
                "(agent.session_stall_timeout)",
                session_key,
                idle_seconds,
                timeout_seconds,
                mins,
                activity.get("last_activity_desc")
                or activity.get("last_activity_description")
                or "unknown",
                activity.get("provenance")
                or activity.get("last_activity_provenance")
                or "unknown",
            )
            source = getattr(pending_event, "source", None)
            chat_id = getattr(source, "chat_id", None) if source is not None else None
            if not chat_id:
                logger.warning(
                    "Session stall notify skipped (no chat_id): session=%s",
                    session_key,
                )
                # Cannot deliver; latch to avoid log spam every tick.
                notified_map[session_key] = True
                continue
            # #76354 review S2: re-read pending state + activity timestamp
            # IMMEDIATELY before delivery. The snapshot above ages while
            # earlier candidates in this pass await their sends; an agent
            # that made progress (or drained its queue) in that window must
            # not receive a false stall notice. Abort and leave the latch
            # un-set so the next tick re-evaluates from scratch.
            still_pending = (
                (getattr(adapter, "_pending_messages", None) or {}).get(
                    session_key
                )
                is not None
                or bool(
                    (getattr(self, "_queued_events", None) or {}).get(
                        session_key
                    )
                )
            )
            fresh_idle = resolve_session_idle_seconds_from_activity(
                self._session_activity_for_stall(session_key),
                now=time.time(),
            )
            if not still_pending or (
                fresh_idle is not None and fresh_idle < timeout_seconds
            ):
                logger.info(
                    "Session stall notify aborted (no longer stale): "
                    "session=%s pending=%s fresh_idle=%s",
                    session_key,
                    still_pending,
                    fresh_idle,
                )
                # Re-arm: drop any stale latch so a FUTURE genuine stall
                # episode notifies again.
                notified_map.pop(session_key, None)
                continue
            try:
                metadata = (
                    self._thread_metadata_for_source(source)
                    if source is not None and hasattr(self, "_thread_metadata_for_source")
                    else None
                )
                # Round-2 #2: bound the send. A wedged adapter transport
                # (network hang, dead websocket) must not block the whole
                # watcher pass — sibling candidates in this loop would never
                # be evaluated and the watcher itself would stop ticking.
                try:
                    result = await asyncio.wait_for(
                        adapter.send(
                            str(chat_id),
                            format_session_stall_notification(idle_seconds),
                            metadata=metadata,
                        ),
                        timeout=_STALL_NOTIFY_SEND_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "Session stall notify send timed out after %.0fs "
                        "for %s; will retry next tick",
                        _STALL_NOTIFY_SEND_TIMEOUT_SECONDS,
                        session_key,
                    )
                    continue  # do not latch; retry next tick
                # Adapters often return SendResult(success=False) instead of raising.
                if result is not None and getattr(result, "success", True) is False:
                    logger.warning(
                        "Session stall notify failed for %s: %s",
                        session_key,
                        getattr(result, "error", "send returned success=False"),
                    )
                    continue  # do not latch; retry next tick
                sent += 1
                notified_map[session_key] = True
            except Exception as exc:
                logger.warning(
                    "Session stall notify failed for %s: %s",
                    session_key,
                    exc,
                )
                # Do not latch — retry next watcher tick until delivery or episode clear.

        # Drop latches for sessions that no longer appear in any pending map.
        for key in list(notified_map.keys()):
            if key not in candidates:
                notified_map.pop(key, None)

        return sent

    async def _session_stall_watcher(self, interval: float = 30.0):
        """Periodic pending-inbound + stale-activity stall watchdog (#72016).

        Progress comes only from ``get_activity_summary()`` (#72039).
        Pending inbound is a notify policy gate, not a progress clock.
        Notify-only: does not kill the turn (contrast ``gateway_timeout`` /
        ``shutdown_watchdog``).
        """
        # Short initial delay so startup reconnect noise does not false-fire.
        await asyncio.sleep(min(30.0, max(1.0, float(interval))))
        while self._running:
            try:
                timeout = self._session_stall_timeout_seconds()
                if timeout > 0:
                    await self._check_session_stalls(timeout)
            except Exception as exc:
                logger.debug("Session stall watcher error: %s", exc)
            # Interruptible sleep
            steps = max(1, int(float(interval)))
            for _ in range(steps):
                if not self._running:
                    break
                await asyncio.sleep(1)

    def _sibling_thread_run_keys(self, source: SessionSource, own_key: str) -> list:
        """Find running-agent keys for OTHER participants in the same thread.

        Only applies when the message originates in a thread.  In per-user
        thread mode (``thread_sessions_per_user=True``) each participant gets
        an isolated session key of the form
        ``agent:main:{platform}:{chat_type}:{chat_id}:{thread_id}:{user_id}``,
        so a run started by another user is invisible to the caller's own
        ``/stop``.  This returns the keys of any *actually running* agents
        (not the pending sentinel, not the caller's own key) whose key shares
        the caller's ``{chat_id}:{thread_id}`` prefix.

        Returns an empty list when the source is not in a thread, or when no
        sibling runs exist — callers must still gate on authorization.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        thread_id = getattr(source, "thread_id", None)
        chat_id = getattr(source, "chat_id", None)
        if not thread_id or not chat_id:
            return []
        platform = source.platform.value
        chat_type = getattr(source, "chat_type", None) or ""
        # Prefix that every per-user key in this thread shares, up to and
        # including the thread_id segment.  Matching either the exact
        # shared-thread key or any key with a further (user_id) segment
        # (prefix + ":") avoids cross-matching an unrelated thread whose id
        # merely starts with this one.
        prefix = ":".join(
            ["agent:main", platform, chat_type, str(chat_id), str(thread_id)]
        )
        matches = []
        for key, agent in self._running_agent_items():
            if key == own_key:
                continue
            if agent is _AGENT_PENDING_SENTINEL or not agent:
                continue
            if key == prefix or key.startswith(prefix + ":"):
                matches.append(key)
        return matches

    # ────────────────────────────────────────────────────────────────
    # /goal — persistent cross-turn goals (Ralph-style loop)
    # ────────────────────────────────────────────────────────────────

    def _goal_max_turns_from_config(self) -> int:
        """Resolve the configured /goal turn budget for gateway sessions.

        GatewayRunner.config is a GatewayConfig dataclass, not the full
        user config mapping. Top-level config blocks such as ``goals`` are
        therefore only available through hermes_cli.config.load_config().
        """
        try:
            goals_cfg = (
                (self.config or {}).get("goals", {})
                if isinstance(self.config, dict)
                else getattr(self.config, "goals", {}) or {}
            )
            if not goals_cfg:
                from hermes_cli.config import load_config

                goals_cfg = (load_config() or {}).get("goals") or {}
            return int(goals_cfg.get("max_turns", 20) or 20)
        except Exception:
            return 20

    async def _get_goal_manager_for_event(self, event: "MessageEvent"):
        """Return a GoalManager bound to the session for this gateway event.

        Returns ``(manager, session_entry)`` or ``(None, None)`` if the
        goals module can't be loaded.
        """
        try:
            from hermes_cli.goals import GoalManager
        except Exception as exc:
            logger.debug("goal manager unavailable: %s", exc)
            return None, None
        try:
            session_entry = await self.async_session_store.get_or_create_session(event.source)
        except Exception as exc:
            logger.debug("goal manager: session lookup failed: %s", exc)
            return None, None
        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return None, None
        max_turns = self._goal_max_turns_from_config()
        return GoalManager(session_id=sid, default_max_turns=max_turns), session_entry

    async def _send_goal_status_notice(self, source: Any, message: str) -> None:
        """Send a /goal judge status line back to the originating chat/thread."""
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
            return

        try:
            metadata = self._thread_metadata_for_source(source)
        except Exception:
            metadata = None

        result = await adapter.send(source.chat_id, message, metadata=metadata)
        if result is not None and not getattr(result, "success", True):
            logger.warning(
                "goal continuation: status send failed: %s",
                getattr(result, "error", "unknown error"),
            )

    async def _defer_goal_status_notice_after_delivery(self, source: Any, message: str) -> None:
        """Send a /goal status line after the main response is delivered.

        The gateway message handler returns the agent response to the platform
        adapter, which sends it after this method's caller has returned.  For a
        natural Discord/Telegram reading order, goal status belongs after that
        send.  Platform adapters provide a one-shot post-delivery callback for
        exactly this boundary; when unavailable, fall back to direct awaited
        delivery rather than silently dropping the notice.
        """
        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.debug("goal continuation: no adapter for %s", getattr(source, "platform", None))
            return

        async def _deliver() -> None:
            try:
                await self._send_goal_status_notice(source, message)
            except Exception as exc:
                logger.warning("goal continuation: status send failed: %s", exc, exc_info=True)

        try:
            session_key = self._session_key_for_source(source)
        except Exception:
            session_key = None

        if session_key and hasattr(adapter, "register_post_delivery_callback"):
            try:
                generation = None
                active = getattr(adapter, "_active_sessions", {}).get(session_key)
                if active is not None:
                    generation = getattr(active, "_hermes_run_generation", None)
                adapter.register_post_delivery_callback(
                    session_key,
                    _deliver,
                    generation=generation,
                )
                return
            except Exception as exc:
                logger.debug("goal continuation: post-delivery callback registration failed: %s", exc)

        await _deliver()

    async def _post_turn_goal_continuation(
        self,
        *,
        session_entry: Any,
        source: Any,
        final_response: str,
    ) -> None:
        """Run the goal judge after a gateway turn and, if still active,
        enqueue a continuation prompt for the same session.

        Called from ``_handle_message_with_agent`` at turn boundary, AFTER
        the response has been delivered. Safe when no goal is set.

        We use the adapter's pending-message / FIFO machinery so any real
        user message that arrives simultaneously is handled by the same
        queue and takes priority naturally.
        """
        try:
            from hermes_cli.goals import GoalManager
        except Exception as exc:
            logger.debug("goal continuation: goals module unavailable: %s", exc)
            return

        sid = getattr(session_entry, "session_id", None) or ""
        if not sid:
            return

        max_turns = self._goal_max_turns_from_config()

        mgr = GoalManager(session_id=sid, default_max_turns=max_turns)
        if not mgr.is_active():
            return

        try:
            from hermes_cli.goals import gather_background_processes as _gather_bg
            _bg_procs = _gather_bg()
        except Exception:
            _bg_procs = None

        decision = mgr.evaluate_after_turn(
            final_response or "",
            user_initiated=True,
            background_processes=_bg_procs,
        )
        msg = decision.get("message") or ""

        # Defer the status line until after the adapter has delivered the
        # agent's visible final response. The judge runs after the response is
        # produced but before BasePlatformAdapter sends it, so sending here
        # would show "✓ Goal achieved" before the answer itself. Registering
        # an awaited post-delivery callback preserves delivery reliability
        # without reversing the user-visible ordering.
        if msg and source is not None:
            await self._defer_goal_status_notice_after_delivery(source, msg)

        if not decision.get("should_continue"):
            return

        prompt = decision.get("continuation_prompt") or ""
        if not prompt or source is None:
            return

        # Enqueue via the adapter's FIFO so a user message already in
        # flight preempts the continuation naturally.
        try:
            adapter = self._adapter_for_source(source)
            _quick_key = self._session_key_for_source(source)
            if adapter and _quick_key:
                cont_event = MessageEvent(
                    text=prompt,
                    message_type=MessageType.TEXT,
                    source=source,
                    message_id=None,
                    channel_prompt=None,
                )
                self._enqueue_fifo(_quick_key, cont_event, adapter)
        except Exception as exc:
            logger.debug("goal continuation: enqueue failed: %s", exc)

    def _set_session_env(self, context: SessionContext) -> list:
        """Set session context variables for the current async task.

        Uses ``contextvars`` instead of ``os.environ`` so that concurrent
        gateway messages cannot overwrite each other's session state.

        Returns a list of reset tokens; pass them to ``_clear_session_env``
        in a ``finally`` block.
        """
        from gateway.session_context import set_session_vars
        # Propagate the adapter's async-delivery capability so async tools
        # (terminal notify_on_complete / watch_patterns, delegate_task
        # background=True) know whether this channel can wake a later turn.
        # Default True keeps CLI / unknown paths working; stateless adapters
        # (api_server) declare supports_async_delivery=False. Use getattr so
        # bare runners built via object.__new__ (tests) without self.adapters
        # don't blow up — they simply default to supported.
        _adapters = getattr(self, "adapters", None) or {}
        _adapter = _adapters.get(context.source.platform)
        _async_delivery = getattr(_adapter, "supports_async_delivery", True)
        return set_session_vars(
            platform=context.source.platform.value,
            chat_id=context.source.chat_id,
            chat_type=(
                str(context.source.chat_type) if context.source.chat_type else ""
            ),
            chat_name=context.source.chat_name or "",
            thread_id=str(context.source.thread_id) if context.source.thread_id else "",
            user_id=str(context.source.user_id) if context.source.user_id else "",
            user_name=str(context.source.user_name) if context.source.user_name else "",
            session_key=context.session_key,
            message_id=str(context.source.message_id) if context.source.message_id else "",
            profile=getattr(context.source, "profile", "") or "",
            async_delivery=_async_delivery,
            cron_session="",
        )

    def _clear_session_env(self, tokens: list) -> None:
        """Restore session context variables to their pre-handler values."""
        from gateway.session_context import clear_session_vars
        clear_session_vars(tokens)

    def _release_running_agent_state(
        self,
        session_key: str,
        *,
        run_generation: Optional[int] = None,
    ) -> bool:
        """Pop ALL per-running-agent state entries for ``session_key``.

        Replaces ad-hoc ``del self._running_agents[key]`` calls scattered
        across the gateway.  Those sites had drifted: some popped only
        ``_running_agents``; some also ``_running_agents_ts``; only one
        path also cleared ``_busy_ack_ts``.  Each missed entry was a
        small, persistent leak — a (str_key → float) tuple per session
        per gateway lifetime.

        Use this at every site that ends a running turn, regardless of
        cause (normal completion, /stop, /reset, /resume, sentinel
        cleanup, stale-eviction).  Per-session state that PERSISTS
        across turns (``_session_model_overrides``, ``_voice_mode``,
        ``_pending_approvals``, ``_update_prompt_pending``) is NOT
        touched here — those have their own lifecycles.

        When ``run_generation`` is provided, only clear the slot if that
        generation is still current for the session.  This prevents an
        older async run whose generation was bumped by /stop or /new from
        clobbering a newer run's state during its own unwind.  Returns
        True when the slot was cleared, False when an ownership guard
        blocked it.
        """
        if not session_key:
            return False
        if run_generation is not None and not self._is_session_run_current(
            session_key, run_generation
        ):
            return False
        state = self._peek_session_state(session_key)
        if state is not None:
            lease = state.turn.lease
            if lease is not None:
                try:
                    lease.release()
                except Exception:
                    logger.debug(
                        "Failed to release active session slot", exc_info=True
                    )
            # One structured reset instead of the old drifting pop-list
            # (agent / started_ts / lease / busy_ack_ts).  Turn-lease tokens
            # are deliberately NOT cleared here — _release_turn_lease owns
            # them (#64934).
            state.turn.clear()
        # Turn boundary: a running-agent slot was just released.  Persist the
        # new (lower) in-flight count so the dashboard readout stays current
        # between lifecycle transitions.  Preserves gateway_state (see
        # _persist_active_agents).
        self._persist_active_agents()
        return True

    def _release_turn_lease(self, session_key: str, run_generation: int) -> bool:
        """Release the turn lease acquired by (``session_key``, ``run_generation``).

        Companion to the acquisition in ``_handle_message_with_agent``
        (#64934). The token map is keyed by (routing key, run generation), so
        this can only ever free the lease its own turn acquired — a stale
        unwind whose generation was bumped by /stop or /new pops ITS token,
        and the registry's identity check refuses it if a newer turn already
        holds the lease. Idempotent and safe for bare test runners built via
        ``object.__new__`` (getattr defaults).
        """
        if not session_key:
            return False
        registry = getattr(self, "_turn_leases", None)
        state = self._peek_session_state(session_key)
        if state is None or registry is None:
            return False
        turn = state.turn
        if turn.lease_token is None or turn.lease_generation != run_generation:
            return False
        token = turn.lease_token
        turn.lease_token = None
        turn.lease_generation = None
        try:
            return registry.release(token)
        except Exception:
            logger.debug("Failed to release turn lease", exc_info=True)
            return False

    def _rebind_turn_lease(
        self, session_key: str, run_generation: int, new_session_id: str
    ) -> bool:
        """Follow a mid-turn session_id rotation with the held turn lease.

        Compression (session-hygiene pre-compression or the agent's own
        compressor) can rotate ``session_entry.session_id`` while this turn
        is in flight. The turn's flush targets the NEW id, so the
        serialization boundary must follow it — otherwise an alias routing
        key resolving the new id (topic tip-walk onto the fresh child) could
        start a concurrent turn the lease never sees (#64934 rotation-alias
        window). Call at every site that reassigns session_entry.session_id
        mid-turn. Fail-open no-op when there is no held token.
        """
        if not session_key or not new_session_id:
            return False
        registry = getattr(self, "_turn_leases", None)
        state = self._peek_session_state(session_key)
        if state is None or registry is None:
            return False
        turn = state.turn
        if turn.lease_token is None or turn.lease_generation != run_generation:
            return False
        try:
            return registry.rebind(turn.lease_token, new_session_id)
        except Exception:
            logger.debug("Failed to rebind turn lease", exc_info=True)
            return False

    def _clear_conversation_scope(self, session_key: str, *, reason: str) -> None:
        """Clear ALL conversation-scoped per-session state for ``session_key``.

        THE single conversation-boundary funnel. Call this — and nothing
        else — whenever a session_key crosses a conversation boundary:
        /new, /resume, auto-reset (idle/daily/suspended), expiry
        finalization, and the compression-exhausted auto-reset.

        Why a funnel: these boundaries used to each carry a hand-copied
        pop-list of the per-session dicts, and the lists drifted every time
        a new dict was added (#48031, #58403, #10702, #35809 were all
        "boundary X forgot dict Y" bugs — e.g. /new cleared the /model
        override but not the /model --once restore snapshot). Adding a new
        conversation-scoped dict now means adding its attribute name to
        _CONVERSATION_SCOPED_STATE below; every boundary picks it up
        automatically.

        Scope rules:
        - Conversation-scoped (cleared here): model/reasoning overrides,
          one-turn restore snapshots, pending model notes, last-resolved
          model cache, queued follow-up events, and the boundary security
          state (approvals, /yolo, slash-confirm, update prompts).
        - Turn-scoped (NOT cleared here): _running_agents/_ts, slot leases,
          turn-lease tokens — owned by _release_running_agent_state and the
          dispatch finally.
        - Idle agent-cache eviction is NOT a conversation boundary: the
          session is still alive and a resumed turn rebuilds from these
          overrides. Only true boundaries call this.

        Safe on bare test runners built via ``object.__new__`` (every
        access is getattr-guarded).
        """
        if not session_key:
            return
        # Structural clear: every conversation-scoped field resets in one
        # call — no per-attribute pop-list to drift.
        state = self._peek_session_state(session_key)
        if state is not None:
            state.conversation.clear()
        # Legacy plain-dict stores still registered in
        # _CONVERSATION_SCOPED_STATE (not yet folded into SessionState),
        # e.g. _pending_model_notes.  SessionState-backed names resolve to
        # MutableMapping views (not dict), so the isinstance(dict) guard
        # skips them — already handled above.
        for attr in _CONVERSATION_SCOPED_STATE:
            store = getattr(self, attr, None)
            if isinstance(store, dict):
                store.pop(session_key, None)
        self._clear_session_boundary_security_state(session_key)
        logger.debug(
            "Cleared conversation scope for %s (%s)", session_key, reason
        )

    def _clear_session_boundary_security_state(self, session_key: str) -> None:
        """Clear per-session control state that must not survive a boundary switch."""
        if not session_key:
            return

        pending_skills_reload_notes = getattr(
            self, "_pending_skills_reload_notes", None
        )
        if isinstance(pending_skills_reload_notes, dict):
            pending_skills_reload_notes.pop(session_key, None)

        _sec_state = self._peek_session_state(session_key)
        if _sec_state is not None:
            _sec_state.persistent.approvals = None
            _sec_state.persistent.update_prompt_pending = False

        try:
            from tools import slash_confirm as _slash_confirm_mod
        except Exception:
            _slash_confirm_mod = None
        if _slash_confirm_mod is not None:
            try:
                _slash_confirm_mod.clear(session_key)
            except Exception as e:
                logger.debug(
                    "Failed to clear slash-confirm state for session boundary %s: %s",
                    session_key,
                    e,
                )

        try:
            from tools.approval import clear_session as _clear_approval_session
        except Exception:
            return

        try:
            _clear_approval_session(session_key)
        except Exception as e:
            logger.debug(
                "Failed to clear approval state for session boundary %s: %s",
                session_key,
                e,
            )

    def _begin_session_run_generation(self, session_key: str) -> int:
        """Claim a fresh run generation token for ``session_key``.

        Every top-level gateway turn gets a monotonically increasing token.
        If a later command like /stop or /new invalidates that token while the
        old worker is still unwinding, the late result can be recognized and
        dropped instead of bleeding into the fresh session.
        """
        if not session_key:
            return 0
        persistent = self._session_state(session_key).persistent
        # Monotonic by design (#28686): incremented here, NEVER reset.
        persistent.run_generation = int(persistent.run_generation) + 1
        return persistent.run_generation

    def _invalidate_session_run_generation(self, session_key: str, *, reason: str = "") -> int:
        """Invalidate any in-flight run token for ``session_key``."""
        generation = self._begin_session_run_generation(session_key)
        if reason:
            logger.info(
                "Invalidated run generation for %s → %d (%s)",
                session_key,
                generation,
                reason,
            )
        return generation

    def _is_session_run_current(self, session_key: str, generation: int) -> bool:
        """Return True when ``generation`` is still current for ``session_key``."""
        if not session_key:
            return True
        state = self._peek_session_state(session_key)
        current = state.persistent.run_generation if state is not None else 0
        return int(current) == int(generation)

    def _bind_adapter_run_generation(
        self,
        adapter: Any,
        session_key: str,
        generation: int | None,
    ) -> None:
        """Bind a gateway run generation to the adapter's active-session event."""
        if not adapter or not session_key or generation is None:
            return
        try:
            interrupt_event = getattr(adapter, "_active_sessions", {}).get(session_key)
            if interrupt_event is not None:
                setattr(interrupt_event, "_hermes_run_generation", int(generation))
        except Exception:
            pass

    def _set_pending_turn_sidecar_notes(self, session_key: str, notes: List[str]) -> None:
        """Stage per-turn must-deliver notes for the next agent run (one-shot)."""
        if not session_key or not notes:
            return
        self._session_state(session_key).conversation.sidecar_notes = list(notes)

    def _consume_pending_turn_sidecar_notes(self, session_key: str) -> List[str]:
        if not session_key:
            return []
        state = self._peek_session_state(session_key)
        if state is None:
            return []
        staged = state.conversation.sidecar_notes
        state.conversation.sidecar_notes = []
        return list(staged) if isinstance(staged, list) else []

    def _pinned_session_context_prompt(
        self, context, redact_pii: bool, session_key: Optional[str]
    ) -> str:
        """Return the session-context prompt, pinned per session.

        Key hit → the pinned bytes are reused VERBATIM (immunizes the
        composed system prompt against renderer nondeterminism); key miss →
        re-render ``build_session_context_prompt`` and re-pin (a legitimate
        cache bust: rename, topic edit, /sethome, redact_pii flip, ...).
        """
        _eph_key = self._ephemeral_change_key(context, redact_pii)
        _eph_pin = None
        if session_key:
            _pin_state = self._peek_session_state(session_key)
            _eph_pin = _pin_state.conversation.ephemeral_pin if _pin_state else None
        if _eph_pin is not None and _eph_pin[0] == _eph_key:
            return _eph_pin[1]
        text = build_session_context_prompt(context, redact_pii=redact_pii)
        if session_key:
            self._session_state(session_key).conversation.ephemeral_pin = (
                _eph_key,
                text,
            )
        return text

    @staticmethod
    def _ephemeral_change_key(context, redact_pii: bool) -> str:
        """Hash the exact inputs ``build_session_context_prompt`` renders.

        This key decides when the pinned per-session context-prompt bytes are
        reused verbatim vs re-rendered.  The maintained invariant (guarded by
        the parity test in tests/gateway/test_prompt_tail_freeze.py): any
        input whose change alters the rendered bytes MUST appear here —
        omission means a stale pinned prompt (cosmetic staleness); inclusion
        of an extra field only costs a spurious re-render.
        """
        import hashlib

        src = context.source
        platform = src.platform.value if src.platform else ""

        discord_ids: tuple = ()
        discord_tools = ""
        if src.platform == Platform.DISCORD:
            from gateway.session import _discord_tools_loaded

            discord_tools = "1" if _discord_tools_loaded() else "0"
            discord_ids = (
                str(src.guild_id or ""),
                str(src.parent_chat_id or ""),
                str(src.thread_id or ""),
                str(src.chat_id or ""),
                # Only PRESENCE is rendered (the id itself is delivered
                # per-turn in the user message) — keying on the value would
                # re-render every message for zero byte change.
                "1" if src.message_id else "0",
            )

        # Slack renders a capability-aware platform note gated on
        # _slack_tools_loaded() — the gate state must appear in the key
        # (same parity contract as the Discord gate above) so a config /
        # MCP-registration flip re-renders once instead of serving a
        # stale pinned note for the rest of the session.
        slack_tools = ""
        if src.platform == Platform.SLACK:
            from gateway.session import _slack_tools_loaded

            slack_tools = "1" if _slack_tools_loaded() else "0"

        try:
            from hermes_constants import display_hermes_home

            home_display = str(display_hermes_home())
        except Exception:
            home_display = ""

        key_tuple = (
            platform,
            str(src.chat_id or ""),
            str(src.thread_id or ""),
            str(src.chat_type or ""),
            str(src.chat_name or ""),
            str(src.chat_topic or ""),
            str(src.user_name or ""),
            str(src.user_id or ""),
            str(getattr(src, "profile", None) or ""),
            bool(context.shared_multi_user_session),
            discord_ids,
            discord_tools,
            slack_tools,
            tuple(p.value for p in context.connected_platforms),
            tuple(
                (
                    p.value,
                    str(getattr(hc, "name", "") or ""),
                    str(getattr(hc, "chat_id", "") or ""),
                )
                for p, hc in context.home_channels.items()
            ),
            bool(redact_pii),
            home_display,
        )
        return hashlib.sha256(repr(key_tuple).encode("utf-8")).hexdigest()



# Conversation-scoped per-session state registry (legacy contract).
# The state itself now lives in ``SessionState.conversation`` (see
# gateway/session_state.py) and boundaries clear it structurally via
# ``ConversationState.clear()`` — adding a field to ConversationState means
# every boundary picks it up automatically.  This tuple is retained for:
#   (a) plain-dict conversation-scoped stores not yet folded into
#       SessionState (currently ``_pending_model_notes``), which
#       _clear_conversation_scope still pops per-key; and
#   (b) the public test contract (tests import and iterate this tuple).
# History: boundaries used to each carry a hand-copied pop-list that drifted
# whenever a new dict was added (#48031, #58403, #10702, #35809).
#
# NOT in this list (different lifecycles):
# - _running_agents/_running_agents_ts/_active_session_leases/_busy_ack_ts/
#   _turn_lease_tokens: turn-scoped, owned by _release_running_agent_state
#   and the dispatch finally.
# - _session_run_generation: monotonic by design; clearing it would reset
#   the counter and break stale-run detection (#28686).
# - _agent_cache: has its own eviction path (_evict_cached_agent) with
#   resource cleanup; boundaries call it explicitly.
# - _pending_approvals/_update_prompt_pending/slash-confirm/tool-approval
#   state: cleared via _clear_session_boundary_security_state, which
#   _clear_conversation_scope calls.

_CONVERSATION_SCOPED_STATE: tuple = (
    "_session_model_overrides",
    "_pending_one_turn_model_restores",
    "_session_reasoning_overrides",
    "_session_service_tier_overrides",
    "_pending_model_notes",
    "_last_resolved_model",
    "_queued_events",
    # Stall-watchdog "already notified" latch (#72016). Cleared on /new so a
    # fresh conversation can warn again if it later stalls with pending inbound.
    "_session_stall_notified",
    # Staged-but-never-consumed sidecar notes (turn aborted between staging
    # and run_sync) must not leak into a future conversation's first user
    # message — session keys are source-derived and REUSED.
    "_pending_turn_sidecar_notes",
)
