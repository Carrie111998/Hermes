"""Agent-cache lifecycle methods for ``GatewayRunner``.

Extracted from ``gateway/run.py`` (god-file decomposition campaign, wave 1).
Holds the LRU-cap + idle-TTL eviction cluster: eviction, soft release,
pre-evict memory commit, and the message-count re-baseline.

Behavior-neutral: every method is lifted verbatim from ``GatewayRunner``.
The module-level constants ``_AGENT_CACHE_MAX_SIZE``,
``_AGENT_CACHE_IDLE_TTL_SECS`` and ``_AGENT_PENDING_SENTINEL`` stay in
``gateway/run.py`` (tests import/monkeypatch them there) and are imported
lazily inside the methods that use them, so ``monkeypatch.setattr`` on
``gateway.run`` keeps working. The module-level ``logger`` is
``logging.getLogger("gateway.run")`` so log records keep the exact name.
"""


from __future__ import annotations

import logging
import threading
import time

from typing import Any, List, Optional

logger = logging.getLogger("gateway.run")


class GatewayAgentCacheMixin:

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

    def _evict_cached_agent(self, session_key: str) -> None:
        """Remove a cached agent for a session (called on /new, /model, etc).

        Pops the entry AND soft-releases the evicted agent's LLM client
        pool so the httpx connection (sockets + held buffers) is freed
        promptly rather than waiting on CPython GC — AIAgent holds
        reference cycles (callbacks, tool state) that delay refcount
        collection, so a manual release is required to keep gateway RSS
        flat across many /new, /model, undo and reset operations (#29298,
        same leak class as #25315).

        The release is soft (``release_clients()``): it frees the client
        pool and per-turn child subagents but PRESERVES the session's
        terminal sandbox, browser daemon, and tracked bg processes (keyed
        on task_id), because the session may resume with a freshly-built
        agent.  Call sites that want a hard teardown (true conversation
        boundaries like /new) already call ``_cleanup_agent_resources``
        before evicting; ``release_clients`` is idempotent and safe to
        run again after that (the client is already None).

        Cleanup runs on a daemon thread so we never block holding
        ``_agent_cache_lock`` on slow socket teardown — mirrors the
        cap-enforcer and idle-sweeper paths.
        """
        # Prompt-stability state rides the agent-cache lifecycle: a fresh
        # agent must re-render its session-context bytes (the pin) and re-see
        # the current voice-channel state once.
        from gateway.run import _AGENT_PENDING_SENTINEL
        _evict_state = self._peek_session_state(session_key)
        if _evict_state is not None:
            _evict_state.conversation.ephemeral_pin = None
            _evict_state.conversation.vc_last = None

        _lock = getattr(self, "_agent_cache_lock", None)
        evicted = None
        if _lock:
            with _lock:
                evicted = self._agent_cache.pop(session_key, None)
        else:
            _cache = getattr(self, "_agent_cache", None)
            if _cache is not None:
                evicted = _cache.pop(session_key, None)

        agent = evicted[0] if isinstance(evicted, tuple) and evicted else evicted
        if agent is None or agent is _AGENT_PENDING_SENTINEL:
            return

        # Don't tear down an agent that's actively mid-turn — its client,
        # sandbox and child subagents are in use by the running request.
        running_ids = {
            id(a)
            for _, a in self._running_agent_items()
            if a is not None and a is not _AGENT_PENDING_SENTINEL
        }
        if id(agent) in running_ids:
            return

        try:
            threading.Thread(
                target=self._release_evicted_agent_soft,
                args=(agent,),
                daemon=True,
                name=f"agent-evict-{str(session_key)[:24]}",
            ).start()
        except Exception:
            # If we can't spawn a thread (interpreter shutdown), release
            # inline as a best-effort fallback.
            try:
                self._release_evicted_agent_soft(agent)
            except Exception:
                pass

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
        if interrupt_depth == 0:
            from agent.session_activity import ActivityProvenance
            from gateway.run import time  # keep patch("gateway.run.time") seam (precedent #77752)

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
        from gateway.run import _AGENT_CACHE_IDLE_TTL_SECS, _AGENT_PENDING_SENTINEL
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

