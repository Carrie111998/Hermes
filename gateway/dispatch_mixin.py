"""Inbound message dispatch and event-routing methods for GatewayRunner.

Extracted from ``gateway/run.py`` as part of the god-file decomposition campaign
(``~/.hermes/plans/god-file-decomposition.md``, Phase 3 mechanical mixin lifts —
gate 4 of #54962). This mixin holds the gateway's message dispatch core: the
inbound pipeline (``_handle_message`` / ``_handle_message_with_agent``), the
busy-session slash commands (/stop, /new, /queue, /steer, /goal, ...), the
active-session-slot admission control, and the destructive-slash confirmation
gates. It is the largest mixin lifted from ``GatewayRunner`` (28 methods,
~4,810 LOC, slice 26).

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. Module-level run.py helpers
(``_hermes_home``, ``_load_gateway_config``, ``_AGENT_PENDING_SENTINEL``, ...)
are imported lazily inside the method that uses them — a deferred
``from gateway.run import ...`` resolves at call time, when ``gateway.run`` is
fully loaded, so this module never imports ``gateway.run`` at import time -> no
import cycle. The module-level ``logger`` matches run.py's
(``logging.getLogger("gateway.run")``) so extracted log records keep their
original logger name.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Union

from agent.i18n import t
from agent.interrupt_compat import request_hard_interrupt
from gateway.config import Platform
from gateway.platforms.base import (
    EphemeralReply,
    MessageEvent,
    MessageType,
    merge_pending_message_event,
)
from gateway.session import (
    SessionSource,
    build_channel_continuity_note,
    build_session_context,
)

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")



class GatewayDispatchMixin:


    def _has_setup_skill(self) -> bool:
        """Check if the hermes-agent-setup skill is installed."""
        try:
            from tools.skill_manager_tool import _find_skill
            return _find_skill("hermes-agent-setup") is not None
        except Exception:
            return False


    async def _handle_reaction_event(self, ctx: Dict[str, Any]) -> None:
        """Fan a normalised platform reaction event out to the HookRegistry.

        Adapters call this via ``set_reaction_handler`` for every
        platform-native reaction event they surface. The adapter-supplied
        ``event_name`` ("reaction:added" / "reaction:removed") becomes the
        hook event so user hooks subscribe with the same name scheme as the
        existing ``agent:*`` family. Errors never block the adapter's event
        loop — the hook contract is non-blocking.
        """
        event_name = str(ctx.get("event_name") or "reaction:added")
        try:
            await self.hooks.emit(event_name, ctx)
        except Exception:
            logger.debug("[Gateway] reaction hook emit failed", exc_info=True)


    def _get_max_concurrent_sessions(self) -> Optional[int]:
        """Return the configured active chat session cap, if enabled."""
        try:
            from hermes_cli.active_sessions import resolve_max_concurrent_sessions

            return resolve_max_concurrent_sessions(getattr(self, "config", None))
        except Exception:
            return None


    def _active_session_limit_message(self, session_key: str) -> Optional[str]:
        """Return a user-facing rejection when starting a new session exceeds the cap."""
        max_sessions = self._get_max_concurrent_sessions()
        if max_sessions is None:
            return None
        if self._is_session_running(session_key):
            return None
        active_count = self._running_agent_count()
        if active_count < max_sessions:
            return None
        from hermes_cli.active_sessions import active_session_limit_message

        return active_session_limit_message(active_count, max_sessions)


    def _claim_active_session_slot(
        self,
        session_key: str,
        source: SessionSource,
    ) -> tuple[Any, Optional[str]]:
        """Claim a cross-process active-session slot for a new gateway turn."""
        if self._is_session_running(session_key):
            return None, None
        local_limit_message = self._active_session_limit_message(session_key)
        if local_limit_message is not None:
            return None, local_limit_message
        try:
            from hermes_cli.active_sessions import try_acquire_active_session

            platform = source.platform.value if source and source.platform else "gateway"
            return try_acquire_active_session(
                session_id=session_key,
                surface=f"gateway:{platform}",
                config=getattr(self, "config", None),
                metadata={
                    "platform": platform,
                    "chat_id": getattr(source, "chat_id", "") or "",
                    "user_id": getattr(source, "user_id", "") or "",
                },
            )
        except Exception as exc:
            logger.warning("Failed to claim active session slot: %s", exc)
            return None, None


    @staticmethod
    def _agent_has_active_subagents(running_agent: Any) -> bool:
        """Return True when *running_agent* is currently driving subagents
        via the ``delegate_task`` tool.

        Background (#30170): ``AIAgent.interrupt()`` cascades through the
        parent's ``_active_children`` list and calls ``interrupt()`` on
        every child synchronously, which aborts in-flight subagent work
        and produces a fallback cascade with no actionable signal.
        Demoting ``busy_input_mode='interrupt'`` to ``queue`` semantics
        whenever this helper returns True protects subagent work from
        conversational follow-ups while leaving the explicit ``/stop``
        path (which goes through ``_interrupt_and_clear_session``)
        untouched. Safe-by-default: returns False on any attribute or
        lock error so a missing/broken parent never blocks the existing
        interrupt path.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL

        if running_agent is None or running_agent is _AGENT_PENDING_SENTINEL:
            return False
        children = getattr(running_agent, "_active_children", None)
        # AIAgent always initialises this as a concrete list (see
        # agent/agent_init.py). Reject anything that isn't a real
        # collection — this guards against ``MagicMock()._active_children``
        # auto-creating a truthy stub in tests and triggering the demotion
        # against an agent that doesn't actually have subagents.
        if not isinstance(children, (list, tuple, set)):
            return False
        if not children:
            return False
        lock = getattr(running_agent, "_active_children_lock", None)
        try:
            if lock is not None:
                with lock:
                    return bool(children)
            return bool(children)
        except Exception:
            return False


    async def _session_has_compression_in_flight(self, session_key: str) -> bool:
        """Return True when a compression lock is held for this session's id.

        Context compression is interrupt-protected (#23975) but gateway
        ``interrupt`` busy-input mode can still start a follow-up turn against
        the pre-rotation parent while compression is mid-flight, producing
        orphaned compression siblings (#56391). Callers demote interrupt to
        queue when this returns True.

        Both blocking sources — the ``session_store`` lock + JSON load, and the
        SQLite ``get_compression_lock_holder`` SELECT — are offloaded to a
        worker thread so a large state.db never freezes the event loop (#5).
        """
        session_store = getattr(self, "session_store", None)
        if not session_key or session_store is None:
            return False
        try:
            session_id = await asyncio.to_thread(
                self._lookup_session_id_under_store_lock, session_store, session_key
            )
        except (AttributeError, TypeError):
            return False
        except Exception:
            logger.warning(
                "Compression in-flight check failed while reading session %s; "
                "treating compression as active to avoid interrupting a possible "
                "parent-session rotation",
                session_key,
                exc_info=True,
            )
            return True
        if not session_id:
            return False
        session_db = getattr(self, "_session_db", None)
        if session_db is None:
            return False
        raw_db = getattr(session_db, "_db", session_db)
        try:
            holder = await asyncio.to_thread(
                raw_db.get_compression_lock_holder, str(session_id)
            )
            return bool(holder)
        except (AttributeError, TypeError):
            return False
        except Exception:
            logger.warning(
                "Compression in-flight check failed while reading lock holder "
                "for session %s; treating compression as active to avoid "
                "interrupting a possible parent-session rotation",
                session_id,
                exc_info=True,
            )
            return True


    async def _prepare_busy_steer_text(self, event: MessageEvent) -> str:
        """Return steerable text for a busy follow-up, transcribing voice first.

        Fresh and queued voice messages reach the normal inbound STT pipeline,
        but successful steer messages intentionally bypass that queue. Without
        preprocessing here, a media-only voice follow-up has an empty text
        payload and steer mode silently degrades to queue mode.

        Audio file attachments remain files; only voice-message media follows
        the automatic STT contract used by ``_prepare_inbound_message_text``.
        If transcription fails, preserve any caption and let the existing
        steer fallback handle an otherwise empty event without losing it.

        Routes through ``_transcribe_and_echo_pending_voice`` — the single
        out-of-band transcription choke point shared with the interrupt
        monitor and the pending-drain path — so the STT call is made at most
        once per platform message (cached on the event) and the transcript
        echo respects the count-based ledger.  If steering later falls back
        to queue mode, the drain path reuses the cached transcript instead of
        paying for a second STT call or re-echoing the same line.
        """
        text = (event.text or "").strip()
        if not self._pending_event_audio_paths(event):
            return text

        adapter = self._adapter_for_source(event.source)
        enriched_text, successful_transcripts = await self._transcribe_and_echo_pending_voice(
            event,
            adapter,
            event.source,
            text,
            log_context="Busy-steer",
        )
        if not successful_transcripts:
            return text
        return (enriched_text or text).strip()


    async def _handle_active_session_busy_message(self, event: MessageEvent, session_key: str) -> bool:
        # --- Authorization gate (#17775) ---
        # The cold path (_handle_message) checks _is_user_authorized before
        # creating a session.  The busy path must enforce the same check;
        # otherwise unauthorized users in shared threads (Slack/Telegram/Discord)
        # can inject messages into an active session they don't own.
        from gateway.run import (
            _AGENT_PENDING_SENTINEL,
            _build_media_placeholder,
            _hermes_home,
            _load_gateway_config,
            _platform_config_key
        )

        if not self._is_user_authorized(event.source):
            logger.warning(
                "Dropping message from unauthorized user in active session: "
                "user=%s (%s), platform=%s, session=%s",
                event.source.user_id,
                event.source.user_name,
                event.source.platform.value if event.source.platform else "unknown",
                session_key,
            )
            return True  # handled (silently dropped); do not fall through

        # --- Draining case (gateway restarting/stopping) ---
        if self._draining:
            adapter = self._adapter_for_source(event.source)
            if not adapter:
                return True

            reply_anchor = self._reply_anchor_for_event(event)
            thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
            if self._queue_during_drain_enabled():
                self._queue_or_replace_pending_event(session_key, event)
                message = f"⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back."
            else:
                message = f"⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now."

            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content=message,
                reply_to=(
                    reply_anchor
                    if event.source.platform == Platform.TELEGRAM
                    and event.source.chat_type == "dm"
                    and event.source.thread_id
                    else (None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id)
                ),
                metadata=thread_meta,
            )
            return True

        # --- Approval response routing (#46866) ---
        # When the agent is blocked waiting for a dangerous-command approval,
        # plain-text responses like "yes" or "approve" must be routed to the
        # approval handler instead of being steered/queued/interrupted.
        # Otherwise approval via messaging platforms never succeeds — the
        # reply is queued behind a turn that can't start until the approval
        # resolves, so the approval times out and auto-denies (a deadlock).
        #
        # Slash forms (/approve, /deny) already bypass to the runner at the
        # base-adapter guard.  This handles the bare-word forms (Signal/SMS
        # users naturally type "yes" rather than "/approve").  Gating on
        # has_blocking_approval(session_key) is the disambiguator that keeps
        # a conversational "yes" from triggering a dangerous command when no
        # approval is actually pending (design intent — see run.py "Pending
        # exec approvals are handled by /approve and /deny" note).
        #
        # We reuse the canonical /approve and /deny handlers rather than
        # re-deriving the resolution + i18n messaging: they resolve the
        # waiting thread, resume typing, AND return a localized confirmation
        # string.  The busy-handler path does not auto-send that return, so
        # we deliver it ourselves (mirroring the draining-case send above).
        try:
            from tools.approval import has_blocking_approval
            if has_blocking_approval(session_key):
                _raw_text = (event.text or "").strip().lower()
                _approve_words = {"approve", "yes", "ok", "okay", "confirm", "y", "👍"}
                _deny_words = {"deny", "no", "reject", "cancel", "n", "👎"}
                _approval_handler = None
                _normalized_args = ""
                if _raw_text in _approve_words:
                    _approval_handler = self._handle_approve_command
                elif _raw_text in _deny_words:
                    _approval_handler = self._handle_deny_command
                elif _raw_text in {"always", "approve always", "always approve"}:
                    _approval_handler = self._handle_approve_command
                    _normalized_args = "always"
                elif _raw_text in {"session", "approve session", "session approve"}:
                    _approval_handler = self._handle_approve_command
                    _normalized_args = "session"
                if _approval_handler is not None:
                    # Synthesize the canonical "/approve [args]" / "/deny"
                    # command text so the slash handlers parse modifiers via
                    # event.get_command_args().  Always use a literal "/" —
                    # MessageEvent.is_command()/get_command_args() only
                    # recognize the "/" prefix, not the per-platform display
                    # prefix ("!" on Slack/Matrix).
                    _verb = "approve" if _approval_handler is self._handle_approve_command else "deny"
                    _synth = f"/{_verb}"
                    if _normalized_args:
                        _synth = f"{_synth} {_normalized_args}"
                    event.text = _synth
                    _reply = await _approval_handler(event)
                    logger.info(
                        "Approval response via plain text: session=%s verb=%s args=%r",
                        session_key, _verb, _normalized_args,
                    )
                    _adapter = self._adapter_for_source(event.source)
                    if _adapter and _reply:
                        _text, _eph_ttl = _adapter._unwrap_ephemeral(_reply)
                        if _text:
                            _anchor = self._reply_anchor_for_event(event)
                            await _adapter._send_with_retry(
                                chat_id=event.source.chat_id,
                                content=_text,
                                reply_to=_anchor,
                                metadata=self._thread_metadata_for_source(event.source, _anchor),
                            )
                    return True
        except Exception:
            logger.warning(
                "Plain-text approval routing failed for session %s; "
                "falling through to busy handling",
                session_key, exc_info=True,
            )

        # Normal busy case (agent actively running a task)
        adapter = self._adapter_for_source(event.source)
        if not adapter:
            return False  # let default path handle it

        # --- Internal synthetic events must never interrupt/steer ---
        # Async-delegation completions (delegate_task(background=true)) and
        # background-process completions (terminal notify_on_complete) re-enter
        # the originating session as internal MessageEvents. When the session
        # is busy, treating them like a user TEXT message means interrupt-mode
        # (the default busy_text_mode) aborts the active turn AND sends a "⚡
        # Interrupting current task" ack — exactly the opposite of the design
        # invariant that a completion surfaces as a NEW turn only when idle and
        # never splices into a running turn. Fall through to the base adapter,
        # which queues internal events silently (no interrupt, no ack) so they
        # cascade after the current turn finishes.
        if getattr(event, "internal", False):
            return False

        _busy_state = self._peek_session_state(session_key)
        running_agent = _busy_state.turn.agent if _busy_state else None

        effective_mode = self._busy_input_mode
        busy_text_mode = getattr(self, "_busy_text_mode", "interrupt")
        if (
            event.message_type == MessageType.TEXT
            and busy_text_mode == "queue"
            and effective_mode != "steer"
        ):
            return False

        # Steer mode: inject mid-run via running_agent.steer() instead of
        # queueing + interrupting.  If the agent isn't running yet
        # (sentinel) or lacks steer(), or the payload is empty, fall back
        # to queue semantics so nothing is lost.
        # #30170 — Subagent protection. ``AIAgent.interrupt()`` cascades
        # to every entry in the parent's ``_active_children`` list and
        # aborts in-flight ``delegate_task`` work. Demote ``interrupt``
        # to ``queue`` when the parent is currently driving subagents so
        # a conversational follow-up doesn't destroy minutes of subagent
        # work. Explicit ``/stop`` and ``/new`` slash commands go through
        # ``_interrupt_and_clear_session`` and are unaffected — the
        # operator still has a way to force-cancel everything.
        demoted_for_subagents = (
            effective_mode == "interrupt"
            and self._agent_has_active_subagents(running_agent)
        )
        if demoted_for_subagents:
            logger.info(
                "Demoting busy_input_mode 'interrupt' to 'queue' for session %s "
                "because the running agent has active subagents (#30170)",
                session_key,
            )
            effective_mode = "queue"
        demoted_for_compression = (
            effective_mode == "interrupt"
            and await self._session_has_compression_in_flight(session_key)
        )
        if demoted_for_compression:
            logger.info(
                "Demoting busy_input_mode 'interrupt' to 'queue' for session %s "
                "because context compression is in flight (#56391)",
                session_key,
            )
            effective_mode = "queue"
        steered = False
        redirected = False
        if effective_mode == "steer":
            steer_text = await self._prepare_busy_steer_text(event)
            # A follow-up qualifies for steering when it is plain text, OR
            # when every attachment is STT-eligible voice media whose
            # transcript was just folded into steer_text — otherwise a voice
            # note in steer mode silently degrades to queue mode (#58780).
            _steer_media_urls = getattr(event, "media_urls", None) or []
            _steer_all_voice = bool(_steer_media_urls) and (
                len(self._pending_event_audio_paths(event)) == len(_steer_media_urls)
            )
            can_steer = (
                steer_text
                and (
                    (
                        event.message_type == MessageType.TEXT
                        and not event.media_urls
                        and not event.media_types
                    )
                    or _steer_all_voice
                )
                and running_agent is not None
                and running_agent is not _AGENT_PENDING_SENTINEL
                and hasattr(running_agent, "steer")
            )
            if can_steer:
                try:
                    steered = bool(running_agent.steer(steer_text))
                except Exception as exc:
                    logger.warning("Gateway steer failed for session %s: %s", session_key, exc)
                    steered = False
            if not steered:
                # Fall back to queue (merge into pending messages, no interrupt)
                effective_mode = "queue"
        elif (
            effective_mode == "interrupt"
            and event.message_type == MessageType.TEXT
            and not event.media_urls
            and not event.media_types
            and running_agent is not None
            and running_agent is not _AGENT_PENDING_SENTINEL
            and getattr(running_agent, "_supports_active_turn_redirect", False) is True
            and hasattr(running_agent, "redirect")
        ):
            try:
                redirected = bool(running_agent.redirect((event.text or "").strip()))
            except Exception as exc:
                logger.warning("Gateway redirect failed for session %s: %s", session_key, exc)
                redirected = False

        # Store the message so it's processed as the next turn after the
        # current run finishes (or is interrupted).  Skip this for a
        # successful steer — the text already landed inside the run and
        # must NOT also be replayed as a next-turn user message.
        #
        # Route through _queue_or_replace_pending_event (the same FIFO
        # infrastructure used by busy queue-mode and /queue) rather than a
        # raw merge_pending_message_event(merge_text=True). The raw merge
        # newline-joins consecutive TEXT follow-ups into a SINGLE pending
        # turn, destroying message boundaries — so two separate user
        # messages sent while the agent was busy (interrupt mode, or a
        # steer that fell back to queue) arrived as one mashed-together
        # turn (#43066 sub-bug 2). The FIFO path gives each text its own
        # turn in arrival order while still preserving photo-burst / album
        # merge semantics for media.
        if not steered and not redirected:
            self._queue_or_replace_pending_event(session_key, event)

        is_queue_mode = effective_mode == "queue"
        is_steer_mode = effective_mode == "steer"
        is_redirect_mode = effective_mode == "interrupt" and redirected

        # If not in queue/steer mode, interrupt the running agent immediately.
        # This aborts in-flight tool calls and causes the agent loop to exit
        # at the next check point.
        if (
            effective_mode == "interrupt"
            and not redirected
            and running_agent
            and running_agent is not _AGENT_PENDING_SENTINEL
        ):
            try:
                _interrupt_text = event.text
                _media_urls = getattr(event, "media_urls", None) or []
                if self._pending_event_audio_paths(event):
                    _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                        event,
                        adapter,
                        event.source,
                        event.text or "",
                        log_context="Voice-busy-interrupt",
                    )
                elif not _interrupt_text and _media_urls:
                    _interrupt_text = _build_media_placeholder(event)
                running_agent.interrupt(_interrupt_text)
            except Exception:
                pass  # don't let interrupt failure block the ack

        # Check if busy ack is disabled — skip sending but still process the input.
        # Placed before debounce so we don't stamp a "last ack" timestamp that was
        # never actually delivered.
        busy_ack_enabled = os.environ.get("HERMES_GATEWAY_BUSY_ACK_ENABLED", "true").lower() == "true"
        if not busy_ack_enabled:
            logger.debug("Busy ack suppressed for session %s", session_key)
            return True  # input still processed, just no ack sent

        # Debounce before consulting config-heavy display settings. Rapid
        # follow-ups should be processed but should not trigger another config
        # read just to discover that no ack will be sent.
        _BUSY_ACK_COOLDOWN = 30
        now = time.time()
        last_ack = _busy_state.turn.busy_ack_ts if _busy_state else 0
        if now - last_ack < _BUSY_ACK_COOLDOWN:
            return True  # interrupt sent (if not queue), ack already delivered recently

        from gateway.display_config import resolve_display_setting
        platform_key = _platform_config_key(event.source.platform)

        # In steer mode the user's text has already been injected into the
        # active run. Some mobile chat setups want that steering to be silent,
        # like STT transcript echo suppression: keep the behavior, drop only
        # the confirmation bubble.
        if is_steer_mode:
            steer_ack_env = os.environ.get("HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED")
            if steer_ack_env is not None:
                steer_ack_enabled = steer_ack_env.strip().lower() in {"1", "true", "yes", "on"}
            else:
                steer_ack_enabled = bool(
                    resolve_display_setting(
                        _load_gateway_config(),
                        platform_key,
                        "busy_steer_ack_enabled",
                        True,
                    )
                )
            if not steer_ack_enabled:
                logger.debug("Busy steer ack suppressed for session %s", session_key)
                return True

        self._session_state(session_key).turn.busy_ack_ts = now

        # Build a status-rich acknowledgment. Mobile chat defaults keep this
        # terse; detailed iteration/tool state is still available in logs and
        # can be opted in per platform via display.platforms.<platform>.busy_ack_detail.
        status_parts = []
        busy_ack_detail_enabled = bool(
            resolve_display_setting(
                _load_gateway_config(),
                _platform_config_key(event.source.platform),
                "busy_ack_detail",
                True,
            )
        )

        if busy_ack_detail_enabled and running_agent and running_agent is not _AGENT_PENDING_SENTINEL:
            try:
                summary = running_agent.get_activity_summary()
                iteration = summary.get("api_call_count", 0)
                max_iter = summary.get("max_iterations", 0)
                current_tool = summary.get("current_tool")
                start_ts = _busy_state.turn.started_ts if _busy_state else 0
                if start_ts:
                    elapsed_min = int((now - start_ts) / 60)
                    if elapsed_min > 0:
                        status_parts.append(f"{elapsed_min} min elapsed")
                if max_iter:
                    status_parts.append(f"iteration {iteration}/{max_iter}")
                if current_tool:
                    status_parts.append(f"running: {current_tool}")
            except Exception:
                pass

        status_detail = f" ({', '.join(status_parts)})" if status_parts else ""
        if is_steer_mode:
            message = (
                f"⏩ Steered into current run{status_detail}. "
                f"Your message arrives after the next tool call."
            )
        elif is_redirect_mode:
            message = (
                f"↪ Redirected current run{status_detail}. "
                f"I'll adjust using your correction."
            )
        elif is_queue_mode and demoted_for_subagents:
            # #30170 — explain the demotion so the user knows their
            # follow-up didn't accidentally kill the subagent and
            # discovers `/stop` as the explicit escape hatch.
            message = (
                f"⏳ Subagent working{status_detail} — your message is queued for "
                f"when it finishes (use /stop to cancel everything)."
            )
        elif is_queue_mode and demoted_for_compression:
            message = (
                f"⏳ Compressing context{status_detail} — your message is queued for "
                f"when it finishes (use /stop to cancel everything)."
            )
        elif is_queue_mode:
            message = (
                f"⏳ Queued for the next turn{status_detail}. "
                f"I'll respond once the current task finishes."
            )
        else:
            message = (
                f"⚡ Interrupting current task{status_detail}. "
                f"I'll respond to your message shortly."
            )

        # First-touch onboarding: the very first time a user sends a message
        # while the agent is busy, append a one-time hint explaining the
        # queue/interrupt knob.  Flag is persisted to config.yaml so it never
        # fires again on this install.
        try:
            from agent.onboarding import (
                BUSY_INPUT_FLAG,
                busy_input_hint_gateway,
                is_seen,
                mark_seen,
            )
            _user_cfg = _load_gateway_config()
            if not is_seen(_user_cfg, BUSY_INPUT_FLAG):
                if is_steer_mode:
                    _hint_mode = "steer"
                elif is_queue_mode:
                    _hint_mode = "queue"
                elif is_redirect_mode:
                    _hint_mode = "redirect"
                else:
                    _hint_mode = "interrupt"
                message = (
                    f"{message}\n\n"
                    f"{busy_input_hint_gateway(_hint_mode)}"
                )
                mark_seen(_hermes_home / "config.yaml", BUSY_INPUT_FLAG)
        except Exception as _onb_err:
            logger.debug("Failed to apply busy-input onboarding hint: %s", _onb_err)

        reply_anchor = self._reply_anchor_for_event(event)
        thread_meta = self._thread_metadata_for_source(event.source, reply_anchor)
        try:
            await adapter._send_with_retry(
                chat_id=event.source.chat_id,
                content=message,
                reply_to=(
                    reply_anchor
                    if event.source.platform == Platform.TELEGRAM
                    and event.source.chat_type == "dm"
                    and event.source.thread_id
                    else (None if event.source.platform == Platform.TELEGRAM and event.source.thread_id else event.message_id)
                ),
                metadata=thread_meta,
            )
        except Exception as e:
            logger.debug("Failed to send busy-ack: %s", e)

        return True


    async def _dispatch_busy_slash_command(
        self, event: MessageEvent, cmd_def, quick_key: str, source,
    ):
        """Dispatch a recognized slash command while an agent is running.

        Resolution order:
          1. ``busy_handler`` — special mid-run variant (e.g. /goal's
             control-verb whitelist, /queue's FIFO enqueue, /model's
             custom reject text).
          2. ``busy_policy == "dispatch"`` — the command's normal handler.
          3. Catch-all busy-reject text. Rejecting is required rather than
             falling through to interrupt + discard: commands like /model,
             /reasoning, /voice, /insights, /title, /resume, /retry,
             /undo, /compress, /usage, /reload-mcp, /sethome, /reset (all
             registered as Discord slash commands) would interrupt the
             agent AND get silently discarded by the slash-command safety
             net, producing a zero-char response. See #5057, #6252, #10370.
        """
        name = cmd_def.name
        policy = getattr(cmd_def, "busy_policy", "reject")
        handler_key = getattr(cmd_def, "busy_handler", None)

        if handler_key:
            special = {
                "start": self._busy_start_command,
                "stop": self._busy_stop_command,
                "new": self._busy_new_command,
                "queue": self._busy_queue_command,
                "steer": self._busy_steer_command,
                "egress": self._busy_egress_command,
                "goal": self._busy_goal_command,
            }.get(handler_key)
            if special is not None:
                return await special(event, quick_key, source)
            reject_text = self._BUSY_REJECT_TEXT.get(handler_key)
            if reject_text is not None:
                return reject_text

        if policy in ("dispatch", "interrupt_then_dispatch"):
            plain = {
                "status": self._handle_status_command,
                "context": self._handle_context_command,
                "restart": self._handle_restart_command,
                "approve": self._handle_approve_command,
                "deny": self._handle_deny_command,
                "agents": self._handle_agents_command,
                "background": self._handle_background_command,
                "kanban": self._handle_kanban_command,
                "subgoal": self._handle_subgoal_command,
                "yolo": self._handle_yolo_command,
                "verbose": self._handle_verbose_command,
                "footer": self._handle_footer_command,
                "help": self._handle_help_command,
                "commands": self._handle_commands_command,
                "profile": self._handle_profile_command,
                "update": self._handle_update_command,
                "version": self._handle_version_command,
            }.get(name)
            if plain is not None:
                return await plain(event)
            logger.warning(
                "busy_policy=%s for /%s has no mid-run handler — "
                "falling back to busy-reject", policy, name,
            )

        # Catch-all: any other recognized slash command reached the
        # running-agent guard. Reject gracefully rather than falling
        # through to interrupt + discard.
        return (
            f"⏳ Agent is running — `/{name}` can't run "
            f"mid-turn. Wait for the current response or `/stop` first."
        )


    async def _busy_start_command(self, event: MessageEvent, quick_key: str, source):
        # Telegram sends /start for bot launches/deep-links. Treat it as a
        # platform ping, not a user command: no help dump, no agent
        # interrupt, no queued text.
        logger.info("Ignoring /start platform ping for active session %s", quick_key)
        return ""


    async def _busy_egress_command(self, event: MessageEvent, quick_key: str, source):
        from hermes_cli.proxy_cli import format_status_text

        return format_status_text()


    async def _busy_stop_command(self, event: MessageEvent, quick_key: str, source):
        # /stop must hard-kill the session when an agent is running.
        # A soft interrupt (agent.interrupt()) doesn't help when the agent
        # is truly hung — the executor thread is blocked and never checks
        # _interrupt_requested.  Force-clean _running_agents so the session
        # is unlocked and subsequent messages are processed normally.
        from gateway.run import _INTERRUPT_REASON_STOP

        await self._interrupt_and_clear_session(
            quick_key,
            source,
            interrupt_reason=_INTERRUPT_REASON_STOP,
            invalidation_reason="stop_command",
        )
        logger.info("STOP for session %s — agent interrupted, session lock released", quick_key)
        return EphemeralReply(t("gateway.stop.stopped"))


    async def _busy_new_command(self, event: MessageEvent, quick_key: str, source):
        # /reset and /new must bypass the running-agent guard so they
        # actually dispatch as commands instead of being queued as user
        # text (which would be fed back to the agent with the same
        # broken history — #2170).  Interrupt the agent first, then
        # clear the adapter's pending queue so the stale "/reset" text
        # doesn't get re-processed as a user message after the
        # interrupt completes.
        # Clear any pending messages so the old text doesn't replay
        from gateway.run import _INTERRUPT_REASON_RESET

        await self._interrupt_and_clear_session(
            quick_key,
            source,
            interrupt_reason=_INTERRUPT_REASON_RESET,
            invalidation_reason="new_command",
        )
        # Clean up the running agent entry so the reset handler
        # doesn't think an agent is still active.
        return await self._handle_reset_command(event)


    async def _busy_queue_command(self, event: MessageEvent, quick_key: str, source):
        # /queue <prompt> — queue without interrupting.
        # Semantics: each /queue invocation produces its own full agent
        # turn, processed in FIFO order after the current run (and any
        # earlier /queue items) finishes.  Messages are NOT merged.
        queued_text = event.get_command_args().strip()
        # Preserve media/reply payloads: a /queue carrying a photo,
        # document, or reply context is valid even with no prompt text
        # (e.g. "/queue" as the caption of an image). Dropping these
        # fields silently lost the attachment when the queued turn ran.
        has_media = bool(getattr(event, "media_urls", None))
        if not queued_text and not has_media:
            return "Usage: /queue <prompt>"
        adapter = self._adapter_for_source(source)
        if adapter:
            queued_event = MessageEvent(
                text=queued_text,
                message_type=event.message_type if has_media else MessageType.TEXT,
                source=event.source,
                raw_message=event.raw_message,
                message_id=event.message_id,
                media_urls=list(getattr(event, "media_urls", []) or []),
                media_types=list(getattr(event, "media_types", []) or []),
                reply_to_message_id=event.reply_to_message_id,
                reply_to_text=event.reply_to_text,
                reply_to_author_id=event.reply_to_author_id,
                reply_to_author_name=event.reply_to_author_name,
                reply_to_is_own_message=event.reply_to_is_own_message,
                auto_skill=event.auto_skill,
                channel_prompt=event.channel_prompt,
                channel_context=event.channel_context,
                internal=event.internal,
                timestamp=event.timestamp,
            )
            self._enqueue_fifo(quick_key, queued_event, adapter)
        depth = self._queue_depth(quick_key, adapter=self._adapter_for_source(source))
        if depth <= 1:
            return "Queued for the next turn."
        return f"Queued for the next turn. ({depth} queued)"


    async def _busy_steer_command(self, event: MessageEvent, quick_key: str, source):
        # /steer <prompt> — inject mid-run after the next tool call.
        # Unlike /queue (turn boundary), /steer lands BETWEEN tool-call
        # iterations inside the same agent run, by appending to the
        # last tool result's content. No interrupt, no new user turn,
        # no role-alternation violation.
        from gateway.run import _AGENT_PENDING_SENTINEL

        steer_text = event.get_command_args().strip()
        if not steer_text:
            return "Usage: /steer <prompt>"
        _steer_state = self._peek_session_state(quick_key)
        running_agent = _steer_state.turn.agent if _steer_state else None
        if running_agent is _AGENT_PENDING_SENTINEL:
            # Agent hasn't started yet — queue as turn-boundary fallback.
            adapter = self._adapter_for_source(source)
            if adapter:
                queued_event = MessageEvent(
                    text=steer_text,
                    message_type=MessageType.TEXT,
                    source=event.source,
                    message_id=event.message_id,
                    channel_prompt=event.channel_prompt,
                    channel_context=event.channel_context,
                )
                self._enqueue_fifo(quick_key, queued_event, adapter)
            return "Agent still starting — /steer queued for the next turn."
        if running_agent and hasattr(running_agent, "steer"):
            try:
                accepted = running_agent.steer(steer_text)
            except Exception as exc:
                logger.warning("Steer failed for session %s: %s", quick_key, exc)
                return f"⚠️ Steer failed: {exc}"
            if accepted:
                preview = steer_text[:60] + ("..." if len(steer_text) > 60 else "")
                return f"⏩ Steer queued — arrives after the next tool call: '{preview}'"
            return "Steer rejected (empty payload)."
        # Running agent is missing or lacks steer() — fall back to queue.
        adapter = self._adapter_for_source(source)
        if adapter:
            queued_event = MessageEvent(
                text=steer_text,
                message_type=MessageType.TEXT,
                source=event.source,
                message_id=event.message_id,
                channel_prompt=event.channel_prompt,
                channel_context=event.channel_context,
            )
            self._enqueue_fifo(quick_key, queued_event, adapter)
        return "No active agent — /steer queued for the next turn."


    async def _busy_goal_command(self, event: MessageEvent, quick_key: str, source):
        # /goal is safe mid-run for status/pause/clear/wait (inspection
        # and control-plane only — doesn't interrupt the running turn).
        # Setting a new goal text mid-run is rejected with the same
        # "wait or /stop" message as /model so we don't race a second
        # continuation prompt against the current turn.
        _goal_arg = (event.get_command_args() or "").strip().lower()
        _goal_verb = _goal_arg.split(None, 1)[0] if _goal_arg else ""
        # Exact-match control verbs (unchanged semantics), plus the
        # wait/unwait barrier verbs which take a pid argument.
        _is_control = (
            not _goal_arg
            or _goal_arg in {"status", "pause", "resume", "clear", "stop", "done", "unwait"}
            or _goal_verb == "wait"
        )
        if _is_control:
            return await self._handle_goal_command(event)
        return "Agent is running — use /goal status / pause / clear / wait mid-run, or /stop before setting a new goal."


    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        """
        Handle an incoming message from any platform.
        
        This is the core message processing pipeline:
        1. Check user authorization
        2. Check for commands (/new, /reset, etc.)
        3. Check for running agent and interrupt if needed
        4. Get or create session
        5. Build context for agent
        6. Run agent conversation
        7. Return response
        """
        from gateway.run import (
            _AGENT_PENDING_SENTINEL,
            _build_media_placeholder,
            _check_unavailable_skill,
            _float_env,
            _hermes_home,
            _is_slack_ignored_channel
        )

        source = event.source

        # 🔴 Cross-session leak guard. This handler runs inside a per-message
        # asyncio task created via create_task(), which snapshots the spawning
        # context with copy_context(). If a *concurrent* message had already
        # bound its session via set_session_vars() when this task was created,
        # we inherited ITS HERMES_SESSION_* ContextVars. Until we bind our own
        # (a few steps down, in _set_session_env), any subprocess spawned here
        # would read the foreign session's identity via the subprocess-env
        # bridge — the _UNSET-strip guard there can't help because the vars are
        # set-to-foreign, not _UNSET. Reset to _UNSET now so that window strips
        # safe (no session) instead of leaking the sibling's. See
        # gateway/session_context.reset_session_vars + the inheritance test.
        try:
            from gateway.session_context import reset_session_vars
            reset_session_vars()
        except Exception:
            logger.debug("reset_session_vars failed at handler entry", exc_info=True)

        # Internal events (e.g. background-process completion notifications)
        # are system-generated and must skip user authorization.
        is_internal = bool(getattr(event, "internal", False))

        # Ignored-channel guard runs FIRST — before startup-restore queueing,
        # plugin hooks, auth, and session setup — so a configured ignored
        # channel can never reach pairing/auth/session state (#51899).
        # getattr: bare test runners construct GatewayRunner via
        # object.__new__ without config (see AGENTS.md pitfall on
        # object.__new__ test pattern).
        if (
            not is_internal
            and getattr(source, "platform", None) == Platform.SLACK
            and _is_slack_ignored_channel(
                getattr(self, "config", None), getattr(source, "chat_id", None)
            )
        ):
            logger.info(
                "Dropping Slack message from configured ignored channel %s",
                getattr(source, "chat_id", None),
            )
            return None

        if (
            getattr(self, "_startup_restore_in_progress", False)
            and not is_internal
            and not getattr(event, "_hermes_startup_restore_replay", False)
        ):
            self._queue_startup_restore_event(event)
            return None

        # scale-to-zero (Phase 0, 0.B/F13): stamp the gateway-scoped last-inbound
        # clock for real (user-originated) inbound only. Internal/system events
        # (background-process completions, startup-restore replays) are NOT
        # traffic — counting them would keep a genuinely idle gateway awake. This
        # clock is what the idle predicate (gateway/scale_to_zero.is_idle) reads.
        if not is_internal:
            self._scale_to_zero_note_real_inbound()

        # Fire pre_gateway_dispatch plugin hook for user-originated messages.
        # Plugins receive the MessageEvent and may return a dict influencing flow:
        #   {"action": "skip",    "reason": ...}    -> drop (no reply, plugin handled)
        #   {"action": "rewrite", "text":  ...}     -> replace event.text, continue
        #   {"action": "allow"}   /   None          -> normal dispatch
        # Hook runs BEFORE auth so plugins can handle unauthorized senders
        # (e.g. customer handover ingest) without triggering the pairing flow.
        if not is_internal:
            try:
                from hermes_cli.lifecycle import invoke_hook as _invoke_hook
                _hook_results = _invoke_hook(
                    "pre_gateway_dispatch",
                    event=event,
                    gateway=self,
                    # getattr: bare-runner tests build GatewayRunner via
                    # object.__new__ without __init__ (pitfall #17), and the
                    # hook must not fail dispatch over a missing attribute.
                    session_store=getattr(self, "session_store", None),
                )
            except Exception as _hook_exc:
                logger.warning("pre_gateway_dispatch invocation failed: %s", _hook_exc)
                _hook_results = []

            for _result in _hook_results:
                if not isinstance(_result, dict):
                    continue
                _action = _result.get("action")
                if _action == "skip":
                    logger.info(
                        "pre_gateway_dispatch skip: reason=%s platform=%s chat=%s",
                        _result.get("reason"),
                        source.platform.value if source.platform else "unknown",
                        source.chat_id or "unknown",
                    )
                    return None
                if _action == "rewrite":
                    _new_text = _result.get("text")
                    if isinstance(_new_text, str):
                        event = dataclasses.replace(event, text=_new_text)
                        source = event.source
                    break
                if _action == "allow":
                    break

        if is_internal:
            pass
        elif source.user_id is None:
            # Messages with no user identity (Telegram service messages,
            # channel forwards, anonymous admin posts, sender_chat) can't
            # be paired, but they can still be authorized via a
            # chat-scoped allowlist (e.g. TELEGRAM_GROUP_ALLOWED_CHATS
            # authorizes every member of the listed chat regardless of
            # sender). Defer to _is_user_authorized so that path runs.
            if not self._is_user_authorized(source):
                logger.debug("Ignoring message with no user_id from %s", source.platform.value)
                return None
        elif not self._is_user_authorized(source):
            logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
            # In DMs: offer pairing code. In groups: silently ignore.
            if (
                source.chat_type == "dm"
                and self._get_unauthorized_dm_behavior(
                    source.platform,
                    profile=source.profile,
                )
                == "pair"
            ):
                platform_name = source.platform.value if source.platform else "unknown"
                pairing_store = self._pairing_store_for(source)
                if pairing_store is None:
                    logger.error(
                        "Cannot offer pairing code on %s: no pairing store",
                        platform_name,
                    )
                    return None
                # Rate-limit ALL pairing responses (code or rejection) to
                # prevent spamming the user with repeated messages when
                # multiple DMs arrive in quick succession.
                if pairing_store._is_rate_limited(platform_name, source.user_id):
                    return None
                code = pairing_store.generate_code(
                    platform_name, source.user_id, source.user_name or ""
                )
                if code:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        store_profile = getattr(pairing_store, "profile", None)
                        profile_arg = (
                            f"-p {store_profile} "
                            if isinstance(store_profile, str)
                            and store_profile
                            and store_profile != "default"
                            else ""
                        )
                        await adapter.send(
                            source.chat_id,
                            f"Hi~ I don't recognize you yet!\n\n"
                            f"Here's your pairing code: `{code}`\n\n"
                            f"Ask the bot owner to run:\n"
                            f"`hermes {profile_arg}pairing approve "
                            f"{platform_name} {code}`"
                        )
                else:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        await adapter.send(
                            source.chat_id,
                            "Too many pairing requests right now~ "
                            "Please try again later!"
                        )
                    # Record rate limit so subsequent messages are silently ignored
                    pairing_store._record_rate_limit(platform_name, source.user_id)
            return None
        
        # Intercept messages that are responses to a pending /update prompt.
        # The update process (detached) wrote .update_prompt.json; the watcher
        # forwarded it to the user; now the user's reply goes back via
        # .update_response so the update process can continue.
        #
        # IMPORTANT: recognized slash commands must bypass this interception.
        # Otherwise control/session commands like /new or /help get silently
        # consumed as update answers instead of being dispatched normally.
        _quick_key = self._session_key_for_source(source)
        _up_state = self._peek_session_state(_quick_key)
        if _up_state is not None and _up_state.persistent.update_prompt_pending:
            raw = (event.text or "").strip()
            # Accept /approve and /deny as shorthand for yes/no
            cmd = event.get_command()
            if cmd in {"approve", "yes"}:
                response_text = "y"
            elif cmd in {"deny", "no"}:
                response_text = "n"
            else:
                _recognized_cmd = None
                if cmd:
                    try:
                        from hermes_cli.commands import resolve_command as _resolve_update_cmd
                    except Exception:
                        _resolve_update_cmd = None
                    if _resolve_update_cmd is not None:
                        try:
                            _cmd_def = _resolve_update_cmd(cmd)
                            _recognized_cmd = _cmd_def.name if _cmd_def else None
                        except Exception:
                            _recognized_cmd = None
                if _recognized_cmd:
                    response_text = ""
                else:
                    response_text = raw
            if response_text:
                response_path = _hermes_home / ".update_response"
                prompt_path = _hermes_home / ".update_prompt.json"
                try:
                    tmp = response_path.with_suffix(".tmp")
                    tmp.write_text(response_text, encoding="utf-8")
                    tmp.replace(response_path)
                    prompt_path.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to write update response: %s", e)
                    return f"✗ Failed to send response to update process: {e}"
                _up_state.persistent.update_prompt_pending = False
                label = response_text if len(response_text) <= 20 else response_text[:20] + "…"
                return f"✓ Sent `{label}` to the update process."
            # Recognized slash command during a pending update prompt:
            # unblock the detached update subprocess by writing a blank
            # response so ``_gateway_prompt`` returns the prompt's default
            # (typically a safe "n" / skip) and exits cleanly instead of
            # blocking on stdin until the 30-minute watcher timeout.
            # The slash command then falls through to normal dispatch.
            if _recognized_cmd:
                response_path = _hermes_home / ".update_response"
                prompt_path = _hermes_home / ".update_prompt.json"
                try:
                    tmp = response_path.with_suffix(".tmp")
                    tmp.write_text("", encoding="utf-8")
                    tmp.replace(response_path)
                    prompt_path.unlink(missing_ok=True)
                    logger.info(
                        "Recognized /%s during pending update prompt for %s; "
                        "cancelled prompt with default and dispatching command",
                        _recognized_cmd,
                        _quick_key,
                    )
                except OSError as e:
                    logger.warning(
                        "Failed to write cancel response for pending update prompt: %s",
                        e,
                    )
                _up_state.persistent.update_prompt_pending = False

        # Intercept messages that are responses to a pending clarify.
        # Open-ended prompts and "Other" responses are captured as free text;
        # direct replies to multi-choice prompts are accepted too ("2" maps
        # to the second option, arbitrary text becomes a custom answer). Slash
        # commands still bypass this path so /stop and friends keep working.
        _clarify_mod = None
        try:
            from tools import clarify_gateway as _clarify_mod
            _pending_clarify = _clarify_mod.get_pending_for_session(
                _quick_key, include_choice_prompts=True,
            )
        except Exception:
            _pending_clarify = None
        if _pending_clarify is not None and _clarify_mod is not None:
            _clarify_has_audio = bool(self._pending_event_audio_paths(event))
            _raw_clarify_reply = await self._prepare_clarify_reply_text(event)
            if _clarify_has_audio and not _raw_clarify_reply:
                logger.info(
                    "Gateway retained pending clarify after voice transcription "
                    "produced no usable text (session=%s, id=%s)",
                    _quick_key,
                    _pending_clarify.clarify_id,
                )
                return ""
            # Skip slash commands — the user clearly wanted to issue a
            # command, not answer the clarify.  Leave the clarify pending
            # so the user can retry; if it times out, the agent unblocks
            # with an empty response.
            if _raw_clarify_reply and not _raw_clarify_reply.startswith("/"):
                _resolved = _clarify_mod.resolve_text_response_for_session(
                    _quick_key, _raw_clarify_reply,
                )
                if _resolved:
                    logger.info(
                        "Gateway intercepted clarify text response (session=%s, id=%s)",
                        _quick_key, _pending_clarify.clarify_id,
                    )
                    # The clarify callback pauses the platform typing/status
                    # indicator while waiting so Slack users can type their
                    # answer. The active agent resumes as soon as this reply
                    # resolves the wait, so re-enable its indicator here too.
                    # Without this, Slack stays silent until the independent
                    # long-running heartbeat fires (three minutes by default).
                    _clarify_adapter = self._adapter_for_source(source)
                    if _clarify_adapter:
                        try:
                            _clarify_adapter.resume_typing_for_chat(source.chat_id)
                        except Exception:
                            logger.debug(
                                "Failed to resume typing after clarify response",
                                exc_info=True,
                            )
                    # Acknowledge with empty string so adapters that emit
                    # the agent's response don't double-post.  The agent
                    # itself will produce the next user-facing message.
                    return ""

        # Intercept messages that are responses to a pending /reload-mcp
        # (or future) slash-confirm prompt.  Recognized confirm replies are
        # /approve, /always, /cancel (plus short aliases).  Anything else
        # falls through to normal dispatch — a stale pending confirm does
        # NOT block other commands.
        #
        # Important: if a dangerous-command approval is ALSO pending (agent
        # blocked inside tools/approval.py), the tool approval takes
        # precedence — /approve there unblocks the waiting tool thread.
        # Slash-confirm only catches /approve when no tool approval is live.
        from tools import slash_confirm as _slash_confirm_mod
        _pending_confirm = _slash_confirm_mod.get_pending(_quick_key)
        _tool_approval_live = False
        try:
            from tools.approval import has_blocking_approval
            _tool_approval_live = has_blocking_approval(_quick_key)
        except Exception:
            _tool_approval_live = False
        if _pending_confirm and not _tool_approval_live:
            _raw_reply = (event.text or "").strip()
            # Accept bang-prefixed replies (`!always`, `!cancel`) verbatim.
            # Slack/Matrix instruction text shows the `!` prefix (typed `/`
            # is blocked in Slack threads), but the adapters only rewrite
            # `!<known-command>` — `always`/`cancel` are confirm keywords,
            # not registered commands, so the `!` survives to here.
            _norm_reply = _raw_reply.lstrip("!/").lower()
            _cmd_reply = event.get_command()
            _confirm_choice = None
            if _cmd_reply in {"approve", "yes", "ok", "confirm"}:
                _confirm_choice = "once"
            elif _cmd_reply in {"always", "remember"}:
                _confirm_choice = "always"
            elif _cmd_reply in {"cancel", "no", "deny", "nevermind"}:
                _confirm_choice = "cancel"
            elif _norm_reply in {"approve", "approve once", "once"}:
                _confirm_choice = "once"
            elif _norm_reply in {"always", "always approve"}:
                _confirm_choice = "always"
            elif _norm_reply in {"cancel", "nevermind", "no"}:
                _confirm_choice = "cancel"
            if _confirm_choice is not None:
                _resolved = await _slash_confirm_mod.resolve(
                    _quick_key, _pending_confirm.get("confirm_id"), _confirm_choice,
                )
                return _resolved or ""
            # Stale pending + unrelated command: drop the pending state so
            # the confirm doesn't block normal usage indefinitely.  The user
            # clearly moved on.
            _slash_confirm_mod.clear_if_stale(_quick_key)

        # PRIORITY handling when an agent is already running for this session.
        # Default behavior is to interrupt immediately so user text/stop messages
        # are handled with minimal latency.
        #
        # Special case: Telegram/photo bursts often arrive as multiple near-
        # simultaneous updates. Do NOT interrupt for photo-only follow-ups here;
        # let the adapter-level batching/queueing logic absorb them.

        # Staleness eviction: detect leaked locks from hung/crashed handlers.
        # With inactivity-based timeout, active tasks can run for hours, so
        # wall-clock age alone isn't sufficient.  Evict only when the agent
        # has been *idle* beyond the inactivity threshold (or when the agent
        # object has no activity tracker and wall-clock age is extreme).
        _raw_stale_timeout = _float_env("HERMES_AGENT_TIMEOUT", 1800)
        _quick_state = self._peek_session_state(_quick_key)
        _stale_ts = _quick_state.turn.started_ts if _quick_state else 0
        if _quick_state is not None and _quick_state.turn.agent is not None and _stale_ts:
            _stale_age = time.time() - _stale_ts
            _stale_agent = _quick_state.turn.agent
            # Never evict the pending sentinel — it was just placed moments
            # ago during the async setup phase before the real agent is
            # created.  Sentinels have no get_activity_summary(), so the
            # idle check below would always evaluate to inf >= timeout and
            # immediately evict them, racing with the setup path.
            _stale_idle = float("inf")  # assume idle if we can't check
            _stale_detail = ""
            if _stale_agent and hasattr(_stale_agent, "get_activity_summary"):
                try:
                    _sa = _stale_agent.get_activity_summary()
                    _stale_idle = _sa.get("seconds_since_activity", float("inf"))
                    _stale_detail = (
                        f" | last_activity={_sa.get('last_activity_desc', 'unknown')} "
                        f"({_stale_idle:.0f}s ago) "
                        f"| iteration={_sa.get('api_call_count', 0)}/{_sa.get('max_iterations', 0)}"
                    )
                except Exception:
                    pass
            # Evict if: agent is idle beyond timeout, OR wall-clock age is
            # extreme (10x timeout or 2h, whichever is larger — catches
            # cases where the agent object was garbage-collected).
            _wall_ttl = max(_raw_stale_timeout * 10, 7200) if _raw_stale_timeout > 0 else float("inf")
            _should_evict = (
                _stale_agent is not _AGENT_PENDING_SENTINEL
                and (
                    (_raw_stale_timeout > 0 and _stale_idle >= _raw_stale_timeout)
                    or _stale_age > _wall_ttl
                )
            )
            if _should_evict:
                logger.warning(
                    "Evicting stale _running_agents entry for %s "
                    "(age: %.0fs, idle: %.0fs, timeout: %.0fs)%s",
                    _quick_key, _stale_age, _stale_idle,
                    _raw_stale_timeout, _stale_detail,
                )
                self._invalidate_session_run_generation(
                    _quick_key,
                    reason="stale_running_agent_eviction",
                )
                self._release_running_agent_state(_quick_key)

        if self._is_session_running(_quick_key):
            # Resolve the command once; every command's mid-run behavior is
            # declared on its CommandDef (busy_policy / busy_handler in
            # hermes_cli/commands.py) and dispatched through the single
            # resolver _dispatch_busy_slash_command below — no per-command
            # if-chain here.
            from hermes_cli.commands import resolve_command as _resolve_cmd_inner
            _evt_cmd = event.get_command()
            _cmd_def_inner = _resolve_cmd_inner(_evt_cmd) if _evt_cmd else None

            # /status and /context are intentionally pre-gate so users
            # always see session state.
            if _cmd_def_inner and _cmd_def_inner.name == "status":
                return await self._handle_status_command(event)
            if _cmd_def_inner and _cmd_def_inner.name == "context":
                return await self._handle_context_command(event)

            # Slash command access control on the running-agent fast-path.
            # Mirrors the cold-path gate further below so non-admin users
            # can't bypass gating just because an agent happens to be busy.
            # /status above is intentionally pre-gate so users always see
            # session state. /help and /whoami fall under the always-allowed
            # floor inside _check_slash_access.
            if _evt_cmd and _cmd_def_inner is not None:
                _denied = self._check_slash_access(source, _cmd_def_inner.name)
                if _denied is not None:
                    return _denied

            # Any recognized slash command: dispatch according to its
            # declared busy_policy (dispatch / interrupt_then_dispatch /
            # reject). Unrecognized commands and plain text fall through
            # to the interrupt/queue logic below.
            if _cmd_def_inner:
                return await self._dispatch_busy_slash_command(
                    event, _cmd_def_inner, _quick_key, source,
                )

            if event.message_type == MessageType.PHOTO:
                logger.debug("PRIORITY photo follow-up for session %s — queueing without interrupt", _quick_key)
                adapter = self._adapter_for_source(source)
                if adapter:
                    merge_pending_message_event(adapter._pending_messages, _quick_key, event)
                return None

            _telegram_followup_grace = float(
                os.getenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0")
            )
            _grace_state = self._peek_session_state(_quick_key)
            _started_at = _grace_state.turn.started_ts if _grace_state else 0
            if (
                source.platform == Platform.TELEGRAM
                and event.message_type == MessageType.TEXT
                and _telegram_followup_grace > 0
                and _started_at
                and (time.time() - _started_at) <= _telegram_followup_grace
            ):
                logger.debug(
                    "Telegram follow-up arrived %.2fs after run start for %s — queueing without interrupt",
                    time.time() - _started_at,
                    _quick_key,
                )
                adapter = self._adapter_for_source(source)
                if adapter:
                    if self._busy_input_mode == "queue":
                        self._enqueue_fifo(_quick_key, event, adapter)
                    else:
                        merge_pending_message_event(
                            adapter._pending_messages,
                            _quick_key,
                            event,
                            merge_text=True,
                        )
                return None

            _ra_state = self._peek_session_state(_quick_key)
            running_agent = _ra_state.turn.agent if _ra_state else None
            if running_agent is _AGENT_PENDING_SENTINEL:
                # Agent is being set up but not ready yet.
                if event.get_command() == "stop":
                    # Force-clean the sentinel so the session is unlocked.
                    self._release_running_agent_state(_quick_key)
                    logger.info("HARD STOP (pending) for session %s — sentinel cleared", _quick_key)
                    return EphemeralReply("⚡ Force-stopped. The agent was still starting — session unlocked.")
                # Queue the message so it will be picked up after the
                # agent starts.
                adapter = self._adapter_for_source(source)
                if adapter:
                    merge_pending_message_event(
                        adapter._pending_messages,
                        _quick_key,
                        event,
                        merge_text=True,
                    )
                return None
            if self._draining:
                if self._queue_during_drain_enabled():
                    self._queue_or_replace_pending_event(_quick_key, event)
                return (
                    f"⏳ Gateway {self._status_action_gerund()} — queued for the next turn after it comes back."
                    if self._queue_during_drain_enabled()
                    else f"⏳ Gateway is {self._status_action_gerund()} and is not accepting another turn right now."
                )
            if self._busy_input_mode == "queue":
                logger.debug("PRIORITY queue follow-up for session %s", _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            if self._busy_input_mode == "steer":
                # Steer mode: inject text into the running agent mid-run via
                # agent.steer().  Falls back to queue semantics if the payload
                # is empty, the agent lacks steer(), or steer() rejects.
                steer_text = (event.text or "").strip()
                steered = False
                if (
                    event.message_type == MessageType.TEXT
                    and not event.media_urls
                    and not event.media_types
                    and steer_text
                    and hasattr(running_agent, "steer")
                ):
                    try:
                        steered = bool(running_agent.steer(steer_text))
                    except Exception as exc:
                        logger.warning("PRIORITY steer failed for session %s: %s", _quick_key, exc)
                        steered = False
                if steered:
                    logger.debug("PRIORITY steer for session %s", _quick_key)
                    return None
                logger.debug("PRIORITY steer-fallback-to-queue for session %s", _quick_key)
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            # #30170 — Subagent protection (PRIORITY path). Same rationale
            # as ``_handle_active_session_busy_message``: an interrupt
            # cascades through ``_active_children`` and aborts in-flight
            # delegate_task work. Demote to queue semantics when the
            # parent is currently driving subagents so a conversational
            # follow-up doesn't destroy minutes of subagent progress.
            # /stop reaches its dedicated handler above, so the operator
            # still has a clean escape hatch.
            if self._agent_has_active_subagents(running_agent):
                logger.info(
                    "PRIORITY interrupt demoted to queue for session %s "
                    "because the running agent has active subagents (#30170)",
                    _quick_key,
                )
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            # #56391 — Compression protection (PRIORITY path). Same
            # rationale as ``_handle_active_session_busy_message``: context
            # compression is interrupt-protected (#23975), but an interrupt
            # here starts a new turn against the pre-rotation parent
            # session while the still-running compression later rotates
            # the id out from under it, forking orphaned compression
            # siblings. Demote to queue semantics so the follow-up waits
            # for the in-flight compression + rotation to land.
            if await self._session_has_compression_in_flight(_quick_key):
                logger.info(
                    "PRIORITY interrupt demoted to queue for session %s "
                    "because context compression is in flight (#56391)",
                    _quick_key,
                )
                self._queue_or_replace_pending_event(_quick_key, event)
                return None
            # Text-only corrections redirect the live turn (preserving
            # displayed context) when the runtime supports it; media/voice and
            # older runtimes fall back to the proven interrupt path below.
            if (
                event.message_type == MessageType.TEXT
                and not event.media_urls
                and not event.media_types
                and getattr(running_agent, "_supports_active_turn_redirect", False)
                is True
                and hasattr(running_agent, "redirect")
            ):
                try:
                    if running_agent.redirect((event.text or "").strip()):
                        logger.debug("PRIORITY redirect for session %s", _quick_key)
                        return None
                except Exception as exc:
                    logger.warning(
                        "PRIORITY redirect failed for session %s: %s",
                        _quick_key,
                        exc,
                    )
            logger.debug("PRIORITY interrupt for session %s", _quick_key)
            _interrupt_text = event.text
            _media_urls = getattr(event, "media_urls", None) or []
            if self._pending_event_audio_paths(event):
                _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                    event,
                    self._adapter_for_source(source),
                    source,
                    event.text or "",
                    log_context="Voice-priority-interrupt",
                )
            elif not _interrupt_text and _media_urls:
                _interrupt_text = _build_media_placeholder(event)
            running_agent.interrupt(_interrupt_text)
            # NOTE: self._pending_messages was write-only (never consumed).
            # The actual interrupt message is delivered via adapter._pending_messages
            # which is read by _run_agent. Removed to prevent unbounded growth.
            return None

        # Check for commands
        command = event.get_command()

        from hermes_cli.commands import (
            GATEWAY_KNOWN_COMMANDS,
            is_gateway_known_command,
            resolve_command as _resolve_cmd,
        )

        # Resolve aliases to canonical name so dispatch and hook names
        # don't depend on the exact alias the user typed.
        _cmd_def = _resolve_cmd(command) if command else None
        canonical = _cmd_def.name if _cmd_def else command

        # Expand alias quick commands before built-in dispatch so targets like
        # /model openai/gpt-5.5 --provider openrouter reach the /model handler.
        # Preserve built-in precedence; aliases only need early handling when
        # the typed command is not already known.
        if command and _cmd_def is None:
            if isinstance(self.config, dict):
                quick_commands = self.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(self.config, "quick_commands", {}) or {}
            if isinstance(quick_commands, dict) and command in quick_commands:
                qcmd = quick_commands[command]
                if qcmd.get("type") == "alias":
                    target = (qcmd.get("target") or "").strip()
                    if target:
                        target = target if target.startswith("/") else f"/{target}"
                        target_command = target.lstrip("/")
                        user_args = event.get_command_args().strip()
                        event.text = f"{target} {user_args}".strip()
                        command = target_command.split()[0] if target_command else target_command
                        _cmd_def = _resolve_cmd(command) if command else None
                        canonical = _cmd_def.name if _cmd_def else command

        # Per-platform slash command access control. Only kicks in when the
        # operator has set ``allow_admin_from`` for the source's scope (DM
        # vs group). When unset → backward-compat: every allowed user can
        # run every command. When set → non-admins can run only commands in
        # ``user_allowed_commands`` (plus the always-allowed floor: /help,
        # /whoami). Plain chat is unaffected — only slash commands gate.
        if command and canonical and is_gateway_known_command(canonical):
            _denied = self._check_slash_access(source, canonical)
            if _denied is not None:
                return _denied

        # Fire the ``command:<canonical>`` hook for any recognized slash
        # command — built-in OR plugin-registered. Handlers can return a
        # dict with ``{"decision": "deny" | "handled" | "rewrite", ...}``
        # to intercept dispatch before core handling runs. This replaces
        # the previous fire-and-forget emit(): return values are now
        # honored, but handlers that return nothing behave exactly as
        # before (telemetry-style hooks keep working).
        if command and is_gateway_known_command(canonical):
            raw_args = event.get_command_args().strip()
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "command": canonical,
                "raw_command": command,
                "args": raw_args,
                "raw_args": raw_args,
            }
            try:
                hook_results = await self.hooks.emit_collect(
                    f"command:{canonical}", hook_ctx
                )
            except Exception as _hook_err:
                logger.debug(
                    "command:%s hook dispatch failed (non-fatal): %s",
                    canonical, _hook_err,
                )
                hook_results = []

            for hook_result in hook_results:
                if not isinstance(hook_result, dict):
                    continue
                decision = str(hook_result.get("decision", "")).strip().lower()
                if not decision or decision == "allow":
                    continue
                if decision == "deny":
                    message = hook_result.get("message")
                    if isinstance(message, str) and message:
                        return message
                    return f"Command `/{command}` was blocked by a hook."
                if decision == "handled":
                    message = hook_result.get("message")
                    return message if isinstance(message, str) and message else None
                if decision == "rewrite":
                    new_command = str(
                        hook_result.get("command_name", "")
                    ).strip().lstrip("/")
                    if not new_command:
                        continue
                    new_args = str(hook_result.get("raw_args", "")).strip()
                    event.text = f"/{new_command} {new_args}".strip()
                    command = event.get_command()
                    _cmd_def = _resolve_cmd(command) if command else None
                    canonical = _cmd_def.name if _cmd_def else command
                    break

        if canonical == "new":
            if await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
                return self._telegram_topic_root_new_message()
            async def _do_reset():
                return await self._handle_reset_command(event)
            return await self._maybe_confirm_destructive_slash(
                event=event,
                command="new",
                title="/new",
                detail=(
                    "This starts a fresh session and discards the current "
                    "conversation history."
                ),
                execute=_do_reset,
            )

        if canonical == "topic":
            return await self._handle_topic_command(event)
        
        if canonical == "help":
            return await self._handle_help_command(event)

        if canonical == "start":
            logger.info("Ignoring /start platform ping for session %s", _quick_key)
            return ""

        if canonical == "commands":
            return await self._handle_commands_command(event)
        
        if canonical == "profile":
            return await self._handle_profile_command(event)

        if canonical == "whoami":
            return await self._handle_whoami_command(event)

        if canonical == "status":
            return await self._handle_status_command(event)

        if canonical == "egress":
            from hermes_cli.proxy_cli import format_status_text

            return format_status_text()

        if canonical == "context":
            return await self._handle_context_command(event)

        if canonical == "agents":
            return await self._handle_agents_command(event)

        if canonical == "platform":
            return await self._handle_platform_command(event)

        if canonical == "restart":
            return await self._handle_restart_command(event)
        
        if canonical == "stop":
            return await self._handle_stop_command(event)
        
        if canonical == "reasoning":
            return await self._handle_reasoning_command(event)

        if canonical == "memory":
            return await self._handle_memory_command(event)

        if canonical == "skills":
            return await self._handle_skills_command(event)

        if canonical == "learn":
            # Open-ended: rewrite the turn to a standards-guided prompt and fall
            # through to normal agent processing. The live agent gathers the
            # sources the user described (dirs via read_file, URLs via
            # web_extract, this conversation, pasted text) and authors the skill
            # via skill_manage. Mirrors the /blueprint fall-through so role
            # alternation is preserved. No engine, works on any backend.
            from agent.learn_prompt import build_learn_prompt

            _learn_req = event.get_command_args().strip()
            _ack = (
                "Learning a skill from what you described…"
                if _learn_req
                else "Learning a skill from this conversation…"
            )
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug("learn ack send failed", exc_info=True)
            try:
                event.text = build_learn_prompt(_learn_req)
                # fall through to agent processing
            except Exception:
                return "Could not start /learn — please try again."

        if canonical == "init":
            # /init: rewrite the turn to a guidance-laden prompt and fall
            # through to normal agent processing (same fall-through as /learn
            # so role alternation is preserved). The live agent scans the
            # project with its own read-only tools and writes/updates
            # AGENTS.md via write_file. No engine, works on any backend.
            from hermes_cli.init_command import build_init_prompt_for_cwd

            _init_notes = event.get_command_args().strip()
            try:
                _init_prompt = build_init_prompt_for_cwd(extra=_init_notes)
            except Exception:
                return "Could not start /init — please try again."
            _ack = (
                "Updating AGENTS.md from a project scan…"
                if "UPDATE the existing AGENTS.md" in _init_prompt
                else "Generating AGENTS.md from a project scan…"
            )
            try:
                adapter = self._adapter_for_source(source)
                if adapter:
                    _ack_meta = self._thread_metadata_for_source(source)
                    await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
            except Exception:
                logger.debug("init ack send failed", exc_info=True)
            event.text = _init_prompt
            # fall through to agent processing

        if canonical == "fast":
            return await self._handle_fast_command(event)

        if canonical == "verbose":
            return await self._handle_verbose_command(event)

        if canonical == "footer":
            return await self._handle_footer_command(event)

        if canonical == "yolo":
            return await self._handle_yolo_command(event)

        if canonical == "approvals":
            return await self._handle_approvals_command(event)

        if canonical == "model":
            return await self._handle_model_command(event)

        if canonical == "codex-runtime":
            return await self._handle_codex_runtime_command(event)

        if canonical == "personality":
            return await self._handle_personality_command(event)

        if canonical == "kanban":
            return await self._handle_kanban_command(event)

        if canonical == "suggestions":
            return await self._handle_suggestions_command(event)

        if canonical == "blueprint":
            _blueprint_result = await self._handle_blueprint_command(event)
            _blueprint_seed = getattr(_blueprint_result, "agent_seed", None)
            if _blueprint_seed:
                # Blueprint matched — rewrite the turn to the seed and fall
                # through to _handle_message_with_agent so the agent asks the
                # user for each slot value conversationally and then calls the
                # cronjob tool (the /steer fall-through pattern). The seed
                # enters as a normal user turn, preserving role alternation.
                # Send the "Setting up X…" ack first so the user gets the same
                # immediate feedback CLI users see, instead of silence until
                # the agent's first question.
                _ack = getattr(_blueprint_result, "text", "") or ""
                if _ack:
                    try:
                        adapter = self._adapter_for_source(source)
                        if adapter:
                            _ack_meta = self._thread_metadata_for_source(source)
                            await adapter.send(str(source.chat_id), _ack, metadata=_ack_meta)
                    except Exception:
                        logger.debug("blueprint ack send failed", exc_info=True)
                try:
                    event.text = _blueprint_seed
                except Exception:
                    return getattr(_blueprint_result, "text", "") or None
            else:
                return getattr(_blueprint_result, "text", "") or None

        if canonical == "retry":
            return await self._handle_retry_command(event)
        
        if canonical == "undo":
            async def _do_undo():
                return await self._handle_undo_command(event)
            _undo_n = 1
            _undo_raw = event.get_command_args().strip()
            if _undo_raw:
                try:
                    _undo_n = max(1, int(_undo_raw.split()[0]))
                except (ValueError, IndexError):
                    _undo_n = 1
            _undo_detail = (
                "This removes the last user/assistant exchange from history."
                if _undo_n == 1
                else f"This removes the last {_undo_n} user turns from history."
            )
            return await self._maybe_confirm_destructive_slash(
                event=event,
                command="undo",
                title="/undo",
                detail=_undo_detail,
                execute=_do_undo,
            )
        
        if canonical == "sethome":
            return await self._handle_set_home_command(event)

        if canonical == "compress":
            return await self._handle_compress_command(event)

        if canonical == "usage":
            return await self._handle_usage_command(event)

        if canonical == "topup":
            return await self._handle_topup_command(event)

        if canonical == "insights":
            return await self._handle_insights_command(event)

        if canonical == "reload-mcp":
            return await self._handle_reload_mcp_command(event)

        if canonical == "reload-skills":
            return await self._handle_reload_skills_command(event)

        if canonical == "bundles":
            return await self._handle_bundles_command(event)

        if canonical == "approve":
            return await self._handle_approve_command(event)

        if canonical == "deny":
            return await self._handle_deny_command(event)

        if canonical == "update":
            return await self._handle_update_command(event)

        if canonical == "version":
            return await self._handle_version_command(event)

        if canonical == "debug":
            return await self._handle_debug_command(event)

        if canonical == "title":
            return await self._handle_title_command(event)

        if canonical == "resume":
            return await self._handle_resume_command(event)

        if canonical == "sessions":
            return await self._handle_sessions_command(event)

        if canonical == "branch":
            return await self._handle_branch_command(event)

        if canonical == "rollback":
            return await self._handle_rollback_command(event)

        if canonical == "diff":
            return await self._handle_diff_command(event)

        if canonical == "background":
            return await self._handle_background_command(event)

        if canonical == "queue":
            queue_payload = event.get_command_args().strip()
            if not queue_payload:
                return "Usage: /queue <prompt>"
            try:
                event.text = queue_payload
            except Exception:
                pass

        if canonical == "steer":
            # No active agent — /steer has no tool call to inject into.
            # Strip the prefix so downstream treats it as a normal user
            # message. If the payload is empty, surface the usage hint.
            steer_payload = event.get_command_args().strip()
            if not steer_payload:
                return "Usage: /steer <prompt>  (no agent is running; sending as a normal message)"
            try:
                event.text = steer_payload
            except Exception:
                pass
            # Do NOT return — fall through to _handle_message_with_agent
            # at the end of this function so the rewritten text is sent
            # to the agent as a regular user turn.

        if canonical == "goal":
            return await self._handle_goal_command(event)

        if canonical == "moa":
            # /moa is one-shot sugar only: run a single prompt through the
            # default MoA preset, then restore the prior model. To *switch* to a
            # MoA preset for the session, pick it from the model picker (MoA
            # presets surface as a virtual "Mixture of Agents" provider).
            from hermes_cli.moa_config import (
                moa_usage,
                normalize_moa_config,
            )
            from hermes_cli.config import load_config

            moa_payload = event.get_command_args().strip()
            if not moa_payload:
                return moa_usage()
            try:
                cfg = load_config()
                moa_cfg = normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})
            except Exception:
                moa_cfg = normalize_moa_config({})
            preset = moa_cfg["default_preset"]
            try:
                event.text = moa_payload
                _moa_state = self._session_state(_quick_key)
                event._moa_restore_override = _moa_state.conversation.model_override
                _moa_state.conversation.model_override = {
                    "provider": "moa",
                    "model": preset,
                    "base_url": "moa://local",
                    "api_key": "moa-virtual-provider",
                    "api_mode": "chat_completions",
                }
                self._evict_cached_agent(_quick_key)
                event._moa_disable_after_turn = True
            except Exception:
                return "Failed to prepare MoA turn."

        if canonical == "subgoal":
            return await self._handle_subgoal_command(event)

        if canonical == "voice":
            return await self._handle_voice_command(event)

        if self._draining:
            return f"⏳ Gateway is {self._status_action_gerund()} and is not accepting new work right now."

        # User-defined quick commands (bypass agent loop, no LLM call)
        if command:
            if isinstance(self.config, dict):
                quick_commands = self.config.get("quick_commands", {}) or {}
            else:
                quick_commands = getattr(self.config, "quick_commands", {}) or {}
            if not isinstance(quick_commands, dict):
                quick_commands = {}
            if command in quick_commands:
                # Quick commands are slash capabilities too — and type:exec
                # ones run a shell command in the gateway process. The early
                # gate above only fires for registry-known commands, so quick
                # commands (never in the registry) would otherwise reach this
                # dispatch sink unchecked. Apply the same admin/user policy to
                # the raw typed name here so non-admins can't invoke admin-only
                # quick commands. (#44727)
                _denied = self._check_slash_access(source, command)
                if _denied is not None:
                    return _denied
                qcmd = quick_commands[command]
                if qcmd.get("type") == "exec":
                    exec_cmd = qcmd.get("command", "")
                    if exec_cmd:
                        try:
                            # Sanitize env to prevent credential leakage —
                            # quick commands run in the gateway process which
                            # has all API keys in os.environ.
                            from tools.environments.local import build_subprocess_env
                            sanitized_env = build_subprocess_env()
                            proc = await asyncio.create_subprocess_shell(
                                exec_cmd,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                                env=sanitized_env,
                            )
                            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
                            output = (stdout or stderr).decode().strip()
                            # Redact any remaining sensitive patterns in output
                            if output:
                                from agent.redact import redact_sensitive_text
                                output = redact_sensitive_text(output)
                            return output if output else "Command returned no output."
                        except asyncio.TimeoutError:
                            return "Quick command timed out (30s)."
                        except Exception as e:
                            return f"Quick command error: {e}"
                    else:
                        return f"Quick command '/{command}' has no command defined."
                elif qcmd.get("type") == "alias":
                    target = (qcmd.get("target") or "").strip()
                    if target:
                        target = target if target.startswith("/") else f"/{target}"
                        target_command = target.lstrip("/")
                        user_args = event.get_command_args().strip()
                        event.text = f"{target} {user_args}".strip()
                        command = target_command.split()[0] if target_command else target_command
                        # Fall through to normal command dispatch below
                    else:
                        return f"Quick command '/{command}' has no target defined."
                else:
                    return f"Quick command '/{command}' has unsupported type (supported: 'exec', 'alias')."

        # Plugin-registered slash commands
        if command:
            try:
                from hermes_cli.plugins import get_plugin_command_handler
                # Normalize underscores to hyphens so Telegram's underscored
                # autocomplete form matches plugin commands registered with
                # hyphens. See hermes_cli/commands.py:_build_telegram_menu.
                plugin_handler = get_plugin_command_handler(command.replace("_", "-"))
                if plugin_handler:
                    user_args = event.get_command_args().strip()
                    result = plugin_handler(user_args)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return str(result) if result else None
            except Exception as e:
                logger.warning("Plugin command dispatch failed: %s", e)

        # Skill slash commands: /skill-name loads the skill and sends to agent.
        # resolve_skill_command_key() handles the Telegram underscore/hyphen
        # round-trip so /claude_code from Telegram autocomplete still resolves
        # to the claude-code skill.
        if command:
            # Skill bundles take precedence over individual skill commands —
            # /<bundle> loads multiple skills at once. Mirrors CLI dispatch.
            _bundle_handled = False
            try:
                from agent.skill_bundles import (
                    build_bundle_invocation_message,
                    resolve_bundle_command_key,
                )
                bundle_key = resolve_bundle_command_key(command)
                if bundle_key is not None:
                    user_instruction = event.get_command_args().strip()
                    # Pass the platform explicitly: bundle skill loading
                    # bypasses get_skill_commands()' scan-time disabled
                    # filter, and the gateway serves multiple platforms in
                    # one process, so env-var platform resolution can't be
                    # trusted here. Mirrors the stacked-skill gate (#58888).
                    _bundle_plat = source.platform.value if source.platform else None
                    bundle_result = build_bundle_invocation_message(
                        bundle_key, user_instruction, task_id=_quick_key,
                        platform=_bundle_plat,
                    )
                    if bundle_result:
                        msg, _loaded, missing = bundle_result
                        event.text = msg
                        _bundle_handled = True
                        if missing:
                            logger.info(
                                "Bundle %s skipped missing skills: %s",
                                bundle_key, ", ".join(missing),
                            )
                        # Fall through to normal message processing with bundle content
            except Exception as exc:
                logger.warning("Bundle dispatch failed: %s", exc)

        if command and not locals().get("_bundle_handled", False):
            try:
                from agent.skill_commands import (
                    get_skill_commands,
                    build_skill_invocation_message,
                    resolve_skill_command_key,
                )
                skill_cmds = get_skill_commands()
                cmd_key = resolve_skill_command_key(command)
                if cmd_key is not None:
                    # Check per-platform disabled status before executing.
                    # get_skill_commands() only applies the *global* disabled
                    # list at scan time; per-platform overrides need checking
                    # here because the cache is process-global across platforms.
                    _skill_name = skill_cmds[cmd_key].get("name", "")
                    _plat = source.platform.value if source.platform else None
                    if _plat and _skill_name:
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        if _skill_name in _get_plat_disabled(platform=_plat):
                            return (
                                f"The **{_skill_name}** skill is disabled for {_plat}.\n"
                                f"Enable it with: `hermes skills config`"
                            )
                    user_instruction = event.get_command_args().strip()
                    # Stacked slash-skill invocations: `/skill-a /skill-b do
                    # XYZ` loads every leading skill (up to 5), not just the
                    # first. Inspired by Claude Code v2.1.199. Mirrors CLI.
                    try:
                        from agent.skill_commands import (
                            build_stacked_skill_invocation_message as _build_stacked,
                            split_stacked_skill_commands,
                        )
                        extra_keys, stacked_instruction = (
                            split_stacked_skill_commands(user_instruction)
                        )
                    except Exception:
                        _build_stacked = None
                        extra_keys, stacked_instruction = [], user_instruction
                    if extra_keys and _plat:
                        # split_stacked_skill_commands() only resolves that
                        # each extra token is a KNOWN skill command — like
                        # get_skill_commands() itself, it has no per-platform
                        # view. Re-check every stacked skill (not just the
                        # leading one above) against the same disabled list,
                        # or a skill an operator disabled for this platform
                        # still gets its full content loaded via the stack.
                        from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                        _plat_disabled = _get_plat_disabled(platform=_plat)
                        _disabled_extra = [
                            skill_cmds.get(k, {}).get("name", "")
                            for k in extra_keys
                            if skill_cmds.get(k, {}).get("name", "") in _plat_disabled
                        ]
                        if _disabled_extra:
                            return (
                                f"The **{', '.join(_disabled_extra)}** skill(s) in this "
                                f"stacked invocation are disabled for {_plat}.\n"
                                f"Enable them with: `hermes skills config`"
                            )
                    if extra_keys and _build_stacked is not None:
                        stacked_result = _build_stacked(
                            [cmd_key, *extra_keys],
                            stacked_instruction,
                            task_id=_quick_key,
                        )
                        if stacked_result:
                            msg, _loaded, _missing = stacked_result
                            event.text = msg
                            # Fall through to normal message processing
                        else:
                            return f"Failed to load stacked skills for /{command}."
                    else:
                        msg = build_skill_invocation_message(
                            cmd_key, user_instruction, task_id=_quick_key
                        )
                        if msg:
                            event.text = msg
                            # Fall through to normal message processing with skill content
                else:
                    # Not an active skill — check if it's a known-but-disabled or
                    # uninstalled skill and give actionable guidance.
                    _unavail_msg = _check_unavailable_skill(command)
                    if _unavail_msg:
                        return _unavail_msg
                    # Genuinely unrecognized /command: not a built-in, not a
                    # plugin, not a skill, not a known-inactive skill. Warn
                    # the user instead of silently forwarding it to the LLM
                    # as free text (which leads to silent-failure behavior
                    # like the model inventing a delegate_task call).
                    # Normalize to hyphenated form before checking known
                    # built-ins (command may be an alias target set by the
                    # quick-command block above, so _cmd_def can be stale).
                    if command.replace("_", "-") not in GATEWAY_KNOWN_COMMANDS:
                        logger.warning(
                            "Unrecognized slash command /%s from %s — "
                            "replying with unknown-command notice",
                            command,
                            source.platform.value if source.platform else "?",
                        )
                        return (
                            f"Unknown command `/{command}`. "
                            f"Type /commands to see what's available, "
                            f"or resend without the leading slash to send "
                            f"as a regular message."
                        )
            except Exception as e:
                logger.debug("Skill command check failed (non-fatal): %s", e)
        
        # Pending exec approvals are handled by /approve and /deny commands above.
        # No bare text matching — "yes" in normal conversation must not trigger
        # execution of a dangerous command.

        if not is_internal and await asyncio.to_thread(
            self._is_telegram_topic_root_lobby, source
        ):
            # Debounce the lobby reminder so a user who forgets about
            # topic mode and fires ten prompts doesn't get ten copies.
            if self._should_send_telegram_lobby_reminder(source):
                return self._telegram_topic_root_lobby_message()
            return None

        # ── External-drain new-turn gate (Phase 2) ────────────────────
        # When NAS has engaged an external drain (.drain_request.json present,
        # observed by _drain_control_watcher), refuse to START a new turn so
        # the in-flight set can only fall to zero — eliminating the TOCTOU race
        # (D4a: stop accepting new turns FIRST, then NAS polls until
        # active_agents==0). In-flight turns are untouched; this only blocks the
        # claim of a NEW session slot. Internal/system events (restart-recovery
        # replays, background-process completions) bypass the gate — they are
        # not user-initiated new work and must still flow during a drain.
        # Reversible: once the marker is removed the gate opens again.
        if self._external_drain_active and not is_internal:
            logger.info(
                "Refusing new turn for session %s — external drain active.",
                _quick_key,
            )
            return (
                "⏳ This agent is draining for a maintenance action and isn't "
                "accepting new turns right now. It'll be back in a moment — "
                "please resend shortly."
            )

        # ── Claim this session before any await ───────────────────────
        # Between here and _run_agent registering the real AIAgent, there
        # are numerous await points (hooks, vision enrichment, STT,
        # session hygiene compression).  Without this sentinel a second
        # message arriving during any of those yields would pass the
        # "already running" guard and spin up a duplicate agent for the
        # same session — corrupting the transcript.
        _active_session_lease, _limit_message = self._claim_active_session_slot(
            _quick_key,
            source,
        )
        if _limit_message is not None:
            logger.info(
                "Rejecting new active session %s: max_concurrent_sessions reached",
                _quick_key,
            )
            return _limit_message
        _claim_state = self._session_state(_quick_key)
        if _active_session_lease is not None:
            _claim_state.turn.lease = _active_session_lease
        _claim_state.turn.agent = _AGENT_PENDING_SENTINEL
        _claim_state.turn.started_ts = time.time()
        self._persist_active_agents()
        _run_generation = self._begin_session_run_generation(_quick_key)

        try:
            _agent_result = await self._handle_message_with_agent(event, source, _quick_key, _run_generation)
            # Goal continuation: after the agent returns a final response
            # for this turn, check any standing /goal — the judge will
            # either mark it done, pause it (budget), or enqueue a
            # continuation prompt back through the adapter FIFO so the
            # next turn makes more progress. Wrapped in try/except so a
            # broken judge never breaks normal message handling.
            try:
                _final_text = ""
                if isinstance(_agent_result, dict):
                    _final_text = str(_agent_result.get("final_response") or "")
                elif isinstance(_agent_result, str):
                    _final_text = _agent_result
                # Skip for empty responses (interrupted / errored) — the
                # judge would almost always say "continue" and we'd loop
                # on error. Let the user drive the next turn.
                if _final_text.strip():
                    try:
                        session_entry = await self.async_session_store.get_or_create_session(source)
                    except Exception:
                        session_entry = None
                    if session_entry is not None:
                        await self._post_turn_goal_continuation(
                            session_entry=session_entry,
                            source=source,
                            final_response=_final_text,
                        )
            except Exception as _goal_exc:
                logger.debug("goal continuation hook failed: %s", _goal_exc)
            return _agent_result
        finally:
            # MoA one-shot restore must run on EVERY exit path, not just
            # success. The restore data lives on the per-turn event object
            # (_moa_restore_override), which is discarded once the event goes
            # out of scope — so if _handle_message_with_agent raises, a restore
            # in the try block would be skipped and the MoA override would leak
            # permanently (every later message silently fans out through MoA).
            # Putting it in finally guarantees the revert on success, exception,
            # and interrupt alike.
            self._restore_moa_one_shot(event, _quick_key)
            self._restore_pending_one_turn_model_override(_quick_key)
            # Unconditional release covers every exit path. _release_running_agent_state
            # is idempotent (pop-on-absent is harmless) and, called without a
            # run_generation guard, always clears the slot regardless of which
            # generation it holds. This evicts the zombie left when session_reset
            # bumps the generation (N -> N+1) mid-flight: gen-N's guarded release
            # inside _run_agent returns False, and the old sentinel-only check here
            # missed the leftover real agent — locking the session out forever (#28686).
            self._release_running_agent_state(_quick_key)
            # Turn lease (#64934): release THIS turn's lease token — keyed by
            # (routing key, run generation) so this unwind can only ever free
            # the lease its own turn acquired, never a newer turn's.
            self._release_turn_lease(_quick_key, _run_generation)


    async def _handle_message_with_agent(self, event, source, _quick_key: str, run_generation: int):
        """Inner handler that runs under the _running_agents sentinel guard."""
        from gateway.run import (
            _GATEWAY_HYGIENE_PLATFORM,
            _drain_gateway_watch_events,
            _float_env,
            _format_gateway_process_notification,
            _hermes_home,
            _home_target_env_var,
            _is_gateway_hidden_reasoning_incomplete_turn,
            _load_gateway_config,
            _message_timestamps_enabled,
            _normalize_empty_agent_response,
            _platform_config_key,
            _record_hygiene_cooldown,
            _resolve_gateway_display_bool,
            _resolve_gateway_model,
            _sanitize_gateway_final_response,
            _seed_hygiene_system_prompt,
            _should_clear_resume_pending_after_turn,
            _stamp_hygiene_compression_provenance
        )

        _msg_start_time = time.time()
        _platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        _msg_preview = (event.text or "")[:80].replace("\n", " ")
        _reply_id = getattr(event, "reply_to_message_id", None)
        _reply_txt = (getattr(event, "reply_to_text", None) or "")[:80].replace("\n", " ")
        logger.info(
            "inbound message: platform=%s user=%s chat=%s msg=%r reply_to_id=%s reply_to_text=%r",
            _platform_name, source.user_name or source.user_id or "unknown",
            source.chat_id or "unknown", _msg_preview, _reply_id, _reply_txt,
        )

        # Get or create session
        # Topic-mode DMs: rewrite a stale/foreign thread_id to the user's
        # last-active topic so a cross-topic Reply or stripped plain reply
        # doesn't fragment the conversation across sessions.
        recovered = await asyncio.to_thread(self._recover_telegram_topic_thread_id, source)
        if recovered is not None:
            logger.info(
                "telegram topic recovery: chat=%s user=%s %r -> %s",
                source.chat_id, source.user_id, source.thread_id, recovered,
            )
            source = dataclasses.replace(source, thread_id=recovered)
            try:
                event.source = source
            except Exception:
                pass

        session_entry = await self.async_session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        pinned_session_id = str(
            (getattr(event, "metadata", None) or {}).get("gateway_session_id") or ""
        ).strip()
        if pinned_session_id:
            resolved_entry = await self._resolve_async_delegation_session(
                session_entry,
                pinned_session_id,
            )
            if resolved_entry is None:
                return
            session_entry = resolved_entry
        self._cache_session_source(session_key, source)
        if await asyncio.to_thread(self._is_telegram_topic_lane, source):
            try:
                binding = (await self._session_db.get_telegram_topic_binding(
                    chat_id=str(source.chat_id),
                    thread_id=str(source.thread_id),
                )) if self._session_db else None
            except Exception:
                logger.debug("Failed to read Telegram topic binding", exc_info=True)
                binding = None
            if binding:
                bound_session_id = str(binding.get("session_id") or "")
                # Heal bindings that point at a pre-compression parent: walk
                # the compression-continuation chain forward to its tip so the
                # next message resumes the compressed child instead of
                # reloading the oversized parent transcript (#20470/#29712/
                # #33414). Returns the input unchanged when the session isn't
                # a compression parent, so this is cheap and safe.
                if bound_session_id and self._session_db is not None:
                    try:
                        canonical_session_id = await self._session_db.get_compression_tip(
                            bound_session_id,
                        )
                    except Exception:
                        logger.debug(
                            "compression-tip lookup failed for %s",
                            bound_session_id, exc_info=True,
                        )
                        canonical_session_id = bound_session_id
                    if (
                        canonical_session_id
                        and canonical_session_id != bound_session_id
                    ):
                        bound_session_id = canonical_session_id
                if bound_session_id and bound_session_id != session_entry.session_id:
                    # Route the override through SessionStore so the session_key
                    # → session_id mapping is persisted to disk and the previous
                    # lane session is ended cleanly. Mutating session_entry in
                    # place here created a split-brain state where the JSON
                    # index pointed at one id but code downstream used another.
                    switched = await self.async_session_store.switch_session(session_key, bound_session_id)
                    if switched is not None:
                        session_entry = switched
                # If the stored binding pointed at a parent, rewrite it to the
                # canonical descendant now that we've followed the chain.
                if (
                    bound_session_id
                    and bound_session_id != str(binding.get("session_id") or "")
                ):
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="compression-tip-walk",
                    )
            else:
                try:
                    await asyncio.to_thread(self._record_telegram_topic_binding, source, session_entry)
                except Exception:
                    logger.debug("Failed to record Telegram topic binding", exc_info=True)
        # Capture and immediately consume was_auto_reset so it does not
        # re-fire on subsequent messages — preventing the cleanup from
        # wiping model/reasoning overrides set between turns (Closes #48031).
        _was_auto_reset = getattr(session_entry, "was_auto_reset", False)
        if _was_auto_reset:
            # Treat auto-reset as a full conversation boundary — clear every
            # conversation-scoped per-session dict in one funnel call so the
            # fresh session does not inherit the previous conversation's
            # model/reasoning overrides, a queued "/model switched" note, or
            # a stale resolved-model cache (#48031, #58403). See
            # _CONVERSATION_SCOPED_STATE.
            self._clear_conversation_scope(session_key, reason="auto_reset")
            # Evict the cached agent so the fresh session does not inherit the
            # previous conversation's context_compressor._previous_summary —
            # the cache is keyed on the stable session_key, so an auto-reset
            # otherwise reuses the old agent and leaks prior history into new
            # compaction summaries. Mirrors /reset and the compression-exhausted
            # path (#9893). Covers daily/idle/suspended auto-reset.
            self._evict_cached_agent(session_key)
            session_entry.was_auto_reset = False
        
        # Emit session:start for new or auto-reset sessions
        _is_new_session = (
            session_entry.created_at == session_entry.updated_at
            or _was_auto_reset
            or getattr(session_entry, "is_fresh_reset", False)
        )
        # Consume the is_fresh_reset flag immediately so it doesn't leak
        # onto subsequent messages in the same session (issue #6508).
        if getattr(session_entry, "is_fresh_reset", False):
            session_entry.is_fresh_reset = False
        if _is_new_session:
            await self.hooks.emit("session:start", {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "session_id": session_entry.session_id,
                "session_key": session_key,
            })
        
        # Build session context
        context = build_session_context(source, self.config, session_entry)
        
        # Set session context variables for tools (task-local, concurrency-safe)
        _session_env_tokens = self._set_session_env(context)
        
        # Read privacy.redact_pii from config (re-read per message)
        _redact_pii = False
        persist_user_message = None
        persist_user_timestamp = None
        try:
            _pcfg = _load_gateway_config()
            _redact_pii = bool((_pcfg.get("privacy") or {}).get("redact_pii", False))
        except Exception:
            pass

        # Build the context prompt to inject.  The render is pinned per
        # session, keyed by a hash of the exact renderer inputs
        # (_ephemeral_change_key).  A key hit reuses the pinned bytes verbatim
        # so the composed system prompt cannot drift turn-over-turn; a key
        # miss (thread rename, /sethome, redact_pii flip, ...) re-renders
        # once — the only legitimate cache busts.
        context_prompt = self._pinned_session_context_prompt(
            context, _redact_pii, session_key
        )

        # Per-turn must-deliver notes.  These used to be appended to
        # context_prompt (the ephemeral system prompt), which guaranteed a
        # turn1→turn2 system-prompt diff and a full agent rebuild.  They now
        # ride the current user message via the api_content sidecar instead
        # (staged below, consumed in run_sync → build_turn_context).
        turn_sidecar_notes: List[str] = []

        # If the previous session expired and was auto-reset, deliver a notice
        # so the agent knows this is a fresh conversation (not an intentional /reset).
        if _was_auto_reset:
            reset_reason = getattr(session_entry, 'auto_reset_reason', None) or 'idle'
            if reset_reason == "suspended":
                context_note = "[System note: The user's previous session was stopped and suspended. This is a fresh conversation with no prior context.]"
            elif reset_reason == "daily":
                context_note = "[System note: The user's session was automatically reset by the daily schedule. This is a fresh conversation with no prior context.]"
            elif reset_reason == "resume_pending_expired":
                context_note = "[System note: The previous gateway session could not be recovered after a restart (API recovery timed out). This is a fresh conversation — use /resume to restore history if needed.]"
            else:
                context_note = "[System note: The user's previous session expired due to inactivity. This is a fresh conversation with no prior context.]"
            # Slack/Discord channels/threads are long-lived: point the agent at
            # the specific prior same-channel session so it recalls that context
            # via session_search instead of an unrelated recent session.  Returns
            # None (appends nothing) for other platforms or when there's no prior
            # activity to recall.  Deterministic — no extra API/DB calls (#36220).
            try:
                continuity_note = build_channel_continuity_note(session_entry, source)
            except Exception:
                continuity_note = None
            if continuity_note:
                context_note = context_note + "\n\n" + continuity_note
            turn_sidecar_notes.append(context_note)

            # Send a user-facing notification explaining the reset, unless:
            # - notifications are disabled in config
            # - the platform is excluded (e.g. api_server, webhook)
            # - the expired session had no activity (nothing was cleared)
            try:
                policy = self.session_store.config.get_reset_policy(
                    platform=source.platform,
                    session_type=getattr(source, 'chat_type', 'dm'),
                )
                platform_name = source.platform.value if source.platform else ""
                had_activity = getattr(session_entry, 'reset_had_activity', False)
                # Suspended and restart-recovery-expired sessions always notify
                # regardless of policy.notify — the user had an active session
                # that was silently replaced, so they need to know they can
                # /resume it.  Idle/daily resets respect the policy flag.
                should_notify = reset_reason in {"suspended", "resume_pending_expired"} or (
                    policy.notify
                    and had_activity
                    and platform_name not in policy.notify_exclude_platforms
                )
                if should_notify:
                    adapter = self._adapter_for_source(source)
                    if adapter:
                        if reset_reason == "suspended":
                            reason_text = "previous session was stopped or interrupted"
                        elif reset_reason == "resume_pending_expired":
                            reason_text = "gateway restart recovery timed out"
                        elif reset_reason == "daily":
                            reason_text = f"daily schedule at {policy.at_hour}:00"
                        else:
                            hours = policy.idle_minutes // 60
                            mins = policy.idle_minutes % 60
                            duration = f"{hours}h" if not mins else f"{hours}h {mins}m" if hours else f"{mins}m"
                            reason_text = f"inactive for {duration}"
                        notice = (
                            f"◐ Session automatically reset ({reason_text}). "
                            f"Conversation history cleared.\n"
                            f"Use /resume to browse and restore a previous session.\n"
                            f"Adjust reset timing in config.yaml under session_reset."
                        )
                        try:
                            session_info = await asyncio.to_thread(
                                self._reset_notice_session_info, source
                            )
                            if session_info:
                                notice = f"{notice}\n\n{session_info}"
                        except Exception:
                            pass
                        await adapter.send(
                            source.chat_id, notice,
                            metadata=self._thread_metadata_for_source(source),
                        )
            except Exception as e:
                logger.debug("Auto-reset notification failed (non-fatal): %s", e)

            # was_auto_reset is already consumed in the cleanup block above
            # (single source of truth); only the reset reason needs clearing here.
            session_entry.auto_reset_reason = None

        # Auto-load skill(s) for topic/channel bindings (Telegram DM Topics,
        # Discord channel_skill_bindings).  Supports a single name or ordered list.
        # Only inject on NEW sessions — ongoing conversations already have the
        # skill content in their conversation history from the first message.
        _auto = getattr(event, "auto_skill", None)
        if _is_new_session and _auto:
            _skill_names = [_auto] if isinstance(_auto, str) else list(_auto)
            try:
                from agent.skill_commands import _load_skill_payload, _build_skill_message
                _combined_parts: list[str] = []
                _loaded_names: list[str] = []
                for _sname in _skill_names:
                    _loaded = _load_skill_payload(_sname, task_id=_quick_key)
                    if _loaded:
                        _loaded_skill, _skill_dir, _display_name = _loaded
                        _note = (
                            f'[IMPORTANT: The "{_display_name}" skill is auto-loaded. '
                            f"Follow its instructions for this session.]"
                        )
                        _part = _build_skill_message(_loaded_skill, _skill_dir, _note)
                        if _part:
                            _combined_parts.append(_part)
                            _loaded_names.append(_sname)
                    else:
                        logger.warning("[Gateway] Auto-skill '%s' not found", _sname)
                if _combined_parts:
                    # Append the user's original text after all skill payloads
                    _combined_parts.append(event.text)
                    event.text = "\n\n".join(_combined_parts)
                    logger.info(
                        "[Gateway] Auto-loaded skill(s) %s for session %s",
                        _loaded_names, session_key,
                    )
            except Exception as e:
                logger.warning("[Gateway] Failed to auto-load skill(s) %s: %s", _skill_names, e)

        # ── Turn lease (#64934) ────────────────────────────────────────
        # Session resolution is FINAL here (get_or_create → async-delegation
        # pinning → topic tip-walk switch_session are all above). Serialize
        # the [load history → run → flush] region per resolved SESSION_ID:
        # when a second routing key is mapped to this same session_id, its
        # turn waits here for the previous turn's flush instead of loading a
        # stale history base and interleaving transcript writes. Same-key
        # messages never reach this point mid-turn (adapter + runner guards
        # hold them), so the lock is uncontended outside the alias-key route.
        # Fail-open: on timeout the token comes back degraded and the turn
        # proceeds unserialized (never a wedged session). Released in
        # _handle_message's finally via _release_turn_lease — granted per
        # (routing key, run generation) so a stale unwind can't release a
        # newer turn's lease.
        _lease_registry = getattr(self, "_turn_leases", None)
        if _lease_registry is not None:
            _lease_token = await _lease_registry.acquire(
                session_entry.session_id,
                owner_key=_quick_key,
                generation=run_generation,
                timeout=_float_env("HERMES_AGENT_TIMEOUT", 1800),
            )
            if _lease_token is not None:
                _lease_state = self._session_state(_quick_key).turn
                _lease_state.lease_token = _lease_token
                _lease_state.lease_generation = run_generation

        # Load conversation history from transcript
        history = await self.async_session_store.load_transcript(session_entry.session_id)
        
        # -----------------------------------------------------------------
        # Session hygiene: auto-compress pathologically large transcripts
        #
        # Long-lived gateway sessions can accumulate enough history that
        # every new message rehydrates an oversized transcript, causing
        # repeated truncation/context failures.  Detect this early and
        # compress proactively — before the agent even starts.  (#628)
        #
        # Token source priority:
        # 1. Actual API-reported prompt_tokens from the last turn
        #    (stored in session_entry.last_prompt_tokens)
        # 2. Rough char-based estimate (str(msg)//4). Overestimates
        #    by 30-50% on code/JSON-heavy sessions, but that just
        #    means hygiene fires a bit early — safe and harmless.
        # -----------------------------------------------------------------
        if history and len(history) >= 4:
            from agent.model_metadata import (
                estimate_messages_tokens_rough,
                get_model_context_length_async,
            )

            # Read model + compression config from config.yaml.
            # NOTE: hygiene threshold is intentionally HIGHER than the agent's
            # own compressor (0.85 vs 0.50).  Hygiene is a safety net for
            # sessions that grew too large between turns — it fires pre-agent
            # to prevent API failures.  The agent's own compressor handles
            # normal context management during its tool loop with accurate
            # real token counts.  Having hygiene at 0.50 caused premature
            # compression on every turn in long gateway sessions.
            _hyg_model = "anthropic/claude-sonnet-4.6"
            _hyg_threshold_pct = 0.85
            _hyg_compression_enabled = True
            _hyg_hard_msg_limit = 5000
            _hyg_timeout_seconds = 30.0
            _hyg_total_ceiling_seconds = 600.0
            _hyg_failure_cooldown_seconds = 300.0
            _hyg_config_context_length = None
            _hyg_provider = None
            _hyg_base_url = None
            _hyg_api_key = None
            _hyg_configured_model = None
            _hyg_configured_provider = None
            _hyg_configured_base_url = None
            _hyg_data = {}
            try:
                _hyg_data = _load_gateway_config()
                if _hyg_data:
                    # Resolve model name (same logic as run_sync)
                    _model_cfg = _hyg_data.get("model", {})
                    if isinstance(_model_cfg, str):
                        _hyg_model = _model_cfg
                    elif isinstance(_model_cfg, dict):
                        _hyg_model = _model_cfg.get("default") or _model_cfg.get("model") or _hyg_model
                        # Read explicit context_length override from model config
                        # (same as run_agent.py lines 995-1005)
                        _raw_ctx = _model_cfg.get("context_length")
                        if _raw_ctx is not None:
                            try:
                                _hyg_config_context_length = int(_raw_ctx)
                            except (TypeError, ValueError):
                                pass
                        # Read provider for accurate context detection
                        _hyg_provider = _model_cfg.get("provider") or None
                        _hyg_base_url = _model_cfg.get("base_url") or None

                    # Read compression settings — only use enabled flag.
                    # The threshold is intentionally separate from the agent's
                    # compression.threshold (hygiene runs higher).
                    _comp_cfg = _hyg_data.get("compression", {})
                    if isinstance(_comp_cfg, dict):
                        _hyg_compression_enabled = str(
                            _comp_cfg.get("enabled", True)
                        ).lower() in {"true", "1", "yes"}
                        _raw_hard_limit = _comp_cfg.get("hygiene_hard_message_limit")
                        if _raw_hard_limit is not None:
                            try:
                                _parsed = int(_raw_hard_limit)
                                if _parsed > 0:
                                    _hyg_hard_msg_limit = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_timeout = _comp_cfg.get("hygiene_timeout_seconds")
                        if _raw_timeout is not None:
                            try:
                                _parsed = float(_raw_timeout)
                                if _parsed > 0:
                                    _hyg_timeout_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        _raw_ceiling = _comp_cfg.get("hygiene_total_ceiling_seconds")
                        if _raw_ceiling is not None:
                            try:
                                _parsed = float(_raw_ceiling)
                                if _parsed > 0:
                                    _hyg_total_ceiling_seconds = _parsed
                            except (TypeError, ValueError):
                                pass
                        # The ceiling can never be tighter than one idle
                        # window, or the extension loop would be dead code.
                        _hyg_total_ceiling_seconds = max(
                            _hyg_total_ceiling_seconds, _hyg_timeout_seconds,
                        )
                        _raw_cooldown = _comp_cfg.get("hygiene_failure_cooldown_seconds")
                        if _raw_cooldown is not None:
                            try:
                                _parsed = float(_raw_cooldown)
                                if _parsed >= 0:
                                    _hyg_failure_cooldown_seconds = _parsed
                            except (TypeError, ValueError):
                                pass

                _hyg_configured_model = _hyg_model
                _hyg_configured_provider = _hyg_provider
                _hyg_configured_base_url = _hyg_base_url

                try:
                    _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(
                        source=source,
                        session_key=session_key,
                        user_config=_hyg_data if isinstance(_hyg_data, dict) else None,
                    )
                    _hyg_provider = _hyg_runtime.get("provider") or _hyg_provider
                    _hyg_base_url = _hyg_runtime.get("base_url") or _hyg_base_url
                    _hyg_api_key = _hyg_runtime.get("api_key") or _hyg_api_key
                except Exception:
                    pass

                if _hyg_config_context_length is not None:
                    try:
                        from hermes_cli.route_identity import should_clear_context_pin_async

                        if await should_clear_context_pin_async(
                            _hyg_configured_model,
                            _hyg_model,
                            _hyg_configured_base_url,
                            _hyg_base_url,
                            _hyg_configured_provider,
                            _hyg_provider,
                        ):
                            _hyg_config_context_length = None
                    except Exception:
                        _hyg_config_context_length = None

                # Check custom_providers per-model context_length
                # (same fallback as run_agent.py lines 1171-1189).
                # Must run after runtime resolution so _hyg_base_url is set.
                if _hyg_config_context_length is None and _hyg_base_url:
                    try:
                        try:
                            from hermes_cli.config import (
                                get_compatible_custom_providers as _gw_gcp,
                                get_custom_provider_context_length as _gw_gccl,
                            )
                            _hyg_custom_providers = _gw_gcp(_hyg_data)
                        except Exception:
                            _hyg_custom_providers = _hyg_data.get("custom_providers")
                            if not isinstance(_hyg_custom_providers, list):
                                _hyg_custom_providers = []
                        _hyg_custom_ctx = _gw_gccl(
                            model=_hyg_model,
                            base_url=_hyg_base_url,
                            custom_providers=_hyg_custom_providers,
                        )
                        if _hyg_custom_ctx:
                            _hyg_config_context_length = int(_hyg_custom_ctx)
                    except (TypeError, ValueError):
                        pass
            except Exception:
                pass

            if _hyg_compression_enabled:
                _hyg_context_length = await get_model_context_length_async(
                    _hyg_model,
                    base_url=_hyg_base_url or "",
                    api_key=_hyg_api_key or "",
                    config_context_length=_hyg_config_context_length,
                    provider=_hyg_provider or "",
                )
                _compress_token_threshold = int(
                    _hyg_context_length * _hyg_threshold_pct
                )
                _warn_token_threshold = int(_hyg_context_length * 0.95)

                _msg_count = len(history)

                # Prefer actual API-reported tokens from the last turn
                # (stored in session entry) over the rough char-based estimate.
                _stored_tokens = session_entry.last_prompt_tokens
                if _stored_tokens > 0:
                    _approx_tokens = _stored_tokens
                    _token_source = "actual"
                else:
                    _approx_tokens = estimate_messages_tokens_rough(history)
                    _token_source = "estimated"
                    # Note: rough estimates overestimate by 30-50% for code/JSON-heavy
                    # sessions, but that just means hygiene fires a bit early — which
                    # is safe and harmless.  The 85% threshold already provides ample
                    # headroom (agent's own compressor runs at 50%).  A previous 1.4x
                    # multiplier tried to compensate by inflating the threshold, but
                    # 85% * 1.4 = 119% of context — which exceeds the model's limit
                    # and prevented hygiene from ever firing for ~200K models (GLM-5).

                # Hard safety valve: force compression if message count is
                # extreme, regardless of token estimates.  This breaks the
                # death spiral where API disconnects prevent token data
                # collection, which prevents compression, which causes more
                # disconnects.  5000 messages is far above any normal session
                # but catches truly runaway growth before it becomes
                # unrecoverable.  Set well clear of legitimate large-context
                # (1M+) sessions doing thousands of short turns — those
                # compress on the token threshold, not this count-based floor.
                # Threshold is configurable via
                # compression.hygiene_hard_message_limit.
                # (#2153)
                _HARD_MSG_LIMIT = _hyg_hard_msg_limit
                _needs_compress = (
                    _approx_tokens >= _compress_token_threshold
                    or _msg_count >= _HARD_MSG_LIMIT
                )

                if _needs_compress:
                    # Use the persistent DB-backed cooldown (same as the
                    # in-conversation compression path in context_compressor.py)
                    # so the cooldown survives gateway restarts. The in-memory
                    # dict was reset on every restart, re-triggering the same
                    # failing compression and wedging session storage (#74136).
                    _session_db = getattr(self, "_session_db", None)
                    if _session_db is not None:
                        _session_db = getattr(_session_db, "_db", _session_db)
                        _getter = getattr(_session_db, "get_compression_failure_cooldown", None)
                        if _getter is not None:
                            try:
                                _cooldown_state = _getter(session_entry.session_id)
                            except Exception:
                                _cooldown_state = None
                            if _cooldown_state and _cooldown_state.get("remaining_seconds", 0) > 0:
                                logger.info(
                                    "Session hygiene: skipping compression for %s; "
                                    "previous failure cooldown active for %.1fs",
                                    session_entry.session_id,
                                    _cooldown_state["remaining_seconds"],
                                )
                                _needs_compress = False

                if _needs_compress:
                    logger.info(
                        "Session hygiene: %s messages, ~%s tokens (%s) — auto-compressing "
                        "(threshold: %s%% of %s = %s tokens)",
                        _msg_count, f"{_approx_tokens:,}", _token_source,
                        int(_hyg_threshold_pct * 100),
                        f"{_hyg_context_length:,}",
                        f"{_compress_token_threshold:,}",
                    )

                    _hyg_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))

                    try:
                        from agent.conversation_compression import CompressionCommitFence
                        from run_agent import AIAgent

                        _hyg_model, _hyg_runtime = self._resolve_session_agent_runtime(
                            source=source,
                            session_key=session_key,
                            user_config=_hyg_data if isinstance(_hyg_data, dict) else None,
                        )
                        if _hyg_runtime.get("api_key"):
                            # Pass the FULL transcript (tool results included).
                            # Filtering to user/assistant-only starved the
                            # compressor: tool results are usually the bulk of
                            # the context, _prune_old_tool_results never saw
                            # them, and short filtered histories tripped the
                            # protect-first/last early-return so nothing was
                            # compressed at all (#3854). The agent loop passes
                            # its full message list to _compress_context — the
                            # gateway now matches.
                            _hyg_msgs = [
                                m for m in history
                                if m.get("role") in {"user", "assistant", "tool"}
                            ]

                            if len(_hyg_msgs) >= 4:
                                try:
                                    _hyg_session_row = await self._session_db.get_session(
                                        session_entry.session_id
                                    )
                                except Exception as exc:
                                    _hyg_session_row = None
                                    logger.warning(
                                        "Session hygiene could not restore the system "
                                        "prompt for session %s: %s. Preserving an empty "
                                        "prompt so the live turn rebuilds it with its "
                                        "configured providers.",
                                        session_entry.session_id,
                                        exc,
                                        exc_info=True,
                                    )
                                _hyg_session_db = getattr(self._session_db, "_db", self._session_db)
                                _hyg_agent = AIAgent(
                                    **_hyg_runtime,
                                    model=_hyg_model,
                                    max_iterations=4,
                                    quiet_mode=True,
                                    skip_memory=True,
                                    enabled_toolsets=["memory"],
                                    session_id=session_entry.session_id,
                                    session_db=_hyg_session_db,
                                )
                                _seed_hygiene_system_prompt(
                                    _hyg_agent,
                                    _hyg_session_row,
                                )
                                # If compression must rebuild instead of retaining
                                # the cached prompt, make the persisted result
                                # deliberately stale for every real gateway surface.
                                _hyg_agent.platform = _GATEWAY_HYGIENE_PLATFORM
                                _hyg_cleanup_deferred = False
                                try:
                                    # Gateway hygiene runs before the user turn
                                    # starts and already owns the session binding.
                                    # Prefer in-place compaction here: it archives
                                    # old rows under the same session id instead of
                                    # minting a continuation child that then has to
                                    # be published back to SessionStore/topic
                                    # bindings.  If no SessionDB is available,
                                    # compress_context leaves this flag false and
                                    # the guard below preserves the transcript.
                                    _hyg_agent.compression_in_place = True
                                    _bind_hyg_state = getattr(
                                        getattr(_hyg_agent, "context_compressor", None),
                                        "bind_session_state",
                                        None,
                                    )
                                    if callable(_bind_hyg_state):
                                        _bind_hyg_state(
                                            _hyg_session_db,
                                            session_entry.session_id,
                                        )
                                    # It must never finalize on close() — close()
                                    # would end the live gateway session row.
                                    _hyg_agent._end_session_on_close = False
                                    _hyg_agent._print_fn = lambda *a, **kw: None

                                    loop = asyncio.get_running_loop()
                                    _hyg_commit_fence = CompressionCommitFence()
                                    _hyg_future = loop.run_in_executor(
                                        None,
                                        lambda: _hyg_agent._compress_context(
                                            _hyg_msgs, "",
                                            approx_tokens=_approx_tokens,
                                            commit_fence=_hyg_commit_fence,
                                        ),
                                    )
                                    try:
                                        # Progress-aware wait: the timeout is an
                                        # INACTIVITY budget, not a total one. The
                                        # compression worker streams its summary
                                        # call and ticks the fence per token
                                        # (CompressionCommitFence.touch_progress),
                                        # so a slow reasoning model that is still
                                        # generating keeps extending the deadline;
                                        # only a genuinely silent worker times out.
                                        # A hard ceiling bounds the total wait so
                                        # a degenerate trickle stream can't hold
                                        # the turn forever.
                                        _hyg_wait_started = time.monotonic()
                                        while True:
                                            # #76354 S3: charge the idle budget
                                            # from the LAST PROGRESS event, not
                                            # from the start of this wait slice —
                                            # otherwise silence can approach 2x
                                            # the configured timeout.
                                            _slice = max(
                                                _hyg_timeout_seconds
                                                - _hyg_commit_fence.seconds_since_progress(),
                                                0.005,
                                            )
                                            try:
                                                _compressed, _ = await asyncio.wait_for(
                                                    asyncio.shield(_hyg_future),
                                                    timeout=_slice,
                                                )
                                                break
                                            except asyncio.TimeoutError:
                                                _hyg_waited = time.monotonic() - _hyg_wait_started
                                                _idle = _hyg_commit_fence.seconds_since_progress()
                                                if (
                                                    _idle < _hyg_timeout_seconds
                                                    and _hyg_waited < _hyg_total_ceiling_seconds
                                                ):
                                                    logger.info(
                                                        "Session hygiene compression for "
                                                        "session %s still streaming after "
                                                        "%.0fs (last progress %.1fs ago) — "
                                                        "extending wait (ceiling %.0fs)",
                                                        session_entry.session_id,
                                                        _hyg_waited, _idle,
                                                        _hyg_total_ceiling_seconds,
                                                    )
                                                    continue
                                                raise
                                    except asyncio.TimeoutError:
                                        _cancelled = None
                                        while _cancelled is None:
                                            # #76354 F1: a hung commit retains the
                                            # fence lock; the lock-free phase
                                            # marker keeps this loop from spinning
                                            # forever while the commit blocks.
                                            if _hyg_commit_fence.commit_in_flight:
                                                _cancelled = False
                                                break
                                            _cancelled = (
                                                _hyg_commit_fence.try_cancel_before_commit()
                                            )
                                            if _cancelled is None:
                                                # Round-2 #5: transient
                                                # lock-setup windows ride
                                                # write patience for seconds;
                                                # 25ms keeps sub-tick latency
                                                # without 1kHz spin.
                                                await asyncio.sleep(0.025)
                                        if not _cancelled:
                                            # The worker crossed the commit boundary just
                                            # before the timeout. The fence poll waited for
                                            # that boundary to finish, so consume the
                                            # completed result instead of treating a
                                            # successful compaction as a timeout.
                                            _compressed, _ = await _hyg_future
                                        else:
                                            # #76354 F4: release the timed-out
                                            # worker's durable lease via the
                                            # holder-qualified hook so the next
                                            # compressor can acquire the lock
                                            # immediately (no ABA against a new
                                            # holder — release is holder-scoped).
                                            _hyg_commit_fence.release_cancelled_compression_lock()
                                            self._defer_agent_cleanup_until_future_done(
                                                _hyg_future,
                                                _hyg_agent,
                                                context="session hygiene timeout",
                                            )
                                            _hyg_cleanup_deferred = True
                                            if _hyg_failure_cooldown_seconds >= 0:
                                                _record_hygiene_cooldown(
                                                    self, session_entry.session_id,
                                                    _hyg_failure_cooldown_seconds,
                                                )
                                            from agent.session_activity import (
                                                ActivityProvenance,
                                            )
                                            _stamp_hygiene_compression_provenance(
                                                _hyg_agent,
                                                "session hygiene compression timed out",
                                                ActivityProvenance.AGENT_COMPRESSION_TIMEOUT,
                                                "hygiene compression timeout "
                                                "activity stamp failed",
                                            )
                                            logger.warning(
                                                "Session hygiene compression for session %s "
                                                "made no progress for %.1fs "
                                                "(total wait %.1fs, ceiling %.1fs); "
                                                "continuing without compression",
                                                session_entry.session_id,
                                                _hyg_commit_fence.seconds_since_progress(),
                                                time.monotonic() - _hyg_wait_started,
                                                _hyg_total_ceiling_seconds,
                                            )
                                            _timeout_msg = (
                                                "⚠️ Context compression timed out "
                                                f"after {_hyg_timeout_seconds:.1f}s "
                                                "with no output from the summary model. "
                                                "No messages were dropped — continuing without "
                                                "compression. Run /compress to retry, /reset for "
                                                "a clean session, or check your "
                                                "auxiliary.compression model configuration."
                                            )
                                            try:
                                                _adapter = self._adapter_for_source(source)
                                                if _adapter and source.chat_id:
                                                    await _adapter.send(
                                                        source.chat_id,
                                                        _timeout_msg,
                                                        metadata=_hyg_meta,
                                                    )
                                            except Exception as _werr:
                                                logger.warning(
                                                    "Failed to deliver compression-timeout "
                                                    "warning to user: %s",
                                                    _werr,
                                                )
                                            raise
                                    except BaseException:
                                        # #76354 F2: non-timeout unwind while the
                                        # detached hygiene worker may still run —
                                        # KeyboardInterrupt, task cancellation, or
                                        # any unexpected error. Revoke commit
                                        # admission (and release the worker's
                                        # durable lease via the holder-qualified
                                        # hook) BEFORE the host unwinds so the
                                        # worker can never commit later.
                                        _hyg_commit_fence.revoke_commit_admission()
                                        if not _hyg_cleanup_deferred:
                                            self._defer_agent_cleanup_until_future_done(
                                                _hyg_future,
                                                _hyg_agent,
                                                context="session hygiene unwind",
                                            )
                                            _hyg_cleanup_deferred = True
                                        raise

                                    # _compress_context ends the old session and creates
                                    # a new session_id.  Write compressed messages into
                                    # the NEW session so the old transcript stays intact
                                    # and searchable via session_search.
                                    _hyg_new_sid = _hyg_agent.session_id
                                    _hyg_rotated = _hyg_new_sid != session_entry.session_id
                                    _hyg_in_place = bool(
                                        getattr(_hyg_agent, "_last_compaction_in_place", False)
                                    )
                                    # Only rewrite the transcript when rotation produced
                                    # a NEW session id.  In-place compaction does NOT
                                    # need a rewrite: archive_and_compact() has already
                                    # soft-archived the previous active rows and inserted
                                    # the compacted messages as the new active set inside
                                    # _compress_context().  Calling rewrite_transcript()
                                    # after in-place compaction would invoke
                                    # replace_messages(active_only=False) which DELETEs
                                    # ALL rows — including the archived turns that
                                    # archive_and_compact() deliberately preserved
                                    # (silent data loss, #61145).
                                    #
                                    # The danger this guards against (mirrors the
                                    # /compress fix #44794/#39704): if _compress_context
                                    # returns a summary but neither rotates nor completes
                                    # archive_and_compact(), the session_id is unchanged
                                    # for a FAILURE reason, and an unconditional
                                    # rewrite_transcript() would DELETE the original
                                    # messages and replace them with only the compressed
                                    # summary (permanent data loss, #21301).
                                    #
                                    # Write-before-repoint (mirrors manual /compress):
                                    # if we repointed session_entry onto the child SID
                                    # and rewrite_transcript then failed (lock/ENOSPC),
                                    # the live entry would already reference a brand-new
                                    # empty session while the turn continues — the
                                    # conversation silently vanishes. Persist the child
                                    # transcript first; only then rebind the live entry.
                                    if _hyg_rotated:
                                        if not await self.async_session_store.rewrite_transcript(
                                            _hyg_new_sid, _compressed
                                        ):
                                            logger.error(
                                                "Session hygiene: failed to persist "
                                                "compressed transcript for rotated "
                                                "session %s → %s; keeping the live "
                                                "entry on the original session so the "
                                                "conversation is not dropped",
                                                session_entry.session_id,
                                                _hyg_new_sid,
                                            )
                                            # Fail closed: treat like no rotation.
                                            _hyg_rotated = False
                                            _hyg_in_place = False
                                        else:
                                            session_entry.session_id = _hyg_new_sid
                                            # The held turn lease follows the
                                            # rotation so an alias key resolving
                                            # the fresh child still serializes
                                            # against this turn (#64934).
                                            self._rebind_turn_lease(
                                                _quick_key, run_generation, _hyg_new_sid
                                            )
                                            await self.async_session_store._save()
                                            await asyncio.to_thread(
                                                self._sync_telegram_topic_binding,
                                                source, session_entry,
                                                reason="hygiene-compression",
                                            )

                                    if _hyg_rotated:
                                        # Reset stored token count — transcript rewritten
                                        session_entry.last_prompt_tokens = 0
                                        history = _compressed
                                        _new_count = len(_compressed)
                                        _new_tokens = estimate_messages_tokens_rough(
                                            _compressed
                                        )
                                    elif _hyg_in_place:
                                        # archive_and_compact() already persisted the
                                        # compacted transcript inside _compress_context.
                                        # Reset counts to match the new active set.
                                        session_entry.last_prompt_tokens = 0
                                        history = _compressed
                                        _new_count = len(_compressed)
                                        _new_tokens = estimate_messages_tokens_rough(
                                            _compressed
                                        )
                                    else:
                                        # No rewrite happened — transcript preserved
                                        # unchanged, so the post-compression counts equal
                                        # the pre-compression ones.
                                        _new_count = _msg_count
                                        _new_tokens = _approx_tokens
                                        logger.warning(
                                            "Gateway hygiene compression for session %s "
                                            "did not rotate or compact in place "
                                            "(no session_db on the hygiene agent) — "
                                            "preserving the original transcript instead "
                                            "of overwriting it with the summary (#21301).",
                                            session_entry.session_id,
                                        )

                                    logger.info(
                                        "Session hygiene: compressed %s → %s msgs, "
                                        "~%s → ~%s tokens",
                                        _msg_count, _new_count,
                                        f"{_approx_tokens:,}", f"{_new_tokens:,}",
                                    )

                                    if _new_tokens >= _warn_token_threshold:
                                        logger.warning(
                                            "Session hygiene: still ~%s tokens after "
                                            "compression",
                                            f"{_new_tokens:,}",
                                        )

                                    # If summary generation failed, the
                                    # compressor aborts entirely and returns
                                    # messages unchanged — nothing is dropped.
                                    # Surface a visible warning to the gateway
                                    # user — agent.log alone is invisible on
                                    # TG/Discord/etc. — so they know the chat
                                    # is "frozen" at the current size and can
                                    # /compress to retry or /reset to start
                                    # fresh.
                                    _comp = getattr(_hyg_agent, "context_compressor", None)
                                    if _comp is not None and getattr(_comp, "_last_compress_aborted", False):
                                        if _hyg_failure_cooldown_seconds >= 0:
                                            _record_hygiene_cooldown(
                                                self, session_entry.session_id,
                                                _hyg_failure_cooldown_seconds,
                                            )
                                        from agent.session_activity import (
                                            ActivityProvenance,
                                        )
                                        _stamp_hygiene_compression_provenance(
                                            _hyg_agent,
                                            "session hygiene compression aborted",
                                            ActivityProvenance.AGENT_COMPRESSION_COOLDOWN,
                                            "hygiene compression abort "
                                            "activity stamp failed",
                                        )
                                        _err = getattr(_comp, "_last_summary_error", None) or "unknown error"
                                        # Force-redact: provider exception text
                                        # may contain credentials; this message
                                        # reaches gateway users directly.
                                        from agent.redact import redact_sensitive_text
                                        _err = redact_sensitive_text(_err, force=True)
                                        _warn_msg = (
                                            "⚠️ Context compression aborted "
                                            f"({_err}). No messages were dropped — "
                                            "conversation is unchanged. Run /compress "
                                            "to retry, /reset for a clean session, or "
                                            "check your auxiliary.compression model "
                                            "configuration."
                                        )
                                        try:
                                            _adapter = self._adapter_for_source(source)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _warn_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning(
                                                "Failed to deliver compression-failure warning to user: %s",
                                                _werr,
                                            )
                                    # Separately: if the user's CONFIGURED aux
                                    # model failed and we recovered by falling
                                    # back to the main model, tell them — a
                                    # misconfigured auxiliary.compression.model
                                    # is something only they can fix, and
                                    # silent recovery would hide it.
                                    elif _comp is not None and getattr(_comp, "_last_aux_model_failure_model", None):
                                        _aux_model = getattr(_comp, "_last_aux_model_failure_model", "")
                                        _aux_err = getattr(_comp, "_last_aux_model_failure_error", None) or "unknown error"
                                        _aux_msg = (
                                            f"ℹ️ Configured compression model `{_aux_model}` "
                                            f"failed ({_aux_err}). Recovered using your main "
                                            "model — context is intact — but you may want to "
                                            "check `auxiliary.compression.model` in config.yaml."
                                        )
                                        try:
                                            _adapter = self._adapter_for_source(source)
                                            if _adapter and source.chat_id:
                                                await _adapter.send(source.chat_id, _aux_msg, metadata=_hyg_meta)
                                        except Exception as _werr:
                                            logger.warning(
                                                "Failed to deliver aux-model-fallback notice to user: %s",
                                                _werr,
                                            )
                                finally:
                                    # Evict the cached agent so the next turn
                                    # rebuilds its system prompt from current
                                    # SOUL.md, memory, and skills.
                                    self._evict_cached_agent(session_key)
                                    if not _hyg_cleanup_deferred:
                                        await self._cleanup_agent_resources_off_loop(
                                            _hyg_agent, context="session hygiene"
                                        )

                    except Exception as e:
                        logger.warning(
                            "Session hygiene auto-compress failed: %s", e
                        )

        # First-message onboarding -- only on the very first interaction ever.
        # Delivered on the current user message (sidecar), NOT the ephemeral
        # system prompt: present-on-turn-1/absent-on-turn-2 was a guaranteed
        # system-prompt diff and agent rebuild.
        if not history and not await self.async_session_store.has_any_sessions():
            # Default first-contact note: a brief self-introduction.
            _intro_note = (
                "[System note: This is the user's very first message ever. "
                "Briefly introduce yourself and mention that /help shows available commands. "
                "Keep the introduction concise -- one or two sentences max.]"
            )
            # Opt-in structured profile-build path. When enabled (default
            # "ask") and not yet offered on this install, swap the plain intro
            # for a consent-gated directive that offers to build a user
            # profile and persists confirmed facts via memory(target="user").
            # The offer fires at most once (onboarding.seen flag); set
            # onboarding.profile_build: off in config.yaml to disable.
            try:
                from agent.onboarding import (
                    PROFILE_BUILD_FLAG,
                    is_seen,
                    mark_seen,
                    profile_build_directive,
                    profile_build_mode,
                )
                _onb_cfg = _load_gateway_config()
                if (
                    profile_build_mode(_onb_cfg) == "ask"
                    and not is_seen(_onb_cfg, PROFILE_BUILD_FLAG)
                ):
                    turn_sidecar_notes.append(profile_build_directive().strip())
                    mark_seen(_hermes_home / "config.yaml", PROFILE_BUILD_FLAG)
                else:
                    turn_sidecar_notes.append(_intro_note)
            except Exception as _pb_err:
                logger.debug(
                    "Profile-build onboarding directive failed, using plain intro: %s",
                    _pb_err,
                )
                turn_sidecar_notes.append(_intro_note)
        
        # One-time prompt if no home channel is set for this platform
        # Skip for webhooks - they deliver directly to configured targets (github_comment, etc.)
        if not history and source.platform and source.platform != Platform.LOCAL and source.platform != Platform.WEBHOOK:
            platform_name = source.platform.value
            env_key = _home_target_env_var(platform_name)
            # Multiplex: home channel may live only in the profile secret
            # scope / PlatformConfig, not process os.environ.
            home_env = ""
            try:
                from agent.secret_scope import get_secret

                home_env = (get_secret(env_key) or "").strip() if env_key else ""
            except Exception:
                home_env = ""
            if not home_env:
                home_env = (os.getenv(env_key) or "").strip() if env_key else ""
            # Also honor in-memory / yaml home_channel on this platform.
            try:
                if not home_env and self.config.get_home_channel(source.platform):
                    home_env = "set"
            except Exception:
                pass
            # Secondary-profile platforms (e.g. Slack on yolo) may only exist
            # under that profile's loaded config — check after scope install.
            if not home_env:
                try:
                    from gateway.config import load_gateway_config as _lgc
                    prof = (getattr(source, "profile", None) or "").strip()
                    if prof and prof != "default":
                        # Already inside profile scope for secondary handlers;
                        # re-read live config for home_channel.
                        _pcfg = _lgc()
                        if _pcfg.get_home_channel(source.platform):
                            home_env = "set"
                except Exception:
                    pass
            if not home_env:
                # Slack dispatches all Hermes commands through a single
                # parent slash command `/hermes`; bare `/sethome` is not
                # registered and would fail with "app did not respond".
                sethome_cmd = (
                    "/hermes sethome"
                    if source.platform == Platform.SLACK
                    else "/sethome"
                )
                notice = (
                    f"📬 No home channel is set for {platform_name.title()}. "
                    f"A home channel is where Hermes delivers cron job results "
                    f"and cross-platform messages.\n\n"
                    f"Type {sethome_cmd} to make this chat your home channel, "
                    f"or ignore to skip."
                )
                await self._deliver_platform_notice(source, notice)
        
        # -----------------------------------------------------------------
        # Voice channel awareness — deliver current voice channel state so
        # the agent knows who is in the channel and who is speaking, without
        # needing a separate tool call.  Delivered on the current user
        # message and ONLY when it changed since the previous turn: the
        # member/speaking serialization differs essentially every turn, and
        # appending it to the ephemeral system prompt forced a full agent
        # rebuild + prompt-cache re-key per message.  The system prompt
        # carries a static pointer line instead (gateway/session.py).
        # -----------------------------------------------------------------
        _vc_note = self._voice_channel_sidecar_note(event, source, session_key)
        if _vc_note:
            turn_sidecar_notes.append(_vc_note)

        # -----------------------------------------------------------------
        # Auto-analyze images sent by the user
        #
        # If the user attached image(s), we run the vision tool eagerly so
        # the conversation model always receives a text description.  The
        # local file path is also included so the model can re-examine the
        # image later with a more targeted question via vision_analyze.
        #
        # We filter to image paths only (by media_type) so that non-image
        # attachments (documents, audio, etc.) are not sent to the vision
        # tool even when they appear in the same message.
        # -----------------------------------------------------------------
        message_text = await self._prepare_profile_scoped_inbound_message_text(
            event=event,
            source=source,
            history=history,
            session_key=session_key,
        )
        if message_text is None:
            return

        # Capture the platform event time as message metadata and keep the
        # persisted transcript clean (strip any leading timestamp prefix).
        # This runs regardless of the toggle so storage stays clean and the
        # send-time is preserved. Only the in-context RENDER (prepending the
        # human-readable prefix the model sees) is gated behind
        # gateway.message_timestamps.enabled — default OFF.
        try:
            from hermes_time import get_timezone as _get_evt_tz
            from gateway.message_timestamps import (
                coerce_message_timestamp as _coerce_msg_ts,
                render_user_content_with_timestamp as _render_msg_ts,
                strip_leading_message_timestamps as _strip_msg_ts,
            )
            _evt_tz = _get_evt_tz()
            _evt_ts = getattr(event, "timestamp", None)
            if message_text and isinstance(message_text, str):
                _clean_message_text, _embedded_ts = _strip_msg_ts(
                    message_text, tz=_evt_tz)
                persist_user_message = _clean_message_text
                _event_epoch = _coerce_msg_ts(_evt_ts, tz=_evt_tz)
                persist_user_timestamp = (
                    _event_epoch if _event_epoch is not None else _embedded_ts
                )
                if _message_timestamps_enabled(_load_gateway_config()):
                    message_text = _render_msg_ts(
                        _clean_message_text,
                        persist_user_timestamp,
                        tz=_evt_tz,
                    )
                else:
                    # Toggle off: model sees the clean message; the timestamp
                    # is still stored as metadata for later opt-in.
                    message_text = _clean_message_text
        except Exception as _ts_err:
            logger.debug("Message timestamp injection failed (non-fatal): %s", _ts_err)

        # Stage the collected must-deliver notes for this turn's agent run
        # (one-shot; consumed in run_sync).  Staged AFTER the message_text
        # early-out above so an aborted turn cannot leak its notes into the
        # next turn's user message.
        if turn_sidecar_notes and session_key:
            self._set_pending_turn_sidecar_notes(session_key, turn_sidecar_notes)

        # Bind this gateway run generation to the adapter's active-session
        # event so deferred post-delivery callbacks can be released by the
        # same run that registered them.
        self._bind_adapter_run_generation(
            self._adapter_for_source(source),
            session_key,
            run_generation,
        )

        try:
            # Emit agent:start hook
            hook_ctx = {
                "platform": source.platform.value if source.platform else "",
                "user_id": source.user_id,
                "chat_id": source.chat_id or "",
                "thread_id": str(getattr(source, "thread_id", None)) if getattr(source, "thread_id", None) else "",
                "chat_type": getattr(source, "chat_type", "") or "",
                "session_id": session_entry.session_id,
                "message": message_text[:500],
            }
            await self.hooks.emit("agent:start", hook_ctx)

            # Run the agent. Capture the session id that this run was launched
            # against so post-run compression publication can be identity-guarded
            # below; a /new or another lifecycle transition may move
            # session_entry.session_id while the old run is still unwinding.
            _run_start_session_id = session_entry.session_id
            _turn_started_monotonic = time.monotonic()
            agent_result = await self._run_agent(
                message=message_text,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=_run_start_session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=self._reply_anchor_for_event(event),
                channel_prompt=event.channel_prompt,
                moa_config=getattr(event, "_moa_config", None),
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
                message_type=event.message_type,
            )
            _turn_seconds = time.monotonic() - _turn_started_monotonic

            # Stop persistent typing indicator now that the agent is done.
            # Slack AI status is scoped to a thread/workspace, so preserve the
            # same routing metadata used by the response delivery path.
            try:
                _typing_adapter = self._adapter_for_source(source)
                _stop_with_metadata = getattr(
                    type(_typing_adapter), "_stop_typing_with_metadata", None
                )
                _stop_typing = getattr(type(_typing_adapter), "stop_typing", None)
                if _typing_adapter and callable(_stop_with_metadata):
                    await _typing_adapter._stop_typing_with_metadata(
                        source.chat_id,
                        self._thread_metadata_for_source(
                            source, self._reply_anchor_for_event(event)
                        ),
                    )
                elif _typing_adapter and callable(_stop_typing):
                    await _typing_adapter.stop_typing(source.chat_id)
            except Exception:
                pass

            if not self._is_session_run_current(_quick_key, run_generation):
                logger.info(
                    "Discarding stale agent result for %s — generation %d is no longer current",
                    _quick_key or "?",
                    run_generation,
                )
                _stale_adapter = self._adapter_for_source(source)
                if getattr(type(_stale_adapter), "pop_post_delivery_callback", None) is not None:
                    _stale_adapter.pop_post_delivery_callback(
                        _quick_key,
                        generation=run_generation,
                    )
                elif _stale_adapter and hasattr(_stale_adapter, "_post_delivery_callbacks"):
                    _stale_adapter._post_delivery_callbacks.pop(_quick_key, None)
                return None

            response = agent_result.get("final_response") or ""
            # Hidden-reasoning-only retry exhaustion: the loop's sentinel text
            # ("Codex response remained incomplete after 3 continuation
            # attempts") doubles as final_response, so it would be delivered
            # verbatim into the channel — where peer agents can ingest it as a
            # completed assistant turn (#51628). Blank it here so the normal
            # empty-response handling (and the suppression below) applies.
            if _is_gateway_hidden_reasoning_incomplete_turn(agent_result):
                response = ""
            try:
                from gateway.response_filters import is_intentional_silence_agent_result
                _intentional_silence = is_intentional_silence_agent_result(
                    agent_result, response,
                )
            except Exception:
                _intentional_silence = False

            # Convert the agent's internal "(empty)" sentinel into a
            # user-friendly message.  "(empty)" means the model failed to
            # produce visible content after exhausting all retries (nudge,
            # prefill, empty-retry, fallback).  Sending the raw sentinel
            # looks like a bug; a short explanation is more helpful.
            if response == "(empty)" and not _intentional_silence:
                response = (
                    "⚠️ The model returned no response after processing tool "
                    "results. This can happen with some models — try again or "
                    "rephrase your question."
                )
            agent_messages = agent_result.get("messages", [])
            _response_time = time.time() - _msg_start_time
            _api_calls = agent_result.get("api_calls", 0)
            _resp_len = len(response)
            logger.info(
                "response ready: platform=%s chat=%s time=%.1fs api_calls=%d response=%d chars",
                _platform_name, source.chat_id or "unknown",
                _response_time, _api_calls, _resp_len,
            )

            # NOTE: the cross-process cache-coherence re-baseline
            # (_refresh_agent_cache_message_count) is intentionally deferred
            # until AFTER this turn's transcript persistence block below — it
            # must include the first-turn `session_meta` marker row and the
            # compression session_id swap, both of which happen later.  See
            # the call site after the `update_session(...)` write.

            # Successful turn — clear any stuck-loop counter for this session.
            # This ensures the counter only accumulates across CONSECUTIVE
            # restarts where the session was active (never completed).
            #
            # Also clear the resume_pending flag (set by drain-timeout
            # shutdown) — the turn ran to completion, so recovery
            # succeeded and subsequent messages should no longer receive
            # the restart-interruption system note.
            if session_key and _should_clear_resume_pending_after_turn(agent_result):
                self._clear_restart_failure_count(session_key)
                try:
                    await self.async_session_store.clear_resume_pending(session_key)
                except Exception as _e:
                    logger.debug(
                        "clear_resume_pending failed for %s: %s",
                        session_key, _e,
                    )

            # Normalize empty responses: surface errors, partial failures, and
            # the case where agent did work but returned no text. Fix for #18765.
            if not _intentional_silence:
                response = _normalize_empty_agent_response(
                    agent_result, response, history_len=len(history),
                )
                response = _sanitize_gateway_final_response(source.platform, response)

            # Ordering contract: the agent thread already updated the contextvar
            # in conversation_compression.py; propagate to SessionEntry + _save().
            # If the agent's session_id changed during compression, update
            # session_entry so transcript writes below go to the right session.
            if agent_result.get("session_id") and agent_result["session_id"] != session_entry.session_id:
                if session_entry.session_id == _run_start_session_id:
                    session_entry.session_id = agent_result["session_id"]
                    # The held turn lease follows the rotation: the transcript
                    # persistence below writes to the NEW id, so the
                    # serialization boundary must move with it or an alias
                    # key resolving the fresh child could interleave (#64934).
                    self._rebind_turn_lease(
                        _quick_key, run_generation, session_entry.session_id
                    )
                    await self.async_session_store._save()
                    await self.async_session_store._record_gateway_session_peer(
                        session_entry.session_id,
                        session_key,
                        source,
                    )
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="agent-result-compression",
                    )
                else:
                    logger.info(
                        "Skipping agent-result session split sync for %s because "
                        "the session binding moved from %s to %s before "
                        "compression finished",
                        session_key or "?",
                        _run_start_session_id,
                        session_entry.session_id,
                    )

            # Prepend reasoning/thinking if display is enabled (per-platform).
            # Mattermost requires explicit per-platform opt-in because this is
            # scratch text, not ordinary final-answer content.
            try:
                _show_reasoning_effective = _resolve_gateway_display_bool(
                    _load_gateway_config(),
                    _platform_config_key(source.platform),
                    "show_reasoning",
                    default=bool(getattr(self, "_show_reasoning", False)),
                    platform=source.platform,
                    require_platform_override_for={Platform.MATTERMOST},
                )
            except Exception:
                _show_reasoning_effective = (
                    False
                    if source.platform == Platform.MATTERMOST
                    else getattr(self, "_show_reasoning", False)
                )
            if _show_reasoning_effective and response and not _intentional_silence:
                last_reasoning = agent_result.get("last_reasoning")
                if last_reasoning:
                    from gateway.stream_consumer import escape_code_fences_for_display
                    # Collapse long reasoning to keep messages readable
                    lines = last_reasoning.strip().splitlines()
                    if len(lines) > 15:
                        display_reasoning = "\n".join(lines[:15])
                        display_reasoning += f"\n_... ({len(lines) - 15} more lines)_"
                    else:
                        display_reasoning = last_reasoning.strip()
                    # Render style is per-platform: Discord defaults to "-# "
                    # subtext (native small grey metadata text); other
                    # platforms keep the fenced code block.
                    try:
                        from gateway.display_config import resolve_display_setting
                        _reasoning_style = resolve_display_setting(
                            _load_gateway_config(),
                            _platform_config_key(source.platform),
                            "reasoning_style",
                            "code",
                        )
                    except Exception:
                        _reasoning_style = "code"
                    if _reasoning_style == "subtext":
                        _quoted = "\n".join(
                            f"-# {ln}" if ln else "-#" for ln in display_reasoning.splitlines()
                        )
                        response = f"-# 💭 Reasoning\n{_quoted}\n\n{response}"
                    elif _reasoning_style == "blockquote":
                        _quoted = "\n".join(
                            f"> {ln}" if ln else ">" for ln in display_reasoning.splitlines()
                        )
                        response = f"> 💭 **Reasoning:**\n{_quoted}\n\n{response}"
                    else:
                        # Escape ``` inside reasoning so inner fences don't
                        # break the outer code block used to render it.
                        display_reasoning = escape_code_fences_for_display(display_reasoning)
                        response = f"💭 **Reasoning:**\n```\n{display_reasoning}\n```\n\n{response}"

            # Runtime-metadata footer — only on the FINAL message of the turn.
            # Off by default (display.runtime_footer.enabled=false).  When
            # streaming already delivered the body, we can't mutate the sent
            # text, so we fire a separate trailing send below.
            _footer_line = ""
            try:
                from gateway.runtime_footer import build_footer_line as _bfl
                _footer_line = _bfl(
                    user_config=_load_gateway_config(),
                    platform_key=_platform_config_key(source.platform),
                    model=agent_result.get("model"),
                    context_tokens=agent_result.get("last_prompt_tokens", 0) or 0,
                    context_length=agent_result.get("context_length") or None,
                    cwd=os.environ.get("TERMINAL_CWD", ""),
                    turn_seconds=_turn_seconds,
                )
            except Exception as _footer_err:
                logger.debug("runtime_footer build failed: %s", _footer_err)
                _footer_line = ""
            if _footer_line and response and not agent_result.get("already_sent") and not _intentional_silence:
                response = f"{response}\n\n{_footer_line}"

            # Emit agent:end hook
            await self.hooks.emit("agent:end", {
                **hook_ctx,
                "response": (response or "")[:500],
            })
            
            # Check for pending process watchers (check_interval on background processes)
            try:
                from tools.process_registry import process_registry
                # Detach the current batch atomically (see crash-recovery drain
                # above): reassign to a fresh list so a watcher appended by a
                # concurrent session during the yield isn't dropped by clear().
                watchers = process_registry.pending_watchers
                process_registry.pending_watchers = []
                for i, watcher in enumerate(watchers):
                    asyncio.create_task(self._run_process_watcher(watcher))
                    if i % 100 == 99:
                        await asyncio.sleep(0)
            except Exception as e:
                logger.error("Process watcher setup error: %s", e)

            # Drain watch pattern notifications that arrived during the agent run.
            # Watch events and completions share the same queue; process
            # completions are already handled by the per-process watcher task
            # above, so we only inject watch-type events here.
            #
            # Async-delegation completions ALSO ride this shared queue but are
            # owned by the dedicated _async_delegation_watcher (started at
            # boot), which covers both the idle and post-turn cases with a
            # single consumer — so we leave them on the queue here.
            try:
                from tools.process_registry import process_registry as _pr
                _watch_events = _drain_gateway_watch_events(_pr.completion_queue)
                for evt in _watch_events:
                    synth_text = _format_gateway_process_notification(evt)
                    if synth_text:
                        try:
                            await self._inject_watch_notification(synth_text, evt)
                        except Exception as e2:
                            logger.error("Watch notification injection error: %s", e2)
            except Exception as e:
                logger.debug("Watch queue drain error: %s", e)

            # NOTE: Dangerous command approvals are now handled inline by the
            # blocking gateway approval mechanism in tools/approval.py.  The agent
            # thread blocks until the user responds with /approve or /deny, so by
            # the time we reach here the approval has already been resolved.  The
            # old post-loop pop_pending + approval_hint code was removed in favour
            # of the blocking approach that mirrors CLI's synchronous input().
            
            # Save the full conversation to the transcript, including tool calls.
            # This preserves the complete agent loop (tool_calls, tool results,
            # intermediate reasoning) so sessions can be resumed with full context
            # and transcripts are useful for debugging and training data.
            #
            # IMPORTANT: For context-overflow failures (compression exhausted,
            # generic 400 on large sessions) we must NOT persist the user's
            # message — doing so would grow the session further and cause the
            # same failure on the next attempt, an infinite loop. (#1630, #9893)
            #
            # Transient failures (429, timeout, connection error, provider 5xx)
            # are different: the session is not oversized, and silently dropping
            # the user message causes severe context loss on retry — the agent
            # forgets what was just asked.  Persist the user turn so the
            # conversation is preserved. (#7100)
            agent_failed_early = bool(agent_result.get("failed"))
            hidden_reasoning_incomplete = _is_gateway_hidden_reasoning_incomplete_turn(
                agent_result
            )
            _err_str_for_classify = str(agent_result.get("error", "")).lower()
            # Use specific multi-word phrases (not bare "exceed" or "token")
            # to avoid false positives on transient errors like "rate limit
            # exceeded" or "invalid auth token". Matches run_agent.py's
            # own context-length classifier.
            is_context_overflow_failure = agent_failed_early and (
                bool(agent_result.get("compression_exhausted"))
                or any(p in _err_str_for_classify for p in (
                    "context length", "context size", "context window",
                    "maximum context", "token limit", "too many tokens",
                    "reduce the length", "exceeds the limit",
                    "request entity too large", "prompt is too long",
                    "payload too large", "input is too long",
                ))
                or ("400" in _err_str_for_classify and len(history) > 50)
            )
            if is_context_overflow_failure:
                logger.info(
                    "Skipping transcript persistence for context-overflow "
                    "failure in session %s to prevent session growth loop.",
                    session_entry.session_id,
                )
            elif agent_failed_early:
                logger.info(
                    "Transient agent failure in session %s — persisting user "
                    "message so conversation context is preserved on retry.",
                    session_entry.session_id,
                )
            elif hidden_reasoning_incomplete:
                logger.warning(
                    "Suppressing hidden-reasoning-only incomplete gateway turn "
                    "for session %s: %s",
                    session_entry.session_id,
                    agent_result.get("error", "processing incomplete"),
                )

            # When compression is exhausted, the session is permanently too
            # large to process.  Auto-reset it so the next message starts
            # fresh instead of replaying the same oversized context in an
            # infinite fail loop.  (#9893)
            #
            # A lock-contended defer is the OPPOSITE case: the session is
            # temporarily uncompressible only because a concurrent path holds
            # the compression lock and is actively shrinking it. Never wipe
            # the session for that — retry-next-message semantics apply
            # (#69870 lock-skip consumer; salvaged from #49874).
            if agent_result.get("compression_deferred"):
                logger.info(
                    "Compression deferred for session %s — the compression "
                    "lock is held by a concurrent compressor. Keeping the "
                    "session intact; the next message retries normally.",
                    session_entry.session_id if session_entry else "?",
                )
            elif agent_result.get("compression_exhausted") and session_entry and session_key:
                logger.info(
                    "Auto-resetting session %s after compression exhaustion.",
                    session_entry.session_id,
                )
                new_entry = await self.async_session_store.reset_session(session_key)
                self._evict_cached_agent(session_key)
                # Conversation boundary: one funnel call clears every
                # conversation-scoped per-session dict (#58403 and siblings).
                # See _CONVERSATION_SCOPED_STATE.
                self._clear_conversation_scope(
                    session_key, reason="compression_exhausted_reset"
                )
                if new_entry is not None:
                    # Drop the stale reference to the bloated compressed child and
                    # re-point the Telegram topic binding at the fresh session.
                    # Compression rotated session_entry.session_id to the oversized
                    # compressed child earlier this turn (the agent-result sync
                    # above), and that _sync also rewrote the (chat_id, thread_id)
                    # -> bloated-child binding. reset_session swaps in a clean,
                    # parentless session, but without re-syncing the binding the
                    # next inbound message in this topic gets switch_session'd back
                    # onto the bloated child by the binding-heal walk, reloads the
                    # oversized transcript, and re-triggers compression exhaustion
                    # forever (#35809 — regression of the #9893/#10063 auto-reset).
                    # No-op on non-topic lanes.
                    session_entry = new_entry
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="compression-exhausted-reset",
                    )
                response = (response or "") + (
                    "\n\n🔄 Session auto-reset — the conversation exceeded the "
                    "maximum context size and could not be compressed further. "
                    "Your next message will start a fresh session."
                )

            ts = time.time()  # Unix epoch float — consistent with DB storage
            
            # If this is a fresh session (no history), write the full tool
            # definitions as the first entry so the transcript is self-describing
            # -- the same list of dicts sent as tools=[...] in the API request.
            if is_context_overflow_failure:
                pass  # Skip all transcript writes — don't grow a broken session
            elif not history:
                tool_defs = agent_result.get("tools", [])
                await self.async_session_store.append_to_transcript(
                    session_entry.session_id,
                    {
                        "role": "session_meta",
                        "tools": tool_defs or [],
                        "model": _resolve_gateway_model(),
                        "platform": source.platform.value if source.platform else "",
                        "timestamp": ts,
                    }
                )
            
            # The agent already persisted these messages to SQLite via
            # _flush_messages_to_session_db(), so skip the DB write here
            # to prevent the duplicate-write bug (#860 / #42039). This holds
            # for the codex app-server runtime too: although it early-returns
            # and bypasses conversation_loop's per-step flushes, it flushes its
            # own projected assistant/tool messages before returning and
            # reports agent_persisted=True (see agent/codex_runtime.py). Reading
            # the flag (default = self._session_db is not None) keeps the
            # persistence contract explicit and lets any future non-persisting
            # runtime opt into a gateway-side write by returning False.
            agent_persisted = agent_result.get("agent_persisted", self._session_db is not None)

            # Find only the NEW messages from this turn (skip history we loaded).
            # Use the filtered history length (history_offset) that was actually
            # passed to the agent, not len(history) which includes session_meta
            # entries that were stripped before the agent saw them.
            if is_context_overflow_failure:
                pass  # handled above — skip all transcript writes
            elif agent_failed_early or hidden_reasoning_incomplete:
                # Transient failure (429/timeout/5xx): persist only the user
                # message so the next message can load a transcript that
                # reflects what was said.  Skip the assistant error text since
                # it's a gateway-generated hint, not model output. Hidden-
                # reasoning-only incomplete turns follow the same persistence
                # rule so peer-agent channels don't ingest them as completed
                # assistant turns. (#7100, #51628)
                _user_entry = {
                    "role": "user",
                    "content": (
                        persist_user_message
                        if persist_user_message is not None
                        else message_text
                    ),
                    "timestamp": (
                        persist_user_timestamp
                        if persist_user_timestamp is not None
                        else ts
                    ),
                }
                if event.message_id:
                    _user_entry["message_id"] = str(event.message_id)
                # Dedupe: skip if this platform message_id is already in the
                # transcript (prevents duplicate user turns on Telegram retries
                # after transient failures). #47237
                _skip_persist = (
                    event.message_id
                    and await self.async_session_store.has_platform_message_id(
                        session_entry.session_id, str(event.message_id)
                    )
                )
                if _skip_persist:
                    logger.info(
                        "Skipping duplicate user turn "
                        "(message_id=%s) in session %s",
                        event.message_id, session_entry.session_id,
                    )
                else:
                    await self.async_session_store.append_to_transcript(
                        session_entry.session_id,
                        _user_entry,
                        skip_db=agent_persisted,
                    )
            else:
                history_len = agent_result.get("history_offset", len(history))
                new_messages = agent_messages[history_len:] if len(agent_messages) > history_len else []

                # If no new messages found (edge case), fall back to simple user/assistant
                if not new_messages:
                    _user_entry = {
                        "role": "user",
                        "content": (
                            persist_user_message
                            if persist_user_message is not None
                            else message_text
                        ),
                        "timestamp": (
                            persist_user_timestamp
                            if persist_user_timestamp is not None
                            else ts
                        ),
                    }
                    if event.message_id:
                        _user_entry["message_id"] = str(event.message_id)
                    await self.async_session_store.append_to_transcript(
                        session_entry.session_id,
                        _user_entry,
                        skip_db=agent_persisted,
                    )
                    if response:
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id,
                            {"role": "assistant", "content": response, "timestamp": ts},
                            skip_db=agent_persisted,
                        )
                else:
                    # Attach the inbound platform message_id to the first user
                    # entry written this turn so platform-level quote-resolution
                    # (e.g. Yuanbao QuoteContextMiddleware's transcript fallback)
                    # can find earlier @bot messages by their original message_id.
                    _user_msg_id_attached = False
                    for msg in new_messages:
                        # Skip system messages (they're rebuilt each run)
                        if msg.get("role") == "system":
                            continue
                        # Add timestamp to each message for debugging
                        entry = {**msg, "timestamp": ts}
                        if (
                            not _user_msg_id_attached
                            and msg.get("role") == "user"
                            and event.message_id
                            and "message_id" not in entry
                        ):
                            entry["message_id"] = str(event.message_id)
                            _user_msg_id_attached = True
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id, entry,
                            skip_db=agent_persisted,
                        )
            
            # Token counts and model are now persisted by the agent directly.
            # Keep only last_prompt_tokens here for context-window tracking and
            # compression decisions.
            await self.async_session_store.update_session(
                session_entry.session_key,
                last_prompt_tokens=agent_result.get("last_prompt_tokens", 0),
            )

            # Re-baseline the cached agent's message_count snapshot now that
            # ALL of this turn's transcript writes are done — the agent's
            # flushed user/assistant/tool rows AND the first-turn `session_meta`
            # marker appended above.  The cross-process coherence guard (#45966)
            # snapshots the count at agent-BUILD time (before this turn's own
            # writes) and never refreshes it on reuse, so without this the
            # process's own turn grows message_count and the next turn sees a
            # mismatch and rebuilds the agent — destroying prompt caching.
            #
            # This MUST run after the `session_meta` append: that row also
            # increments message_count, so re-baselining before it (the old
            # position) left the snapshot one short and the guard mis-fired on
            # turn 2 of EVERY fresh gateway conversation, rebuilding the cached
            # agent and busting the prompt cache.  Running here also uses the
            # compaction-updated session_id (the agent_result session_id swap
            # above), matching this function's documented contract.  Refreshing
            # here makes the guard fire only on a DIFFERENT process's writes.
            # Fail-safe inside the helper.
            await self._refresh_agent_cache_message_count(
                session_key, session_entry.session_id
            )

            # Intentional silence is a delivery decision, not a transcript
            # mutation.  The agent's [SILENT]/NO_REPLY assistant turn above is
            # still persisted in session history so later turns keep normal
            # user/assistant alternation; only the outbound chat delivery is
            # suppressed.
            if _intentional_silence:
                logger.info(
                    "Suppressing intentional silence marker for session %s",
                    session_entry.session_id,
                )
                response = ""

            # Auto voice reply: send TTS audio before the text response
            _already_sent = bool(agent_result.get("already_sent"))
            # Skip when streaming TTS already delivered audio for this turn (#60671).
            _stts_adapter = self._adapter_for_source(source)
            _streaming_tts_done = (
                _stts_adapter is not None
                and bool(getattr(_stts_adapter, "_streaming_tts_turn_completed", lambda *_a, **_k: False)(session_key, run_generation))
            )
            if (
                not _streaming_tts_done
                and self._should_send_voice_reply(event, response, agent_messages, already_sent=_already_sent)
            ):
                await self._send_voice_reply(event, response)

            # If streaming already delivered the response, extract and
            # deliver any MEDIA: files before returning None.  Streaming
            # sends raw text chunks that include MEDIA: tags — the normal
            # post-processing in _process_message_background is skipped
            # when already_sent is True, so media files would never be
            # delivered without this.
            #
            # Never skip when the agent failed — the error message is new
            # content the user hasn't seen (streaming only sent earlier
            # partial output before the failure).  Without this guard,
            # users see the agent "stop responding without explanation."
            if agent_result.get("already_sent") and not agent_result.get("failed"):
                if response:
                    _media_adapter = self._adapter_for_source(source)
                    if _media_adapter:
                        await self._deliver_media_from_response(
                            response, event, _media_adapter,
                        )
                # Streaming already delivered the body text, but the footer was
                # intentionally held back (see the `not already_sent` gate above).
                # Send it now as a small trailing message so Telegram/Discord/etc.
                # still surface the runtime metadata on the final reply.
                if _footer_line:
                    try:
                        _foot_adapter = self._adapter_for_source(source)
                        if _foot_adapter:
                            await _foot_adapter.send(
                                source.chat_id,
                                _footer_line,
                                metadata=self._thread_metadata_for_source(source, self._reply_anchor_for_event(event)),
                            )
                    except Exception as _e:
                        logger.debug("trailing footer send failed: %s", _e)
                return None

            return response
            
        except Exception as e:
            # Stop typing indicator on error too, retaining Slack thread/workspace
            # routing so a failed turn cannot leave its status visible.
            try:
                _err_adapter = self._adapter_for_source(source)
                _stop_with_metadata = getattr(
                    type(_err_adapter), "_stop_typing_with_metadata", None
                )
                _stop_typing = getattr(type(_err_adapter), "stop_typing", None)
                if _err_adapter and callable(_stop_with_metadata):
                    await _err_adapter._stop_typing_with_metadata(
                        source.chat_id,
                        self._thread_metadata_for_source(
                            source, self._reply_anchor_for_event(event)
                        ),
                    )
                elif _err_adapter and callable(_stop_typing):
                    await _err_adapter.stop_typing(source.chat_id)
            except Exception:
                pass
            logger.exception("Agent error in session %s", session_key)
            # Crash-resilience for failures that happen before AIAgent enters
            # run_conversation() (for example: provider/httpx client init
            # failures). In that path the agent cannot persist the current
            # inbound turn itself, so append the user message here once. If the
            # agent already reached its early turn-start persistence, the latest
            # transcript user row will match and we skip the duplicate.
            try:
                if 'message_text' in locals() and message_text is not None and session_entry is not None:
                    _already_persisted = False
                    try:
                        _recent_transcript = await self.async_session_store.load_transcript(session_entry.session_id)
                    except Exception:
                        _recent_transcript = []
                    for _msg in reversed(_recent_transcript[-10:]):
                        if _msg.get("role") == "user":
                            _expected_user_content = (
                                persist_user_message
                                if persist_user_message is not None
                                else message_text
                            )
                            _already_persisted = (_msg.get("content") == _expected_user_content)
                            break
                    if not _already_persisted:
                        _user_entry = {
                            "role": "user",
                            "content": (
                                persist_user_message
                                if persist_user_message is not None
                                else message_text
                            ),
                            "timestamp": (
                                persist_user_timestamp
                                if persist_user_timestamp is not None
                                else time.time()
                            ),
                        }
                        if getattr(event, "message_id", None):
                            _user_entry["message_id"] = str(event.message_id)
                        await self.async_session_store.append_to_transcript(
                            session_entry.session_id,
                            _user_entry,
                        )
            except Exception:
                logger.debug("Failed to persist inbound user message after agent exception", exc_info=True)
            # Log full details server-side only; never expose raw exception
            # types or messages to end users (info-leakage risk).
            status_hint = ""
            status_code = getattr(e, "status_code", None)
            _hist_len = len(history) if 'history' in locals() else 0
            if status_code == 401:
                status_hint = " Check your API key or run `claude /login` to refresh OAuth credentials."
            elif status_code == 402:
                status_hint = " Your API balance or quota is exhausted. Check your provider dashboard."
            elif status_code == 429:
                # Check if this is a plan usage limit (resets on a schedule) vs a transient rate limit
                _err_body = getattr(e, "response", None)
                _err_json = {}
                try:
                    if _err_body is not None:
                        _err_json = _err_body.json().get("error", {})
                        if not isinstance(_err_json, dict):
                            _err_json = {}
                except Exception:
                    pass
                if _err_json.get("type") == "usage_limit_reached":
                    _resets_in = _err_json.get("resets_in_seconds")
                    if _resets_in and _resets_in > 0:
                        import math
                        _hours = math.ceil(_resets_in / 3600)
                        status_hint = f" Your plan's usage limit has been reached. It resets in ~{_hours}h."
                    else:
                        status_hint = " Your plan's usage limit has been reached. Please wait until it resets."
                else:
                    status_hint = " You are being rate-limited. Please wait a moment and try again."
            elif status_code == 529:
                status_hint = " The API is temporarily overloaded. Please try again shortly."
            elif status_code in {400, 500}:
                # 400 with a large session is context overflow.
                # 500 with a large session often means the payload is too large
                # for the API to process — treat it the same way.
                if _hist_len > 50:
                    return (
                        "⚠️ Session too large for the model's context window.\n"
                        "Use /compact to compress the conversation, or "
                        "/reset to start fresh."
                    )
                elif status_code == 400:
                    status_hint = " The request was rejected by the API."
            return (
                f"Sorry, I encountered an unexpected error.{status_hint}\n"
                "Try again or use /reset to start a fresh session."
            )
        finally:
            # Restore session context variables to their pre-handler state
            self._clear_session_env(_session_env_tokens)


    def _check_slash_access(
        self, source: SessionSource, canonical_cmd: str
    ) -> Optional[str]:
        """Return a denial message if ``source`` cannot run ``canonical_cmd``,
        else None. Used by both the cold and running-agent dispatch paths
        in ``_handle_message`` so admin/user gating can't be bypassed by
        an in-flight agent.

        Backward-compat semantics live in
        :func:`gateway.slash_access.policy_for_source` — when the operator
        hasn't set ``allow_admin_from`` for the scope, the policy returns
        ``enabled=False`` and this method always returns None.
        """
        from gateway.slash_access import policy_for_source as _policy_for_source

        if not canonical_cmd:
            return None
        policy = _policy_for_source(self.config, source)
        if not policy.enabled or policy.can_run(source.user_id, canonical_cmd):
            return None
        logger.info(
            "Slash command /%s denied for %s:%s (not admin, not in user_allowed_commands)",
            canonical_cmd,
            source.platform.value if source.platform else "?",
            source.user_id,
        )
        allowed_preview = sorted(policy.user_allowed_commands)
        if allowed_preview:
            suffix = (
                "You can run: "
                + ", ".join(f"/{c}" for c in allowed_preview[:12])
                + ("…" if len(allowed_preview) > 12 else "")
                + ". Use /whoami for the full list."
            )
        else:
            suffix = (
                "No slash commands are enabled for non-admins on this "
                "platform. Ask an admin to add you to allow_admin_from "
                "or to set user_allowed_commands."
            )
        return f"⛔ /{canonical_cmd} is admin-only here. {suffix}"


    def _is_stale_restart_redelivery(self, event: MessageEvent) -> bool:
        """Return True if this /restart is a Telegram re-delivery we already handled.

        The previous gateway wrote ``.restart_last_processed.json`` with the
        triggering platform + update_id when it processed the /restart.  If
        we now see a /restart on the same platform with an update_id <= that
        recorded value, it is a redelivery when this process booted from that
        restart. Otherwise the marker must still be recent (< 5 minutes).

        Only applies to Telegram today (the only platform that exposes a
        numeric cross-session update ordering); other platforms return False.
        """
        from gateway.run import _hermes_home

        if event is None or event.source is None:
            return False
        if event.platform_update_id is None:
            return False
        if event.source.platform is None:
            return False
        # Only Telegram populates platform_update_id currently; be explicit
        # so future platforms aren't accidentally gated by this check.
        try:
            platform_value = event.source.platform.value
        except Exception:
            return False
        if platform_value != "telegram":
            return False

        try:
            marker_path = _hermes_home / ".restart_last_processed.json"
            if not marker_path.exists():
                # Belt-and-suspenders for when the dedup marker goes missing
                # (manually cleaned up, or the previous cycle's write failed).
                # Without a marker the update_id comparison below can't run, so
                # a redelivered /restart would sail through and re-restart the
                # gateway — an infinite loop (issue #18528).
                #
                # Suppress ONLY when we can independently confirm we just came
                # out of a restart cycle: this process booted from a
                # chat-originated /restart (_booted_from_restart) AND is still
                # within a short post-boot window. This never swallows a
                # genuine first /restart on a fresh boot (no restart marker on
                # boot → flag stays False). Consume the flag one-shot so a
                # legitimate /restart sent later in the same session is honored.
                if (
                    getattr(self, "_booted_from_restart", False)
                    and time.time() - getattr(self, "_startup_time", 0.0) < 60
                ):
                    self._booted_from_restart = False
                    return True
                return False
            data = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception:
            return False

        if data.get("platform") != platform_value:
            return False
        recorded_uid = data.get("update_id")
        if not isinstance(recorded_uid, int):
            return False
        if event.platform_update_id > recorded_uid:
            return False

        # A service-managed restart can legitimately take longer than the
        # marker's normal five-minute trust window while adapters, cron, and
        # in-flight deliveries drain. If this process booted from the recorded
        # chat restart, the first same-or-older update is still that restart's
        # redelivery regardless of elapsed wall time. Consume the boot signal
        # one-shot so a later genuine command is evaluated normally.
        if getattr(self, "_booted_from_restart", False):
            self._booted_from_restart = False
            return True

        # Staleness guard: ignore markers older than 5 minutes.  A legitimately
        # old marker (e.g. crash recovery where notify never fired) should not
        # swallow a fresh /restart from the user.
        requested_at = data.get("requested_at")
        if isinstance(requested_at, (int, float)):
            if time.time() - requested_at > 300:
                return False
        return True


    async def _handle_suggestions_command(self, event: MessageEvent) -> str:
        """Handle /suggestions in the gateway.

        Delegates to the shared handler so CLI and gateway never drift. The
        origin is built from the event source so an accepted suggestion's job
        delivers back to this chat/thread.
        """
        args = (event.get_command_args() or "").strip()
        source = event.source
        origin = None
        try:
            platform = getattr(source.platform, "value", None) or str(getattr(source, "platform", "") or "")
            chat_id = getattr(source, "chat_id", None)
            if platform and chat_id:
                origin = {
                    "platform": platform,
                    "chat_id": str(chat_id),
                    "chat_name": getattr(source, "chat_name", None),
                    "thread_id": getattr(source, "thread_id", None),
                }
        except Exception:
            origin = None
        try:
            from hermes_cli.suggestions_cmd import handle_suggestions_command

            return handle_suggestions_command(args, origin=origin, surface="gateway")
        except Exception as e:
            logger.debug("suggestions command failed: %s", e)
            return f"Suggestions command failed: {e}"


    async def _handle_blueprint_command(self, event: MessageEvent):
        """Handle /blueprint in the gateway.

        Delegates to the shared handler so CLI, TUI, and gateway never drift.
        Returns a BlueprintCommandResult: ``text`` is shown to the user, and if
        ``agent_seed`` is set the dispatch site rewrites ``event.text`` to the
        seed and falls through to the agent (the ``/steer`` pattern) so the
        agent gathers the slot values conversationally. Origin is built from the
        event source so a directly created blueprint job delivers back to this chat.
        """
        args = (event.get_command_args() or "").strip()
        source = event.source
        origin = None
        try:
            platform = getattr(source.platform, "value", None) or str(getattr(source, "platform", "") or "")
            chat_id = getattr(source, "chat_id", None)
            if platform and chat_id:
                origin = {
                    "platform": platform,
                    "chat_id": str(chat_id),
                    "chat_name": getattr(source, "chat_name", None),
                    "thread_id": getattr(source, "thread_id", None),
                }
        except Exception:
            origin = None
        try:
            from hermes_cli.blueprint_cmd import handle_blueprint_command

            return handle_blueprint_command(args, origin=origin, surface="gateway")
        except Exception as e:
            logger.debug("blueprint command failed: %s", e)
            from hermes_cli.blueprint_cmd import BlueprintCommandResult

            return BlueprintCommandResult(f"Cron blueprint command failed: {e}")


    async def _execute_mcp_reload(self, event: MessageEvent) -> str:
        """Actually disconnect, reconnect, and notify MCP tool changes.

        Split out from ``_handle_reload_mcp_command`` so the confirmation
        wrapper can invoke the same path whether the user confirmed via
        button, text reply, or has the confirm gate disabled.
        """
        loop = asyncio.get_running_loop()
        try:
            from tools.mcp_tool import shutdown_mcp_servers, discover_mcp_tools, _servers, _lock

            # Capture old server names before shutdown
            with _lock:
                old_servers = set(_servers.keys())

            # Read new config before shutting down, so we know what will be added/removed
            # Shutdown existing connections
            await loop.run_in_executor(None, shutdown_mcp_servers)

            # Reconnect by discovering tools (reads config.yaml fresh)
            new_tools = await loop.run_in_executor(None, discover_mcp_tools)

            # Compute what changed
            with _lock:
                connected_servers = set(_servers.keys())

            added = connected_servers - old_servers
            removed = old_servers - connected_servers
            reconnected = connected_servers & old_servers

            lines = [t("gateway.reload_mcp.header")]
            if reconnected:
                lines.append(t("gateway.reload_mcp.reconnected", names=", ".join(sorted(reconnected))))
            if added:
                lines.append(t("gateway.reload_mcp.added", names=", ".join(sorted(added))))
            if removed:
                lines.append(t("gateway.reload_mcp.removed", names=", ".join(sorted(removed))))
            if not connected_servers:
                lines.append(t("gateway.reload_mcp.none_connected"))
            else:
                lines.append(t("gateway.reload_mcp.tools_available", tools=len(new_tools), servers=len(connected_servers)))

            # Refresh cached agents so existing sessions see new MCP tools on
            # their next turn — without this, the user has to `/new` (which
            # discards conversation history) to pick up tools from a server
            # that was just added or reconnected. The user has already
            # consented to the prompt-cache invalidation via the slash-confirm
            # gate in _handle_reload_mcp_command before we reach this point.
            try:
                from tools.mcp_tool import refresh_agent_mcp_tools
                _cache = getattr(self, "_agent_cache", None)
                _cache_lock = getattr(self, "_agent_cache_lock", None)
                if _cache_lock is not None and _cache:
                    with _cache_lock:
                        for _sess_key, _entry in list(_cache.items()):
                            try:
                                _agent = _entry[0] if isinstance(_entry, tuple) else _entry
                            except Exception:
                                continue
                            if _agent is None:
                                continue
                            # Preserve each cached agent's build-time toolset
                            # selection EXACTLY: a gateway session built with a
                            # restricted enabled_toolsets (e.g. ["safe"]) must
                            # NOT silently gain tools after a reload. This is the
                            # opposite of the interactive CLI/TUI /reload-mcp,
                            # which is a single user re-applying their own config
                            # edit; gateway agents are per-session and may be
                            # deliberately locked down. (Contract is asserted by
                            # test_reload_mcp_preserves_per_agent_toolset_overrides.)
                            refresh_agent_mcp_tools(_agent, quiet_mode=True)
            except Exception as _exc:
                logger.debug(
                    "Failed to update cached agent tools after MCP reload: %s",
                    _exc,
                )

            # Inject a message at the END of the session history so the
            # model knows tools changed on its next turn.  Appended after
            # all existing messages to preserve prompt-cache for the prefix.
            change_parts = []
            if added:
                change_parts.append(f"Added servers: {', '.join(sorted(added))}")
            if removed:
                change_parts.append(f"Removed servers: {', '.join(sorted(removed))}")
            if reconnected:
                change_parts.append(f"Reconnected servers: {', '.join(sorted(reconnected))}")
            tool_summary = f"{len(new_tools)} MCP tool(s) now available" if new_tools else "No MCP tools available"
            change_detail = ". ".join(change_parts) + ". " if change_parts else ""
            reload_msg = {
                "role": "user",
                "content": f"[IMPORTANT: MCP servers have been reloaded. {change_detail}{tool_summary}. The tool list for this conversation has been updated accordingly.]",
            }
            try:
                session_entry = await self.async_session_store.get_or_create_session(event.source)
                await self.async_session_store.append_to_transcript(
                    session_entry.session_id, reload_msg
                )
            except Exception:
                pass  # Best-effort; don't fail the reload over a transcript write

            return "\n".join(lines)

        except Exception as e:
            logger.warning("MCP reload failed: %s", e)
            return t("gateway.reload_mcp.failed", error=e)


    async def _maybe_confirm_destructive_slash(
        self,
        *,
        event: MessageEvent,
        command: str,
        title: str,
        detail: str,
        execute,
    ) -> Union[str, "EphemeralReply", None]:
        """Gate a destructive session slash command (/new, /reset, /undo).

        ``execute`` is an async callable ``execute() -> str | EphemeralReply``
        that performs the destructive action.  If the
        ``approvals.destructive_slash_confirm`` config gate is off, ``execute``
        runs immediately (returning its result).  Otherwise this routes
        through ``_request_slash_confirm`` — native yes/no buttons on
        Telegram/Discord/Slack, text fallback elsewhere.

        Three-option resolution:

          - ``once``  — run ``execute`` and return its result
          - ``always`` — persist ``approvals.destructive_slash_confirm: false``,
                        then run ``execute``
          - ``cancel`` — return a "cancelled" message; do not run ``execute``
        """
        # Gate check.
        confirm_required = True
        try:
            cfg = self._read_user_config()
            approvals = cfg.get("approvals") if isinstance(cfg, dict) else None
            if isinstance(approvals, dict):
                confirm_required = bool(approvals.get("destructive_slash_confirm", True))
        except Exception:
            pass

        if not confirm_required:
            return await execute()

        session_key = self._session_key_for_source(event.source)

        async def _on_confirm(choice: str):
            if choice == "cancel":
                return f"🟡 /{command} cancelled. Conversation unchanged."
            persisted = False
            if choice == "always":
                try:
                    from cli import save_config_value
                    # save_config_value swallows its own errors and reports the
                    # outcome in the return value, so the try block alone says
                    # nothing about whether the write landed.
                    persisted = bool(
                        save_config_value("approvals.destructive_slash_confirm", False)
                    )
                    if persisted:
                        logger.info(
                            "User opted out of destructive slash confirm (session=%s)",
                            session_key,
                        )
                    else:
                        logger.warning(
                            "Could not persist destructive_slash_confirm=false "
                            "(session=%s); config.yaml is not writable",
                            session_key,
                        )
                except Exception as exc:
                    logger.warning(
                        "Failed to persist destructive_slash_confirm=false: %s", exc,
                    )
            result = await execute()
            if choice == "always":
                if persisted:
                    note = (
                        "\n\nℹ️ Future /clear, /new, /reset, and /undo will run "
                        "without confirmation. Re-enable via "
                        "`approvals.destructive_slash_confirm: true` in config.yaml."
                    )
                else:
                    # The user did approve this run, so the action still goes
                    # ahead, but the preference did not stick and the prompt
                    # will be back next time. Say so rather than promising an
                    # opt-out that was never written.
                    note = (
                        "\n\n⚠️ Could not save that preference (config.yaml is not "
                        "writable), so /clear, /new, /reset, and /undo will ask "
                        "again next time. To silence it permanently, set "
                        "`approvals.destructive_slash_confirm: false` in config.yaml."
                    )
                if isinstance(result, str):
                    return result + note
                # EphemeralReply or other: leave untouched, since the note would
                # mangle structured replies.
                return result
            return result

        _p = self._typed_command_prefix_for(event.source.platform)
        prompt_message = (
            f"⚠️ **Confirm /{command}**\n\n"
            f"{detail}\n\n"
            "Choose:\n"
            "• **Approve Once** — proceed this time only\n"
            "• **Always Approve** — proceed and silence this prompt permanently\n"
            "• **Cancel** — keep current conversation\n\n"
            f"_Text fallback: reply `{_p}approve`, `{_p}always`, or `{_p}cancel`._"
        )
        return await self._request_slash_confirm(
            event=event,
            command=command,
            title=title,
            message=prompt_message,
            handler=_on_confirm,
        )


    async def _request_slash_confirm(
        self,
        *,
        event: MessageEvent,
        command: str,
        title: str,
        message: str,
        handler,
    ) -> Optional[str]:
        """Ask the user to confirm an expensive slash command.

        ``handler`` is an async callable ``handler(choice: str) -> str``
        where ``choice`` is ``"once"``, ``"always"``, or ``"cancel"``.
        The handler runs on the event loop when the user responds; its
        return value is sent back as a gateway message.

        Returns a short acknowledgment string to send immediately (before
        the user's response).  If buttons rendered successfully the ack
        is ``None`` (buttons are self-explanatory); if we fell back to
        text the message itself IS the ack.
        """
        from tools import slash_confirm as _slash_confirm_mod

        source = event.source
        session_key = self._session_key_for_source(source)
        # Bare-runner test harnesses (object.__new__(GatewayRunner)) skip
        # __init__ and don't have the counter attribute — fall back to a
        # local counter so tests don't AttributeError.  Real runs always
        # have the instance attribute.
        counter = getattr(self, "_slash_confirm_counter", None)
        if counter is None:
            import itertools as _itertools
            counter = _itertools.count(1)
            self._slash_confirm_counter = counter
        confirm_id = f"{next(counter)}"

        # Register the pending confirm FIRST so a super-fast button click
        # cannot race the send_slash_confirm return.
        _slash_confirm_mod.register(session_key, confirm_id, command, handler)

        adapter = self._adapter_for_source(source)
        metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))

        used_buttons = False
        if adapter is not None:
            try:
                button_result = await adapter.send_slash_confirm(
                    chat_id=source.chat_id,
                    title=title,
                    message=message,
                    session_key=session_key,
                    confirm_id=confirm_id,
                    metadata=metadata,
                )
                if button_result and getattr(button_result, "success", False):
                    used_buttons = True
            except Exception as exc:
                logger.debug(
                    "send_slash_confirm failed for %s on %s: %s",
                    command, source.platform, exc,
                )

        if used_buttons:
            # Buttons rendered — no redundant text ack.
            return None
        # Text fallback — return the prompt message as the direct reply.
        return message


    def _read_user_config(self) -> Dict[str, Any]:
        """Read the user's raw config.yaml (cached) for gate lookups.

        Used by slash-confirm gates that must reflect on-disk state changes
        (e.g. a prior "Always Approve" click) without a gateway restart.
        """
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}


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
