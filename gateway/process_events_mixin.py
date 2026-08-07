"""Background-process event methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, wave 1).
Holds the process-event cluster: event-source resolution, watch
notification injection, completion classification/delivery, async
delegation routing, and the two watcher loops.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``_parse_session_key``, ``_format_gateway_process_notification``,
``_non_conversational_metadata`` and
``_redact_gateway_user_facing_secrets`` stay in ``gateway/run.py`` (shared
with staying methods) and are imported lazily inside the methods that use
them. The module-level ``logger`` is ``logging.getLogger("gateway.run")``
so log records keep the exact name.
"""


from __future__ import annotations

import asyncio
import logging

from typing import Optional

from gateway.config import Platform, _BUILTIN_PLATFORM_VALUES
from gateway.platforms.base import MessageEvent, MessageType

logger = logging.getLogger("gateway.run")


class GatewayProcessEventsMixin:

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

