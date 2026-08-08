"""Notification / async-delegation / watcher delivery methods for GatewayRunner.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition Phase 3).
These are the methods that deliver platform/update/restart notices, watch
background process events, and inject async-delegation completions as
synthetic gateway messages. They use only ``self`` state plus module-level
helpers, so they live on a mixin that ``GatewayRunner`` inherits — every
``self.*`` call site resolves identically via the MRO, making this a
behavior-neutral move that lifts ~1,500 LOC out of run.py.

Module-level run.py helpers a method needs (``_parse_session_key``,
``_non_conversational_metadata``, ``_hermes_home``, etc.) are imported lazily
inside the method body — a deferred ``from gateway.run import ...`` resolves at
call time (run.py fully loaded by then), avoiding an import cycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional, cast

from gateway.config import Platform, _BUILTIN_PLATFORM_VALUES
from gateway.delivery import resolve_delivery_transport
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


class GatewayNotificationsMixin:
    """Notification / async-delegation / watcher delivery methods for GatewayRunner."""

    async def _deliver_platform_notice(self, source, content: str) -> None:
        """Deliver a setup/operational notice using platform-specific privacy rules."""
        from gateway.run import _is_slack_ignored_channel
        adapter = self._adapter_for_source(source)
        if not adapter:
            return

        config = getattr(self, "config", None)
        if (
            config
            and getattr(source, "platform", None) == Platform.SLACK
            and _is_slack_ignored_channel(config, getattr(source, "chat_id", None))
        ):
            logger.info(
                "Skipping Slack platform notice for configured ignored channel %s",
                getattr(source, "chat_id", None),
            )
            return

        notice_delivery = "public"
        if config and hasattr(config, "get_notice_delivery"):
            notice_delivery = config.get_notice_delivery(source.platform)

        metadata = self._thread_metadata_for_source(source)
        if notice_delivery == "private" and getattr(source, "user_id", None):
            try:
                result = await adapter.send_private_notice(
                    source.chat_id,
                    source.user_id,
                    content,
                    metadata=metadata,
                )
                if getattr(result, "success", False):
                    return
            except Exception:
                logger.debug(
                    "[%s] send_private_notice failed, falling back to public",
                    getattr(source, "platform", "?"),
                    exc_info=True,
                )

        await adapter.send(source.chat_id, content, metadata=metadata)

    async def _resolve_async_delegation_session(
        self,
        session_entry: SessionEntry,
        pinned_session_id: str,
    ) -> Optional[SessionEntry]:
        """Resolve an async completion to its verified owning gateway session.

        A compression rotation ends the physical parent row while continuing
        the same logical conversation in a child.  Follow that lineage, but
        never let a late completion override an unrelated /new or restored
        route.  Unknown ownership remains fail-closed; the result is still
        available in the delegation records.
        """
        session_db = cast(Any, self._session_db)
        if session_db is None:
            logger.warning(
                "Async-delegation completion has no session database; "
                "dropping injection (#55578 fail-closed)."
            )
            return None

        pinned_row = None
        try:
            pinned_row = await session_db.get_session(pinned_session_id)
        except Exception:
            logger.debug(
                "Async-delegation parent lookup failed for %s",
                pinned_session_id,
                exc_info=True,
            )

        if pinned_row is None:
            logger.warning(
                "Async-delegation completion has unknown spawning session %s; "
                "dropping injection (#55578 fail-closed).",
                pinned_session_id,
            )
            return None

        target_session_id = pinned_session_id
        follows_compression = False
        if pinned_row.get("ended_at"):
            if pinned_row.get("end_reason") != "compression":
                logger.warning(
                    "Async-delegation completion pinned to ended session %s "
                    "(end_reason=%r); dropping injection instead of resurrecting it "
                    "(#55578 fail-closed).",
                    pinned_session_id,
                    pinned_row.get("end_reason"),
                )
                return None

            follows_compression = True
            try:
                target_session_id = await session_db.get_compression_tip(
                    pinned_session_id
                )
            except Exception:
                logger.debug(
                    "Async-delegation compression-tip lookup failed for %s",
                    pinned_session_id,
                    exc_info=True,
                )
                target_session_id = None

            if not target_session_id or target_session_id == pinned_session_id:
                logger.warning(
                    "Async-delegation completion pinned to compressed session %s "
                    "without a continuation; dropping injection.",
                    pinned_session_id,
                )
                return None

            try:
                tip_row = await session_db.get_session(target_session_id)
            except Exception:
                tip_row = None
            if tip_row is None or tip_row.get("ended_at"):
                logger.warning(
                    "Async-delegation compression continuation %s is %s; "
                    "dropping injection.",
                    target_session_id,
                    "unknown" if tip_row is None else "ended",
                )
                return None

            route_owns_lineage = session_entry.session_id in {
                pinned_session_id,
                target_session_id,
            }
            if not route_owns_lineage:
                # A long-running delegation may survive multiple compression
                # rotations.  Accept an intermediate stale route only when its
                # own verified compression tip is the same live target.
                try:
                    route_row = await session_db.get_session(session_entry.session_id)
                    route_tip = (
                        await session_db.get_compression_tip(session_entry.session_id)
                        if route_row is not None
                        and route_row.get("ended_at")
                        and route_row.get("end_reason") == "compression"
                        else None
                    )
                except Exception:
                    route_tip = None
                route_owns_lineage = route_tip == target_session_id

            if not route_owns_lineage:
                logger.warning(
                    "Async-delegation completion for compression lineage %s -> %s "
                    "does not own current route %s; dropping injection.",
                    pinned_session_id,
                    target_session_id,
                    session_entry.session_id,
                )
                return None

        if target_session_id == session_entry.session_id:
            return session_entry

        prior_session_id = session_entry.session_id
        if follows_compression:
            switched = await self.async_session_store.advance_compression_session(
                session_entry.session_key,
                prior_session_id,
                target_session_id,
            )
        else:
            switched = await self.async_session_store.switch_session(
                session_entry.session_key,
                target_session_id,
            )
        if switched is None:
            logger.warning(
                "Async-delegation completion could not bind routing key %s to "
                "owning session %s; dropping injection.",
                session_entry.session_key,
                target_session_id,
            )
            return None

        logger.info(
            "Pinned async-delegation completion to owning session %s "
            "(was %s) for routing key %s (#57498)",
            target_session_id,
            prior_session_id,
            session_entry.session_key,
        )
        return switched

    def _build_process_event_source(self, evt: dict):
        """Resolve the canonical source for a synthetic background-process event.

        Prefer the persisted session-store origin for the event's session key.
        Falling back to the currently active foreground event is what causes
        cross-topic bleed, so don't do that.
        """
        from gateway.run import _parse_session_key
        from gateway.session import SessionSource

        session_key = str(evt.get("session_key") or "").strip()
        derived_platform = ""
        derived_chat_type = ""
        derived_chat_id = ""

        if session_key:
            try:
                self.session_store._ensure_loaded()
                entry = self.session_store._entries.get(session_key)
                if entry and getattr(entry, "origin", None):
                    return entry.origin
            except Exception as exc:
                logger.debug(
                    "Synthetic process-event session-store lookup failed for %s: %s",
                    session_key,
                    exc,
                )

            cached_source = self._get_cached_session_source(session_key)
            if cached_source is not None:
                return cached_source

            _parsed = _parse_session_key(session_key)
            if _parsed:
                derived_platform = _parsed["platform"]
                derived_chat_type = _parsed["chat_type"]
                derived_chat_id = _parsed["chat_id"]

        platform_name = str(evt.get("platform") or derived_platform or "").strip().lower()
        chat_type = str(evt.get("chat_type") or derived_chat_type or "").strip().lower()
        chat_id = str(evt.get("chat_id") or derived_chat_id or "").strip()
        if not platform_name or not chat_type or not chat_id:
            logger.warning(
                "Synthetic event source unresolvable: "
                "session_key=%r platform=%r chat_type=%r chat_id=%r "
                "evt_type=%s",
                session_key, platform_name, chat_type, chat_id,
                evt.get("type", "?"),
            )
            return None

        try:
            platform = Platform(platform_name)
            # Reject arbitrary strings that create dynamic pseudo-members.
            # Built-in platforms are always valid; plugin platforms must be
            # registered in the platform registry.
            if platform.value not in _BUILTIN_PLATFORM_VALUES:
                try:
                    from gateway.platform_registry import platform_registry
                    if not platform_registry.is_registered(platform.value):
                        raise ValueError(platform_name)
                except Exception:
                    raise ValueError(platform_name)
        except Exception:
            logger.warning(
                "Synthetic process event has invalid platform metadata: %r",
                platform_name,
            )
            return None

        return SessionSource(
            platform=platform,
            chat_id=chat_id,
            chat_type=chat_type,
            thread_id=str(evt.get("thread_id") or "").strip() or None,
            user_id=str(evt.get("user_id") or "").strip() or None,
            user_name=str(evt.get("user_name") or "").strip() or None,
        )

    async def _inject_watch_notification(
        self, synth_text: str, evt: dict,
    ) -> Optional[bool]:
        """Inject a watch/completion notification as a synthetic message event.

        Routing must come from the queued event itself, not from whatever
        foreground message happened to be active when the queue was drained.
        Returns ``True`` after adapter acceptance, ``False`` after a retryable
        adapter failure, and ``None`` when the event has no gateway route. This
        is not a transactional boundary: a process crash after adapter
        acceptance can still cause durable at-least-once replay.
        """
        from gateway.run import _parse_session_key
        source = self._build_process_event_source(evt)
        if not source:
            # API-server-originated sessions bind a RAW session key (the
            # X-Hermes-Session-Id value — see _bind_api_server_session), not a
            # structured ``agent:main:...`` key, so _build_process_event_source
            # cannot derive routing metadata from it and returns None above.
            # Recover the raw session id and wake the real session via the API
            # server's own /v1/chat/completions entry point instead of
            # dropping the event.
            raw_sid = str(evt.get("origin_session_id") or "").strip()
            if not raw_sid:
                _sk = str(evt.get("session_key") or "").strip()
                if _sk and _parse_session_key(_sk) is None:
                    raw_sid = _sk
            if raw_sid:
                adapter = self.adapters.get(Platform.API_SERVER)
                from gateway.wake import adapter_supports_push, deliver_wake
                if adapter is not None and not adapter_supports_push(adapter):
                    try:
                        logger.info(
                            "Watch pattern notification — waking api_server "
                            "session %s via self-post",
                            raw_sid,
                        )
                        await deliver_wake(adapter, text=synth_text, session_id=raw_sid)
                        return True
                    except Exception as e:
                        logger.warning(
                            "Watch notification self-post wake failed for "
                            "session %s: %s",
                            raw_sid, e,
                        )
                        return False
                logger.warning(
                    "Dropping watch notification for raw session %s: no "
                    "api_server adapter to self-post through",
                    raw_sid,
                )
                return None
            logger.warning(
                "Dropping watch notification with no routing metadata for process %s",
                evt.get("session_id", "unknown"),
            )
            return None
        platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        adapter = None
        for p, a in self.adapters.items():
            if p.value == platform_name:
                adapter = a
                break
        if not adapter:
            return None
        from gateway.wake import adapter_supports_push as _wake_push_ok
        if not _wake_push_ok(adapter):
            # Non-push adapter (api_server) resolved WITH routing metadata:
            # its chat_id is the raw session id (see _bind_api_server_session,
            # which binds chat_id = session_id). handle_message would run the
            # wake under a build_session_key()-derived key that never matches
            # the raw X-Hermes-Session-Id session — self-post instead.
            from gateway.wake import deliver_wake
            raw_sid = str(evt.get("origin_session_id") or "").strip() or str(source.chat_id or "")
            try:
                logger.info(
                    "Watch pattern notification — waking api_server session "
                    "%s via self-post",
                    raw_sid,
                )
                await deliver_wake(adapter, text=synth_text, session_id=raw_sid)
                return True
            except Exception as e:
                logger.warning(
                    "Watch notification self-post wake failed for session "
                    "%s: %s",
                    raw_sid, e,
                )
                return False
        try:
            metadata = {}
            parent_session_id = str(evt.get("parent_session_id") or "").strip()
            if parent_session_id:
                metadata["gateway_session_id"] = parent_session_id
            synth_event = MessageEvent(
                text=synth_text,
                message_type=MessageType.TEXT,
                source=source,
                internal=True,
                message_id=str(evt.get("message_id") or "").strip() or None,
                metadata=metadata,
            )
            logger.info(
                "Watch pattern notification — injecting for %s chat=%s thread=%s",
                platform_name,
                source.chat_id,
                source.thread_id,
            )
            await adapter.handle_message(synth_event)
            return True
        except Exception as e:
            logger.error("Watch notification injection error: %s", e)
            return False

    @staticmethod
    def _completion_delivery_identity(evt: dict) -> Optional[tuple[str, str, object]]:
        """Return a producer-stable identity when one is available.

        Delegation UUIDs identify one producer completion. Process session IDs
        are normally unique too, but include the persisted spawn epoch so an
        explicitly reused ID represents a distinct process incarnation. Legacy
        process events without ``started_at`` are delivered without deduplication
        rather than risking suppression of a real completion.
        """
        evt_type = str(evt.get("type") or "")
        if evt_type == "async_delegation":
            producer_id = str(evt.get("delegation_id") or "")
            return (evt_type, producer_id, "") if producer_id else None
        if evt_type == "completion":
            producer_id = str(evt.get("session_id") or "")
            started_at = evt.get("started_at")
            if producer_id and started_at is not None:
                return (evt_type, producer_id, started_at)
        return None

    async def _classify_completion_target(self, parent_session_id: str) -> str:
        """Classify an async-completion delivery target before adapter acceptance.

        Returns one of:

        - ``"deliver"`` — the spawning session is live, or ended by a
          compression rotation with a verified live continuation. The inner
          #55578 resolver (:meth:`_resolve_async_delegation_session`) still
          owns the actual route retarget; this pre-flight only proves the
          completion is deliverable so the durable ack stays honest.
        - ``"terminal"`` — the spawning session is gone for good (unknown, or
          ended at an explicit user boundary such as /new). Delivery can never
          succeed; the durable row should be terminally dropped rather than
          falsely acknowledged as delivered or replayed forever as pending.
        - ``"retry"`` — transient uncertainty (session DB unavailable, lookup
          error, or a compression rotation caught mid-flight before its
          continuation exists). The claim should be released so a later
          consumer can retry; the attempt cap bounds the churn.
        """
        session_db = getattr(self, "_session_db", None)
        if session_db is None:
            return "retry"
        try:
            parent = await session_db.get_session(parent_session_id)
        except Exception:
            logger.debug(
                "Async-completion pre-flight parent lookup failed for %s",
                parent_session_id, exc_info=True,
            )
            return "retry"
        if parent is None:
            return "terminal"
        if not parent.get("ended_at"):
            return "deliver"
        if parent.get("end_reason") != "compression":
            return "terminal"
        try:
            tip_session_id = await session_db.get_compression_tip(parent_session_id)
            if not tip_session_id or tip_session_id == parent_session_id:
                # Rotation caught mid-flight: parent is compression-ended but
                # its continuation isn't visible yet. Retry, don't drop.
                return "retry"
            tip = await session_db.get_session(tip_session_id)
        except Exception:
            logger.debug(
                "Async-completion pre-flight tip lookup failed for %s",
                parent_session_id, exc_info=True,
            )
            return "retry"
        if tip is None or tip.get("ended_at"):
            return "retry"
        return "deliver"

    async def _deliver_completion_notification(
        self, synth_text: str, evt: dict,
    ) -> Optional[bool]:
        """Deliver once per live gateway, or return False for a retry.

        ``True`` means this caller reached adapter acceptance, ``False`` means
        injection failed and the claim was released for retry, and ``None``
        means either another same-lifecycle caller owns/delivered the producer
        event or the event has no gateway route. No cross-process exactly-once
        guarantee is claimed.
        """
        identity = self._completion_delivery_identity(evt)
        durable_claim_id = ""
        durable_delegation_id = ""
        if evt.get("type") == "async_delegation":
            durable_delegation_id = str(evt.get("delegation_id") or "")
            if durable_delegation_id:
                try:
                    from tools.async_delegation import claim_completion_delivery

                    durable_claim_id = f"gateway:{id(self)}:{__import__('uuid').uuid4().hex}"
                    if not claim_completion_delivery(
                        durable_delegation_id, durable_claim_id,
                    ):
                        return None
                except Exception as exc:
                    logger.warning(
                        "Could not claim durable async completion %s: %s",
                        durable_delegation_id, exc,
                    )
                    return False
            parent_session_id = str(evt.get("parent_session_id") or "").strip()
            if parent_session_id:
                # Pre-flight (#65838-class): adapter acceptance is NOT proof of
                # delivery — the inner #55578 resolver can still fail closed
                # inside the message pipeline AFTER the adapter accepted, which
                # would falsely acknowledge the durable row as delivered.
                # Verify the target here, before acceptance, and give drops an
                # honest durable disposition.
                verdict = await self._classify_completion_target(parent_session_id)
                if verdict == "terminal":
                    logger.warning(
                        "Async delegation %s targets permanently-gone session %s; "
                        "terminally dropping delivery (result remains in the "
                        "delegation records).",
                        durable_delegation_id or "<legacy>", parent_session_id,
                    )
                    if durable_claim_id:
                        try:
                            from tools.async_delegation import drop_completion_delivery

                            drop_completion_delivery(
                                durable_delegation_id, durable_claim_id,
                            )
                        except Exception:
                            logger.debug(
                                "Could not drop durable completion claim",
                                exc_info=True,
                            )
                    return None
                if verdict == "retry":
                    if durable_claim_id:
                        try:
                            from tools.async_delegation import release_completion_delivery

                            release_completion_delivery(
                                durable_delegation_id, durable_claim_id,
                            )
                        except Exception:
                            logger.debug(
                                "Could not release durable completion claim",
                                exc_info=True,
                            )
                    return False
        if identity is not None:
            with self._completion_delivery_lock:
                if (
                    identity in self._completion_deliveries_inflight
                    or identity in self._completion_deliveries_delivered
                ):
                    return None
                self._completion_deliveries_inflight.add(identity)

        accepted = False
        try:
            injection_result = await self._inject_watch_notification(synth_text, evt)
            if injection_result is not True:
                return injection_result
            accepted = True

            if identity is not None:
                with self._completion_delivery_lock:
                    self._completion_deliveries_inflight.discard(identity)
                    self._completion_deliveries_delivered[identity] = None
                    while (
                        len(self._completion_deliveries_delivered)
                        > self._completion_delivery_retention
                    ):
                        self._completion_deliveries_delivered.popitem(last=False)

            # If the durable async-delegation producer branch is present, its
            # SQLite row remains the authoritative replay state. Acknowledge it
            # after adapter acceptance; this gateway keeps no parallel ledger.
            if durable_claim_id:
                try:
                    from tools.async_delegation import complete_completion_delivery

                    complete_completion_delivery(
                        durable_delegation_id, durable_claim_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "Could not acknowledge durable async completion %s: %s",
                        durable_delegation_id, exc,
                    )
            return True
        finally:
            if identity is not None and not accepted:
                with self._completion_delivery_lock:
                    self._completion_deliveries_inflight.discard(identity)
            if durable_claim_id and not accepted:
                try:
                    from tools.async_delegation import release_completion_delivery

                    release_completion_delivery(
                        durable_delegation_id, durable_claim_id,
                    )
                except Exception:
                    logger.debug("Could not release durable completion claim", exc_info=True)

    def _enrich_async_delegation_routing(self, evt: dict) -> None:
        """Fill platform/chat_id/thread_id/chat_type on an async-delegation event.

        Async-delegation completion events only carry ``session_key`` (the
        daemon worker has no access to the per-message routing metadata the
        terminal background watcher captures at spawn time). Parse the
        session_key into the routing fields ``_build_process_event_source``
        expects. Best-effort: a CLI-origin event (empty session_key) is left
        as-is and simply won't route on the gateway.
        """
        from gateway.run import _parse_session_key
        if evt.get("platform"):
            return  # already enriched
        parsed = _parse_session_key(evt.get("session_key", "") or "")
        if not parsed:
            return
        evt["platform"] = parsed.get("platform", "")
        evt["chat_type"] = parsed.get("chat_type", "")
        evt["chat_id"] = parsed.get("chat_id", "")
        if parsed.get("thread_id"):
            evt["thread_id"] = parsed["thread_id"]

    async def _async_delegation_watcher(self, interval: float = 2.0) -> None:
        """Drain async-delegation completions and inject them as new turns.

        Background subagents (``delegate_task(background=true)``) run on the
        async-delegation daemon executor — they have no per-process watcher
        task, so their completion events would only be seen by the post-turn
        queue drain. This watcher covers the IDLE case: when a background
        subagent finishes while no agent turn is running, its result still
        re-enters the originating session promptly.

        Mirrors the CLI's idle ``process_loop`` drain. Stays silent when the
        queue has nothing for us; ignores non-async event types (those are
        handled by ``_run_process_watcher`` / the post-turn drain).
        """
        from gateway.run import _format_gateway_process_notification
        await asyncio.sleep(3)  # let platforms finish connecting
        from tools.process_registry import process_registry as _pr
        while self._running:
            try:
                # Peek the queue for async-delegation events. We must NOT
                # consume watch/completion events here (other drains own them),
                # so requeue anything that isn't ours.
                requeue = []
                async_events = []
                while not _pr.completion_queue.empty():
                    try:
                        evt = _pr.completion_queue.get_nowait()
                    except Exception:
                        break
                    if evt.get("type") == "async_delegation":
                        async_events.append(evt)
                    else:
                        requeue.append(evt)
                for evt in requeue:
                    _pr.completion_queue.put(evt)
                for evt in async_events:
                    self._enrich_async_delegation_routing(evt)
                    synth_text = _format_gateway_process_notification(evt)
                    if not synth_text:
                        continue
                    try:
                        delivered = await self._deliver_completion_notification(synth_text, evt)
                        if delivered is False:
                            _pr.completion_queue.put(evt)
                    except Exception as e:
                        _pr.completion_queue.put(evt)
                        logger.error("Async delegation injection error: %s", e)
            except Exception as e:
                logger.debug("Async delegation watcher error: %s", e)
            await asyncio.sleep(interval)

    async def _run_process_watcher(self, watcher: dict) -> None:
        """
        Periodically check a background process and push updates to the user.

        Runs as an asyncio task. Stays silent when nothing changed.
        Auto-removes when the process exits or is killed.

        Notification mode (from ``display.background_process_notifications``):
          - ``all``    — running-output updates + final message
          - ``result`` — final completion message only
          - ``error``  — final message only when exit code != 0
          - ``off``    — no messages at all
        """
        from gateway.run import _non_conversational_metadata, _redact_gateway_user_facing_secrets
        from tools.process_registry import process_registry

        session_id = watcher["session_id"]
        interval = watcher["check_interval"]
        session_key = watcher.get("session_key", "")
        platform_name = watcher.get("platform", "")
        chat_id = watcher.get("chat_id", "")
        thread_id = watcher.get("thread_id", "")
        user_id = watcher.get("user_id", "")
        user_name = watcher.get("user_name", "")
        message_id = str(watcher.get("message_id") or "").strip() or None
        agent_notify = watcher.get("notify_on_complete", False)
        notify_mode = self._load_background_notifications_mode()

        logger.debug("Process watcher started: %s (every %ss, notify=%s, agent_notify=%s)",
                      session_id, interval, notify_mode, agent_notify)

        if notify_mode == "off" and not agent_notify:
            # Still wait for the process to exit so we can log it, but don't
            # push any messages to the user.
            while True:
                await asyncio.sleep(interval)
                session = process_registry.get(session_id)
                if session is None or session.exited:
                    break
            logger.debug("Process watcher ended (silent): %s", session_id)
            return

        last_output_len = 0
        while True:
            await asyncio.sleep(interval)

            session = process_registry.get(session_id)
            if session is None:
                break

            current_output_len = len(session.output_buffer)
            has_new_output = current_output_len > last_output_len
            last_output_len = current_output_len

            if session.exited:
                # --- Agent-triggered completion: inject synthetic message ---
                # Skip if the agent already consumed the result via wait/log.
                # poll() is read-only and intentionally does NOT mark consumed
                # (#10156) — a status check must not suppress this delivery turn.
                from tools.process_registry import format_process_notification, process_registry as _pr_check
                if agent_notify and not _pr_check.is_completion_consumed(session_id):
                    from agent.redact import redact_terminal_output
                    from tools.ansi_strip import strip_ansi
                    _command = getattr(session, "command", "") or ""
                    _raw = strip_ansi(session.output_buffer) if session.output_buffer else ""
                    _raw = redact_terminal_output(_raw, _command)
                    _command = _redact_gateway_user_facing_secrets(_command)
                    # Truncate at line boundaries so notifications never start
                    # mid-line (fixes #23284). Keep the last ~2000 chars but
                    # snap to the nearest preceding newline, then prepend a
                    # truncation marker when output was cut.
                    _LIMIT = 2000
                    if len(_raw) > _LIMIT:
                        _tail = _raw[-_LIMIT:]
                        _nl = _tail.find("\n")
                        _tail = _tail[_nl + 1:] if _nl != -1 else _tail
                        _out = f"[… output truncated — showing last {len(_tail)} chars]\n{_tail}"
                    else:
                        _out = _raw
                    completion_evt = {
                        "type": "completion",
                        "session_id": session_id,
                        "session_key": session_key,
                        "platform": platform_name,
                        "chat_type": watcher.get("chat_type", ""),
                        "chat_id": chat_id,
                        "thread_id": thread_id,
                        "user_id": user_id,
                        "user_name": user_name,
                        "message_id": message_id,
                        "started_at": getattr(session, "started_at", None),
                        "command": _command,
                        "exit_code": session.exit_code,
                        "completion_reason": getattr(session, "completion_reason", "exited"),
                        "termination_source": getattr(session, "termination_source", ""),
                        "output": _out,
                    }
                    synth_text = format_process_notification(completion_evt)
                    if not synth_text:
                        break
                    delivered = await self._deliver_completion_notification(
                        synth_text, completion_evt,
                    )
                    if delivered is False:
                        # The process remains terminal; retry after failed
                        # adapter injection instead of suppressing the result.
                        continue
                    break

                # --- Normal text-only notification ---
                # Skip when the agent already consumed this completion via
                # wait/log (#65379): process(wait) returned the exit code and
                # output inline, so the raw "[Background process ... finished
                # with exit code ...]" message would be a duplicate delivery
                # of the same completion. The agent_notify branch above
                # already honors _completion_consumed; without this check its
                # skip FALLS THROUGH to this block and re-delivers the output
                # the agent is actively summarizing. poll() is read-only and
                # intentionally does not mark consumed (#10156), so a status
                # check never suppresses this message.
                if _pr_check.is_completion_consumed(session_id):
                    logger.debug(
                        "Process watcher: completion for %s already consumed "
                        "via wait/log — skipping raw notification (#65379)",
                        session_id,
                    )
                    break
                # Decide whether to notify based on mode
                should_notify = (
                    notify_mode in {"all", "result"}
                    or (notify_mode == "error" and session.exit_code not in {0, None})
                )
                if should_notify:
                    new_output = session.output_buffer[-1000:] if session.output_buffer else ""
                    if new_output:
                        from agent.redact import redact_terminal_output
                        new_output = redact_terminal_output(
                            new_output, getattr(session, "command", "") or ""
                        )
                    message_text = (
                        f"[Background process {session_id} finished with exit code {session.exit_code}~ "
                        f"Here's the final output:\n{new_output}]"
                    )
                    adapter = None
                    for p, a in self.adapters.items():
                        if p.value == platform_name:
                            adapter = a
                            break
                    if adapter and chat_id:
                        try:
                            send_meta = {"thread_id": thread_id} if thread_id else None
                            await adapter.send(
                                chat_id,
                                message_text,
                                metadata=_non_conversational_metadata(send_meta, platform=platform_name),
                            )
                        except Exception as e:
                            logger.error("Watcher delivery error: %s", e)
                break

            elif has_new_output and notify_mode == "all" and not agent_notify:
                # New output available -- deliver status update (only in "all" mode)
                # Skip periodic updates for agent_notify watchers (they only care about completion)
                new_output = session.output_buffer[-500:] if session.output_buffer else ""
                if new_output:
                    from agent.redact import redact_terminal_output
                    new_output = redact_terminal_output(
                        new_output, getattr(session, "command", "") or ""
                    )
                message_text = (
                    f"[Background process {session_id} is still running~ "
                    f"New output:\n{new_output}]"
                )
                adapter = None
                for p, a in self.adapters.items():
                    if p.value == platform_name:
                        adapter = a
                        break
                if adapter and chat_id:
                    try:
                        send_meta = {"thread_id": thread_id} if thread_id else None
                        await adapter.send(
                            chat_id,
                            message_text,
                            metadata=_non_conversational_metadata(send_meta, platform=platform_name),
                        )
                    except Exception as e:
                        logger.error("Watcher delivery error: %s", e)

        logger.debug("Process watcher ended: %s", session_id)

    def _schedule_update_notification_watch(self) -> None:
        """Ensure a background task is watching for update completion."""
        existing_task = getattr(self, "_update_notification_task", None)
        if existing_task and not existing_task.done():
            return

        try:
            self._update_notification_task = asyncio.create_task(
                self._watch_update_progress()
            )
        except RuntimeError:
            logger.debug("Skipping update notification watcher: no running event loop")

    async def _watch_update_progress(
        self,
        poll_interval: float = 2.0,
        stream_interval: float = 4.0,
        timeout: float = 1800.0,
    ) -> None:
        """Watch ``hermes update --gateway``, streaming output + forwarding prompts.

        Polls ``.update_output.txt`` for new content and sends chunks to the
        user periodically.  Detects ``.update_prompt.json`` (written by the
        update process when it needs user input) and forwards the prompt to
        the messenger.  The user's next message is intercepted by
        ``_handle_message`` and written to ``.update_response``.
        """
        from gateway.run import _hermes_home, _non_conversational_metadata
        pending_path = _hermes_home / ".update_pending.json"
        claimed_path = _hermes_home / ".update_pending.claimed.json"
        output_path = _hermes_home / ".update_output.txt"
        exit_code_path = _hermes_home / ".update_exit_code"
        prompt_path = _hermes_home / ".update_prompt.json"

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        # Resolve the adapter and chat_id for sending messages
        adapter = None
        chat_id = None
        session_key = None
        metadata = None
        for path in (claimed_path, pending_path):
            if path.exists():
                try:
                    pending = json.loads(path.read_text(encoding="utf-8"))
                    platform_str = pending.get("platform")
                    chat_id = pending.get("chat_id")
                    chat_type = pending.get("chat_type")
                    session_key = pending.get("session_key")
                    thread_id = pending.get("thread_id")
                    message_id = pending.get("message_id")
                    if platform_str and chat_id:
                        platform = Platform(platform_str)
                        adapter = self.adapters.get(platform)
                        metadata = self._thread_metadata_for_target(
                            platform,
                            chat_id,
                            thread_id,
                            chat_type=chat_type,
                            reply_to_message_id=message_id,
                            adapter=adapter,
                        )
                        # Fallback session key if not stored (old pending files)
                        if not session_key:
                            session_key = f"{platform_str}:{chat_id}"
                    break
                except Exception:
                    pass

        if not adapter or not chat_id:
            logger.warning("Update watcher: cannot resolve adapter/chat_id, falling back to completion-only")
            # Fall back to completion-only: wait for the exit code and send the
            # final notification. _send_update_notification re-resolves the
            # adapter on every call, so when the target platform is still
            # reconnecting it returns False and keeps the markers. Keep polling
            # until it actually delivers (returns True) instead of giving up
            # after the first completion check — otherwise a platform that
            # reconnects a few seconds after completion never gets notified.
            while (pending_path.exists() or claimed_path.exists()) and loop.time() < deadline:
                if exit_code_path.exists() and await self._send_update_notification():
                    return
                await asyncio.sleep(poll_interval)
            if (pending_path.exists() or claimed_path.exists()) and not exit_code_path.exists():
                exit_code_path.write_text("124", encoding="utf-8")
                await self._send_update_notification()
            return

        def _strip_ansi(text: str) -> str:
            from tools.ansi_strip import strip_ansi
            return strip_ansi(text)

        bytes_sent = 0
        last_stream_time = loop.time()
        buffer = ""

        async def _flush_buffer() -> None:
            """Send buffered output to the user."""
            nonlocal buffer, last_stream_time
            if not buffer.strip():
                buffer = ""
                return
            # Chunk to fit message limits (Telegram: 4096, others: generous)
            clean = _strip_ansi(buffer).strip()
            buffer = ""
            last_stream_time = loop.time()
            if not clean:
                return
            # Split into chunks if too long
            max_chunk = 3500
            chunks = [clean[i:i + max_chunk] for i in range(0, len(clean), max_chunk)]
            for chunk in chunks:
                try:
                    await adapter.send(
                        chat_id,
                        f"```\n{chunk}\n```",
                        metadata=_non_conversational_metadata(metadata, platform=platform),
                    )
                except Exception as e:
                    logger.debug("Update stream send failed: %s", e)

        while loop.time() < deadline:
            # Check for completion
            if exit_code_path.exists():
                # Read any remaining output
                if output_path.exists():
                    try:
                        content = output_path.read_text(encoding="utf-8")
                        if len(content) > bytes_sent:
                            buffer += content[bytes_sent:]
                            bytes_sent = len(content)
                    except OSError:
                        pass
                await _flush_buffer()

                # Send final status
                try:
                    exit_code_raw = exit_code_path.read_text(encoding="utf-8").strip() or "1"
                    exit_code = int(exit_code_raw)
                    if exit_code == 0:
                        await adapter.send(
                            chat_id,
                            "✅ Hermes update finished.",
                            metadata=_non_conversational_metadata(metadata, platform=platform),
                        )
                    else:
                        await adapter.send(
                            chat_id,
                            "❌ Hermes update failed (exit code {}).".format(exit_code),
                            metadata=_non_conversational_metadata(metadata, platform=platform),
                        )
                    logger.info("Update finished (exit=%s), notified %s", exit_code, session_key)
                except Exception as e:
                    logger.warning("Update final notification failed: %s", e)

                # Cleanup
                for p in (pending_path, claimed_path, output_path,
                          exit_code_path, prompt_path):
                    p.unlink(missing_ok=True)
                (_hermes_home / ".update_response").unlink(missing_ok=True)
                _up_done = self._peek_session_state(session_key)
                if _up_done is not None:
                    _up_done.persistent.update_prompt_pending = False
                return

            # Check for new output
            if output_path.exists():
                try:
                    content = output_path.read_text(encoding="utf-8")
                    if len(content) > bytes_sent:
                        buffer += content[bytes_sent:]
                        bytes_sent = len(content)
                except OSError:
                    pass

            # Flush buffer periodically
            if buffer.strip() and (loop.time() - last_stream_time) >= stream_interval:
                await _flush_buffer()

            # Check for prompts — only forward if we haven't already sent
            # one that's still awaiting a response.  Without this guard the
            # watcher would re-read the same .update_prompt.json every poll
            # cycle and spam the user with duplicate prompt messages.
            _up_pending_state = (
                self._peek_session_state(session_key) if session_key else None
            )
            if (prompt_path.exists() and session_key
                    and not (
                        _up_pending_state is not None
                        and _up_pending_state.persistent.update_prompt_pending
                    )):
                try:
                    prompt_data = json.loads(prompt_path.read_text(encoding="utf-8"))
                    prompt_text = prompt_data.get("prompt", "")
                    default = prompt_data.get("default", "")
                    if prompt_text:
                        # Flush any buffered output first so the user sees
                        # context before the prompt
                        await _flush_buffer()
                        # Try platform-native buttons first (Discord, Telegram)
                        sent_buttons = False
                        if getattr(type(adapter), "send_update_prompt", None) is not None:
                            try:
                                await adapter.send_update_prompt(
                                    chat_id=chat_id,
                                    prompt=prompt_text,
                                    default=default,
                                    session_key=session_key,
                                    metadata=_non_conversational_metadata(metadata, platform=platform),
                                )
                                sent_buttons = True
                            except Exception as btn_err:
                                logger.debug("Button-based update prompt failed: %s", btn_err)
                        if not sent_buttons:
                            default_hint = f" (default: {default})" if default else ""
                            _p = getattr(adapter, "typed_command_prefix", "/")
                            await adapter.send(
                                chat_id,
                                f"⚕ **Update needs your input:**\n\n"
                                f"{prompt_text}{default_hint}\n\n"
                                f"Reply `{_p}approve` (yes) or `{_p}deny` (no), "
                                f"or type your answer directly.",
                                metadata=_non_conversational_metadata(metadata, platform=platform),
                            )
                        # Keep the prompt marker on disk until the user
                        # answers. If the gateway restarts mid-prompt, the
                        # next watcher can recover by re-forwarding it from
                        # disk. Duplicate sends in the same process are
                        # still suppressed by _update_prompt_pending.
                        self._session_state(
                            session_key
                        ).persistent.update_prompt_pending = True
                        # .update_response to continue — it doesn't re-check
                        logger.info("Forwarded update prompt to %s: %s", session_key, prompt_text[:80])
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Failed to read update prompt: %s", e)

            await asyncio.sleep(poll_interval)

        # Timeout
        if not exit_code_path.exists():
            logger.warning("Update watcher timed out after %.0fs", timeout)
            exit_code_path.write_text("124", encoding="utf-8")
            await _flush_buffer()
            try:
                await adapter.send(
                    chat_id,
                    "❌ Hermes update timed out after 30 minutes.",
                    metadata=_non_conversational_metadata(metadata, platform=platform),
                )
            except Exception:
                pass
            for p in (pending_path, claimed_path, output_path,
                      exit_code_path, prompt_path):
                p.unlink(missing_ok=True)
            (_hermes_home / ".update_response").unlink(missing_ok=True)
            _up_timeout_state = self._peek_session_state(session_key)
            if _up_timeout_state is not None:
                _up_timeout_state.persistent.update_prompt_pending = False

    async def _send_update_notification(self) -> bool:
        """If an update finished, notify the user.

        Returns False when the update is still running so a caller can retry
        later. Returns True after a definitive send/skip decision.

        This is the legacy notification path used when the streaming watcher
        cannot resolve the adapter (e.g. after a gateway restart where the
        platform hasn't reconnected yet).
        """
        from gateway.run import _hermes_home, _non_conversational_metadata
        pending_path = _hermes_home / ".update_pending.json"
        claimed_path = _hermes_home / ".update_pending.claimed.json"
        output_path = _hermes_home / ".update_output.txt"
        exit_code_path = _hermes_home / ".update_exit_code"

        if not pending_path.exists() and not claimed_path.exists():
            return False

        cleanup = True
        active_pending_path = claimed_path
        try:
            if pending_path.exists():
                try:
                    pending_path.replace(claimed_path)
                except FileNotFoundError:
                    if not claimed_path.exists():
                        return True
            elif not claimed_path.exists():
                return True

            pending = json.loads(claimed_path.read_text(encoding="utf-8"))
            platform_str = pending.get("platform")
            chat_id = pending.get("chat_id")
            chat_type = pending.get("chat_type")
            thread_id = pending.get("thread_id")
            message_id = pending.get("message_id")

            if not exit_code_path.exists():
                logger.info("Update notification deferred: update still running")
                cleanup = False
                active_pending_path = pending_path
                claimed_path.replace(pending_path)
                return False

            exit_code_raw = exit_code_path.read_text(encoding="utf-8").strip() or "1"
            exit_code = int(exit_code_raw)

            # Read the captured update output
            output = ""
            if output_path.exists():
                output = output_path.read_text(encoding="utf-8")

            # Resolve adapter
            platform = Platform(platform_str)
            adapter = self.adapters.get(platform)

            if not adapter and chat_id:
                # The update finished, but the target platform has not
                # reconnected yet (common right after the restart that
                # `hermes update` triggers). Treating "adapter missing" as a
                # definitive skip would delete the markers and silently lose the
                # completion notification — the user never learns whether the
                # update succeeded or timed out. Preserve the markers instead so
                # a later retry (the watcher poll loop, or the next gateway
                # startup) can deliver the result once the adapter is back.
                logger.info(
                    "Update notification deferred: %s adapter not connected yet",
                    platform_str,
                )
                cleanup = False
                active_pending_path = pending_path
                claimed_path.replace(pending_path)
                return False

            if adapter and chat_id:
                metadata = self._thread_metadata_for_target(
                    platform,
                    chat_id,
                    thread_id,
                    chat_type=chat_type,
                    reply_to_message_id=message_id,
                    adapter=adapter,
                )
                # Strip ANSI escape codes for clean display
                from tools.ansi_strip import strip_ansi
                output = strip_ansi(output).strip()
                if output:
                    if len(output) > 3500:
                        output = "…" + output[-3500:]
                    if exit_code == 0:
                        msg = f"✅ Hermes update finished.\n\n```\n{output}\n```"
                    else:
                        msg = f"❌ Hermes update failed.\n\n```\n{output}\n```"
                elif exit_code == 0:
                    msg = "✅ Hermes update finished successfully."
                else:
                    msg = "❌ Hermes update failed. Check the gateway logs or run `hermes update` manually for details."
                await adapter.send(
                    chat_id,
                    msg,
                    metadata=_non_conversational_metadata(metadata, platform=platform),
                )
                logger.info(
                    "Sent post-update notification to %s:%s (exit=%s)",
                    platform_str,
                    chat_id,
                    exit_code,
                )
        except Exception as e:
            logger.warning("Post-update notification failed: %s", e)
        finally:
            if cleanup:
                active_pending_path.unlink(missing_ok=True)
                claimed_path.unlink(missing_ok=True)
                output_path.unlink(missing_ok=True)
                exit_code_path.unlink(missing_ok=True)

        return True

    async def _send_restart_notification(self) -> Optional[tuple[str, str, Optional[str]]]:
        """Notify the chat that initiated /restart that the gateway is back."""
        from gateway.run import _hermes_home, _non_conversational_metadata
        notify_path = _hermes_home / ".restart_notify.json"
        if not notify_path.exists():
            return None

        try:
            data = json.loads(notify_path.read_text(encoding="utf-8"))
            platform_str = data.get("platform")
            chat_id = data.get("chat_id")
            chat_type = data.get("chat_type")
            thread_id = data.get("thread_id")
            message_id = data.get("message_id")

            if not platform_str or not chat_id:
                return None

            platform = Platform(platform_str)
            transport = resolve_delivery_transport(platform, self.config, self.adapters)
            if transport is None:
                logger.debug(
                    "Restart notification skipped: no live transport for %s",
                    platform_str,
                )
                return None

            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                logger.info(
                    "Restart notification suppressed: %s has gateway_restart_notification=false",
                    platform_str,
                )
                return None

            metadata = self._thread_metadata_for_target(
                platform,
                chat_id,
                thread_id,
                chat_type=chat_type,
                reply_to_message_id=message_id,
                adapter=transport.adapter,
            )
            if data.get("delivered_via_upstream_relay") is True:
                metadata = dict(metadata or {})
                if data.get("user_id"):
                    metadata["user_id"] = str(data["user_id"])
                if data.get("scope_id"):
                    metadata["scope_id"] = str(data["scope_id"])
            result = await transport.send(
                platform,
                str(chat_id),
                "♻ Gateway restarted successfully. Your session continues.",
                metadata=_non_conversational_metadata(metadata, platform=platform),
            )
            # adapter.send() catches provider errors (e.g. "Chat not found")
            # and returns SendResult(success=False) rather than raising, so
            # we must inspect the result before claiming success — otherwise
            # the log line is misleading and hides real delivery failures.
            if result is not None and getattr(result, "success", True) is False:
                logger.warning(
                    "Restart notification to %s:%s was not delivered: %s",
                    platform_str,
                    chat_id,
                    getattr(result, "error", "send returned success=False"),
                )
                return None

            logger.info(
                "Sent restart notification to %s:%s",
                platform_str,
                chat_id,
            )
            return str(platform_str), str(chat_id), str(thread_id) if thread_id else None
        except Exception as e:
            logger.warning("Restart notification failed: %s", e)
            return None
        finally:
            notify_path.unlink(missing_ok=True)

    async def _send_home_channel_startup_notifications(
        self,
        *,
        skip_targets: Optional[set[tuple[str, str, Optional[str]]]] = None,
    ) -> set[tuple[str, str, Optional[str]]]:
        """Notify configured home channels that the gateway is back online.

        The notification is best-effort and sent once per connected platform
        home channel. ``skip_targets`` lets startup avoid duplicate messages
        when a more specific restart notification is queued for the same chat.
        """
        from gateway.run import _non_conversational_metadata
        delivered: set[tuple[str, str, Optional[str]]] = set()
        skipped = skip_targets or set()
        message = "♻️ Gateway online — Hermes is back and ready."

        for platform, platform_cfg in self.config.platforms.items():
            home = platform_cfg.home_channel
            if not home or not home.chat_id:
                continue

            transport = resolve_delivery_transport(platform, self.config, self.adapters)
            if transport is None:
                continue

            if not platform_cfg.gateway_restart_notification:
                logger.info(
                    "Home-channel startup notification suppressed: %s has gateway_restart_notification=false",
                    platform.value,
                )
                continue

            target = (platform.value, str(home.chat_id), str(home.thread_id) if home.thread_id else None)
            if target in skipped or target in delivered:
                continue

            try:
                metadata = self._thread_metadata_for_target(
                    platform,
                    home.chat_id,
                    home.thread_id,
                    adapter=transport.adapter,
                )
                if transport.is_relay:
                    metadata = dict(metadata or {})
                    if home.user_id:
                        metadata["user_id"] = home.user_id
                    if home.scope_id:
                        metadata["scope_id"] = home.scope_id
                send_metadata = _non_conversational_metadata(metadata, platform=platform)
                if send_metadata is not None or transport.is_relay:
                    result = await transport.send(
                        platform,
                        str(home.chat_id),
                        message,
                        metadata=send_metadata,
                    )
                else:
                    result = await transport.adapter.send(str(home.chat_id), message)
                if result is not None and getattr(result, "success", True) is False:
                    logger.warning(
                        "Home-channel startup notification failed for %s:%s: %s",
                        platform.value,
                        home.chat_id,
                        getattr(result, "error", "send returned success=False"),
                    )
                    continue

                delivered.add(target)
                logger.info(
                    "Sent home-channel startup notification to %s:%s",
                    platform.value,
                    home.chat_id,
                )
            except Exception as exc:
                logger.warning(
                    "Home-channel startup notification failed for %s:%s: %s",
                    platform.value,
                    home.chat_id,
                    exc,
                )

        return delivered

    def _reset_notice_session_info(self, source: SessionSource) -> str:
        """Session-info block for the auto-reset notice, profile-scoped.

        When multiplexing, resolve model/provider/context inside the profile
        serving ``source`` — otherwise the banner advertises the base config's
        model while the session actually runs on the profile's (#59003).
        Mirrors ``_run_agent``'s gating so single-profile gateways never
        enter the scope.

        Call via ``asyncio.to_thread`` from async handlers: under the scope,
        resolution can do blocking work (credential refresh, context-length
        HTTP probes) that must not run on the event loop. The scope is entered
        inside this method, so contextvars behave correctly in the worker
        thread.
        """
        from gateway.run import _profile_runtime_scope
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                return self._format_session_info()
        return self._format_session_info()

    def _format_session_info(self) -> str:
        """Resolve current model config and return a formatted info block.

        Surfaces model, provider, context length, and endpoint so gateway
        users can immediately see if context detection went wrong (e.g.
        local models falling to the 128K default).
        """
        from gateway.run import _load_gateway_config, _resolve_gateway_model, _resolve_runtime_agent_kwargs
        from agent.model_metadata import get_model_context_length, DEFAULT_FALLBACK_CONTEXT

        model = _resolve_gateway_model()
        config_context_length = None
        provider = None
        base_url = None
        api_key = None
        custom_provs = None
        data = None
        configured_model = None
        configured_provider = None
        configured_base_url = None

        try:
            data = _load_gateway_config()
            if data:
                model_cfg = data.get("model", {})
                if isinstance(model_cfg, dict):
                    configured_model = model_cfg.get("default") or model_cfg.get("model")
                    raw_ctx = model_cfg.get("context_length")
                    if raw_ctx is not None:
                        try:
                            config_context_length = int(raw_ctx)
                        except (TypeError, ValueError):
                            pass
                    provider = model_cfg.get("provider") or None
                    base_url = model_cfg.get("base_url") or None
                    configured_provider = provider
                    configured_base_url = base_url
                try:
                    from hermes_cli.config import get_compatible_custom_providers
                    custom_provs = get_compatible_custom_providers(data)
                except Exception:
                    custom_provs = data.get("custom_providers")
        except Exception:
            pass

        # Resolve runtime credentials for probing
        try:
            runtime = _resolve_runtime_agent_kwargs()
            provider = runtime.get("provider") or provider
            base_url = runtime.get("base_url") or base_url
            api_key = runtime.get("api_key")
        except Exception:
            pass

        if config_context_length is not None:
            try:
                from hermes_cli.route_identity import should_clear_context_pin

                if should_clear_context_pin(
                    configured_model,
                    model,
                    configured_base_url,
                    base_url,
                    configured_provider,
                    provider,
                ):
                    config_context_length = None
            except Exception:
                config_context_length = None

        if config_context_length is None and custom_provs and base_url:
            try:
                from hermes_cli.config import get_custom_provider_context_length

                custom_ctx = get_custom_provider_context_length(
                    model=model,
                    base_url=base_url,
                    custom_providers=custom_provs,
                )
                if custom_ctx:
                    config_context_length = custom_ctx
            except Exception:
                pass

        context_length = get_model_context_length(
            model,
            base_url=base_url or "",
            api_key=api_key or "",
            config_context_length=config_context_length,
            provider=provider or "",
            custom_providers=custom_provs,
        )

        # Format context source hint
        if config_context_length is not None:
            ctx_source = "config"
        elif context_length == DEFAULT_FALLBACK_CONTEXT:
            ctx_source = "default — set model.context_length in config to override"
        else:
            ctx_source = "detected"

        # Format context length for display
        if context_length >= 1_000_000:
            ctx_display = f"{context_length / 1_000_000:.1f}M"
        elif context_length >= 1_000:
            ctx_display = f"{context_length // 1_000}K"
        else:
            ctx_display = str(context_length)

        lines = [
            f"◆ Model: `{model}`",
            f"◆ Provider: {provider or 'openrouter'}",
            f"◆ Context: {ctx_display} tokens ({ctx_source})",
        ]

        # Show endpoint for local/custom setups
        if base_url and ("localhost" in base_url or "127.0.0.1" in base_url or "0.0.0.0" in base_url):
            lines.append(f"◆ Endpoint: {base_url}")

        return "\n".join(lines)
