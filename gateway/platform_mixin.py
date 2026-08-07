"""Platform adapter lifecycle methods for ``GatewayRunner``.

Extracted verbatim from ``gateway/run.py`` (god-file decomposition campaign,
Phase 3 mechanical mixin lifts). This mixin holds the platform-adapter
cluster: adapter connect/disconnect/teardown timeouts, fatal-error handling,
the reconnect watcher, secondary-profile (multiplex) adapter startup,
adapter credential/listener claim conflict detection, the handoff watcher,
and per-profile name resolution.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. Neutral dependencies import at
module top; helpers that stay in ``gateway/run.py`` (``_profile_runtime_scope``,
``_reconnect_backoff``, ``_dispose_unused_adapter``, ``MultiplexConfigError``,
the timeout defaults, ...) are imported lazily inside the using method (a
deferred ``from gateway.run import ...`` resolves at call time, when
``gateway.run`` is fully loaded) so this module never imports ``gateway.run``
at import time -> no import cycle. The module-level ``logger`` keeps the
original logger name (``"gateway.run"``) so log records are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from agent.async_utils import consume_detached_task_result
from agent.i18n import t
from gateway.config import (
    Platform,
    platform_binds_port as _platform_binds_port,
)
from gateway.delivery import (
    looks_like_telegram_private_chat_id,
    resolve_delivery_transport,
)
from gateway.platforms.base import BasePlatformAdapter
from gateway.session import SessionSource

logger = logging.getLogger("gateway.run")


class GatewayPlatformMixin:
    async def _await_adapter_cleanup_with_timeout(
        self, awaitable: Awaitable[Any], timeout: float
    ) -> bool:
        """Wait for adapter cleanup without letting cancellation swallowing hang us.

        ``asyncio.wait_for`` cancels an overdue child but then waits for it to
        exit. An adapter close path that catches ``CancelledError`` can therefore
        block recovery forever. Keep ownership of the old task through its done
        callback, but release the runner at the deadline.
        """
        if timeout <= 0:
            await awaitable
            return True

        task = asyncio.ensure_future(awaitable)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(consume_detached_task_result)
            raise
        if task in done:
            await task
            return True

        task.cancel()
        task.add_done_callback(consume_detached_task_result)
        return False

    async def _safe_adapter_disconnect(self, adapter, platform) -> None:
        """Call adapter.disconnect() defensively, swallowing any error.

        Used when adapter.connect() failed or raised — the adapter may
        have allocated partial resources (aiohttp.ClientSession, poll
        tasks, child subprocesses) that would otherwise leak and surface
        as "Unclosed client session" warnings at process exit.

        Must tolerate partial-init state and never raise, since callers
        use it inside error-handling blocks.
        """
        timeout = self._adapter_disconnect_timeout_secs()
        try:
            completed = await self._await_adapter_cleanup_with_timeout(
                adapter.disconnect(), timeout
            )
            if not completed:
                logger.warning(
                    "Timed out after %.1fs while disconnecting %s adapter; continuing shutdown",
                    timeout,
                    platform.value if platform is not None else "adapter",
                )
        except Exception as e:
            logger.debug(
                "Defensive %s disconnect after failed connect raised: %s",
                platform.value if platform is not None else "adapter",
                e,
            )

    async def _bounded_adapter_teardown(
        self, adapter, platform, *, profile: Optional[str] = None
    ) -> None:
        """Tear down one adapter on the shutdown path with bounded awaits.

        Both ``cancel_background_tasks()`` and ``disconnect()`` can block
        indefinitely when a platform's network state is half-dead (e.g. a
        wedged Feishu/Lark WebSocket thread waiting on I/O). An unbounded
        await here stalls the entire shutdown sequence past systemd's
        ``TimeoutStopSec``; the resulting SIGKILL skips ``atexit`` PID-file
        cleanup, so the next start dies with "PID file race lost" (#14128).

        Each await uses the existing per-adapter timeout budget
        (``HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT``). On timeout the old
        task is cancelled and detached, then teardown forces forward progress;
        the loop never hangs even if an adapter swallows cancellation. Never
        raises.
        """
        timeout = self._adapter_disconnect_timeout_secs()
        suffix = f" (profile: {profile})" if profile else ""
        started_at = time.monotonic()
        try:
            cancelled = await self._await_adapter_cleanup_with_timeout(
                adapter.cancel_background_tasks(), timeout
            )
            if not cancelled:
                logger.warning(
                    "✗ %s background-task cancel timed out after %.1fs - forcing continue%s",
                    platform.value, timeout, suffix,
                )
        except Exception as e:
            logger.debug("✗ %s background-task cancel error%s: %s", platform.value, suffix, e)
        try:
            disconnected = await self._await_adapter_cleanup_with_timeout(
                adapter.disconnect(), timeout
            )
            if disconnected:
                logger.info(
                    "✓ %s disconnected (%.2fs)%s",
                    platform.value, time.monotonic() - started_at, suffix,
                )
            else:
                logger.warning(
                    "✗ %s disconnect timed out after %.1fs - forcing continue%s",
                    platform.value, timeout, suffix,
                )
        except Exception as e:
            logger.error(
                "✗ %s disconnect error after %.2fs%s: %s",
                platform.value, time.monotonic() - started_at, suffix, e,
            )

    def _adapter_disconnect_timeout_secs(self) -> float:
        """Return the per-adapter disconnect timeout used during shutdown."""
        from gateway.run import _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT

        raw = os.getenv("HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_ADAPTER_DISCONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        return _ADAPTER_DISCONNECT_TIMEOUT_SECS_DEFAULT

    def _platform_connect_timeout_secs(self, platform=None) -> float:
        """Return the per-platform connect timeout used during startup/retry."""
        from gateway.run import (
            _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT,
            _TELEGRAM_CONNECT_TIMEOUT_SECS_DEFAULT,
        )

        raw = os.getenv("HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT", "").strip()
        if raw:
            try:
                timeout = float(raw)
            except ValueError:
                logger.warning(
                    "Ignoring invalid HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT=%r",
                    raw,
                )
            else:
                return max(0.0, timeout)
        if platform == Platform.TELEGRAM:
            return _TELEGRAM_CONNECT_TIMEOUT_SECS_DEFAULT
        return _PLATFORM_CONNECT_TIMEOUT_SECS_DEFAULT

    async def _connect_adapter_with_timeout(
        self, adapter, platform, *, is_reconnect: bool = False
    ) -> bool:
        """Connect an adapter without allowing one platform to block others.

        ``is_reconnect`` is forwarded to ``adapter.connect()`` so platform
        adapters can distinguish a cold first boot (drop any stale
        server-side queue) from a watcher reconnect after a prolonged outage
        (preserve the queue so messages sent during the outage are delivered
        rather than silently dropped — #46621).
        """
        timeout = self._platform_connect_timeout_secs(platform)
        if timeout <= 0:
            return await adapter.connect(is_reconnect=is_reconnect)
        # Use the detach-on-timeout pattern instead of plain asyncio.wait_for:
        # asyncio.wait_for cancels the overdue task but then waits for it to
        # exit. An adapter connect() that catches CancelledError can therefore
        # block recovery forever (the watcher never reaches the next retry).
        # Keep ownership of the old task through its done callback, but
        # release the runner at the deadline (#70344).
        task = asyncio.ensure_future(
            adapter.connect(is_reconnect=is_reconnect)
        )
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(consume_detached_task_result)
            raise
        if task in done:
            result = await task
            return bool(result)
        task.cancel()
        task.add_done_callback(consume_detached_task_result)
        raise TimeoutError(
            f"{platform.value} connect timed out after {timeout:g}s"
        )

    async def _connect_initial_adapter_with_timeout(self, adapter, platform) -> bool:
        """Connect one cold-start adapter with tightly scoped replace intent.

        The capability is visible only while this initial connect is awaited.
        Reconnects call ``_connect_adapter_with_timeout`` directly and adapters
        also default to deny, so a later network recovery can never evict a
        healthy token holder.
        """
        adapter._platform_lock_takeover_allowed = bool(
            self._platform_lock_takeover_on_start
        )
        try:
            return await self._connect_adapter_with_timeout(adapter, platform)
        finally:
            adapter._platform_lock_takeover_allowed = False

    async def _handle_adapter_fatal_error(self, adapter: BasePlatformAdapter) -> None:
        """React to an adapter failure after startup.

        If the error is retryable (e.g. network blip, DNS failure), queue the
        platform for background reconnection instead of giving up permanently.

        The notification arrives on the failing adapter's own polling task,
        and the disconnect inside the handler can cancel that task mid-flight:
        disconnect()'s current-task guard misses it because
        _safe_adapter_disconnect runs the close in a wrapper task. A cancelled
        handler dies between the fatal log and the reconnect queue, silently
        stranding the platform (observed 2026-07-21: telegram popped from
        adapters but never queued after a travel network outage). Run the real
        work in a detached task that adapter teardown cannot cancel.
        """
        tasks = getattr(self, "_fatal_handler_tasks", None)
        if tasks is None:
            tasks = self._fatal_handler_tasks = set()
        task = asyncio.create_task(self._handle_adapter_fatal_error_detached(adapter))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        # Await so callers that expect completion still get it — but through
        # shield(): Task.cancel() on the caller also cancels the future it is
        # awaiting (_fut_waiter), so a plain `await task` would tunnel the
        # cancellation straight into the "detached" task. shield() absorbs
        # it: the caller sees CancelledError, the handler runs to completion.
        await asyncio.shield(task)

    async def _handle_adapter_fatal_error_detached(
        self, adapter: BasePlatformAdapter
    ) -> None:
        """Run the fatal handler; if the platform still ends up stranded
        (not reconnected, not queued, not intentionally disabled), exit the
        gateway with failure so the service manager restarts it instead of
        leaving a silent partial outage."""
        try:
            await self._handle_adapter_fatal_error_impl(adapter)
        except Exception:
            logger.exception(
                "Fatal-error handling for %s raised unexpectedly",
                adapter.platform.value,
            )
        finally:
            platform = adapter.platform
            shutdown_event = getattr(self, "_shutdown_event", None)
            stranded = (
                adapter.fatal_error_retryable
                and platform not in self.adapters
                and platform not in getattr(self, "_failed_platforms", {})
                and not (shutdown_event is not None and shutdown_event.is_set())
            )
            if stranded:
                logger.error(
                    "%s adapter was lost without entering the reconnection "
                    "queue; exiting gateway so the service manager restarts it.",
                    platform.value,
                )
                self._exit_reason = (
                    f"{platform.value} adapter lost without reconnection queue"
                )
                self._exit_with_failure = True
                await self.stop()

    async def _handle_adapter_fatal_error_impl(self, adapter: BasePlatformAdapter) -> None:
        # Snapshot the current owner of this platform slot before doing
        # anything else. If it's neither this adapter nor empty, a different
        # adapter has already taken over (e.g. this is a delayed notification
        # from a background retry chain that raced with, and lost to, a
        # reconnect that already succeeded). Acting on a stale notification
        # would overwrite an already-healthy platform's runtime status and
        # incorrectly re-queue it for reconnection, so bail out before any of
        # that happens.
        existing = self.adapters.get(adapter.platform)
        if existing is not None and existing is not adapter:
            logger.debug(
                "Ignoring stale fatal error from a superseded %s adapter instance: %s",
                adapter.platform.value,
                adapter.fatal_error_code or "unknown",
            )
            return

        logger.error(
            "Fatal %s adapter error (%s): %s",
            adapter.platform.value,
            adapter.fatal_error_code or "unknown",
            adapter.fatal_error_message or "unknown error",
        )
        # Phase 7 Unit 7d-B: a relay credential revoked by opt-out is not an
        # error to retry — render it as a clean "disabled" state, not red
        # "fatal"/"retrying". (The code is set non-retryable, so it also drops
        # out of the reconnect queue below.)
        if adapter.fatal_error_code == "relay_disabled":
            platform_state = "disabled"
        elif adapter.fatal_error_retryable:
            platform_state = "retrying"
        else:
            platform_state = "fatal"
        self._update_platform_runtime_status(
            adapter.platform.value,
            platform_state=platform_state,
            error_code=adapter.fatal_error_code,
            error_message=adapter.fatal_error_message,
        )

        if existing is adapter:
            # Claim this adapter for teardown before awaiting disconnect() —
            # a second fatal-error notification for the same adapter (e.g.
            # from a concurrent recovery path) would otherwise still see
            # itself as "existing" during the await below and disconnect()
            # the same object twice.
            self.adapters.pop(adapter.platform, None)
            self.delivery_router.adapters = self.adapters
            # A half-closed transport can wedge an adapter's native close()
            # indefinitely. Reuse the shutdown-path timeout so this runtime
            # fatal handler always reaches the reconnect queue.
            await self._safe_adapter_disconnect(adapter, adapter.platform)

        # Queue retryable failures for background reconnection
        if adapter.fatal_error_retryable:
            platform_config = self.config.platforms.get(adapter.platform)
            if platform_config and adapter.platform not in self._failed_platforms:
                self._failed_platforms[adapter.platform] = {
                    "config": platform_config,
                    "attempts": 0,
                    "next_retry": time.monotonic(),
                }
                logger.info(
                    "%s queued for background reconnection",
                    adapter.platform.value,
                )
                # Ensure the reconnect watcher is alive — if it died (e.g. from
                # exhausting its restart budget), respawn it so queued platforms
                # are not permanently stranded (#70344).
                self._ensure_reconnect_watcher_running()

        if not self.adapters and not self._failed_platforms:
            self._exit_reason = adapter.fatal_error_message or "All messaging adapters disconnected"
            if adapter.fatal_error_retryable:
                self._exit_with_failure = True
                logger.error("No connected messaging platforms remain. Shutting down gateway for service restart.")
            else:
                logger.error("No connected messaging platforms remain. Shutting down gateway cleanly.")
            await self.stop()
        elif not self.adapters and self._failed_platforms:
            # All platforms are down and queued for background reconnection.
            # Keep the gateway alive so:
            #   • cron jobs still run
            #   • the reconnect watcher can recover platforms when the
            #     underlying problem clears (proxy comes back, user runs
            #     `hermes whatsapp`, etc.)
            # We used to exit-with-failure here to trigger systemd restart,
            # but that converted a transient outage into a restart loop and
            # killed in-process state every time. The reconnect watcher
            # already handles long-running recovery — let it do its job.
            logger.warning(
                "No connected messaging platforms remain, but %d platform(s) "
                "queued for reconnection — gateway staying alive, watcher will "
                "retry in background.",
                len(self._failed_platforms),
            )

    async def _handoff_watcher(self, interval: float = 2.0) -> None:
        """Background task that processes pending CLI→gateway session handoffs.

        Polls ``state.db`` for sessions in ``handoff_state='pending'`` and,
        for each one:

        1. Atomically claims it (pending → running).
        2. Resolves the destination platform's configured home channel.
        3. Re-binds the gateway's session_key for that home channel to the
           CLI's existing session_id via ``session_store.switch_session`` so
           the full role-aware transcript replays on the next agent turn.
        4. Forges a synthetic ``MessageEvent`` (``internal=True``) with a
           handoff-notice text and dispatches through the normal gateway
           message pipeline so the agent runs and replies on the platform.
        5. Marks the row ``completed`` (or ``failed`` with ``handoff_error``).

        The CLI process is poll-blocked on the row's terminal state and
        prints the result to the user.
        """
        # Initial delay so the gateway is fully connected to its platforms
        # before we try to dispatch handoffs through them.
        await asyncio.sleep(5)
        while self._running:
            try:
                if self._session_db is None:
                    await asyncio.sleep(interval)
                    continue
                pending = await self._session_db.list_pending_handoffs()
                for row in pending:
                    session_id = row.get("id")
                    if not session_id:
                        continue
                    if not await self._session_db.claim_handoff(session_id):
                        # Another tick or another gateway already claimed it.
                        continue
                    try:
                        await self._process_handoff(row)
                        await self._session_db.complete_handoff(session_id)
                    except Exception as exc:
                        logger.warning(
                            "Handoff for session %s failed: %s",
                            session_id, exc, exc_info=True,
                        )
                        await self._session_db.fail_handoff(session_id, str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Handoff watcher tick error: %s", exc, exc_info=True)
            await asyncio.sleep(interval)

    async def _process_handoff(self, row: Dict[str, Any]) -> None:
        """Execute one handoff row. Raises on failure (caller marks failed)."""
        from gateway.config import Platform
        from gateway.session import SessionSource, build_session_key
        from gateway.platforms.base import MessageEvent

        cli_session_id = row["id"]
        platform_name = (row.get("handoff_platform") or "").strip().lower()
        if not platform_name:
            raise RuntimeError("handoff_platform is empty")

        # Resolve platform enum
        try:
            platform = Platform(platform_name)
        except (ValueError, KeyError):
            raise RuntimeError(f"unknown platform '{platform_name}'")

        # Adapter must be live. A relay-fronted gateway registers ONE adapter
        # under Platform.RELAY that fronts N logical platforms — so a literal
        # adapters.get(discord) misses even though "discord" is deliverable.
        # resolve_delivery_transport is the shared alias-aware resolver (native
        # adapter wins; relay eligible only when its authenticated transport
        # advertises it fronts the logical platform).
        transport = resolve_delivery_transport(platform, self.config, self.adapters)
        if not transport:
            raise RuntimeError(
                f"platform '{platform_name}' is not active in this gateway"
            )
        adapter = transport.adapter

        # Home channel must be configured
        home = self.config.get_home_channel(platform)
        if not home or not home.chat_id:
            raise RuntimeError(
                f"no home channel configured for {platform_name}; "
                f"run /sethome on the desired chat first"
            )

        cli_title = row.get("title") or cli_session_id[:8]

        # Try to create a fresh thread on the destination so the handoff
        # has its own scrollback. Adapter returns None if threading isn't
        # supported (Matrix/WhatsApp/Signal/SMS) or if creation failed
        # (no permission, topics-mode off, parent is a DM, etc.). When
        # None we fall through to using the home channel directly — the
        # synthetic turn still lands; just without thread isolation.
        thread_name = f"Hermes — {cli_title}"
        try:
            new_thread_id = await adapter.create_handoff_thread(
                str(home.chat_id), thread_name,
            )
        except Exception as exc:
            logger.debug(
                "Handoff: create_handoff_thread raised on %s: %s",
                platform_name, exc, exc_info=True,
            )
            new_thread_id = None

        # Use the new thread if the adapter created one; otherwise fall
        # back to whatever thread (if any) the home channel was configured
        # with.
        effective_thread_id = new_thread_id or (
            str(home.thread_id) if home.thread_id else None
        )

        # Determine chat_type/user_id for the destination source.
        #
        # Telegram private-chat DM topics are represented differently from
        # group/forum threads by the inbound adapter. A handoff-created topic
        # in a positive Telegram chat_id must therefore use the same DM-topic
        # source shape as the user's next real message; otherwise the synthetic
        # handoff turn binds a generic `thread` session key while real replies
        # arrive on a `dm` session key.
        home_chat_id = str(home.chat_id)
        is_telegram_private_chat = (
            platform == Platform.TELEGRAM
            and looks_like_telegram_private_chat_id(home_chat_id)
        )

        if new_thread_id and not is_telegram_private_chat:
            dest_chat_type = "thread"
            dest_user_id = "system:handoff"
        else:
            # No thread — assume DM-style for the home channel. For Telegram
            # private-chat topics, use the real user id (same as chat_id) so
            # topic-mode checks and binding persistence see the same identity as
            # subsequent inbound user messages.
            dest_chat_type = "dm"
            dest_user_id = home_chat_id if is_telegram_private_chat else "system:handoff"

        # Discord thread destinations must key on the thread's OWN id, not the
        # parent channel's, because the Discord adapter builds organic in-thread
        # messages with ``chat_id == thread id`` — so ``build_session_key``
        # yields ``…:thread:{thread}:{thread}``. If the handoff keys on the
        # parent channel (``…:thread:{parent}:{thread}``) the next real user
        # reply in the thread resolves to a DIFFERENT session_key and spawns a
        # fresh session instead of continuing the handed-off one.
        #
        # This is Discord-specific: Slack and Telegram adapters key organic
        # thread messages with ``chat_id == parent_channel`` and the thread
        #/topic id only in ``thread_id``, so for those platforms the parent
        # channel is correct (and the deeper chat_type normalization — handoff
        # uses "thread" but Slack organic uses "group" — is a separate issue).
        if platform == Platform.DISCORD and dest_chat_type == "thread" and effective_thread_id:
            dest_chat_id = str(effective_thread_id)
        else:
            dest_chat_id = home_chat_id
        dest_source = SessionSource(
            platform=platform,
            chat_id=dest_chat_id,
            chat_name=home.name,
            chat_type=dest_chat_type,
            user_id=dest_user_id,
            user_name="Handoff",
            thread_id=effective_thread_id,
        )

        # Compute the gateway's session_key for that destination using the
        # same rules its adapters use, so switch_session targets the right
        # entry. For thread destinations build_session_key keys without
        # user_id (thread_sessions_per_user defaults to False) — so the
        # next real user message in the thread shares this same session.
        platform_cfg = self.config.platforms.get(platform)
        extra = platform_cfg.extra if platform_cfg else {}
        session_key = build_session_key(
            dest_source,
            group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
        )

        # Make sure there's an entry in the session_store for this key. If
        # the home channel has never been used, get_or_create_session
        # creates one; switch_session then re-points it.
        await self.async_session_store.get_or_create_session(dest_source)

        # Re-bind the destination key to the CLI session_id. switch_session
        # ends the prior session in SQLite and reopens the CLI session under
        # the new key. The CLI's transcript becomes the active one for the
        # gateway from this moment on.
        switched = await self.async_session_store.switch_session(session_key, cli_session_id)
        if switched is None:
            raise RuntimeError(
                f"could not switch session key {session_key} → {cli_session_id}"
            )

        # Evict any cached AIAgent for this session_key so the next dispatch
        # rebuilds it against the CLI session_id (mirrors /resume / /branch).
        self._evict_cached_agent(session_key)

        # Cancel any in-flight running-agent state for the destination key
        # so the synthetic turn isn't queued behind a stale running flag.
        self._release_running_agent_state(session_key)

        synthetic_text = (
            f"[Session was just handed off from CLI (\"{cli_title}\") to this "
            f"channel. The full prior conversation history is loaded above. "
            f"Briefly confirm you're working here and summarize what we were "
            f"working on, so the user can continue from this device.]"
        )

        synthetic_event = MessageEvent(
            text=synthetic_text,
            source=dest_source,
            internal=True,
        )

        logger.info(
            "Handoff: dispatching synthetic turn for CLI session %s → %s "
            "(home=%s, thread=%s, session_key=%s)",
            cli_session_id, platform_name, home.chat_id, effective_thread_id,
            session_key,
        )

        # Dispatch through the runner directly. Going through
        # adapter.handle_message would spawn a background task and we'd
        # lose synchronous error visibility; calling _handle_message inline
        # keeps the success/failure path observable for the watcher.
        response_text = await self._handle_message(synthetic_event)
        if not response_text:
            # Streaming may have already delivered the response inline.
            # Either way, agent ran without raising — count as success.
            return

        # Send the agent's reply to the destination. Route to the new
        # thread if we created one; otherwise the configured home channel
        # (which may itself carry a thread_id). Send through the resolved
        # transport (not adapter.send directly) so a relay-fronted logical
        # platform is stamped on the outbound frame (send_for_platform).
        send_metadata: Dict[str, Any] = {}
        if effective_thread_id:
            send_metadata["thread_id"] = effective_thread_id
        try:
            result = await transport.send(
                platform,
                str(home.chat_id),
                response_text,
                send_metadata or None,
            )
        except Exception as exc:
            raise RuntimeError(f"adapter.send failed: {exc}") from exc

        if not getattr(result, "success", True):
            err = getattr(result, "error", "send returned success=False")
            raise RuntimeError(f"adapter.send failed: {err}")

    def _active_profile_name(self) -> str:
        """Return the profile name this gateway represents."""
        try:
            from hermes_cli.profiles import get_active_profile_name
            return get_active_profile_name() or "default"
        except Exception:
            return "default"

    def _ensure_reconnect_watcher_running(self) -> None:
        """Ensure the platform reconnect watcher background task is alive.

        If the tracked reconnect watcher task has died (e.g. from exhausting
        its restart budget, or a terminal exception that _spawn_supervised
        could not recover), respawns it so platforms queued for reconnection
        are not permanently stranded. Called after queueing a retryable fatal
        error in _handle_adapter_fatal_error (#70344).
        """
        if not getattr(self, "_running", False):
            return
        task = getattr(self, "_reconnect_watcher_task", None)
        if task is not None and not task.done():
            return  # already alive
        logger.warning(
            "Reconnect watcher task is dead (done=%s) — respawning",
            task.done() if task is not None else "N/A",
        )
        self._reconnect_watcher_task = self._spawn_supervised(
            self._platform_reconnect_watcher,
            "platform_reconnect_watcher",
            on_spawn=lambda t: setattr(self, "_reconnect_watcher_task", t),
        )

    async def _platform_reconnect_watcher(self) -> None:
        """Background task that periodically retries connecting failed platforms.

        Uses exponential backoff: 30s → 60s → 120s → 240s → 300s (cap).
        Retryable failures (network/DNS blips) keep retrying at the backoff
        cap indefinitely — they self-heal once connectivity returns, so a
        transient outage never requires manual intervention. Non-retryable
        failures (bad auth, etc.) drop out of the queue immediately. The
        circuit breaker (``_pause_failed_platform`` / ``/platform pause``)
        remains available for manual operator control via ``/platform list``
        and ``/platform resume <name>``, but is no longer triggered
        automatically — auto-pausing a recovered platform was the cause of
        bots silently staying dead after a transient DNS failure.
        """
        from gateway.run import (
            _dispose_unused_adapter,
            _platform_has_bot_credential,
            _reconnect_backoff,
        )

        await asyncio.sleep(10)  # initial delay — let startup finish
        while self._running:
            if not self._failed_platforms:
                # Nothing to reconnect — sleep and check again
                for _ in range(30):
                    if not self._running:
                        return
                    if self._failed_platforms:
                        break
                    await asyncio.sleep(1)
                continue

            now = time.monotonic()
            for platform in list(self._failed_platforms.keys()):
                if not self._running:
                    return
                info = self._failed_platforms.get(platform)
                if info is None:
                    # Removed concurrently (e.g. a manual /platform resume,
                    # or a reconnect that succeeded via a different path)
                    # between the snapshot above and this lookup. Not an
                    # error -- just nothing to do for it this pass.
                    continue
                # Skip paused platforms entirely — they need explicit
                # /platform resume to come back.
                if info.get("paused"):
                    continue
                if now < info["next_retry"]:
                    continue  # not time yet

                platform_config = info["config"]
                attempt = info["attempts"] + 1
                # Empty-token primary configs can never reconnect; drop them so
                # multiplex setups where a secondary profile owns the bot do
                # not spin forever (#64674).
                if not _platform_has_bot_credential(platform, platform_config):
                    logger.warning(
                        "Reconnect %s: no bot credential on queued config, "
                        "removing from retry queue",
                        platform.value,
                    )
                    del self._failed_platforms[platform]
                    continue
                logger.info(
                    "Reconnecting %s (attempt %d)...",
                    platform.value, attempt,
                )

                adapter = None
                try:
                    adapter = self._create_adapter(platform, platform_config)
                    if not adapter:
                        logger.warning(
                            "Reconnect %s: adapter creation returned None, removing from retry queue",
                            platform.value,
                        )
                        del self._failed_platforms[platform]
                        continue

                    adapter.set_message_handler(self._primary_message_handler())
                    adapter.set_fatal_error_handler(self._handle_adapter_fatal_error)
                    adapter.set_session_store(self.session_store)
                    adapter.set_busy_session_handler(self._handle_active_session_busy_message)
                    _set_reaction = getattr(adapter, "set_reaction_handler", None)
                    if callable(_set_reaction):
                        _set_reaction(self._handle_reaction_event)
                    adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
                    adapter.set_authorization_check(self._make_adapter_auth_check(adapter.platform))
                    adapter._busy_text_mode = self._busy_text_mode

                    # Reconnect after an outage: preserve the platform's
                    # server-side update queue so messages sent while the bot
                    # was offline are delivered rather than dropped (#46621).
                    success = await self._connect_adapter_with_timeout(
                        adapter, platform, is_reconnect=True
                    )
                    if success:
                        self.adapters[platform] = adapter
                        self._sync_voice_mode_state_to_adapter(adapter)
                        # Wire voice input callback on reconnect as well (#60623).
                        if hasattr(adapter, "_voice_input_callback"):
                            adapter._voice_input_callback = self._handle_voice_channel_input
                        self.delivery_router.adapters = self.adapters
                        del self._failed_platforms[platform]
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="connected",
                            error_code=None,
                            error_message=None,
                        )
                        logger.info("✓ %s reconnected successfully", platform.value)

                        # Rebuild channel directory with the new adapter
                        try:
                            from gateway.channel_directory import build_channel_directory
                            await build_channel_directory(self.adapters)
                        except Exception:
                            pass

                        # A platform that was offline at gateway startup never
                        # got its restart-interrupted sessions auto-resumed —
                        # the startup pass skips sessions whose adapter isn't
                        # connected yet. Now that it's back, retry the
                        # auto-resume scoped to this platform so recovery
                        # doesn't silently wait for a manual user message.
                        try:
                            self._schedule_resume_pending_sessions(platform=platform)
                        except Exception:
                            logger.debug(
                                "resume-pending reschedule after %s reconnect failed",
                                platform.value,
                                exc_info=True,
                            )
                    # Check if the failure is non-retryable
                    elif adapter.has_fatal_error and not adapter.fatal_error_retryable:
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="fatal",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message,
                        )
                        logger.warning(
                            "Reconnect %s: non-retryable error (%s), removing from retry queue",
                            platform.value, adapter.fatal_error_message,
                        )
                        # The adapter is about to be dropped from the queue
                        # without ever being installed on self.adapters, so
                        # nothing else will call disconnect() on it. We must
                        # dispose it here, otherwise the resource owners it
                        # constructed in __init__ (ResponseStore for
                        # APIServerAdapter, etc.) leak 2 fds each. The
                        # gateway hits the 2560-fd limit after ~12h of
                        # failed reconnects at the 300s backoff cap (#37011).
                        await _dispose_unused_adapter(adapter)
                        del self._failed_platforms[platform]
                    else:
                        self._update_platform_runtime_status(
                            platform.value,
                            platform_state="retrying",
                            error_code=adapter.fatal_error_code,
                            error_message=adapter.fatal_error_message or "failed to reconnect",
                        )
                        backoff = _reconnect_backoff(attempt)
                        info["attempts"] = attempt
                        info["next_retry"] = time.monotonic() + backoff
                        logger.info(
                            "Reconnect %s failed, next retry in %ds",
                            platform.value, backoff,
                        )
                        # Same fd-leak concern as the non-retryable branch
                        # above: the adapter failed to connect and is being
                        # thrown away. Without an explicit dispose call, the
                        # resources it opened in __init__ stay open until
                        # the next GC pass — and aiohttp/SQLite handles
                        # don't get GC'd promptly, so 2 fds/retry leak at
                        # 300s backoff cap = ~12 fds/hour (#37011).
                        await _dispose_unused_adapter(adapter)
                        # Retryable failures (network/DNS blips) keep retrying
                        # at the backoff cap indefinitely — they self-heal once
                        # connectivity returns. We do NOT auto-pause them: a
                        # transient outage must never require manual `/platform
                        # resume` to recover. Non-retryable failures (bad auth,
                        # etc.) already drop out of the queue via the
                        # `not fatal_error_retryable` branch above, so anything
                        # reaching here is by definition retryable.
                except Exception as e:
                    if adapter is not None:
                        # An exception escaping the connect call path
                        # (DNS timeout, aiohttp server.start() crash, etc.)
                        # leaves the adapter in the same unowned state as
                        # the two branches above. Dispose so __init__
                        # resources don't accumulate while the watcher
                        # keeps retrying.
                        await _dispose_unused_adapter(adapter)
                    self._update_platform_runtime_status(
                        platform.value,
                        platform_state="retrying",
                        error_code=None,
                        error_message=str(e),
                    )
                    backoff = _reconnect_backoff(attempt)
                    info["attempts"] = attempt
                    info["next_retry"] = time.monotonic() + backoff
                    logger.warning(
                        "Reconnect %s error: %s, next retry in %ds",
                        platform.value, e, backoff,
                    )
                    # A raised exception during reconnect (connect timeout, DNS
                    # resolution failure, etc.) is inherently transient — keep
                    # retrying at the backoff cap rather than auto-pausing.

            # Check every 10 seconds for platforms that need reconnection
            for _ in range(10):
                if not self._running:
                    return
                await asyncio.sleep(1)

    async def _cancel_secondary_profile_reconnect_tasks(self) -> None:
        """Cancel profile-scoped reconnects before tearing down their registry.

        A reconnect can be waiting in adapter setup while shutdown begins. It
        must not republish an adapter after the secondary registry is drained.
        Waiting is bounded by the same adapter-cleanup budget; if a task does
        not finish in time, the stopped runner state still prevents it from
        installing an adapter when it eventually resumes.
        """
        pending = self._profile_failed_platforms
        if not isinstance(pending, dict):
            return
        current = asyncio.current_task()
        tasks: list[asyncio.Task] = []
        for profile_pending in pending.values():
            if not isinstance(profile_pending, dict):
                continue
            for task in profile_pending.values():
                if isinstance(task, asyncio.Task) and task is not current and not task.done():
                    tasks.append(task)
        for task in tasks:
            task.cancel()
        timeout = self._adapter_disconnect_timeout_secs()
        if tasks and timeout > 0:
            _done, unfinished = await asyncio.wait(tasks, timeout=timeout)
            if unfinished:
                logger.warning(
                    "Timed out waiting for %d secondary profile reconnect task(s) during shutdown",
                    len(unfinished),
                )
        pending.clear()

    def _start_systemd_watchdog(self) -> bool:
        """Start sd_notify only after a configured gateway is truly running."""
        if not self._running or self.config.systemd_watchdog_seconds <= 0:
            return False
        if self._systemd_watchdog is not None:
            return True

        from gateway.systemd_notify import SystemdWatchdog

        watchdog = SystemdWatchdog(config_enabled=True)
        if not watchdog.start():
            return False
        self._systemd_watchdog = watchdog
        watchdog.ready("Hermes Gateway running")
        return True

    async def _stop_systemd_watchdog(self) -> None:
        """Stop heartbeats before any potentially long shutdown drain."""
        watchdog = self._systemd_watchdog
        if watchdog is None:
            return
        self._systemd_watchdog = None
        await watchdog.stop()

    async def _start_secondary_profile_adapters(self) -> int:
        """Bring up adapters for every non-active profile this gateway serves.

        Returns the number of secondary adapters that connected. No-op (returns
        0) unless ``gateway.multiplex_profiles`` is on.

        Each profile's adapters are created and connected under that profile's
        HERMES_HOME + secret scope (``_profile_runtime_scope``), stored in
        ``self._profile_adapters[profile]``, and given a message handler that
        stamps ``source.profile`` before delegating to the shared
        ``_handle_message`` — so the agent turn resolves that profile's config,
        skills, and credentials. Same-platform credential collisions (two
        profiles polling the same bot token) are detected and refused here, the
        only point that sees every profile's resolved credentials together.
        """
        from gateway.run import MultiplexConfigError, SecondaryPortBindingConfigError

        if not getattr(self.config, "multiplex_profiles", False):
            return 0

        try:
            from hermes_cli.profiles import profiles_to_serve, get_active_profile_name
        except Exception:
            return 0

        active = get_active_profile_name() or "default"
        connected = 0
        # Resource claim -> profile that owns it. Credential claims prevent two
        # profiles polling the same account; listener claims prevent sidecars
        # with distinct credentials from binding the same endpoint.
        claimed: Dict[tuple, str] = {}
        for _plat, _ad in self.adapters.items():
            fp = self._adapter_credential_fingerprint(_ad)
            if fp is not None:
                claimed[(_plat, fp)] = active
            listener_claim = self._adapter_listener_claim(_plat, _ad)
            if listener_claim is not None:
                claimed[listener_claim] = active
        # A retryable primary still owns its configured credential and listener.
        # Reserve both while it is queued so a secondary cannot take the endpoint
        # before the reconnect watcher retries the primary adapter.
        for retry_info in getattr(self, "_failed_platforms", {}).values():
            for claim_name in ("credential_claim", "listener_claim"):
                retry_claim = retry_info.get(claim_name)
                if isinstance(retry_claim, tuple):
                    claimed[retry_claim] = active

        for profile_name, profile_home in profiles_to_serve(multiplex=True):
            if profile_name == active:
                continue  # handled by the primary startup loop
            try:
                connected += await self._start_one_profile_adapters(
                    profile_name, profile_home, claimed
                )
            except SecondaryPortBindingConfigError as e:
                logger.warning(
                    "Skipping secondary profile '%s' due to port-binding config error: %s",
                    profile_name,
                    e,
                )
            except MultiplexConfigError:
                raise
            except Exception as e:
                logger.error(
                    "Failed to start adapters for profile '%s': %s",
                    profile_name, e, exc_info=True,
                )

        # Record served profiles in runtime status for `hermes status`.
        try:
            from gateway.status import write_runtime_status
            from gateway.pairing import PairingStore
            served = [active] + sorted(self._profile_adapters.keys())
            # Per-profile PairingStores so authz_mixin can route pairing
            # checks to the right whitelist. The active profile gets a store
            # at its HERMES_HOME; additional served profiles resolve from
            # their own profile homes. See gateway.pairing.PairingStore.
            for name in served:
                if name and name not in self.pairing_stores:
                    self.pairing_stores[name] = (
                        self.pairing_store
                        if name == active
                        else PairingStore(profile=name)
                    )
            write_runtime_status(served_profiles=served)
        except Exception:
            logger.debug("could not record served_profiles", exc_info=True)

        return connected

    async def _start_one_profile_adapters(
        self, profile_name: str, profile_home: "Path", claimed: Dict[tuple, str]
    ) -> int:
        """Create+connect one profile's adapters under its runtime scope."""
        from gateway.config import load_gateway_config

        from gateway.run import (
            MultiplexConfigError,
            SecondaryPortBindingConfigError,
            _own_policy_open_startup_violation,
            _profile_runtime_scope,
        )

        with _profile_runtime_scope(profile_home):
            profile_cfg = load_gateway_config()
            violation = _own_policy_open_startup_violation(profile_cfg)
        if violation:
            raise MultiplexConfigError(
                f"Profile '{profile_name}' enables {violation}. "
                "Enable GATEWAY_ALLOW_ALL_USERS or the platform allow-all flag "
                "for that profile, or change dm_policy/group_policy away from "
                "'open'."
            )

        port_binding_platforms = sorted(
            platform.value
            for platform, platform_config in profile_cfg.platforms.items()
            if platform_config.enabled
            and _platform_binds_port(platform.value, platform_config.extra)
        )
        if port_binding_platforms:
            joined = ", ".join(port_binding_platforms)
            raise SecondaryPortBindingConfigError(
                f"Profile '{profile_name}' enables port-binding platform(s) "
                f"{joined}, but gateway.multiplex_profiles is on. The default "
                f"profile owns the single shared HTTP listener and serves every "
                f"profile through the /p/{profile_name}/ URL prefix. Remove "
                f"these platform entries from profile '{profile_name}'s config.yaml "
                f"or configure them only on the default profile."
            )

        profile_map = self._profile_adapters.setdefault(profile_name, {})
        connected = 0
        for platform, platform_config in profile_cfg.platforms.items():
            if not platform_config.enabled:
                continue
            # Relay is shared process-level ingress in multiplex mode. The
            # active profile owns the one connection; connector-stamped
            # source.profile routes inbound turns to secondary profiles.
            if (
                getattr(self.config, "multiplex_profiles", False)
                and platform is Platform.RELAY
            ):
                continue
            try:
                with _profile_runtime_scope(profile_home):
                    adapter = self._create_adapter(platform, platform_config)
            except Exception as e:
                logger.error(
                    "[MULTIPLEX] Profile '%s': _create_adapter('%s') raised %s",
                    profile_name,
                    platform.value,
                    e,
                    exc_info=True,
                )
                continue
            if not adapter:
                logger.warning(
                    "[MULTIPLEX] Profile '%s': skipping platform '%s' - adapter creation returned None",
                    profile_name,
                    platform.value,
                )
                continue

            # Same-token conflict detection — refuse a duplicate poll.
            credential_claim = self._adapter_credential_claim(platform, adapter)
            if credential_claim is not None:
                owner = claimed.get(credential_claim)
                if owner is not None:
                    logger.error(
                        "Profile '%s' and '%s' both configure %s with the same "
                        "credential — refusing to start the duplicate (one "
                        "credential cannot be consumed twice). Give each profile "
                        "its own %s credential.",
                        owner, profile_name, platform.value, platform.value,
                    )
                    # This adapter has not connected and therefore owns no
                    # resources to clean up. Calling disconnect here can mutate
                    # the shared platform state and, for a same-credential Photon
                    # adapter, shut down the primary profile's live sidecar.
                    continue

            listener_claim = self._adapter_listener_claim(platform, adapter)
            if listener_claim is not None:
                owner = claimed.get(listener_claim)
                if owner is not None:
                    bind, port = listener_claim[-2:]
                    logger.error(
                        "Profile '%s' and '%s' both configure %s sidecars on "
                        "%s:%s — refusing to start the duplicate listener. "
                        "Set platforms.%s.extra.sidecar_port to a distinct port "
                        "for profile '%s'.",
                        owner,
                        profile_name,
                        platform.value,
                        bind,
                        port,
                        platform.value,
                        profile_name,
                    )
                    # Like credential conflicts, this adapter never connected
                    # and owns no resources that should be disconnected.
                    continue

            self._configure_profile_adapter(adapter, profile_name, platform)

            try:
                with _profile_runtime_scope(profile_home):
                    success = await self._connect_initial_adapter_with_timeout(
                        adapter, platform
                    )
                if success:
                    profile_map[platform] = adapter
                    if credential_claim is not None:
                        claimed[credential_claim] = profile_name
                    if listener_claim is not None:
                        claimed[listener_claim] = profile_name
                    connected += 1
                    logger.info("✓ %s connected (profile: %s)", platform.value, profile_name)
                else:
                    logger.warning("✗ %s failed to connect (profile: %s)", platform.value, profile_name)
                    await self._safe_adapter_disconnect(adapter, platform)
            except Exception as e:
                logger.error("✗ %s error (profile: %s): %s", platform.value, profile_name, e)
                await self._safe_adapter_disconnect(adapter, platform)
        return connected

    def _configure_profile_adapter(
        self,
        adapter: BasePlatformAdapter,
        profile_name: str,
        platform: Platform,
    ) -> None:
        """Install the profile-scoped handlers shared by startup and reconnect."""
        adapter.set_message_handler(self._make_profile_message_handler(profile_name))
        adapter.set_fatal_error_handler(
            self._make_profile_fatal_error_handler(profile_name, platform)
        )
        adapter.set_session_store(self.session_store)
        adapter.set_busy_session_handler(self._handle_active_session_busy_message)
        _set_reaction = getattr(adapter, "set_reaction_handler", None)
        if callable(_set_reaction):
            _set_reaction(self._handle_reaction_event)
        adapter.set_topic_recovery_fn(self._recover_telegram_topic_thread_id)
        adapter.set_authorization_check(
            self._make_adapter_auth_check(platform, profile_name=profile_name)
        )
        adapter._busy_text_mode = self._busy_text_mode

    async def _run_secondary_profile_reconnect(
        self, profile_name: str, platform: Platform
    ) -> None:
        """Reconnect a retryable secondary adapter under its own profile scope."""
        from gateway.run import _profile_runtime_scope, _reconnect_backoff

        attempts = 0
        current_task = asyncio.current_task()
        try:
            while self._running:
                adapter = None
                try:
                    from hermes_cli.profiles import get_profile_dir
                    from gateway.config import load_gateway_config

                    profile_home = get_profile_dir(profile_name)
                    with _profile_runtime_scope(profile_home):
                        profile_config = load_gateway_config().platforms.get(platform)
                        if profile_config is None or not profile_config.enabled:
                            return
                        adapter = self._create_adapter(platform, profile_config)
                        if adapter is None:
                            logger.warning(
                                "Secondary %s reconnect skipped: adapter unavailable (profile: %s)",
                                platform.value,
                                profile_name,
                            )
                            return
                        self._configure_profile_adapter(
                            adapter, profile_name, platform
                        )
                        success = await self._connect_adapter_with_timeout(
                            adapter, platform, is_reconnect=True
                        )

                    if success and self._running:
                        profile_map = self._profile_adapters.setdefault(profile_name, {})
                        if platform not in profile_map:
                            profile_map[platform] = adapter
                            self._sync_voice_mode_state_to_adapter(adapter)
                            logger.info(
                                "✓ %s reconnected (profile: %s)",
                                platform.value,
                                profile_name,
                            )
                            return
                        # A newer reconnect already won the slot while this
                        # attempt was awaiting connect; do not replace it.
                        await self._safe_adapter_disconnect(adapter, platform)
                        return

                    # Shutdown can begin while connect() is in flight. Do not
                    # republish a newly connected adapter after the registry has
                    # been drained; release its partial resources instead.
                    if success:
                        await self._safe_adapter_disconnect(adapter, platform)
                        return

                    await self._safe_adapter_disconnect(adapter, platform)
                    if (
                        getattr(adapter, "has_fatal_error", False)
                        and not getattr(adapter, "fatal_error_retryable", True)
                    ):
                        return
                except asyncio.CancelledError:
                    if adapter is not None:
                        await self._safe_adapter_disconnect(adapter, platform)
                    raise
                except Exception:
                    if adapter is not None:
                        await self._safe_adapter_disconnect(adapter, platform)
                    logger.debug(
                        "Secondary %s reconnect attempt failed (profile: %s)",
                        platform.value,
                        profile_name,
                        exc_info=True,
                    )

                if not self._running:
                    return
                attempts += 1
                backoff = _reconnect_backoff(attempts)
                logger.info(
                    "Secondary %s reconnect retry in %ds (profile: %s)",
                    platform.value,
                    backoff,
                    profile_name,
                )
                await asyncio.sleep(backoff)
        finally:
            pending = self._profile_failed_platforms
            if isinstance(pending, dict):
                profile_pending = pending.get(profile_name)
                task = profile_pending.get(platform) if isinstance(profile_pending, dict) else None
                if not isinstance(task, asyncio.Task) or task is current_task:
                    if isinstance(profile_pending, dict):
                        profile_pending.pop(platform, None)
                        if not profile_pending:
                            pending.pop(profile_name, None)

    def _schedule_secondary_profile_reconnect(
        self, profile_name: str, platform: Platform, adapter: BasePlatformAdapter
    ) -> None:
        """Schedule one runner-owned reconnect without sharing primary secrets."""
        if not self._running or not adapter.fatal_error_retryable:
            return
        pending = self._profile_failed_platforms
        if not isinstance(pending, dict):
            pending = {}
            self._profile_failed_platforms = pending
        profile_pending = pending.setdefault(profile_name, {})
        if platform in profile_pending:
            return
        task = asyncio.create_task(
            self._run_secondary_profile_reconnect(profile_name, platform),
            name=f"secondary-reconnect:{profile_name}:{platform.value}",
        )
        profile_pending[platform] = task
        background_tasks = getattr(self, "_background_tasks", None)
        if not isinstance(background_tasks, set):
            background_tasks = set()
            self._background_tasks = background_tasks
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    def _make_profile_fatal_error_handler(
        self, profile_name: str, platform: Platform
    ) -> Callable[[BasePlatformAdapter], Awaitable[None]]:
        """Route a secondary-profile fatal error to that profile's reconnect slot."""
        async def _handler(adapter: BasePlatformAdapter) -> None:
            await self._handle_profile_adapter_fatal_error(profile_name, platform, adapter)

        return _handler

    async def _handle_profile_adapter_fatal_error(
        self,
        profile_name: str,
        platform: Platform,
        adapter: BasePlatformAdapter,
    ) -> None:
        """Remove a failed multiplexed adapter without touching the primary slot.

        Secondary adapters are owned by ``_profile_adapters`` rather than
        ``self.adapters``. The primary-only fatal handler intentionally ignores
        them; without this route, a fatal secondary Discord client stayed live
        forever after its liveness sampler stopped.
        """
        profile_map = getattr(self, "_profile_adapters", {}).get(profile_name)
        if not isinstance(profile_map, dict) or profile_map.get(platform) is not adapter:
            logger.debug(
                "Ignoring stale fatal error from secondary %s adapter (profile: %s)",
                platform.value,
                profile_name,
            )
            return
        profile_map.pop(platform, None)
        await self._safe_adapter_disconnect(adapter, platform)
        if not self._running:
            return
        self._schedule_secondary_profile_reconnect(profile_name, platform, adapter)
        logger.error(
            "Fatal %s adapter error for multiplexed profile %s (%s)",
            platform.value,
            profile_name,
            adapter.fatal_error_code or "unknown",
        )
        # Reconnect is scoped to the profile's own config and secret mapping;
        # never rebuild a secondary adapter with the default profile's credentials.

    def _make_profile_message_handler(self, profile_name: str):
        """Return a message handler that stamps source.profile then delegates.

        Auth runs inside ``_handle_message`` *before* the agent-turn scope is
        installed. For secondary profiles under multiplex, wrap the whole
        handler in ``_profile_runtime_scope`` so allowlists/tokens from that
        profile's ``.env`` are visible to ``get_secret`` / authz.
        """
        from gateway.run import _profile_runtime_scope
        from hermes_cli.profiles import get_profile_dir

        try:
            profile_home = get_profile_dir(profile_name)
        except Exception:
            profile_home = None

        async def _handler(event):
            try:
                if getattr(event, "source", None) is not None and not event.source.profile:
                    event.source.profile = profile_name
            except Exception:
                pass
            if profile_home is not None:
                with _profile_runtime_scope(profile_home):
                    return await self._handle_message(event)
            return await self._handle_message(event)

        return _handler

    def _make_default_profile_message_handler(self):
        """Scope a multiplexed default-profile message from ingress onward."""
        from gateway.run import _profile_runtime_scope, get_hermes_home

        profile_home = Path(get_hermes_home())

        async def _handler(event):
            with _profile_runtime_scope(profile_home):
                return await self._handle_message(event)

        return _handler

    def _primary_message_handler(self):
        """Return the correctly scoped handler for a primary adapter."""
        if getattr(self.config, "multiplex_profiles", False):
            return self._make_default_profile_message_handler()
        return self._handle_message

    @staticmethod
    def _adapter_credential_claim(
        platform: Platform, adapter: Any
    ) -> Optional[tuple]:
        """Return the exclusive credential resource claimed by an adapter."""
        from gateway.run import GatewayRunner

        fingerprint = GatewayRunner._adapter_credential_fingerprint(adapter)
        if fingerprint is None:
            return None
        return (platform, fingerprint)

    @staticmethod
    def _adapter_listener_claim(platform: Platform, adapter: Any) -> Optional[tuple]:
        """Return the exclusive listener resource claimed by an adapter.

        Photon sidecars are per-profile processes. Even when two profiles use
        different project credentials, their sidecars cannot share a bind and
        port. Represent that endpoint as a claim so multiplex startup rejects
        the later adapter before either ``connect()`` or ``disconnect()`` can
        disturb the first profile.
        """
        if getattr(platform, "value", None) != "photon":
            return None
        bind = getattr(adapter, "_sidecar_bind", None)
        port = getattr(adapter, "_sidecar_port", None)
        if not isinstance(bind, str) or not bind.strip():
            return None
        try:
            port = int(port)
        except (TypeError, ValueError):
            return None
        return ("listener", "photon", bind.strip().lower(), port)

    @staticmethod
    def _adapter_credential_fingerprint(adapter: Any) -> Optional[str]:
        """Return a stable, log-safe fingerprint of an adapter's credential.

        Used only to detect two profiles claiming the same platform credential.
        Returns a salted hash (never the credential itself) of the adapter's
        primary credential, or None when no credential is discoverable (in
        which case we don't attempt conflict detection for it).
        """
        token = None
        for attr in (
            "token",
            "bot_token",
            "_token",
            "api_token",
            "_bot_token",
            # Photon/Spectrum authenticates with project credentials instead
            # of a bot token. Including its secret keeps multiplexed profiles
            # from spawning competing sidecars for the same account and port.
            "_project_secret",
        ):
            val = getattr(adapter, attr, None)
            if isinstance(val, str) and val.strip():
                token = val.strip()
                break
        # Many adapters (e.g. Discord) store the token on their `config`
        # sub-object rather than directly on the adapter. Without this lookup
        # those adapters all return None here, the same-token conflict check
        # is silently skipped, and every profile's adapter for that platform
        # starts polling the same bot token — producing a per-message race
        # for which adapter answers. See test_reads_config_token.
        if not token:
            cfg = getattr(adapter, "config", None)
            if cfg is not None:
                for attr in ("token", "bot_token"):
                    val = getattr(cfg, attr, None)
                    if isinstance(val, str) and val.strip():
                        token = val.strip()
                        break
        if not token:
            config = getattr(adapter, "config", None)
            val = getattr(config, "token", None)
            if isinstance(val, str) and val.strip():
                token = val.strip()
        if not token:
            return None
        import hashlib
        return hashlib.sha256(("hermes-mux:" + token).encode("utf-8")).hexdigest()[:16]

    def _create_adapter(
        self, 
        platform: Platform, 
        config: Any
    ) -> Optional[BasePlatformAdapter]:
        """Create the appropriate adapter for a platform.

        Checks the platform_registry first (plugin adapters), then falls
        through to the built-in if/elif chain for core platforms.
        """
        if hasattr(config, "extra") and isinstance(config.extra, dict):
            config.extra.setdefault(
                "group_sessions_per_user",
                self.config.group_sessions_per_user,
            )
            config.extra.setdefault(
                "thread_sessions_per_user",
                getattr(self.config, "thread_sessions_per_user", False),
            )

        # ── Plugin-registered platforms (checked first) ───────────────────
        try:
            from gateway.platform_registry import platform_registry
            if platform_registry.is_registered(platform.value):
                adapter = platform_registry.create_adapter(platform.value, config)
                if adapter is not None:
                    # Inject a back-reference to the gateway runner so every
                    # adapter can (a) deliver cross-platform admin alerts and
                    # (b) resolve inbound profile routing through
                    # ``runner._profile_name_for_source``. Unconditional:
                    # ``BasePlatformAdapter`` declares ``gateway_runner``, so
                    # this reaches ALL platforms (not just the ones that
                    # pre-declared it), making profile routing platform-generic.
                    adapter.gateway_runner = self
                    return adapter
                # Registered but failed to instantiate — don't silently fall
                # through to built-ins (there are none for plugin platforms).
                logger.error(
                    "Platform '%s' is registered but adapter creation failed "
                    "(check dependencies and config)",
                    platform.value,
                )
                return None
        except Exception as e:
            logger.debug("Platform registry lookup for '%s' failed: %s", platform.value, e)
        # Fall through to built-in adapters below

        if platform == Platform.WHATSAPP_CLOUD:
            from gateway.platforms.whatsapp_cloud import (
                WhatsAppCloudAdapter,
                check_whatsapp_cloud_requirements,
            )
            if not check_whatsapp_cloud_requirements():
                logger.warning(
                    "WhatsApp Cloud: aiohttp/httpx missing — reinstall hermes-agent"
                )
                return None
            return WhatsAppCloudAdapter(config)
        
        elif platform == Platform.SIGNAL:
            from gateway.platforms.signal import (
                SignalAdapter,
                check_signal_requirements,
                validate_signal_config,
            )
            if not check_signal_requirements():
                logger.warning("Signal: runtime requirements not met")
                return None
            if not validate_signal_config(config):
                logger.warning("Signal: SIGNAL_HTTP_URL or SIGNAL_ACCOUNT not configured")
                return None
            return SignalAdapter(config)

        elif platform == Platform.WEIXIN:
            from gateway.platforms.weixin import WeixinAdapter, check_weixin_requirements
            if not check_weixin_requirements():
                logger.warning("Weixin: aiohttp/cryptography not installed")
                return None
            return WeixinAdapter(config)

        elif platform == Platform.API_SERVER:
            from gateway.platforms.api_server import APIServerAdapter, check_api_server_requirements
            if not check_api_server_requirements():
                logger.warning("API Server: aiohttp not installed")
                return None
            adapter = APIServerAdapter(config)
            adapter.gateway_runner = self
            return adapter

        elif platform == Platform.WEBHOOK:
            from gateway.platforms.webhook import WebhookAdapter, check_webhook_requirements
            if not check_webhook_requirements():
                logger.warning("Webhook: aiohttp not installed")
                return None
            adapter = WebhookAdapter(config)
            adapter.gateway_runner = self  # For cross-platform delivery
            return adapter

        elif platform == Platform.MSGRAPH_WEBHOOK:
            from gateway.platforms.msgraph_webhook import (
                MSGraphWebhookAdapter,
                check_msgraph_webhook_requirements,
            )
            if not check_msgraph_webhook_requirements():
                logger.warning("MSGraph webhook: aiohttp not installed")
                return None
            return MSGraphWebhookAdapter(config)

        elif platform == Platform.BLUEBUBBLES:
            from gateway.platforms.bluebubbles import BlueBubblesAdapter, check_bluebubbles_requirements
            if not check_bluebubbles_requirements():
                logger.warning("BlueBubbles: aiohttp/httpx missing or BLUEBUBBLES_SERVER_URL/BLUEBUBBLES_PASSWORD not configured")
                return None
            return BlueBubblesAdapter(config)

        elif platform == Platform.QQBOT:
            from gateway.platforms.qqbot import QQAdapter, check_qq_requirements
            if not check_qq_requirements():
                logger.warning("QQBot: aiohttp/httpx missing or QQ_APP_ID/QQ_CLIENT_SECRET not configured")
                return None
            return QQAdapter(config)

        elif platform == Platform.YUANBAO:
            from gateway.platforms.yuanbao import YuanbaoAdapter, WEBSOCKETS_AVAILABLE
            if not WEBSOCKETS_AVAILABLE:
                logger.warning("Yuanbao: websockets not installed. Run: pip install websockets")
                return None
            return YuanbaoAdapter(config)

        return None

    def _make_adapter_auth_check(
        self,
        platform: Platform,
        profile_name: Optional[str] = None,
    ) -> Callable[[str, Optional[str], Optional[str]], bool]:
        """Build a platform-bound auth callback for adapter use.

        Adapters that fetch external context (e.g. Slack
        ``conversations.replies``) call this through
        ``BasePlatformAdapter._is_sender_authorized`` to mark non-allowlisted
        senders as unverified in LLM context, mitigating indirect prompt
        injection from third parties in shared threads/channels.

        The returned callback delegates to :meth:`_is_user_authorized` so the
        full auth chain — platform allowlists, group allowlists, pairing
        store, allow-all flags — stays the single source of truth.

        ``profile_name`` binds the callback to the secondary adapter's own
        multiplex profile, so its ``SessionSource`` resolves that profile's
        secret scope instead of falling back to the active profile.
        """
        def check(
            user_id: str,
            chat_type: Optional[str] = None,
            chat_id: Optional[str] = None,
        ) -> bool:
            if not user_id:
                return False
            source = SessionSource(
                platform=platform,
                chat_id=chat_id or "",
                chat_type=chat_type or "group",
                user_id=user_id,
                profile=profile_name,
            )
            return self._is_user_authorized(source)
        return check

    def _profile_name_for_source(self, source: SessionSource) -> Optional[str]:
        """Resolve the profile name for an inbound source via configured routes.

        Returns ``None`` when multiplexing is off, no routes are configured, or
        no route matches. Callers (``build_source``,
        ``_resolve_profile_home_for_source``) treat ``None`` as "use the
        default/active profile". When ``gateway.profile_routes`` is configured,
        the most specific matching route wins (guild < channel < thread). See
        :mod:`gateway.profile_routing` for matching rules.

        Gated on ``gateway.multiplex_profiles``: routing stamps
        ``source.profile``, which selects the session-key namespace and batch
        keys — but the profile-scoped agent run only activates under
        multiplexing. Without this gate, a configured route with multiplexing
        off would namespace batch/session keys by profile while the agent
        still runs in ``agent:main``, splitting the two out of agreement.
        """
        config = getattr(self, "config", None)
        if not getattr(config, "multiplex_profiles", False):
            return None
        routes = getattr(config, "profile_routes", None)
        if not routes:
            return None
        from gateway.profile_routing import match_profile_route
        try:
            matched = match_profile_route(
                routes,
                platform=source.platform.value,
                guild_id=getattr(source, "guild_id", None),
                chat_id=source.chat_id,
                thread_id=getattr(source, "thread_id", None),
                parent_chat_id=getattr(source, "parent_chat_id", None),
            )
        except Exception:
            logger.warning(
                "Profile route matching failed for %s/%s, falling back to default",
                source.platform, source.chat_id, exc_info=True,
            )
            return None
        if matched:
            return matched.profile
        logger.debug(
            "No profile route matched: platform=%s chat_id=%s thread_id=%s parent_chat_id=%s",
            source.platform.value, source.chat_id,
            getattr(source, "thread_id", None), getattr(source, "parent_chat_id", None),
        )
        return None

