"""Turn-execution engine methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` as part of the god-file decomposition campaign
(Phase 3 mechanical mixin lifts, slice 29 of #54962). This mixin holds the
turn-execution engine: the agent run entry points (``_run_agent`` /
``_run_agent_inner``), the remote-proxy path (``_run_agent_via_proxy``), the
background-task executor, the cached-agent lifecycle (init / refresh / evict /
sweep), agent resource cleanup, and the gateway-owned thread executor.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
``self.*`` calls resolve unchanged via the MRO. Neutral dependencies import at
module top. Module-level run.py helpers the bodies read (``_load_gateway_config``,
``_hermes_home``, ``_resolve_gateway_model``, ``_profile_runtime_scope``, the
``_AGENT_*`` / ``_GATEWAY_*`` constants, ``TurnRunner``, ...) are imported
lazily inside the method body — a deferred ``from gateway.run import ...``
resolves at call time, when ``gateway.run`` is fully loaded — so this module
never imports ``gateway.run`` at import time -> no import cycle. The
module-level ``logger`` is ``logging.getLogger("gateway.run")`` (the name run.py
uses), so log records keep their original logger name.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import logging
import os
import threading
import time
from contextvars import copy_context
from typing import Any, Dict, List, Optional

from agent.async_utils import safe_schedule_threadsafe
from agent.interrupt_compat import request_hard_interrupt
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, merge_pending_message_event
from gateway.session import SessionSource
from gateway.turn_context import TurnContext

# Match the logger run.py uses (logging.getLogger(__name__) where __name__ ==
# "gateway.run") so extracted log records keep their original logger name.
logger = logging.getLogger("gateway.run")


class GatewayTurnExecMixin:
    """Turn-execution engine methods for ``GatewayRunner``."""



    def _should_emit_long_running_notification(
        self,
        session_key: Optional[str],
        agent: Any,
        executor_task: Optional[Any],
    ) -> bool:
        """Only emit the heartbeat while this task still owns the live run.

        Guards against a stale ``running: delegate_task`` heartbeat outliving the
        run that started it: stop once the executor finishes, the agent is gone,
        or the session key has been rebound to a different live agent (e.g. the
        user sent ``/new`` and a fresh agent took the slot mid-run, #12029).
        """
        if agent is None:
            return False
        if executor_task is not None and executor_task.done():
            return False
        if session_key:
            _hb_state = self._peek_session_state(session_key)
            if (_hb_state.turn.agent if _hb_state else None) is not agent:
                return False
        return True


    def _defer_agent_cleanup_until_future_done(
        self,
        future: asyncio.Future,
        agent: Any,
        *,
        context: str,
    ) -> None:
        """Clean up ``agent`` only after its executor future has finished.

        A timed-out executor call keeps running in its worker thread. Closing
        the agent before that thread exits can tear down clients or providers
        it is still using. Keep a strong task reference and wait for the real
        future before invoking the normal bounded, off-loop cleanup path.
        """

        async def _cleanup_when_done() -> None:
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                # Loop shutdown can cancel this waiter while the executor still
                # runs. Never turn that cancellation into premature cleanup.
                return
            except Exception as exc:
                logger.debug(
                    "Deferred agent worker%s finished with an error: %s",
                    f" ({context})" if context else "",
                    exc,
                )
            await self._cleanup_agent_resources_off_loop(agent, context=context)

        task = asyncio.create_task(_cleanup_when_done())
        tasks = getattr(self, "_deferred_agent_cleanup_tasks", None)
        if tasks is None:
            tasks = set()
            self._deferred_agent_cleanup_tasks = tasks
        tasks.add(task)
        task.add_done_callback(tasks.discard)


    async def _cleanup_agent_resources_off_loop(
        self, agent: Any, *, context: str = ""
    ) -> None:
        """Run _cleanup_agent_resources in a worker thread with a bounded wait.

        Safe to await from coroutines on the gateway event loop: a slow or
        wedged teardown (memory provider IO, subprocess close) can no longer
        block message processing. On timeout the await is cancelled and the
        worker thread is left to finish (or leak) on its own — the caller
        proceeds regardless, exactly as the /new reset path does (#35994).
        """
        if agent is None:
            return
        if context.startswith("shutdown") or context == "session expiry":
            try:
                agent._end_session_on_close = False
            except Exception:
                pass
        try:
            await asyncio.wait_for(
                self._run_in_executor_with_context(
                    self._cleanup_agent_resources, agent
                ),
                timeout=self._CLEANUP_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Agent resource cleanup%s exceeded %ss; proceeding without "
                "blocking the event loop (the worker thread is left to finish "
                "on its own). (#53175)",
                f" ({context})" if context else "",
                self._CLEANUP_TIMEOUT_S,
            )
        except Exception as cleanup_exc:
            logger.warning(
                "Agent resource cleanup%s failed: %s (#53175)",
                f" ({context})" if context else "",
                cleanup_exc,
            )


    def _cleanup_agent_resources(self, agent: Any) -> None:
        """Best-effort cleanup for temporary or cached agent instances."""
        if agent is None:
            return
        try:
            if hasattr(agent, "shutdown_memory_provider"):
                # Drain queued memory writes BEFORE tearing the provider down.
                # The memory manager persists per-turn sync and end-of-session
                # extraction on a single serialized background worker.
                # shutdown_memory_provider() -> shutdown_all() only gives that
                # worker a ~5s bounded drain and abandons (cancels) anything
                # still queued past it, so a /reset — or any gateway session
                # rotation that reaches this cleanup path — could silently drop
                # writes the session had already handed off. The next session
                # then loads stale memory (#73297). Give pending work a bounded
                # head start through the manager's own barrier first, mirroring
                # the CLI exit path (cli.py). Best-effort: a flush failure must
                # never block teardown.
                _mm = getattr(agent, "_memory_manager", None)
                if _mm is not None and hasattr(_mm, "flush_pending"):
                    try:
                        _mm.flush_pending(timeout=10)
                    except Exception:
                        pass
                # Pass the agent's own conversation transcript so memory
                # providers' ``on_session_end`` hooks see the real messages
                # instead of the empty default (#15165). ``_session_messages``
                # is set on ``AIAgent`` (run_agent.py:1518) and refreshed at
                # the end of every ``run_conversation`` turn via
                # ``_persist_session``; on an agent built through
                # ``object.__new__`` (test stubs) the attribute may be
                # absent, so ``getattr`` with a ``None`` default keeps the
                # call signature-compatible with the pre-fix behaviour
                # (``shutdown_memory_provider(messages=None)``).
                session_messages = getattr(agent, "_session_messages", None)
                if isinstance(session_messages, list):
                    agent.shutdown_memory_provider(session_messages)
                else:
                    agent.shutdown_memory_provider()
        except Exception:
            pass
        # Close tool resources (terminal sandboxes, browser daemons,
        # background processes, httpx clients) to prevent zombie
        # process accumulation.
        try:
            if hasattr(agent, "close"):
                agent.close()
        except Exception:
            pass
        # Auxiliary async clients (session_search/web/vision/etc.) live in a
        # process-global cache and are created inside worker threads. Clean up
        # any entries whose event loop is now dead so their httpx transports do
        # not accumulate across gateway turns.
        try:
            from agent.auxiliary_client import cleanup_stale_async_clients
            cleanup_stale_async_clients()
        except Exception:
            pass


    async def _run_background_task(
        self,
        prompt: str,
        source: "SessionSource",
        task_id: str,
        event_message_id: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
    ) -> None:
        """Profile-scoping wrapper around the background agent task.

        When multiplexing is active, resolve the inbound source's profile and
        run the whole task inside ``_profile_runtime_scope`` so credentials
        resolve from that profile's secret scope. Mirrors the pattern in
        ``_run_agent``.
        """
        from gateway.run import _profile_runtime_scope
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return await self._run_background_task_inner(
                prompt, source, task_id, event_message_id, media_urls, media_types,
            )

        profile_home = self._resolve_profile_home_for_source(source)
        with _profile_runtime_scope(profile_home):
            return await self._run_background_task_inner(
                prompt, source, task_id, event_message_id, media_urls, media_types,
            )


    async def _run_background_task_inner(
        self,
        prompt: str,
        source: "SessionSource",
        task_id: str,
        event_message_id: Optional[str] = None,
        media_urls: Optional[List[str]] = None,
        media_types: Optional[List[str]] = None,
    ) -> None:
        """Execute a background agent task and deliver the result to the chat."""
        from gateway.run import _checkpoint_agent_kwargs, _current_max_iterations, _load_gateway_config, _platform_config_key
        from run_agent import AIAgent

        media_urls = media_urls or []
        media_types = media_types or []

        adapter = self._adapter_for_source(source)
        if not adapter:
            logger.warning("No adapter for platform %s in background task %s", source.platform, task_id)
            return

        _thread_metadata = self._thread_metadata_for_source(source, event_message_id)

        try:
            user_config = _load_gateway_config()
            model, runtime_kwargs = self._resolve_session_agent_runtime(
                source=source,
                user_config=user_config,
            )
            if not runtime_kwargs.get("api_key"):
                await adapter.send(
                    source.chat_id,
                    f"❌ Background task {task_id} failed: no provider credentials configured.",
                    metadata=_thread_metadata,
                )
                return

            platform_key = _platform_config_key(source.platform)

            from hermes_cli.tools_config import _get_platform_tools
            enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
            agent_cfg = user_config.get("agent") or {}
            disabled_toolsets = agent_cfg.get("disabled_toolsets") or None

            pr = self._provider_routing
            max_iterations = _current_max_iterations()
            reasoning_config = self._resolve_session_reasoning_config(
                source=source, model=model
            )
            self._reasoning_config = reasoning_config
            self._service_tier = self._resolve_session_service_tier(source=source)
            turn_route = self._resolve_turn_agent_config(prompt, model, runtime_kwargs)

            # Enrich the prompt with image descriptions so the background
            # agent can see user-attached images (same as the main flow).
            enriched_prompt = prompt
            if media_urls:
                image_paths = []
                for i, path in enumerate(media_urls):
                    mtype = media_types[i] if i < len(media_types) else ""
                    if mtype.startswith("image/"):
                        image_paths.append(path)
                if image_paths:
                    try:
                        enriched_prompt = await self._enrich_message_with_vision(
                            prompt, image_paths,
                        )
                    except Exception as e:
                        logger.warning("Background task vision enrichment failed: %s", e)

            def run_sync():
                agent = AIAgent(
                    model=turn_route["model"],
                    **turn_route["runtime"],
                    **_checkpoint_agent_kwargs(user_config),
                    max_iterations=max_iterations,
                    quiet_mode=True,
                    verbose_logging=False,
                    enabled_toolsets=enabled_toolsets,
                    disabled_toolsets=disabled_toolsets,
                    reasoning_config=reasoning_config,
                    service_tier=self._service_tier,
                    request_overrides=turn_route.get("request_overrides"),
                    providers_allowed=pr.get("only"),
                    providers_ignored=pr.get("ignore"),
                    providers_order=pr.get("order"),
                    provider_sort=pr.get("sort"),
                    provider_require_parameters=pr.get("require_parameters", False),
                    provider_data_collection=pr.get("data_collection"),
                    session_id=task_id,
                    platform=platform_key,
                    user_id=source.user_id,
                    user_id_alt=source.user_id_alt,
                    user_name=source.user_name,
                    chat_id=source.chat_id,
                    chat_name=source.chat_name,
                    chat_type=source.chat_type,
                    thread_id=source.thread_id,
                    session_db=getattr(self._session_db, "_db", self._session_db),
                    # Reload from disk — do not reuse the startup snapshot (#60955).
                    fallback_model=self._refresh_fallback_model(),
                )
                try:
                    return agent.run_conversation(
                        user_message=enriched_prompt,
                        task_id=task_id,
                    )
                finally:
                    self._cleanup_agent_resources(agent)

            result = await self._run_in_executor_with_context(run_sync)

            response = result.get("final_response", "") if result else ""
            if not response and result and result.get("error"):
                response = f"Error: {result['error']}"

            # Extract media files from the response
            if response:
                media_files, response = adapter.extract_media(response)
                from gateway.platforms.base import BasePlatformAdapter
                media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
                images, text_content = adapter.extract_images(response)

                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                header = f'✅ Background task complete\nPrompt: "{preview}"\n\n'

                if text_content:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + text_content,
                        metadata=_thread_metadata,
                    )
                elif not images and not media_files:
                    await adapter.send(
                        chat_id=source.chat_id,
                        content=header + "(No response generated)",
                        metadata=_thread_metadata,
                    )

                # Send extracted images
                for image_url, alt_text in (images or []):
                    try:
                        await adapter.send_image(
                            chat_id=source.chat_id,
                            image_url=image_url,
                            caption=alt_text,
                            metadata=_thread_metadata,
                        )
                    except Exception:
                        pass

                # Send media files, routing each by type so a TTS clip
                # arrives as a voice bubble / a clip as a video rather than
                # a generic document. Mirrors the streaming + kanban paths.
                from gateway.platforms.base import (
                    should_send_media_as_audio as _should_send_media_as_audio,
                )
                _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
                _VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
                for media_path, _is_voice in (media_files or []):
                    _ext = os.path.splitext(media_path)[1].lower()
                    try:
                        if _should_send_media_as_audio(source.platform, _ext, _is_voice):
                            await adapter.send_voice(
                                chat_id=source.chat_id,
                                audio_path=media_path,
                                metadata=_thread_metadata,
                            )
                        elif _ext in _VIDEO_EXTS:
                            await adapter.send_video(
                                chat_id=source.chat_id,
                                video_path=media_path,
                                metadata=_thread_metadata,
                            )
                        elif _ext in _IMAGE_EXTS:
                            await adapter.send_image_file(
                                chat_id=source.chat_id,
                                image_path=media_path,
                                metadata=_thread_metadata,
                            )
                        else:
                            await adapter.send_document(
                                chat_id=source.chat_id,
                                file_path=media_path,
                                metadata=_thread_metadata,
                            )
                    except Exception:
                        pass
            else:
                preview = prompt[:60] + ("..." if len(prompt) > 60 else "")
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f'✅ Background task complete\nPrompt: "{preview}"\n\n(No response generated)',
                    metadata=_thread_metadata,
                )

        except Exception as e:
            logger.exception("Background task %s failed", task_id)
            try:
                await adapter.send(
                    chat_id=source.chat_id,
                    content=f"❌ Background task {task_id} failed: {e}",
                    metadata=_thread_metadata,
                )
            except Exception:
                pass


    async def _run_in_executor_with_context(self, func, *args):
        """Run blocking work in the thread pool while preserving session contextvars."""
        loop = asyncio.get_running_loop()
        ctx = copy_context()
        return await loop.run_in_executor(
            self._get_executor(),
            ctx.run,
            func,
            *args,
        )


    def _get_executor(self) -> concurrent.futures.ThreadPoolExecutor:
        """Return the gateway-owned executor for blocking agent work."""
        lock = getattr(self, "_executor_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._executor_lock = lock

        with lock:
            if getattr(self, "_executor_closing", False):
                raise RuntimeError("Gateway is shutting down; executor unavailable")
            executor = getattr(self, "_executor", None)
            if executor is None or getattr(executor, "_shutdown", False):
                executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=10,
                    thread_name_prefix="hermes-gateway",
                )
                self._executor = executor
            return executor


    def _shutdown_executor(self) -> None:
        """Stop the gateway-owned executor without touching the loop default."""
        lock = getattr(self, "_executor_lock", None)
        if lock is None:
            return

        with lock:
            self._executor_closing = True
            executor = getattr(self, "_executor", None)
            self._executor = None

        if executor is None:
            return

        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)


    async def _refresh_agent_cache_message_count(
        self, session_key: str, session_id: Optional[str]
    ) -> None:
        """Re-baseline a cached agent's stored message_count after THIS turn.

        The cross-process coherence guard (#45966) compares the session's
        on-disk ``message_count`` against the count snapshotted next to the
        cached agent, and rebuilds the agent on a mismatch.  But the snapshot
        is taken at agent-BUILD time — before this turn writes its own user +
        assistant (+ tool) rows — and the cache entry is never rewritten on a
        reuse.  So without this re-baseline, THIS process's own turn would
        grow ``message_count`` and the very next turn would see a mismatch
        and rebuild the agent — every turn, for every conversation — silently
        destroying the per-conversation prompt caching the cache exists to
        protect.

        Call this once a turn has completed and the agent has flushed its
        rows to the SessionDB.  It snapshots the now-current count (which
        includes this process's own writes) so the guard only fires when a
        DIFFERENT process changes the transcript out from under us.  The
        ``_sig`` is left untouched; only the count element is refreshed, and
        only when the same agent is still cached (no rebuild/eviction raced
        in between).  Fail-safe: any DB error leaves the snapshot as-is, which
        at worst costs one unnecessary rebuild on the next turn.

        When the cache entry records a ``session_id`` (4-tuple form, #54947)
        that differs from the current ``session_id`` — meaning the cache
        was built for a DIFFERENT conversation under the same ``session_key``
        — the snapshot is intentionally left untouched.  Overwriting it with
        the current session's count would corrupt the original conversation's
        baseline and cause the next switch back to fire the cross-process
        guard spuriously.  Fail-safe: the legacy 3-tuple shape (no
        ``session_id``) is still re-baselined as before.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        if self._session_db is None or not session_id:
            return
        _cache_lock = getattr(self, "_agent_cache_lock", None)
        _cache = getattr(self, "_agent_cache", None)
        if not _cache_lock or _cache is None:
            return
        try:
            _sess_row = await self._session_db.get_session(session_id)
            _live = _sess_row.get("message_count", 0) if _sess_row else None
        except Exception:
            return
        if _live is None:
            return
        with _cache_lock:
            cached = _cache.get(session_key)
            # Only re-baseline a live 3-tuple entry; skip pending sentinels,
            # legacy 2-tuples (they intentionally opt out of the guard), and
            # the case where the entry was evicted/rebuilt mid-turn.
            if (
                isinstance(cached, tuple)
                and len(cached) > 2
                and cached[0] is not _AGENT_PENDING_SENTINEL
            ):
                # If the snapshot was taken for a different session_id
                # (same session_key, different conversation), leave the
                # snapshot alone — the current session_id's count belongs
                # to a different DB row (#54947).
                _snapshot_sid = cached[3] if len(cached) > 3 else None
                if _snapshot_sid is not None and _snapshot_sid != session_id:
                    return
                if cached[2] != _live:
                    if _snapshot_sid is None:
                        # Legacy 3-tuple: preserve the original 3-element
                        # shape so existing entries stay compatible with
                        # callers that index ``cached[2]`` directly.
                        _cache[session_key] = (cached[0], cached[1], _live)
                    else:
                        _cache[session_key] = (
                            cached[0], cached[1], _live, _snapshot_sid,
                        )


    @staticmethod
    def _init_cached_agent_for_turn(agent: Any, interrupt_depth: int) -> None:
        """Reset per-turn state on a cached agent before a new turn starts.

        ``_last_activity_ts``, ``_last_activity_desc``, and
        ``_last_activity_provenance`` are only reset for fresh external
        turns (depth 0); they are a semantic triple - description and
        provenance describe the activity *at* ts, so updating one without
        the others would make get_activity_summary() misleading.
        For interrupt-recursive turns all three are preserved so the
        inactivity watchdog can accumulate stuck-turn idle time and fire
        the 30-min timeout (#15654).  The depth-0 reset is still needed:
        a session idle for 29 min would otherwise trip the watchdog before
        the new turn makes its first API call (#9051).
        """
        from gateway.run import time
        if interrupt_depth == 0:
            from agent.session_activity import ActivityProvenance

            agent._last_activity_ts = time.time()
            agent._last_activity_desc = "starting new turn (cached)"
            agent._last_activity_provenance = ActivityProvenance.UNKNOWN
            # Reset the SessionDB flush cursor so the new turn's messages are
            # fully persisted - a stale value from the previous turn would
            # cause `_flush_messages_to_session_db` to skip new rows (#44327).
            if hasattr(agent, "_last_flushed_db_idx"):
                agent._last_flushed_db_idx = 0
        agent._api_call_count = 0


    def _commit_memory_before_soft_evict(self, agent: Any, key: str) -> None:
        """Fire on_session_end extraction before soft-evicting a live agent.

        Soft eviction (``_release_evicted_agent_soft``) deliberately keeps the
        session resumable and does NOT fire ``on_session_end`` — that hook is
        reserved for the true session boundary, tear-down done by
        ``_session_expiry_watcher`` when the session finally expires.

        But the watcher tears down whatever agent it finds in ``_agent_cache``
        at expiry time.  If cache pressure (the LRU cap) soft-evicts a
        finalizable session's agent BEFORE it expires, the watcher later finds
        no cached agent and ``on_session_end`` is silently skipped — memory
        providers never see the transcript (#11205, LRU-cap variant).

        We hold the live, fully-scoped agent right now, so commit its
        end-of-session memory extraction here using the agent's own memory
        manager (correct per-user/chat scoping, no reconstruction).  This uses
        ``commit_memory_session`` — extraction WITHOUT provider teardown — so
        the eviction stays soft and a resumed turn keeps working.

        Only fires for sessions the expiry watcher will eventually finalize
        (finite reset policy).  For ``mode == "none"`` sessions the watcher
        never runs, so there is no missed-boundary to compensate for and we
        skip the commit (the agent is simply released).  Best-effort: any
        failure is swallowed so eviction still proceeds.
        """
        if agent is None or not hasattr(agent, "commit_memory_session"):
            return
        if getattr(agent, "_memory_manager", None) is None:
            return  # no external memory provider — nothing to commit
        try:
            _store = getattr(self, "session_store", None)
            if _store is None:
                return
            _store._ensure_loaded()
            entry = _store._entries.get(key)
            if entry is None:
                return
            # Only compensate when the watcher would otherwise expect to find
            # this agent at expiry (finite policy, not yet expired). Expired
            # sessions are torn down by the watcher directly; mode="none"
            # sessions are never finalized.
            if not _store.is_session_finalizable(entry):
                return
            if _store._is_session_expired(entry):
                return
            messages = getattr(agent, "_session_messages", None)
            agent.commit_memory_session(messages if isinstance(messages, list) else None)
            logger.debug(
                "Committed on_session_end extraction before soft-evicting "
                "finalizable session=%s (cache pressure, pre-expiry)", key,
            )
        except Exception as _e:
            logger.debug("Pre-evict memory commit failed for %s: %s", key, _e)


    def _commit_then_release_soft(self, agent: Any, key: str) -> None:
        """Commit end-of-session memory (if warranted), then soft-release.

        Runs on the daemon eviction thread so the memory-provider call and the
        client teardown never block the caller's held cache lock. Order matters:
        commit uses the live agent's memory manager before ``release_clients``
        drops the message buffer.
        """
        self._commit_memory_before_soft_evict(agent, key)
        self._release_evicted_agent_soft(agent)


    def _release_evicted_agent_soft(self, agent: Any) -> None:
        """Soft cleanup for cache-evicted agents — preserves session tool state.

        Called from _enforce_agent_cache_cap and _sweep_idle_cached_agents.
        Distinct from _cleanup_agent_resources (full teardown) because a
        cache-evicted session may resume at any time — its terminal
        sandbox, browser daemon, and tracked bg processes must outlive
        the Python AIAgent instance so the next agent built for the
        same task_id inherits them.
        """
        if agent is None:
            return
        try:
            if hasattr(agent, "release_clients"):
                agent.release_clients()
            else:
                # Older agent instance (shouldn't happen in practice) —
                # fall back to the legacy full-close path.
                self._cleanup_agent_resources(agent)
        except Exception:
            pass
        # Free conversation history memory — can be tens of MB with tool
        # outputs (file reads, terminal output, search results) on heavy
        # 100+-tool-call sessions. release_clients() deliberately preserves
        # session tool state for resume, but the message list is rebuilt from
        # persisted session JSON on the next turn, so dropping it here is safe.
        if hasattr(agent, "_session_messages"):
            agent._session_messages = []


    def _enforce_agent_cache_cap(self) -> None:
        """Evict oldest cached agents when cache exceeds _AGENT_CACHE_MAX_SIZE.

        Must be called with _agent_cache_lock held.  Resource cleanup
        (memory provider shutdown, tool resource close) is scheduled
        on a daemon thread so the caller doesn't block on slow teardown
        while holding the cache lock.

        Agents currently in _running_agents are SKIPPED — their clients,
        terminal sandboxes, background processes, and child subagents
        are all in active use by the running turn.  Evicting them would
        tear down those resources mid-turn and crash the request.  If
        every candidate in the LRU order is active, we simply leave the
        cache over the cap; it will be re-checked on the next insert.
        """
        from gateway.run import _AGENT_CACHE_MAX_SIZE, _AGENT_PENDING_SENTINEL
        _cache = getattr(self, "_agent_cache", None)
        if _cache is None:
            return
        # OrderedDict.popitem(last=False) pops oldest; plain dict lacks the
        # arg so skip enforcement if a test fixture swapped the cache type.
        if not hasattr(_cache, "move_to_end"):
            return

        # Snapshot of agent instances that are actively mid-turn.  Use id()
        # so the lookup is O(1) and doesn't depend on AIAgent.__eq__ (which
        # MagicMock overrides in tests).
        running_ids = {
            id(a)
            for _, a in self._running_agent_items()
            if a is not None and a is not _AGENT_PENDING_SENTINEL
        }

        # Walk LRU → MRU and evict excess-LRU entries that aren't mid-turn.
        # We only consider entries in the first (size - cap) LRU positions
        # as eviction candidates.  If one of those slots is held by an
        # active agent, we SKIP it without compensating by evicting a
        # newer entry — that would penalise a freshly-inserted session
        # (which has no cache history to retain) while protecting an
        # already-cached long-running one.  The cache may therefore stay
        # temporarily over cap; it will re-check on the next insert,
        # after active turns have finished.
        excess = max(0, len(_cache) - _AGENT_CACHE_MAX_SIZE)
        evict_plan: List[tuple] = []  # [(key, agent), ...]
        if excess > 0:
            ordered_keys = list(_cache.keys())
            for key in ordered_keys[:excess]:
                entry = _cache.get(key)
                agent = entry[0] if isinstance(entry, tuple) and entry else None
                if agent is not None and id(agent) in running_ids:
                    continue  # active mid-turn; don't evict, don't substitute
                evict_plan.append((key, agent))

        for key, _ in evict_plan:
            _cache.pop(key, None)

        remaining_over_cap = len(_cache) - _AGENT_CACHE_MAX_SIZE
        if remaining_over_cap > 0:
            logger.warning(
                "Agent cache over cap (%d > %d); %d excess slot(s) held by "
                "mid-turn agents — will re-check on next insert.",
                len(_cache), _AGENT_CACHE_MAX_SIZE, remaining_over_cap,
            )

        for key, agent in evict_plan:
            logger.info(
                "Agent cache at cap; evicting LRU session=%s (cache_size=%d)",
                key, len(_cache),
            )
            if agent is not None:
                # Commit end-of-session memory extraction, then soft-release,
                # both on the daemon thread so the (possibly network-bound)
                # provider call never blocks the held cache lock. The commit
                # only fires for finalizable-not-yet-expired sessions whose
                # agent would otherwise vanish before the expiry watcher can
                # fire on_session_end (#11205, LRU-cap variant).
                threading.Thread(
                    target=self._commit_then_release_soft,
                    args=(agent, key),
                    daemon=True,
                    name=f"agent-cache-evict-{key[:24]}",
                ).start()


    def _sweep_idle_cached_agents(self) -> int:
        """Evict cached agents whose AIAgent has been idle > _AGENT_CACHE_IDLE_TTL_SECS.

        Safe to call from the session expiry watcher without holding the
        cache lock — acquires it internally.  Returns the number of entries
        evicted.  Resource cleanup is scheduled on daemon threads.

        Agents currently in _running_agents are SKIPPED for the same reason
        as _enforce_agent_cache_cap: tearing down an active turn's clients
        mid-flight would crash the request.
        """
        from gateway.run import _AGENT_CACHE_IDLE_TTL_SECS, _AGENT_PENDING_SENTINEL, time
        _cache = getattr(self, "_agent_cache", None)
        _lock = getattr(self, "_agent_cache_lock", None)
        if _cache is None or _lock is None:
            return 0
        now = time.time()
        to_evict: List[tuple] = []
        running_ids = {
            id(a)
            for _, a in self._running_agent_items()
            if a is not None and a is not _AGENT_PENDING_SENTINEL
        }
        with _lock:
            for key, entry in list(_cache.items()):
                agent = entry[0] if isinstance(entry, tuple) and entry else None
                if agent is None:
                    continue
                if id(agent) in running_ids:
                    continue  # mid-turn — don't tear it down
                last_activity = getattr(agent, "_last_activity_ts", None)
                if last_activity is None:
                    continue
                if (now - last_activity) > _AGENT_CACHE_IDLE_TTL_SECS:
                    # Check whether the session has actually expired in the
                    # session store.  If it hasn't (e.g. daily-reset mode
                    # where the reset fires hours after the user's last
                    # message), keep the agent in cache so the session-store
                    # expiry watcher can still find it and call
                    # on_session_end() with the live transcript.  Skipping
                    # eviction here means the agent stays alive until the
                    # session genuinely expires, at which point the watcher
                    # (gateway/run.py _session_expiry_watcher) tears it down
                    # properly.  (#11205 follow-up)
                    #
                    # BUT only defer when the watcher will EVER finalize this
                    # session.  For a mode == "none" session the watcher never
                    # fires (is_session_finalizable() is False), so deferring
                    # would pin the agent in cache for the gateway's entire
                    # lifetime — the exact leak this idle sweep exists to
                    # relieve.  Those sessions fall through to soft eviction
                    # WITHOUT on_session_end, and that is correct: a mode=="none"
                    # session never reaches a session-end boundary, so there is
                    # no missed on_session_end to compensate for.  (The finite
                    # case — a session evicted under LRU-cap pressure before it
                    # expires — is instead covered by _commit_memory_before_soft_
                    # evict on the cap path, which fires on_session_end via the
                    # live agent's memory manager before releasing it.)
                    session_entry = None
                    _store = getattr(self, "session_store", None)
                    try:
                        if _store is not None:
                            _store._ensure_loaded()
                            session_entry = _store._entries.get(key)
                    except Exception:
                        session_entry = None
                    if (
                        session_entry is not None
                        and _store is not None
                        and _store.is_session_finalizable(session_entry)
                        and not _store._is_session_expired(session_entry)
                    ):
                        continue  # keep agent — finite session hasn't expired
                    to_evict.append((key, agent))
            for key, _ in to_evict:
                _cache.pop(key, None)
        for key, agent in to_evict:
            logger.info(
                "Agent cache idle-TTL evict: session=%s (idle=%.0fs)",
                key, now - getattr(agent, "_last_activity_ts", now),
            )
            threading.Thread(
                target=self._release_evicted_agent_soft,
                args=(agent,),
                daemon=True,
                name=f"agent-cache-idle-{key[:24]}",
            ).start()
        return len(to_evict)


    # ------------------------------------------------------------------
    # Proxy mode: forward messages to a remote Hermes API server
    # ------------------------------------------------------------------

    def _get_proxy_url(self) -> Optional[str]:
        """Return the proxy URL if proxy mode is configured, else None.

        Checks GATEWAY_PROXY_URL env var first (convenient for Docker),
        then ``gateway.proxy_url`` in config.yaml.
        """
        from gateway.run import _load_gateway_config
        url = os.getenv("GATEWAY_PROXY_URL", "").strip()
        if url:
            return url.rstrip("/")
        cfg = _load_gateway_config()
        url = (cfg.get("gateway") or {}).get("proxy_url")
        url = (url or "").strip()
        if url:
            return url.rstrip("/")
        return None


    def _build_stream_consumer_config(
        self,
        source: "SessionSource",
        scfg: Any,
        adapter: Any,
        *,
        on_missing_cursor: str,
    ) -> "tuple[Any, Optional[Callable[[], None]]]":
        """Build the shared ``StreamConsumerConfig`` and the optional
        Telegram pause-typing closure used by both agent-run paths.

        ``on_missing_cursor`` controls how platforms whose adapter sets
        ``SUPPORTS_MESSAGE_EDITING = False`` are handled — both semantics
        are preserved verbatim from the pre-refactor call sites:

        - ``"fallback"`` (proxy path): stream anyway with an empty cursor.
        - ``"raise"`` (in-process agent path): raise ``RuntimeError`` so
          the caller's ``except`` skips streaming entirely.

        Returns ``(consumer_cfg, pause_typing_before_finalize)``.
        """
        from gateway.stream_consumer import StreamConsumerConfig

        _pause_typing_before_finalize = None
        if source.platform == Platform.TELEGRAM and hasattr(adapter, "pause_typing_for_chat"):
            def _pause_typing_before_finalize(
                _adapter=adapter,
                _chat_id=source.chat_id,
            ) -> None:
                _adapter.pause_typing_for_chat(_chat_id)
        # Platforms that don't support editing sent messages
        # (e.g. QQ, WeChat) should skip streaming entirely —
        # without edit support, the consumer sends a partial
        # first message that can never be updated, resulting in
        # duplicate messages (partial + final).
        # (The proxy path instead opts into a cursorless fallback
        # via on_missing_cursor="fallback".)
        _adapter_supports_edit = getattr(adapter, "SUPPORTS_MESSAGE_EDITING", True)
        if not _adapter_supports_edit and on_missing_cursor == "raise":
            raise RuntimeError("skip streaming for non-editable platform")
        _effective_cursor = scfg.cursor if _adapter_supports_edit else ""
        # Some Matrix clients render the streaming cursor
        # as a visible tofu/white-box artifact.  Keep
        # streaming text on Matrix, but suppress the cursor.
        _buffer_only = False
        if source.platform == Platform.MATRIX:
            _effective_cursor = ""
            _buffer_only = True
        # Fresh-final applies to Telegram only — other
        # platforms either edit in place cheaply (Discord,
        # Slack) or don't have the timestamp-on-edit /
        # edit-timestamp-stays-stale problem.
        # (Ported from openclaw/openclaw#72038.)
        _fresh_final_secs = (
            float(getattr(scfg, "fresh_final_after_seconds", 0.0) or 0.0)
            if source.platform == Platform.TELEGRAM
            else 0.0
        )
        _consumer_cfg = StreamConsumerConfig(
            edit_interval=scfg.edit_interval,
            buffer_threshold=scfg.buffer_threshold,
            cursor=_effective_cursor,
            buffer_only=_buffer_only,
            fresh_final_after_seconds=_fresh_final_secs,
            transport=scfg.transport or "edit",
            chat_type=getattr(source, "chat_type", "") or "",
        )
        return _consumer_cfg, _pause_typing_before_finalize


    async def _run_agent_via_proxy(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: "SessionSource",
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        event_message_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forward the message to a remote Hermes API server instead of
        running a local AIAgent.

        When ``GATEWAY_PROXY_URL`` (or ``gateway.proxy_url`` in config.yaml)
        is set, the gateway becomes a thin relay: it handles platform I/O
        (encryption, threading, media) and delegates all agent work to the
        remote server via ``POST /v1/chat/completions`` with SSE streaming.

        This lets a Docker container handle Matrix E2EE while the actual
        agent runs on the host with full access to local files, memory,
        skills, and a unified session store.
        """
        from gateway.run import _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS, _load_gateway_config, _platform_config_key, time
        try:
            from aiohttp import ClientSession as _AioClientSession, ClientTimeout
        except ImportError:
            return {
                "final_response": "⚠️ Proxy mode requires aiohttp. Install with: pip install aiohttp",
                "messages": [],
                "api_calls": 0,
                "tools": [],
            }

        proxy_url = self._get_proxy_url()
        if not proxy_url:
            return {
                "final_response": "⚠️ Proxy URL not configured (GATEWAY_PROXY_URL or gateway.proxy_url)",
                "messages": [],
                "api_calls": 0,
                "tools": [],
            }

        # Scope-aware read: the proxy key is a per-profile credential; under
        # multiplex honor the installed scope's verdict (Slack pattern for
        # the unscoped default-profile loop).
        try:
            from agent.secret_scope import UnscopedSecretError, get_secret

            try:
                proxy_key = (get_secret("GATEWAY_PROXY_KEY") or "").strip()
            except UnscopedSecretError:
                proxy_key = os.getenv("GATEWAY_PROXY_KEY", "").strip()
        except Exception:
            proxy_key = os.getenv("GATEWAY_PROXY_KEY", "").strip()

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)

        # Build messages in OpenAI chat format --------------------------
        #
        # The remote api_server can maintain session continuity via
        # X-Hermes-Session-Id, so it loads its own history.  We only
        # need to send the current user message.  If the remote has
        # no history for this session yet, include what we have locally
        # so the first exchange has context.
        #
        # We always include the current message.  For history, send a
        # compact version (text-only user/assistant turns) — the remote
        # handles tool replay and system prompts.
        api_messages: List[Dict[str, str]] = []

        if context_prompt:
            api_messages.append({"role": "system", "content": context_prompt})

        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            if role in {"user", "assistant"} and content:
                api_messages.append({"role": role, "content": content})

        api_messages.append({"role": "user", "content": message})

        # HTTP headers ---------------------------------------------------
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if proxy_key:
            headers["Authorization"] = f"Bearer {proxy_key}"
        if session_id:
            headers["X-Hermes-Session-Id"] = session_id

        body = {
            "model": "hermes-agent",
            "messages": api_messages,
            "stream": True,
        }

        # Set up platform streaming if available -------------------------
        _stream_consumer = None
        _scfg = getattr(getattr(self, "config", None), "streaming", None)
        if _scfg is None:
            from gateway.config import StreamingConfig
            _scfg = StreamingConfig()

        platform_key = _platform_config_key(source.platform)
        user_config = _load_gateway_config()
        from gateway.display_config import resolve_display_setting
        _plat_streaming = resolve_display_setting(
            user_config, platform_key, "streaming"
        )
        _streaming_enabled = (
            _scfg.enabled and _scfg.transport != "off"
            if _plat_streaming is None
            else bool(_plat_streaming)
        )

        _thread_metadata: Optional[Dict[str, Any]] = self._thread_metadata_for_source(source, event_message_id)

        if _streaming_enabled:
            try:
                from gateway.stream_consumer import GatewayStreamConsumer
                _adapter = self._adapter_for_source(source)
                if _adapter:
                    _consumer_cfg, _pause_typing_before_finalize = (
                        self._build_stream_consumer_config(
                            source, _scfg, _adapter,
                            on_missing_cursor="fallback",
                        )
                    )
                    _stream_consumer = GatewayStreamConsumer(
                        adapter=_adapter,
                        chat_id=source.chat_id,
                        config=_consumer_cfg,
                        metadata=_thread_metadata,
                        on_before_finalize=_pause_typing_before_finalize,
                        initial_reply_to_id=event_message_id,
                        run_still_current=_run_still_current,
                    )
            except Exception as _sc_err:
                logger.debug("Proxy: could not set up stream consumer: %s", _sc_err)

        # Run the stream consumer task in the background
        stream_task = None
        if _stream_consumer:
            stream_task = asyncio.create_task(_stream_consumer.run())

        # Send typing indicator
        _adapter = self._adapter_for_source(source)
        if _adapter:
            try:
                await _adapter.send_typing(source.chat_id, metadata=_thread_metadata)
            except Exception:
                pass

        # Make the HTTP request with SSE streaming -----------------------
        full_response = ""
        _start = time.time()

        try:
            _timeout = ClientTimeout(total=0, sock_read=1800)
            async with _AioClientSession(timeout=_timeout) as session:
                async with session.post(
                    f"{proxy_url}/v1/chat/completions",
                    json=body,
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.warning(
                            "Proxy error (%d) from %s: %s",
                            resp.status, proxy_url, error_text[:500],
                        )
                        return {
                            "final_response": f"⚠️ Proxy error ({resp.status}): {error_text[:300]}",
                            "messages": [],
                            "api_calls": 0,
                            "tools": [],
                        }

                    # Parse SSE stream
                    buffer = ""
                    async for chunk in resp.content.iter_any():
                        if not _run_still_current():
                            logger.info(
                                "Discarding stale proxy stream for %s — generation %d is no longer current",
                                session_key or "?",
                                run_generation or 0,
                            )
                            return {
                                "final_response": "",
                                "messages": [],
                                "api_calls": 0,
                                "tools": [],
                                "history_offset": len(history),
                                "session_id": session_id,
                                "response_previewed": False,
                            }
                        text = chunk.decode("utf-8", errors="replace")
                        buffer += text

                        # Process complete SSE lines
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith("data: "):
                                data = line[6:]
                                if data.strip() == "[DONE]":
                                    break
                                try:
                                    obj = json.loads(data)
                                    choices = obj.get("choices", [])
                                    if choices:
                                        delta = choices[0].get("delta", {})
                                        content = delta.get("content", "")
                                        if content:
                                            full_response += content
                                            if _stream_consumer:
                                                _stream_consumer.on_delta(content)
                                except json.JSONDecodeError:
                                    pass
                        if len(buffer) > _GATEWAY_PROXY_SSE_BUFFER_MAX_CHARS:
                            raise ValueError(
                                "Proxy SSE stream exceeded max buffer size without a line boundary"
                            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Proxy connection error to %s: %s", proxy_url, e)
            if not full_response:
                return {
                    "final_response": f"⚠️ Proxy connection error: {e}",
                    "messages": [],
                    "api_calls": 0,
                    "tools": [],
                }
            # Partial response — return what we got
        finally:
            # Finalize stream consumer
            if _stream_consumer:
                _stream_consumer.finish()
            if stream_task:
                try:
                    await asyncio.wait_for(stream_task, timeout=5.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    stream_task.cancel()

        _elapsed = time.time() - _start
        if not _run_still_current():
            logger.info(
                "Discarding stale proxy result for %s — generation %d is no longer current",
                session_key or "?",
                run_generation or 0,
            )
            return {
                "final_response": "",
                "messages": [],
                "api_calls": 0,
                "tools": [],
                "history_offset": len(history),
                "session_id": session_id,
                "response_previewed": False,
            }
        logger.info(
            "proxy response: url=%s session=%s time=%.1fs response=%d chars",
            proxy_url, (session_id or "")[:20], _elapsed, len(full_response),
        )

        return {
            "final_response": full_response or "(No response from remote agent)",
            "messages": [
                {"role": "user", "content": message},
                {"role": "assistant", "content": full_response},
            ],
            "api_calls": 1,
            "tools": [],
            "history_offset": len(history),
            "session_id": session_id,
            "response_previewed": _stream_consumer is not None and bool(full_response),
        }


    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        _interrupt_depth: int = 0,
        event_message_id: Optional[str] = None,
        channel_prompt: Optional[str] = None,
        moa_config: Optional[dict] = None,
        persist_user_message: Optional[Any] = None,
        persist_user_timestamp: Optional[float] = None,
        message_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Profile-scoping wrapper around the agent run.

        When multiplexing is active, resolve the inbound source's profile and
        run the whole turn inside ``_profile_runtime_scope`` so config/skills/
        memory resolve to that profile's home AND credentials resolve from that
        profile's secret scope (never the process-global ``os.environ``). When
        multiplexing is off this is a transparent pass-through — zero behavior
        change for single-profile gateways.
        """
        from gateway.run import _profile_runtime_scope
        if not getattr(getattr(self, "config", None), "multiplex_profiles", False):
            return await self._run_agent_inner(
                message, context_prompt, history, source, session_id,
                session_key=session_key, run_generation=run_generation,
                _interrupt_depth=_interrupt_depth, event_message_id=event_message_id,
                channel_prompt=channel_prompt, moa_config=moa_config,
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
                message_type=message_type,
            )

        profile_home = self._resolve_profile_home_for_source(source)
        with _profile_runtime_scope(profile_home):
            return await self._run_agent_inner(
                message, context_prompt, history, source, session_id,
                session_key=session_key, run_generation=run_generation,
                _interrupt_depth=_interrupt_depth, event_message_id=event_message_id,
                channel_prompt=channel_prompt, moa_config=moa_config,
                persist_user_message=persist_user_message,
                persist_user_timestamp=persist_user_timestamp,
                message_type=message_type,
            )


    async def _run_agent_inner(
        self,
        message: str,
        context_prompt: str,
        history: List[Dict[str, Any]],
        source: SessionSource,
        session_id: str,
        session_key: str = None,
        run_generation: Optional[int] = None,
        _interrupt_depth: int = 0,
        event_message_id: Optional[str] = None,
        channel_prompt: Optional[str] = None,
        moa_config: Optional[dict] = None,
        persist_user_message: Optional[Any] = None,
        persist_user_timestamp: Optional[float] = None,
        message_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run the agent with the given message and context.

        Returns the full result dict from run_conversation, including:
          - "final_response": str (the text to send back)
          - "messages": list (full conversation including tool calls)
          - "api_calls": int
          - "completed": bool

        This is run in a thread pool to not block the event loop.
        Supports interruption via new messages.
        """
        from gateway.run import TurnRunner, _INTERRUPT_REASON_TIMEOUT, _abandon_timed_out_gateway_turn, _build_media_placeholder, _dequeue_pending_event, _float_env, _gateway_platform_value, _has_platform_display_override, _hermes_home, _is_control_interrupt_message, _load_gateway_config, _non_conversational_metadata, _platform_config_key, _preserve_queued_followup_history_offset, _resolve_gateway_model, _resolve_progress_thread_id, _watch_gateway_turn_inactivity, time
        # ---- Proxy mode: delegate to remote API server ----
        if self._get_proxy_url():
            return await self._run_agent_via_proxy(
                message=message,
                context_prompt=context_prompt,
                history=history,
                source=source,
                session_id=session_id,
                session_key=session_key,
                run_generation=run_generation,
                event_message_id=event_message_id,
            )

        from run_agent import AIAgent
        import queue

        def _run_still_current() -> bool:
            if run_generation is None or not session_key:
                return True
            return self._is_session_run_current(session_key, run_generation)

        user_config = _load_gateway_config()
        platform_key = _platform_config_key(source.platform)

        from hermes_cli.tools_config import _get_platform_tools
        enabled_toolsets = sorted(_get_platform_tools(user_config, platform_key))
        agent_cfg_local = user_config.get("agent") or {}
        disabled_toolsets = agent_cfg_local.get("disabled_toolsets") or None

        display_config = user_config.get("display", {})
        if not isinstance(display_config, dict):
            display_config = {}

        # Per-platform display settings — resolve via display_config module
        # which checks display.platforms.<platform>.<key> first, then
        # display.<key> global, then built-in platform defaults.
        from gateway.display_config import resolve_display_setting

        # Apply tool preview length config (0 = no limit)
        try:
            from agent.display import set_tool_preview_max_len
            _tpl = resolve_display_setting(user_config, platform_key, "tool_preview_length", 0)
            set_tool_preview_max_len(int(_tpl) if _tpl else 0)
        except Exception:
            pass

        # Apply friendly tool labels config (default on) — per-platform aware
        try:
            from agent.display import set_friendly_tool_labels
            _ftl = resolve_display_setting(user_config, platform_key, "friendly_tool_labels", True)
            set_friendly_tool_labels(bool(_ftl))
        except Exception:
            pass

        # Tool progress mode — resolved per-platform with env var fallback
        _resolved_tp = resolve_display_setting(user_config, platform_key, "tool_progress")
        _env_tp = os.getenv("HERMES_TOOL_PROGRESS_MODE")
        _display_cfg = display_config if isinstance(display_config, dict) else {}
        _platforms_cfg = _display_cfg.get("platforms") or {}
        _platform_cfg = _platforms_cfg.get(platform_key) or {}
        _legacy_tp_overrides = _display_cfg.get("tool_progress_overrides") or {}
        _tool_progress_configured = (
            "tool_progress" in _display_cfg
            or (
                isinstance(_platform_cfg, dict)
                and "tool_progress" in _platform_cfg
            )
            or (
                isinstance(_legacy_tp_overrides, dict)
                and platform_key in _legacy_tp_overrides
            )
        )
        progress_mode = (
            _env_tp
            if _env_tp and not _tool_progress_configured
            else (_resolved_tp or _env_tp or "all")
        )
        # Tool progress grouping: "accumulate" (edit one bubble) or "separate" (one msg per tool)
        progress_grouping = resolve_display_setting(user_config, platform_key, "tool_progress_grouping") or "accumulate"
        from gateway.status_phrases import choose_status_phrase, resolve_status_phrase_catalog
        _generic_status_recent: List[str] = []
        _generic_status_catalog = resolve_status_phrase_catalog(user_config, platform_key)

        def _display_surface_mode(
            setting: str,
            *,
            default: bool = False,
            require_platform_override_for: set[Any] | None = None,
            allow_generic: bool = False,
        ) -> str:
            """Return off|raw|generic for a gateway visibility surface."""
            if require_platform_override_for:
                current_platform = _gateway_platform_value(source.platform)
                platform_only = {
                    _gateway_platform_value(item)
                    for item in require_platform_override_for
                }
                if (
                    current_platform in platform_only
                    and not _has_platform_display_override(user_config, platform_key, setting)
                ):
                    return "off"
            value = resolve_display_setting(user_config, platform_key, setting, default)
            if isinstance(value, str) and value.strip().lower() == "generic":
                return "generic" if allow_generic else "off"
            return "raw" if bool(value) else "off"

        def _generic_status_phrase(kind: str, *, tool_name: str | None = None, preview: str | None = None, args: Any = None) -> str:
            try:
                return choose_status_phrase(
                    kind,
                    tool_name=tool_name,
                    preview=preview,
                    args=args,
                    recent=_generic_status_recent,
                    catalog=_generic_status_catalog,
                )
            except Exception as _phrase_err:
                logger.debug("generic status phrase selection failed: %s", _phrase_err)
                return "still on it" if kind in {"heartbeat", "waiting", "long_running", "status"} else "one sec"
        # Disable tool progress for webhooks - they don't support message editing,
        # so each progress line would be sent as a separate message.
        from gateway.config import Platform
        tool_progress_enabled = progress_mode not in {"off", "log"} and source.platform != Platform.WEBHOOK
        # Live working-state status for text-rendering typing indicators
        # (Slack's assistant status line). Independent of tool_progress —
        # Slack defaults tool_progress off (permanent lines spam channels)
        # but the status line is ephemeral, so live status stays useful
        # there. Rendering rides the existing _keep_typing refresh: the
        # callback only stores a phrase on the adapter, costing zero extra
        # platform API calls.
        _live_status_mode = resolve_display_setting(
            user_config, platform_key, "live_status", "full"
        )
        _live_status_adapter = self._adapter_for_source(source)
        if not getattr(_live_status_adapter, "supports_status_text", False):
            _live_status_adapter = None
        if _live_status_mode == "off":
            _live_status_adapter = None
        # "log" mode: tool calls are written to ~/.hermes/logs/tool_calls.log
        # instead of the chat (#3459 / #3458). Gateway-only by design.
        log_mode_enabled = progress_mode == "log" and source.platform != Platform.WEBHOOK
        log_queue: "queue.Queue | None" = queue.Queue() if log_mode_enabled else None
        # Natural assistant status messages are intentionally independent from
        # tool progress and token streaming. Users can keep tool_progress quiet
        # in chat platforms while opting into concise mid-turn updates.
        interim_assistant_messages_mode = _display_surface_mode(
            "interim_assistant_messages",
            default=True,
            require_platform_override_for={Platform.MATTERMOST},
        )
        interim_assistant_messages_enabled = (
            source.platform != Platform.WEBHOOK
            and interim_assistant_messages_mode != "off"
        )
        # thinking_progress is independent — if enabled, we need the progress
        # queue even when tool_progress is off (thinking relay uses same infra).
        # Mattermost requires a per-platform opt-in: global scratch-text display
        # is too easy to leak into busy public threads.
        _thinking_mode = _display_surface_mode(
            "thinking_progress",
            default=False,
            require_platform_override_for={Platform.MATTERMOST},
        )
        _thinking_enabled = _thinking_mode != "off"
        needs_progress_queue = tool_progress_enabled or _thinking_enabled


        # Queue for progress messages (thread-safe)
        progress_queue = queue.Queue() if needs_progress_queue else None
        last_tool = [None]  # Mutable container for tracking in closure
        last_progress_msg = [None]  # Track last message for dedup
        repeat_count = [0]  # How many times the same message repeated
        # True when the previously enqueued progress line was a terminal
        # fenced code block — consecutive terminal calls then drop the
        # repeated "💻 terminal" header and render back-to-back blocks.
        last_was_terminal_block = [False]

        # ── Discord voice "verbal ack before tool calls" ────────────────
        # When the bot is in a voice channel with the continuous mixer
        # installed (discord.voice_fx.enabled), speak a short phrase ("let me
        # look into that") over the ambient idle bed on the FIRST tool call of
        # the turn.  Fires from tool_start_callback (independent of the
        # tool-progress text gate), at most once per turn.  No-op on every
        # other platform / when not in a voice channel.
        _voice_ack_fired = [False]
        _voice_ack_guild: List[Optional[int]] = [None]
        if source.platform == Platform.DISCORD:
            _va = self.adapters.get(Platform.DISCORD)
            # source.chat_id is the linked text channel; resolve the guild whose
            # voice connection is bound to it (mirrors DiscordAdapter.play_tts).
            _vtc = getattr(_va, "_voice_text_channels", None)
            if isinstance(_vtc, dict) and hasattr(_va, "voice_mixer_active"):
                for _gid, _tc in _vtc.items():
                    if str(_tc) == str(source.chat_id) and _va.voice_mixer_active(_gid):
                        _voice_ack_guild[0] = _gid
                        break
        _voice_ack_loop = asyncio.get_running_loop()

        # voice_ack_callback extracted to TurnRunner.voice_ack_callback
        # (published onto turn_ctx after the runner is constructed below).

        # Auto-cleanup of temporary progress bubbles (Telegram + any adapter
        # that implements ``delete_message``). When enabled via
        # ``display.platforms.<platform>.cleanup_progress: true``, message IDs
        # from the tool-progress / "⏳ Working — N min" / status-callback bubbles
        # are collected here and deleted after the final response lands.
        # Failed runs skip cleanup so the bubbles remain as breadcrumbs.
        _cleanup_progress = bool(
            resolve_display_setting(user_config, platform_key, "cleanup_progress")
        )
        _cleanup_adapter = self._adapter_for_source(source) if _cleanup_progress else None
        # getattr, not attribute access — same duck-typed-adapter guard as the
        # edit_message check in send_progress_messages below: a fake/minimal
        # adapter without delete_message means "can't delete", not a crash.
        _cleanup_delete = getattr(type(_cleanup_adapter), "delete_message", None) if _cleanup_adapter is not None else None
        if _cleanup_adapter is not None and (
            _cleanup_delete is None
            or _cleanup_delete is BasePlatformAdapter.delete_message
        ):
            # Adapter doesn't support deletion — silently disable.
            _cleanup_progress = False
            _cleanup_adapter = None
        _cleanup_msg_ids: List[str] = []
        # First-touch onboarding latch: fires at most once per run, even if
        # several tools exceed the threshold.
        long_tool_hint_fired = [False]
        _LONG_TOOL_THRESHOLD_S = 30.0

        turn_ctx = TurnContext(
            source=source,
            _run_still_current=_run_still_current,
            _live_status_adapter=_live_status_adapter,
            _live_status_mode=_live_status_mode,
            _thinking_enabled=_thinking_enabled,
            progress_mode=progress_mode,
            progress_grouping=progress_grouping,
            tool_progress_enabled=tool_progress_enabled,
            progress_queue=progress_queue,
            log_queue=log_queue,
            last_progress_msg=last_progress_msg,
            last_tool=last_tool,
            last_was_terminal_block=last_was_terminal_block,
            repeat_count=repeat_count,
            long_tool_hint_fired=long_tool_hint_fired,
            _LONG_TOOL_THRESHOLD_S=_LONG_TOOL_THRESHOLD_S,
            _cleanup_progress=_cleanup_progress,
            _cleanup_msg_ids=_cleanup_msg_ids,
            message=message,
            AIAgent=AIAgent,
            resolve_display_setting=resolve_display_setting,
            user_config=user_config,
            enabled_toolsets=enabled_toolsets,
            disabled_toolsets=disabled_toolsets,
            log_mode_enabled=log_mode_enabled,
            interim_assistant_messages_enabled=interim_assistant_messages_enabled,
            needs_progress_queue=needs_progress_queue,
            _voice_ack_fired=_voice_ack_fired,
            _voice_ack_guild=_voice_ack_guild,
            _voice_ack_loop=_voice_ack_loop,
            history=history,
            context_prompt=context_prompt,
            channel_prompt=channel_prompt,
            session_id=session_id,
            session_key=session_key,
            run_generation=run_generation,
            _interrupt_depth=_interrupt_depth,
            event_message_id=event_message_id,
            moa_config=moa_config,
            persist_user_message=persist_user_message,
            persist_user_timestamp=persist_user_timestamp,
        )
        turn_runner = TurnRunner(self, turn_ctx)
        # Callback invoked by agent on tool lifecycle events — extracted to
        # TurnRunner.progress_callback (bound method, same signature).
        turn_ctx.progress_callback = turn_runner.progress_callback
        turn_ctx.voice_ack_callback = turn_runner.voice_ack_callback

        # Background task to send progress messages
        # Accumulates tool lines into a single message that gets edited.
        #
        # Threading metadata is platform-specific:
        # - Slack DM threading needs event_message_id fallback (reply thread)
        # - Telegram forum topics use message_thread_id; Hermes-created private
        #   DM topic lanes require both thread metadata and a reply anchor
        # - Feishu only honors reply_in_thread when sending a reply, so topic
        #   progress uses the triggering event message as the reply target
        # - Other platforms should use explicit source.thread_id only
        #
        # Slack honours platforms.slack.extra.reply_in_thread=false: if the
        # user has opted out of threaded replies, don't synthesise a thread
        # for progress messages either — the very first progress message
        # would otherwise create a thread that all subsequent replies
        # (including the final answer) would inherit (#18859).
        _progress_reply_in_thread = True
        if source.platform == Platform.SLACK:
            _slack_adapter_for_progress = self._adapter_for_source(source)
            if _slack_adapter_for_progress is not None:
                try:
                    # Relay lane: the adapter owns mode resolution (nested
                    # platforms.relay.extra.slack subset with flat-key
                    # fallback). Native lane: read the flat extra as before.
                    _mode_fn = getattr(
                        _slack_adapter_for_progress,
                        "_effective_reply_in_thread",
                        None,
                    )
                    if callable(_mode_fn):
                        _progress_reply_in_thread = bool(_mode_fn())
                    else:
                        _progress_reply_in_thread = bool(
                            _slack_adapter_for_progress.config.extra.get(
                                "reply_in_thread", True
                            )
                        )
                except Exception:
                    _progress_reply_in_thread = True
        _progress_thread_id = _resolve_progress_thread_id(
            source.platform, source.thread_id, event_message_id,
            reply_in_thread=_progress_reply_in_thread,
        )
        _progress_metadata = (
            self._thread_metadata_for_source(source, event_message_id)
            if _progress_thread_id == source.thread_id
            else self._thread_metadata_for_target(
                source.platform,
                source.chat_id,
                _progress_thread_id,
                chat_type=getattr(source, "chat_type", None),
                reply_to_message_id=event_message_id,
            )
        ) if _progress_thread_id else None
        _progress_metadata = _non_conversational_metadata(_progress_metadata, platform=source.platform)
        _progress_reply_to = (
            event_message_id
            if source.platform in (Platform.FEISHU, Platform.MATTERMOST) and source.thread_id and event_message_id
            else None
        )

        async def write_tool_log():
            """Drain log_queue and append tool-call lines to tool_calls.log.

            Only active when ``display.tool_progress`` is ``log``. Uses a
            RotatingFileHandler (5MB × 3 backups) so the audit log can't grow
            unbounded, and the shared RedactingFormatter so secrets never land
            on disk.
            """
            if log_queue is None:
                return
            from logging.handlers import RotatingFileHandler

            from agent.redact import RedactingFormatter

            log_dir = _hermes_home / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "tool_calls.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            file_handler.setFormatter(RedactingFormatter("%(message)s"))
            tool_logger = logging.getLogger(f"hermes.tool_calls.{id(log_queue)}")
            tool_logger.setLevel(logging.INFO)
            tool_logger.propagate = False
            tool_logger.addHandler(file_handler)
            try:
                while True:
                    try:
                        tool_logger.info("%s", log_queue.get_nowait())
                    except queue.Empty:
                        await asyncio.sleep(0.3)
                    except Exception as e:
                        logger.error("write_tool_log error: %s", e)
                        await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass
            finally:
                # Drain remaining entries before closing so late tool calls
                # from the final iteration aren't lost.
                while True:
                    try:
                        tool_logger.info("%s", log_queue.get_nowait())
                    except queue.Empty:
                        break
                    except Exception:
                        break
                tool_logger.removeHandler(file_handler)
                try:
                    file_handler.flush()
                    file_handler.close()
                except Exception:
                    pass

        # Extracted to TurnRunner.send_progress_messages. The threading
        # metadata computed above is published onto the shared TurnContext
        # exactly where the original closure's captured locals were bound.
        turn_ctx._progress_metadata = _progress_metadata
        turn_ctx._progress_reply_to = _progress_reply_to
        send_progress_messages = turn_runner.send_progress_messages

        # We need to share the agent instance for interrupt support
        agent_holder = [None]  # Mutable container for the agent instance
        turn_ctx.agent_holder = agent_holder
        result_holder = [None]  # Mutable container for the result
        tools_holder = [None]   # Mutable container for the tool definitions
        stream_consumer_holder = [None]  # Mutable container for stream consumer
        # #60671 — streaming PCM audio consumer.  Created on the gateway
        # event-loop thread (NOT inside run_sync's executor worker) so the
        # outer finalisation / interrupt paths can reference it without a
        # cross-scope NameError.
        streaming_tts_consumer_holder: list = [None]
        turn_ctx.result_holder = result_holder
        turn_ctx.tools_holder = tools_holder
        turn_ctx.stream_consumer_holder = stream_consumer_holder
        turn_ctx.streaming_tts_consumer_holder = streaming_tts_consumer_holder

        # Bridge sync step_callback → async hooks.emit for agent:step events
        _loop_for_step = asyncio.get_running_loop()
        _hooks_ref = self.hooks

        # Bridge extracted to TurnRunner._step_callback_sync; the loop and
        # hooks refs bound just above are published at their original site.
        turn_ctx._loop_for_step = _loop_for_step
        turn_ctx._hooks_ref = _hooks_ref
        turn_ctx._step_callback_sync = turn_runner._step_callback_sync

        # Bridge sync event_callback → async hooks.emit for lifecycle events
        # (e.g. session:compress fires after context compression splits a session)
        # Bridge extracted to TurnRunner._event_callback_sync.
        turn_ctx._event_callback_sync = turn_runner._event_callback_sync

        # Bridge sync status_callback → async adapter.send for context pressure
        _status_adapter = self._adapter_for_source(source)
        _status_chat_id = source.chat_id
        if source.platform == Platform.FEISHU and source.thread_id and event_message_id:
            # Feishu topics only keep messages inside the topic when they are
            # sent via the reply API with reply_in_thread=true. Status/interim,
            # approval, and stream-consumer paths usually only receive metadata,
            # so carry the triggering message id as a Feishu-specific fallback.
            _status_thread_metadata: Optional[Dict[str, Any]] = {
                "thread_id": _progress_thread_id,
                "reply_to_message_id": event_message_id,
            }
        else:
            _status_thread_metadata = (
                self._thread_metadata_for_source(source, event_message_id)
                if _progress_thread_id == source.thread_id
                else self._thread_metadata_for_target(
                    source.platform,
                    source.chat_id,
                    _progress_thread_id,
                    chat_type=getattr(source, "chat_type", None),
                    reply_to_message_id=event_message_id,
                )
            ) if _progress_thread_id else None

        # Bridge extracted to TurnRunner._status_callback_sync; publish the
        # status wiring computed above onto the shared TurnContext at the
        # exact original binding site.
        turn_ctx._status_adapter = _status_adapter
        turn_ctx._status_chat_id = _status_chat_id
        turn_ctx._status_thread_metadata = _status_thread_metadata
        turn_ctx._status_callback_sync = turn_runner._status_callback_sync

        # ---- Streaming TTS consumer setup (#60671) ----
        # Created on the gateway event-loop thread (here, in _run_agent_inner),
        # NOT inside run_sync's executor worker.  This avoids a cross-scope
        # NameError: the outer interrupt / finalisation paths reference the
        # consumer via ``streaming_tts_consumer_holder[0]``.
        #
        # Gates: voice input, auto-TTS enabled for this chat, adapter
        # supports streaming, and a usable streaming TTS provider configured.
        _stts_adapter = self._adapter_for_source(source)
        _is_voice_input = (
            message_type is not None
            and str(getattr(message_type, "value", message_type)).lower() == "voice"
        )
        if (
            _stts_adapter is not None
            and _is_voice_input
            and _stts_adapter._should_auto_tts_for_chat(source.chat_id)
        ):
            try:
                from gateway.streaming_tts_consumer import StreamingTTSConsumer
                from tools.tts_tool import _load_tts_config
                _tts_cfg = _load_tts_config()
                _gateway_loop = self._gateway_loop or asyncio.get_event_loop()
                _stts_consumer = StreamingTTSConsumer(
                    adapter=_stts_adapter,
                    chat_id=source.chat_id,
                    tts_config=_tts_cfg,
                    loop=_gateway_loop,
                    metadata=_status_thread_metadata,
                )
                if _stts_consumer.active:
                    streaming_tts_consumer_holder[0] = _stts_consumer
                    _stts_consumer.start()
                # else: consumer inactive (no streaming provider) — leave
                # the holder as None so the whole-file fallback path runs.
            except Exception as _stts_err:
                logger.debug("Could not set up streaming TTS consumer: %s", _stts_err)

        # run_sync extracted to TurnRunner.run_sync (bound method; the
        # executor call below is unchanged).  Its closed-over locals travel
        # on turn_ctx; `nonlocal message` rebinds became ctx.message writes.
        run_sync = turn_runner.run_sync

        # Start progress message sender if enabled. Gate on needs_progress_queue
        # (tool_progress OR thinking_progress), not tool_progress alone: the
        # sender drains BOTH tool-progress lines and _thinking scratch bubbles.
        # With the old tool_progress-only gate, a thinking_progress:true /
        # tool_progress:off user had the callback queue _thinking messages that
        # no task ever drained — so they silently never appeared.
        progress_task = None
        if needs_progress_queue:
            progress_task = asyncio.create_task(send_progress_messages())

        # Start the tool-call log writer when tool_progress == "log".
        log_task = None
        if log_mode_enabled:
            log_task = asyncio.create_task(write_tool_log())

        # Start stream consumer task — polls for consumer creation since it
        # happens inside run_sync (thread pool) after the agent is constructed.
        stream_task = None

        async def _start_stream_consumer():
            """Wait for the stream consumer to be created, then run it."""
            for _ in range(200):  # Up to 10s wait
                if stream_consumer_holder[0] is not None:
                    await stream_consumer_holder[0].run()
                    return
                await asyncio.sleep(0.05)

        stream_task = asyncio.create_task(_start_stream_consumer())

        # Track this agent as running for this session (for interrupt support)
        # We do this in a callback after the agent is created
        async def track_agent():
            # Wait for agent to be created
            while agent_holder[0] is None:
                await asyncio.sleep(0.05)
            if not session_key:
                return
            # Only promote the sentinel to the real agent if this run is still
            # current.  If /stop or /new bumped the generation while we were
            # spinning up, leave the newer run's slot alone — we'll be
            # discarded by the stale-result check in _handle_message_with_agent.
            if run_generation is not None and not self._is_session_run_current(
                session_key, run_generation
            ):
                logger.info(
                    "Skipping stale agent promotion for %s — generation %s is no longer current",
                    session_key or "",
                    run_generation,
                )
                return
            self._session_state(session_key).turn.agent = agent_holder[0]
            if self._draining:
                self._update_runtime_status("draining")

        tracking_task = asyncio.create_task(track_agent())

        # Monitor for interrupts from the adapter (new messages arriving).
        # This is the PRIMARY interrupt path for regular text messages —
        # Level 1 (base.py) catches them before _handle_message() is reached,
        # so the Level 2 running_agent.interrupt() path never fires.
        # The inactivity poll loop below has a BACKUP check in case this
        # task dies (no error handling = silent death = lost interrupts).
        _interrupt_detected = asyncio.Event()  # shared with backup check

        async def monitor_for_interrupt():
            if not session_key:
                return

            while True:
                await asyncio.sleep(0.2)  # Check every 200ms
                try:
                    # Re-resolve adapter each iteration so reconnects don't
                    # leave us holding a stale reference.
                    _adapter = self._adapter_for_source(source)
                    if not _adapter:
                        continue
                    # Check if adapter has a pending interrupt for this session.
                    # Must use session_key (build_session_key output) — NOT
                    # source.chat_id — because the adapter stores interrupt events
                    # under the full session key.
                    if hasattr(_adapter, 'has_pending_interrupt') and _adapter.has_pending_interrupt(session_key):
                        agent = agent_holder[0]
                        if agent:
                            # Peek at the pending message text WITHOUT consuming it.
                            # The message must remain in _pending_messages so the
                            # post-run dequeue at _dequeue_pending_event() can
                            # retrieve the full MessageEvent (with media metadata).
                            # If we pop here, a race exists: the agent may finish
                            # before checking _interrupt_requested, and the message
                            # is lost — neither the interrupt path nor the dequeue
                            # path finds it.
                            _peek_event = _adapter._pending_messages.get(session_key)
                            pending_text = None
                            if _peek_event is not None:
                                pending_text = _peek_event.text or ""
                                # Transcribe audio media BEFORE signaling the
                                # agent, so voice messages interrupt with the
                                # real transcript instead of an empty string
                                # (or file-path placeholder). Matches the UX
                                # of fresh voice messages including the
                                # optional 🎙️ echo back to the user.
                                _media_urls = getattr(_peek_event, "media_urls", None) or []
                                if self._pending_event_audio_paths(_peek_event):
                                    pending_text, _ = await self._transcribe_and_echo_pending_voice(
                                        _peek_event,
                                        _adapter,
                                        source,
                                        pending_text,
                                        log_context="Voice-interrupt",
                                        metadata={"thread_id": source.thread_id} if source.thread_id else None,
                                    )
                                elif not pending_text and _media_urls:
                                    pending_text = _build_media_placeholder(_peek_event)
                            logger.debug("Interrupt detected from adapter, signaling agent...")
                            agent.interrupt(pending_text)
                            _interrupt_detected.set()
                            # Abort streaming TTS on barge-in (#60671).
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort("barge-in")
                            break
                except asyncio.CancelledError:
                    raise
                except Exception as _mon_err:
                    logger.debug("monitor_for_interrupt error (will retry): %s", _mon_err)

        interrupt_monitor = asyncio.create_task(monitor_for_interrupt())

        # Periodic "still working" notifications for long-running tasks.
        # Fires every N seconds so the user knows the agent hasn't died.
        # Config: agent.gateway_notify_interval in config.yaml, or
        # HERMES_AGENT_NOTIFY_INTERVAL env var.  Default 180s (3 min).
        # 0 = disable notifications.
        _NOTIFY_INTERVAL_RAW = _float_env("HERMES_AGENT_NOTIFY_INTERVAL", 180)
        _NOTIFY_INTERVAL = _NOTIFY_INTERVAL_RAW if _NOTIFY_INTERVAL_RAW > 0 else None
        _long_running_mode = _display_surface_mode(
            "long_running_notifications",
            default=True,
            allow_generic=True,
        )
        if _long_running_mode == "off":
            _NOTIFY_INTERVAL = None
        _notify_start = time.time()

        async def _notify_long_running():
            if _NOTIFY_INTERVAL is None:
                return  # Notifications disabled (gateway_notify_interval: 0)
            _notify_adapter = self._adapter_for_source(source)
            if not _notify_adapter:
                return
            # Track the heartbeat message id so we can edit-in-place on
            # platforms that support it (Telegram, Discord, Slack, etc.)
            # instead of spamming a new "Still working" bubble every
            # interval. Falls back to send-new when edit fails or isn't
            # supported by the adapter.
            _heartbeat_msg_id: Optional[str] = None
            while True:
                await asyncio.sleep(_NOTIFY_INTERVAL)
                # Stop heartbeating once this run no longer owns the session
                # slot or the executor has finished — otherwise a stale
                # "running: delegate_task" bubble can outlive the run that
                # spawned it (#12029). _executor_task is a closure var bound
                # just after this task is scheduled; tolerate the brief window
                # before then (the first wake is _NOTIFY_INTERVAL away anyway).
                try:
                    _exec_ref = _executor_task
                except NameError:
                    _exec_ref = None
                if not self._should_emit_long_running_notification(
                    session_key, agent_holder[0], _exec_ref
                ):
                    break
                _elapsed_mins = int((time.time() - _notify_start) // 60)
                # Include agent activity context if available. Default
                # heartbeat is terse: elapsed + current tool. Verbose
                # iteration counter is gated on busy_ack_detail so users
                # who want it can opt in per platform.
                _agent_ref = agent_holder[0]
                _status_detail = ""
                _want_iteration_detail = bool(
                    resolve_display_setting(
                        user_config,
                        platform_key,
                        "busy_ack_detail",
                        True,
                    )
                )
                if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                    try:
                        _a = _agent_ref.get_activity_summary()
                        _parts = []
                        if _want_iteration_detail:
                            _parts.append(
                                f"iteration {_a['api_call_count']}/{_a['max_iterations']}"
                            )
                        _action = _a.get("current_tool") or _a.get("last_activity_desc")
                        if _action:
                            _parts.append(str(_action))
                        if _parts:
                            _status_detail = " — " + ", ".join(_parts)
                    except Exception:
                        pass
                _heartbeat_text = (
                    _generic_status_phrase("status")
                    if _long_running_mode == "generic"
                    else f"⏳ Working — {_elapsed_mins} min{_status_detail}"
                )
                try:
                    _notify_res = None
                    if _heartbeat_msg_id:
                        try:
                            _notify_res = await _notify_adapter.edit_message(
                                source.chat_id,
                                _heartbeat_msg_id,
                                _heartbeat_text,
                            )
                        except Exception as _ee:
                            logger.debug("Heartbeat edit failed: %s", _ee)
                            _notify_res = None
                    if not (_notify_res and getattr(_notify_res, "success", False)):
                        _notify_res = await _notify_adapter.send(
                            source.chat_id,
                            _heartbeat_text,
                            metadata=_non_conversational_metadata(_status_thread_metadata, platform=source.platform),
                        )
                        if getattr(_notify_res, "success", False) and getattr(
                            _notify_res, "message_id", None
                        ):
                            _heartbeat_msg_id = str(_notify_res.message_id)
                            if _cleanup_progress:
                                _cleanup_msg_ids.append(_heartbeat_msg_id)
                except Exception as _ne:
                    logger.debug("Long-running notification error: %s", _ne)

        _notify_task = asyncio.create_task(_notify_long_running())

        def _stream_confirmed_final_delivery(
            consumer,
            final_text: str,
            *,
            previewed: bool = False,
        ) -> bool:
            """Return True only when the actual final reply reached the user."""
            if consumer is None:
                return False
            if getattr(consumer, "final_response_sent", False):
                # A successful finalize call is not proof the *content* was
                # final: the edit may have carried only the last preview
                # snapshot while the tail generated between that snapshot and
                # stream completion never reached any API call (#71643).
                # Reconcile the recorded turn-final payload against the
                # completed response; only a demonstrable mismatch (False)
                # overrides the flag — None (no record / multi-message split
                # delivery) keeps the legacy trust so overflow splits are not
                # re-sent.
                matcher = getattr(consumer, "delivered_final_matches", None)
                if callable(matcher):
                    try:
                        if matcher(final_text) is False:
                            return False
                    except Exception:
                        pass
                return True
            if previewed:
                has_delivered_text = getattr(consumer, "has_delivered_text", None)
                if callable(has_delivered_text):
                    try:
                        return bool(has_delivered_text(final_text))
                    except Exception:
                        return False
            return False

        try:
            # Run in thread pool to not block.  Use an *inactivity*-based
            # timeout instead of a wall-clock limit: the agent can run for
            # hours if it's actively calling tools / receiving stream tokens,
            # but a hung API call or stuck tool with no activity for the
            # configured duration is caught and killed.  (#4815)
            #
            # Config: agent.gateway_timeout in config.yaml, or
            # HERMES_AGENT_TIMEOUT env var (env var takes precedence).
            # Default 1800s (30 min inactivity).  0 = unlimited.
            _agent_timeout_raw = _float_env("HERMES_AGENT_TIMEOUT", 1800)
            _agent_timeout = _agent_timeout_raw if _agent_timeout_raw > 0 else None
            _agent_warning_raw = _float_env("HERMES_AGENT_TIMEOUT_WARNING", 900)
            _agent_warning = _agent_warning_raw if _agent_warning_raw > 0 else None
            _warning_fired = False

            # A background=true process intentionally survives a successful
            # turn, so capture existing IDs and reap only children created by
            # THIS turn if it times out. The daemon watchdog is independent of
            # asyncio: cgroup memory reclaim may starve the event loop that runs
            # the normal timeout poll, but it need not also postpone cleanup
            # until the loop recovers (#76115).
            from tools.process_registry import process_registry

            _turn_task_id = session_id or ""
            _turn_process_baseline = process_registry.snapshot_running_ids(_turn_task_id)
            turn_ctx.process_task_id = _turn_task_id
            turn_ctx.process_baseline = _turn_process_baseline
            _turn_worker_done = threading.Event()
            _turn_timeout_fired = threading.Event()
            _turn_cleanup_lock = threading.Lock()
            # task_id above is session-scoped, not turn-scoped (#76115
            # review): gate the eventual reap on this exact claim still
            # being current, so a replacement turn that starts on the same
            # session before the watchdog fires doesn't get its own fresh
            # process killed by this turn's stale baseline.
            _turn_run_generation = run_generation
            _turn_is_current = (
                (lambda: self._is_session_run_current(session_key, _turn_run_generation))
                if _turn_run_generation is not None
                else (lambda: True)
            )

            def _run_sync_with_timeout_lifecycle():
                try:
                    return run_sync()
                finally:
                    _turn_worker_done.set()
                    # `.turn.agent` on the session state is only reset to
                    # _AGENT_PENDING_SENTINEL when the *next* turn is
                    # claimed (see _session_state(...).turn.agent = ... at
                    # claim time), so a stale reference to this exact agent
                    # instance stays reachable from
                    # _interrupt_and_clear_session() until then. Clearing
                    # the ownership markers here — the instant this turn's
                    # own worker finishes — closes that window: an
                    # explicit /stop landing on the already-finished turn
                    # no longer reaps background work the turn deliberately
                    # left running (#76115).
                    _finished_agent = agent_holder[0] if agent_holder else None
                    if _finished_agent is not None:
                        _finished_agent._gateway_turn_process_task_id = ""
                        _finished_agent._gateway_turn_process_baseline = frozenset()

            if _agent_timeout is not None:
                threading.Thread(
                    target=_watch_gateway_turn_inactivity,
                    kwargs={
                        "agent_holder": agent_holder,
                        "task_id": _turn_task_id,
                        "process_baseline": _turn_process_baseline,
                        "timeout": _agent_timeout,
                        "worker_done": _turn_worker_done,
                        "timeout_fired": _turn_timeout_fired,
                        "cleanup_lock": _turn_cleanup_lock,
                        "poll_interval": 5.0,
                        "is_still_current": _turn_is_current,
                    },
                    name=f"gateway-turn-watchdog-{_turn_task_id[:12]}",
                    daemon=True,
                ).start()
            _executor_task = asyncio.ensure_future(
                self._run_in_executor_with_context(_run_sync_with_timeout_lifecycle)
            )

            _inactivity_timeout = False
            _POLL_INTERVAL = 5.0

            if _agent_timeout is None:
                # Unlimited — still poll periodically for backup interrupt
                # detection in case monitor_for_interrupt() silently died.
                response = None
                while True:
                    done, _ = await asyncio.wait(
                        {_executor_task}, timeout=_POLL_INTERVAL
                    )
                    if done:
                        response = _executor_task.result()
                        break
                    # Backup interrupt check: if the monitor task died or
                    # missed the interrupt, catch it here.
                    if not _interrupt_detected.is_set() and session_key:
                        _backup_adapter = self._adapter_for_source(source)
                        _backup_agent = agent_holder[0]
                        if (_backup_adapter and _backup_agent
                                and hasattr(_backup_adapter, 'has_pending_interrupt')
                                and _backup_adapter.has_pending_interrupt(session_key)):
                            _bp_event = _backup_adapter._pending_messages.get(session_key)
                            _bp_text = _bp_event.text if _bp_event else None
                            if _bp_event is not None:
                                _bp_media_urls = getattr(_bp_event, "media_urls", None) or []
                                if self._pending_event_audio_paths(_bp_event):
                                    _bp_text, _ = await self._transcribe_and_echo_pending_voice(
                                        _bp_event,
                                        _backup_adapter,
                                        source,
                                        _bp_text or "",
                                        log_context="Voice-backup-interrupt",
                                        metadata={"thread_id": source.thread_id} if source.thread_id else None,
                                    )
                                elif not _bp_text and _bp_media_urls:
                                    _bp_text = _build_media_placeholder(_bp_event)
                            logger.info(
                                "Backup interrupt detected for session %s "
                                "(monitor task state: %s)",
                                session_key,
                                "done" if interrupt_monitor.done() else "running",
                            )
                            _backup_agent.interrupt(_bp_text)
                            _interrupt_detected.set()
                            # Abort streaming TTS on barge-in (#60671).
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort("barge-in")

            else:
                # Poll loop: check the agent's built-in activity tracker
                # (updated by _touch_activity() on every tool call, API
                # call, and stream delta) every few seconds.
                response = None
                while True:
                    done, _ = await asyncio.wait(
                        {_executor_task}, timeout=_POLL_INTERVAL
                    )
                    if done:
                        # Prefer the real result when the worker finished,
                        # even if the watchdog fired in the same window: the
                        # completed run already persisted its reply to session
                        # history, so surfacing the "agent inactive" diagnostic
                        # here would contradict the stored transcript. This
                        # mirrors _abandon_timed_out_gateway_turn's own
                        # worker_done-wins tiebreak (under cleanup_lock).
                        response = _executor_task.result()
                        break
                    if _turn_timeout_fired.is_set():
                        _inactivity_timeout = True
                        break
                    # Agent still running — check inactivity.
                    _agent_ref = agent_holder[0]
                    _idle_secs = 0.0
                    if _agent_ref and hasattr(_agent_ref, "get_activity_summary"):
                        try:
                            _act = _agent_ref.get_activity_summary()
                            _idle_secs = _act.get("seconds_since_activity", 0.0)
                        except Exception:
                            pass
                    # Staged warning: fire once before escalating to full timeout.
                    if (not _warning_fired and _agent_warning is not None
                            and _idle_secs >= _agent_warning):
                        _warning_fired = True
                        _warn_adapter = self._adapter_for_source(source)
                        if _warn_adapter:
                            _elapsed_warn = int(_agent_warning // 60) or 1
                            _remaining_mins = int((_agent_timeout - _agent_warning) // 60) or 1
                            try:
                                await _warn_adapter.send(
                                    source.chat_id,
                                    f"⚠️ No activity for {_elapsed_warn} min. "
                                    f"If the agent does not respond soon, it will "
                                    f"be timed out in {_remaining_mins} min. "
                                    f"You can continue waiting or use /reset.",
                                    metadata=_status_thread_metadata,
                                )
                            except Exception as _warn_err:
                                logger.debug("Inactivity warning send error: %s", _warn_err)
                    if _idle_secs >= _agent_timeout:
                        _inactivity_timeout = True
                        threading.Thread(
                            target=_abandon_timed_out_gateway_turn,
                            kwargs={
                                "agent_holder": agent_holder,
                                "task_id": _turn_task_id,
                                "process_baseline": _turn_process_baseline,
                                "worker_done": _turn_worker_done,
                                "timeout_fired": _turn_timeout_fired,
                                "cleanup_lock": _turn_cleanup_lock,
                                "is_still_current": _turn_is_current,
                            },
                            name=f"gateway-turn-reaper-{_turn_task_id[:12]}",
                            daemon=True,
                        ).start()
                        break
                    # Backup interrupt check (same as unlimited path).
                    if not _interrupt_detected.is_set() and session_key:
                        _backup_adapter = self._adapter_for_source(source)
                        _backup_agent = agent_holder[0]
                        if (_backup_adapter and _backup_agent
                                and hasattr(_backup_adapter, 'has_pending_interrupt')
                                and _backup_adapter.has_pending_interrupt(session_key)):
                            _bp_event = _backup_adapter._pending_messages.get(session_key)
                            _bp_text = _bp_event.text if _bp_event else None
                            if _bp_event is not None:
                                _bp_media_urls = getattr(_bp_event, "media_urls", None) or []
                                if self._pending_event_audio_paths(_bp_event):
                                    _bp_text, _ = await self._transcribe_and_echo_pending_voice(
                                        _bp_event,
                                        _backup_adapter,
                                        source,
                                        _bp_text or "",
                                        log_context="Voice-backup-interrupt",
                                        metadata={"thread_id": source.thread_id} if source.thread_id else None,
                                    )
                                elif not _bp_text and _bp_media_urls:
                                    _bp_text = _build_media_placeholder(_bp_event)
                            logger.info(
                                "Backup interrupt detected for session %s "
                                "(monitor task state: %s)",
                                session_key,
                                "done" if interrupt_monitor.done() else "running",
                            )
                            _backup_agent.interrupt(_bp_text)
                            _interrupt_detected.set()
                            # Abort streaming TTS on barge-in (#60671).
                            _stts = streaming_tts_consumer_holder[0]
                            if _stts is not None:
                                _stts.abort("barge-in")

            if _inactivity_timeout:
                # Build a diagnostic summary from the agent's activity tracker.
                _timed_out_agent = agent_holder[0]
                _activity = {}
                if _timed_out_agent and hasattr(_timed_out_agent, "get_activity_summary"):
                    try:
                        _activity = _timed_out_agent.get_activity_summary()
                    except Exception:
                        pass

                _last_desc = _activity.get("last_activity_desc", "unknown")
                _secs_ago = _activity.get("seconds_since_activity", 0)
                _cur_tool = _activity.get("current_tool")
                _iter_n = _activity.get("api_call_count", 0)
                _iter_max = _activity.get("max_iterations", 0)

                logger.error(
                    "Agent idle for %.0fs (timeout %.0fs) in session %s "
                    "| last_activity=%s | iteration=%s/%s | tool=%s",
                    _secs_ago, _agent_timeout, session_key,
                    _last_desc, _iter_n, _iter_max,
                    _cur_tool or "none",
                )

                # Interrupt the agent if it's still running so the thread
                # pool worker is freed.
                if _timed_out_agent:
                    request_hard_interrupt(_timed_out_agent, _INTERRUPT_REASON_TIMEOUT)

                _timeout_mins = int(_agent_timeout // 60) or 1

                # Construct a user-facing message with diagnostic context.
                _diag_lines = [
                    f"⏱️ Agent inactive for {_timeout_mins} min — no tool calls "
                    f"or API responses."
                ]
                if _cur_tool:
                    _diag_lines.append(
                        f"The agent appears stuck on tool `{_cur_tool}` "
                        f"({_secs_ago:.0f}s since last activity, "
                        f"iteration {_iter_n}/{_iter_max})."
                    )
                else:
                    _diag_lines.append(
                        f"Last activity: {_last_desc} ({_secs_ago:.0f}s ago, "
                        f"iteration {_iter_n}/{_iter_max}). "
                        "The agent may have been waiting on an API response."
                    )
                _diag_lines.append(
                    "To increase the limit, set agent.gateway_timeout in config.yaml "
                    "(value in seconds, 0 = no limit) and restart the gateway.\n"
                    "Try again, or use /reset to start fresh."
                )

                response = {
                    "final_response": "\n".join(_diag_lines),
                    "messages": result_holder[0].get("messages", []) if result_holder[0] else [],
                    "api_calls": _iter_n,
                    "tools": tools_holder[0] or [],
                    "history_offset": 0,
                    "failed": True,
                }

            # Track fallback model state: if the agent switched to a
            # fallback model during this run, persist it so /model shows
            # the actually-active model instead of the config default.
            # Skip eviction when the run failed — evicting a failed agent
            # forces MCP reinit on the next message for no benefit (the
            # same error will recur).  This was the root cause of #7130:
            # a bad model ID triggered fallback → eviction → recreation →
            # MCP reinit → same 400 → loop, burning 91% CPU for hours.
            _agent = agent_holder[0]
            _result_for_fb = result_holder[0]
            _run_failed = _result_for_fb.get("failed") if _result_for_fb else False
            if _agent is not None and hasattr(_agent, 'model') and not _run_failed:
                _cfg_model = _resolve_gateway_model()
                # Normalize _cfg_model the same way AIAgent.__init__ does, so a
                # vendor-prefixed config value (e.g. "deepseek/deepseek-v4-pro")
                # matches the agent's stripped model ("deepseek-v4-pro") on
                # native providers. Without this, _agent.model != _cfg_model is
                # always true for vendor-prefixed config and the cached agent is
                # evicted on every successful turn — destroying prompt caching.
                # Aggregators (openrouter, etc.) keep the vendor/model slug, so
                # they're left untouched.
                try:
                    from hermes_cli.model_normalize import (
                        _AGGREGATOR_PROVIDERS,
                        normalize_model_for_provider,
                    )
                    _agent_provider = getattr(_agent, 'provider', '') or ''
                    if _agent_provider and _agent_provider not in _AGGREGATOR_PROVIDERS:
                        _cfg_model = normalize_model_for_provider(_cfg_model, _agent_provider)
                except Exception:
                    pass
                if _agent.model != _cfg_model and not self._is_intentional_model_switch(session_key, _agent.model):
                    # Fallback activated on a successful run — evict cached
                    # agent so the next message retries the primary model.
                    self._evict_cached_agent(session_key)

            # Check if we were interrupted OR have a queued message (/queue).
            result = result_holder[0]
            adapter = self._adapter_for_source(source)

            # Finalize the streaming-TTS consumer (#60671).
            #
            # finish() is called from the outer event-loop thread (not the
            # executor worker) so early returns from run_sync are also
            # finalised.  wait_complete() drains queued audio; on timeout
            # the consumer is aborted unconditionally — if audio was
            # audible, suppression is preserved so the gateway does not
            # replay from the beginning; if no audio was audible, the
            # whole-file fallback path is permitted.
            _stts = streaming_tts_consumer_holder[0]
            if _stts is not None:
                _stts.finish()
                try:
                    await _stts.wait_complete(timeout=10.0)
                except Exception as _stts_done_err:
                    logger.debug("streaming TTS wait_complete error: %s", _stts_done_err)
                if not _stts.done:
                    # Timeout before or after audible audio: abort to free
                    # the consumer task.  Audible streams retain suppression;
                    # silent streams remain eligible for whole-file fallback.
                    _stts.abort("streaming TTS finalisation timeout")
                    await _stts.wait_complete(timeout=2.0)
                if _stts.suppress_whole_file and adapter is not None:
                    _mark_turn = getattr(adapter, "_mark_streaming_tts_completed_turn", None)
                    if callable(_mark_turn):
                        _mark_turn(session_key, run_generation)

            # Get pending message from adapter.
            # Use session_key (not source.chat_id) to match adapter's storage keys.
            pending_event = None
            pending = None
            if result and adapter and session_key:
                pending_event = _dequeue_pending_event(adapter, session_key)
                # /queue overflow: after consuming the adapter's "next-up"
                # slot, promote the next queued event into it so the
                # recursive run's drain will see it.  This keeps the slot
                # occupied for the full FIFO chain, which (a) preserves
                # order, and (b) causes any mid-chain /queue to correctly
                # route to overflow rather than jumping the queue.
                pending_event = self._promote_queued_event(session_key, adapter, pending_event)
                if result.get("interrupted") and not pending_event and result.get("interrupt_message"):
                    interrupt_message = result.get("interrupt_message")
                    if _is_control_interrupt_message(interrupt_message):
                        logger.info(
                            "Ignoring control interrupt message for session %s: %s",
                            session_key or "?",
                            interrupt_message,
                        )
                    else:
                        pending = interrupt_message
                elif pending_event:
                    # Transcribe audio media on the dequeued event BEFORE it is
                    # handed back as the next user turn, so queued/interrupting
                    # voice messages drain with the real transcript instead of
                    # a file-path placeholder. When configured, echo each
                    # transcript back to the user in the same 🎙️ format as
                    # fresh voice messages.
                    _pending_text = pending_event.text or ""
                    _media_urls = getattr(pending_event, "media_urls", None) or []
                    if self._pending_event_audio_paths(pending_event):
                        pending, _ = await self._transcribe_and_echo_pending_voice(
                            pending_event,
                            adapter,
                            source,
                            _pending_text,
                            log_context="Voice-drain",
                            metadata={"thread_id": source.thread_id} if source.thread_id else None,
                        )
                        if not pending:
                            pending = _build_media_placeholder(pending_event)
                    else:
                        pending = _pending_text or _build_media_placeholder(pending_event)
                    if pending:
                        logger.debug("Processing queued message after agent completion: '%s...'", pending[:40])

            # Leftover /steer: if a steer arrived after the last tool batch
            # (e.g. during the final API call), the agent couldn't inject it
            # and returned it in result["pending_steer"]. Deliver it as the
            # next user turn so it isn't silently dropped.
            if result and not pending and not pending_event:
                _leftover_steer = result.get("pending_steer")
                if _leftover_steer:
                    pending = _leftover_steer
                    logger.debug("Delivering leftover /steer as next turn: '%s...'", pending[:40])

            # Safety net: if the pending text is a slash command (e.g. "/stop",
            # "/new"), discard it — commands should never be passed to the agent
            # as user input.  The primary fix is in base.py (commands bypass the
            # active-session guard), but this catches edge cases where command
            # text leaks through the interrupt_message fallback.
            if pending and pending.strip().startswith("/"):
                _pending_parts = pending.strip().split(None, 1)
                _pending_cmd_word = _pending_parts[0][1:].lower() if _pending_parts else ""
                if _pending_cmd_word:
                    try:
                        from hermes_cli.commands import resolve_command as _rc_pending
                        if _rc_pending(_pending_cmd_word):
                            logger.info(
                                "Discarding command '/%s' from pending queue — "
                                "commands must not be passed as agent input",
                                _pending_cmd_word,
                            )
                            pending_event = None
                            pending = None
                    except Exception:
                        pass

            if self._draining and (pending_event or pending):
                logger.info(
                    "Discarding pending follow-up for session %s during gateway %s",
                    session_key or "?",
                    self._status_action_label(),
                )
                pending_event = None
                pending = None

            if pending_event or pending:
                logger.debug("Processing pending message: '%s...'", pending[:40])

                # Clear the adapter's interrupt event so the next _run_agent call
                # doesn't immediately re-trigger the interrupt before the new agent
                # even makes its first API call (this was causing an infinite loop).
                if adapter and hasattr(adapter, '_active_sessions') and session_key and session_key in adapter._active_sessions:
                    adapter._active_sessions[session_key].clear()

                # Cap recursion depth to prevent resource exhaustion when the
                # user sends multiple messages while the agent keeps failing. (#816)
                if _interrupt_depth >= self._MAX_INTERRUPT_DEPTH:
                    logger.warning(
                        "Interrupt recursion depth %d reached for session %s — "
                        "queueing message instead of recursing.",
                        _interrupt_depth, session_key,
                    )
                    adapter = self._adapter_for_source(source)
                    if adapter and pending_event:
                        merge_pending_message_event(adapter._pending_messages, session_key, pending_event)
                    elif adapter and hasattr(adapter, 'queue_message'):
                        adapter.queue_message(session_key, pending)
                    return result_holder[0] or {"final_response": response, "messages": history}

                was_interrupted = result.get("interrupted")
                if not was_interrupted:
                    # Queued message after normal completion — deliver the first
                    # response before processing the queued follow-up.
                    # Skip if streaming already delivered it.
                    _sc = stream_consumer_holder[0]
                    if _sc and stream_task:
                        try:
                            await asyncio.wait_for(stream_task, timeout=5.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            stream_task.cancel()
                            try:
                                await stream_task
                            except asyncio.CancelledError:
                                pass
                        except Exception as e:
                            logger.debug("Stream consumer wait before queued message failed: %s", e)
                    # The queued branch needs raw ``result`` for interruption,
                    # history, and recursion state, but delivery must use the
                    # finalized task result. The latter contains empty/failure
                    # normalization and any final response processing applied by
                    # _run_agent_task; sending the raw copy bypasses those steps.
                    _delivery_result = response if isinstance(response, dict) else (result or {})
                    _previewed = bool(_delivery_result.get("response_previewed"))
                    first_response = _delivery_result.get("final_response", "")
                    _already_streamed = _stream_confirmed_final_delivery(
                        _sc,
                        first_response,
                        previewed=_previewed,
                    )
                    # Apply the same predicate as the normal completed-turn path.
                    # This direct queued-send branch predates intentional-silence
                    # filtering, so without this check it leaks the literal marker.
                    try:
                        from gateway.response_filters import is_intentional_silence_agent_result
                        _intentional_silence = is_intentional_silence_agent_result(
                            _delivery_result, first_response,
                        )
                    except Exception:
                        _intentional_silence = False
                    if _intentional_silence:
                        logger.info(
                            "Queued follow-up for session %s: suppressing intentional silence marker before continuing.",
                            session_key or "?",
                        )
                    elif first_response and not _already_streamed:
                        try:
                            logger.info(
                                "Queued follow-up for session %s: final stream delivery not confirmed; sending first response before continuing.",
                                session_key or "?",
                            )
                            await adapter.send(
                                source.chat_id,
                                first_response,
                                metadata=_status_thread_metadata,
                            )
                        except Exception as e:
                            logger.warning("Failed to send first response before queued message: %s", e)
                    elif first_response:
                        logger.info(
                            "Queued follow-up for session %s: skipping resend because final streamed delivery was confirmed.",
                            session_key or "?",
                        )
                    # Release deferred bg-review notifications now that the
                    # first response has been delivered.  Pop from the
                    # adapter's callback dict (prevents double-fire in
                    # base.py's finally block) and call it.
                    if getattr(type(adapter), "pop_post_delivery_callback", None) is not None:
                        _bg_cb = adapter.pop_post_delivery_callback(
                            session_key,
                            generation=run_generation,
                        )
                        if callable(_bg_cb):
                            try:
                                _bg_result = _bg_cb()
                                if inspect.isawaitable(_bg_result):
                                    await _bg_result
                            except Exception:
                                pass
                    elif adapter and hasattr(adapter, "_post_delivery_callbacks"):
                        _bg_cb = adapter._post_delivery_callbacks.pop(session_key, None)
                        if callable(_bg_cb):
                            try:
                                _bg_result = _bg_cb()
                                if inspect.isawaitable(_bg_result):
                                    await _bg_result
                            except Exception:
                                pass
                # else: interrupted — discard the interrupted response ("Operation
                # interrupted." is just noise; the user already knows they sent a
                # new message).

                updated_history = result.get("messages", history)
                next_source = source
                next_message = pending
                next_message_id = None
                next_channel_prompt = None
                next_session_key = session_key
                # #60671 — carry the pending event's message_type into the
                # recursive call so queued voice turns can stream TTS and
                # re-mark the generation for the final delivered turn.
                next_message_type = None
                if pending_event is not None:
                    next_source = getattr(pending_event, "source", None) or source
                    if self._is_goal_continuation_event(pending_event) and not self._goal_still_active_for_session(session_id):
                        logger.info(
                            "Discarding stale goal continuation for session %s — goal is no longer active",
                            session_key or "?",
                        )
                        return result
                    # Resolve the follow-up's session key BEFORE preparing the
                    # inbound text: _prepare_inbound_message_text buffers native
                    # image paths under the key it is given, and the recursive
                    # _run_agent below consumes them under next_session_key.
                    # The write and consume keys must match or the images drop.
                    try:
                        next_session_key = self._session_key_for_source(next_source)
                    except Exception:
                        logger.debug(
                            "Queued follow-up session-key resolution failed; reusing %s",
                            session_key or "?",
                            exc_info=True,
                        )
                    next_message = await self._prepare_profile_scoped_inbound_message_text(
                        event=pending_event,
                        source=next_source,
                        history=updated_history,
                        session_key=next_session_key,
                    )
                    if next_message is None:
                        return result
                    next_message_id = self._reply_anchor_for_event(pending_event)
                    next_channel_prompt = getattr(pending_event, "channel_prompt", None)
                    next_message_type = getattr(pending_event, "message_type", None)

                # Clear the completed streaming marker from the prior logical
                # turn so the recursive turn's streaming TTS is not suppressed
                # by the prior turn's completion (#60671).
                _clear_adapter = self._adapter_for_source(source)
                if _clear_adapter is not None and session_key and run_generation is not None:
                    _completed_turns = getattr(_clear_adapter, "_streaming_tts_completed_turns", None)
                    if _completed_turns is not None:
                        _prior_key = getattr(_clear_adapter, "_streaming_tts_turn_key", None)
                        if callable(_prior_key):
                            _pk = _prior_key(session_key, run_generation)
                            if _pk:
                                _completed_turns.discard(_pk)

                # Restart typing indicator so the user sees activity while
                # the follow-up turn runs.  The outer _process_message_background
                # typing task is still alive but may be stale.
                _followup_adapter = self._adapter_for_source(source)
                if _followup_adapter:
                    try:
                        await _followup_adapter.send_typing(
                            source.chat_id,
                            metadata=_status_thread_metadata,
                        )
                    except Exception:
                        pass

                # Re-baseline the cached agent's message_count snapshot before
                # recursing into the in-band queued (/queue) follow-up turn.
                # The first turn has completed and flushed its own user +
                # assistant rows to the SessionDB, so the cross-process
                # coherence guard (#45966) — which this recursive _run_agent
                # call re-enters — would otherwise see the grown on-disk count
                # against the stale build-time snapshot and rebuild the agent
                # on THIS process's OWN writes, destroying the prompt-cache
                # prefix #46237 was merged to preserve.  The existing
                # re-baseline in _handle_message_with_agent only runs after the
                # whole _run_agent chain unwinds — too late for the in-band
                # follow-up.  Use the same (session_key, session_id) the
                # recursive call runs under so the snapshot matches exactly
                # what the follow-up's guard will consult.  Fail-safe in helper.
                await self._refresh_agent_cache_message_count(session_key, session_id)

                followup_result = await self._run_agent(
                    message=next_message,
                    context_prompt=context_prompt,
                    history=updated_history,
                    source=next_source,
                    session_id=session_id,
                    session_key=next_session_key,
                    run_generation=run_generation,
                    _interrupt_depth=_interrupt_depth + 1,
                    event_message_id=next_message_id,
                    channel_prompt=next_channel_prompt,
                    message_type=next_message_type,
                )
                return _preserve_queued_followup_history_offset(result, followup_result)
        finally:
            # Stop progress sender, interrupt monitor, and notification task
            if progress_task:
                progress_task.cancel()
            if log_task:
                log_task.cancel()
            interrupt_monitor.cancel()
            _notify_task.cancel()

            # Wait for stream consumer to finish its final edit
            if stream_task:
                # If the agent never created a stream consumer (e.g. non-
                # streaming code path, or a test stub returning synchronously)
                # there is nothing to flush — cancel immediately instead of
                # waiting out the 5s timeout on a task that's just polling for
                # a consumer that will never arrive.  This was a 5-second
                # cost per non-streaming test run.
                _has_stream_consumer = (
                    stream_consumer_holder
                    and stream_consumer_holder[0] is not None
                )
                if not _has_stream_consumer:
                    stream_task.cancel()
                    try:
                        await stream_task
                    except asyncio.CancelledError:
                        pass
                else:
                    try:
                        await asyncio.wait_for(stream_task, timeout=5.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        stream_task.cancel()
                        try:
                            await stream_task
                        except asyncio.CancelledError:
                            pass

            # Unconditional abort + bounded wait for the streaming-TTS
            # consumer (#60671 hardening).  Covers cancellation / exception
            # paths where the normal finalisation block was skipped.
            _stts_finally = streaming_tts_consumer_holder[0]
            if _stts_finally is not None and not _stts_finally.done:
                _stts_finally.abort("cleanup")
                try:
                    await _stts_finally.wait_complete(timeout=2.0)
                except Exception:
                    pass

            # Clean up tracking
            tracking_task.cancel()
            if session_key:
                # Only release the slot if this run's generation still owns
                # it.  A /stop or /new that bumped the generation while we
                # were unwinding has already installed its own state; this
                # guard prevents an old run from clobbering it on the way
                # out.
                self._release_running_agent_state(
                    session_key, run_generation=run_generation
                )
            if self._draining:
                self._update_runtime_status("draining")

            # Wait for cancelled tasks
            for task in [progress_task, log_task, interrupt_monitor, tracking_task, _notify_task]:
                if task:
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        # If streaming already delivered the response, mark it so the
        # caller's send() is skipped (avoiding duplicate messages).
        # BUT: never suppress delivery when the agent failed — the error
        # message is new content the user hasn't seen, and it must reach
        # them even if streaming had sent earlier partial output.
        #
        # Also never suppress when the final response is "(empty)" — this
        # means the model failed to produce content after tool calls (common
        # with mimo-v2-pro, GLM-5, etc.).  The stream consumer may have
        # sent intermediate text ("Let me search for that…") alongside the
        # tool call, setting already_sent=True, but that text is NOT the
        # final answer.  Suppressing delivery here leaves the user staring
        # at silence.  (#10xxx — "agent stops after web search")
        _sc = stream_consumer_holder[0]
        if isinstance(response, dict) and not response.get("failed"):
            _final = response.get("final_response") or ""
            _is_empty_sentinel = not _final or _final == "(empty)"
            # response_previewed means the interim_assistant_callback already
            # saw the final text, but only suppress the normal send if that
            # exact final text was delivered. Unrelated commentary/progress
            # must not be mistaken for the final response (#14238).
            _previewed = bool(response.get("response_previewed"))
            _content_delivered = bool(
                _sc and getattr(_sc, "final_content_delivered", False)
            )
            # #71643: a *successful* finalize edit can still carry only the
            # last preview snapshot — deltas generated between that edit and
            # stream completion never reach any API call, and both suppression
            # flags are set from the call's success rather than its content.
            # Reconcile the consumer's recorded turn-final payload against the
            # completed response: on a demonstrable mismatch (False) neither
            # final_response_sent nor final_content_delivered may suppress the
            # normal final send. None (no record / multi-message split
            # delivery) keeps legacy trust; the failed-finalize family
            # (#51828 / #33793) is unaffected because those paths leave the
            # flags False or record the complete fallback payload.
            _stale_finalized = False
            if _content_delivered and not _is_empty_sentinel:
                _matcher = getattr(_sc, "delivered_final_matches", None)
                if callable(_matcher):
                    try:
                        _stale_finalized = _matcher(_final) is False
                    except Exception:
                        _stale_finalized = False
                if _stale_finalized:
                    _content_delivered = False
            # Plugin hooks (e.g. transform_llm_output) may have appended content
            # after streaming finished — when the response was transformed, always
            # send the final version so the appended content reaches the client.
            _transformed = bool(response.get("response_transformed"))
            # Only suppress the normal send when the actual final reply reached
            # the user: the stream consumer streamed it (final_response_sent /
            # final_content_delivered), or the interim preview delivered that
            # *exact* final text. Unrelated commentary/progress shown during a
            # compression/session split must not be mistaken for the final
            # response (#14238).
            _streamed = _stream_confirmed_final_delivery(
                _sc,
                _final,
                previewed=_previewed,
            )
            if not _is_empty_sentinel and not _transformed and (_streamed or _content_delivered):
                logger.info(
                    "Suppressing normal final send for session %s: final delivery already confirmed (streamed=%s previewed=%s content_delivered=%s).",
                    session_key or "?",
                    _streamed,
                    _previewed,
                    _content_delivered,
                )
                response["already_sent"] = True
            elif not _is_empty_sentinel and not _transformed and _stale_finalized and _sc is not None:
                # Stale finalize (#71643): the streamed message holds only the
                # last preview snapshot. Prefer editing it up to the complete
                # response (same shape as the transformed branch below) so the
                # user gets one corrected message; on edit failure fall through
                # with already_sent unset so the normal final send delivers the
                # complete text.
                _sc_msg_id = _sc.message_id
                _sc_adapter = getattr(_sc, "adapter", None)
                if _sc_msg_id and _sc_msg_id != "__no_edit__" and _sc_adapter is not None:
                    try:
                        _reconcile_res = await _sc_adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=_sc_msg_id,
                            content=_final,
                            finalize=True,
                        )
                        if getattr(_reconcile_res, "success", True):
                            response["already_sent"] = True
                            logger.info(
                                "Reconciled stale streamed finalize for session %s: edited message %s with the complete response (#71643).",
                                session_key or "?", _sc_msg_id,
                            )
                        else:
                            logger.warning(
                                "Stale-finalize reconciliation edit failed for session %s (%s); sending complete response via normal final send.",
                                session_key or "?",
                                getattr(_reconcile_res, "error", None),
                            )
                    except Exception as _edit_err:
                        logger.warning(
                            "Stale-finalize reconciliation edit failed for session %s: %s; sending complete response via normal final send.",
                            session_key or "?", _edit_err,
                        )
                else:
                    logger.info(
                        "Stale streamed finalize detected for session %s with no editable message; delivering complete response via normal final send (#71643).",
                        session_key or "?",
                    )
            elif not _is_empty_sentinel and _transformed and _sc is not None:
                # Plugin hooks transformed the response after streaming — edit the
                # existing streamed message instead of sending a duplicate.
                _sc_msg_id = _sc.message_id
                if _sc_msg_id:
                    try:
                        await _sc.adapter.edit_message(
                            chat_id=source.chat_id,
                            message_id=_sc_msg_id,
                            content=response["final_response"],
                            finalize=True,
                        )
                        response["already_sent"] = True
                        logger.info(
                            "Edited streamed message %s for session %s to include plugin-transformed content.",
                            _sc_msg_id, session_key or "?",
                        )
                    except Exception as _edit_err:
                        logger.warning(
                            "Failed to edit streamed message for session %s: %s",
                            session_key or "?", _edit_err,
                        )

        # Schedule deletion of tracked temporary progress bubbles after the
        # final response lands. Failed runs skip this so bubbles remain as
        # breadcrumbs for the user to see what work happened. Only fires on
        # adapters that support ``delete_message`` (see init above); failures
        # are swallowed — deletion is best-effort.
        if (
            _cleanup_progress
            and _cleanup_adapter is not None
            and _cleanup_msg_ids
            and session_key
            and isinstance(response, dict)
            and not response.get("failed")
            and hasattr(_cleanup_adapter, "register_post_delivery_callback")
        ):
            _ids_snapshot = list(_cleanup_msg_ids)
            _chat_id_snapshot = source.chat_id
            _adapter_snapshot = _cleanup_adapter
            _loop_snapshot = asyncio.get_running_loop()

            def _cleanup_temp_bubbles() -> None:
                async def _delete_all() -> None:
                    for _mid in _ids_snapshot:
                        try:
                            await _adapter_snapshot.delete_message(
                                _chat_id_snapshot, _mid
                            )
                        except Exception:
                            pass
                try:
                    safe_schedule_threadsafe(
                        _delete_all(), _loop_snapshot,
                        logger=logger,
                        log_message="Temp bubble cleanup scheduling error",
                    )
                except Exception:
                    pass

            try:
                _cleanup_adapter.register_post_delivery_callback(
                    session_key,
                    _cleanup_temp_bubbles,
                    generation=run_generation,
                )
            except Exception as _rpe:
                logger.debug("Post-delivery cleanup registration failed: %s", _rpe)

        return response
