"""Run-generation / conversation-scope methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, wave 1).
Holds the session run-generation guards, the turn-lease release/rebind
helpers, the conversation-scope funnel, and the interrupt-and-clear path.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``_AGENT_PENDING_SENTINEL``, ``_CONVERSATION_SCOPED_STATE`` (a public
contract — tests import it from ``gateway.run``) and
``_reap_gateway_turn_processes`` stay in ``gateway/run.py`` and are
imported lazily inside the methods that use them.
"""


from __future__ import annotations

import inspect
import logging
import threading

from typing import Any, Optional

from agent.interrupt_compat import request_hard_interrupt
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewayRunGenerationMixin:

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
        from gateway.run import _CONVERSATION_SCOPED_STATE
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

    async def _interrupt_and_clear_session(
        self,
        session_key: str,
        source: SessionSource,
        *,
        interrupt_reason: str,
        invalidation_reason: str,
        release_running_state: bool = True,
    ) -> None:
        """Interrupt the current run and clear queued session state consistently."""
        from gateway.run import _AGENT_PENDING_SENTINEL, _reap_gateway_turn_processes
        if not session_key:
            return
        _iac_state = self._peek_session_state(session_key)
        running_agent = _iac_state.turn.agent if _iac_state else None
        _process_task_id = ""
        _process_baseline = None
        if running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            request_hard_interrupt(running_agent, interrupt_reason)
            _process_task_id = getattr(
                running_agent, "_gateway_turn_process_task_id", ""
            )
            _process_baseline = getattr(
                running_agent, "_gateway_turn_process_baseline", None
            )
        # Bump the generation *before* scheduling the reap thread and capture
        # the post-bump value: task_id is session-scoped (task_id ==
        # session_id), so if a replacement turn claims this session and
        # spawns its own process before the reap thread actually runs, that
        # claim bumps the generation again. The closure below then sees a
        # stale generation and skips — the replacement turn's own baseline
        # covers its own cleanup, so nothing is left permanently unreaped.
        _generation_at_interrupt = self._invalidate_session_run_generation(
            session_key, reason=invalidation_reason
        )
        if _process_task_id and _process_baseline is not None:
            threading.Thread(
                target=_reap_gateway_turn_processes,
                args=(_process_task_id, _process_baseline),
                kwargs={
                    "source": "gateway_turn_interrupt",
                    "is_still_current": lambda: self._is_session_run_current(
                        session_key, _generation_at_interrupt
                    ),
                },
                name=f"gateway-turn-reaper-{_process_task_id[:12]}",
                daemon=True,
            ).start()
        adapter = self._adapter_for_source(source)
        interrupt_session_activity = getattr(
            type(adapter), "interrupt_session_activity", None
        )
        if adapter and callable(interrupt_session_activity):
            metadata = self._thread_metadata_for_source(source)
            try:
                params = inspect.signature(interrupt_session_activity).parameters
                accepts_metadata = "metadata" in params or any(
                    param.kind is inspect.Parameter.VAR_KEYWORD
                    for param in params.values()
                )
            except (TypeError, ValueError):
                accepts_metadata = False
            if accepts_metadata:
                await adapter.interrupt_session_activity(
                    session_key, source.chat_id, metadata=metadata
                )
            else:
                await adapter.interrupt_session_activity(session_key, source.chat_id)
        if adapter and hasattr(adapter, "get_pending_message"):
            adapter.get_pending_message(session_key)  # consume and discard
        if _iac_state is not None:
            _iac_state.persistent.pending_command_text = None
        if release_running_state:
            self._release_running_agent_state(session_key)
            # Evict the cached agent: ``_interrupt_requested`` is only
            # cleared by the turn finalizer, so on a hung or still-draining
            # run the flag survives the lock release and kills the session's
            # NEXT message at the top of the tool loop (interrupted=True,
            # api_calls=0, empty response — silently swallowed, #44212).
            # Evicting mirrors the /new and /model paths: the next message
            # rebuilds the agent from session history, while the old agent
            # object keeps its interrupt flag so a hung drain still dies
            # when it unblocks.
            self._evict_cached_agent(session_key)

