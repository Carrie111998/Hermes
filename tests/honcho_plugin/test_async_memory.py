"""Tests for the async-memory Honcho improvements.

Covers:
  - write_frequency parsing (async / turn / session / int)
  - resolve_session_name with session_title
  - HonchoSessionManager.save() routing per write_frequency
  - async writer thread lifecycle and retry
  - flush_all() drains pending messages
  - shutdown() joins the thread
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from plugins.memory.honcho.client import HonchoClientConfig
from plugins.memory.honcho import HonchoMemoryProvider
from plugins.memory.honcho.session import (
    HonchoSession,
    HonchoSessionManager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session(**kwargs) -> HonchoSession:
    return HonchoSession(
        key=kwargs.get("key", "cli:test"),
        user_peer_id=kwargs.get("user_peer_id", "eri"),
        assistant_peer_id=kwargs.get("assistant_peer_id", "hermes"),
        honcho_session_id=kwargs.get("honcho_session_id", "cli-test"),
        messages=kwargs.get("messages", []),
    )


def _make_manager(write_frequency="turn") -> HonchoSessionManager:
    cfg = HonchoClientConfig(
        write_frequency=write_frequency,
        api_key="test-key",
        enabled=True,
    )
    mgr = HonchoSessionManager(config=cfg)
    mgr._honcho = MagicMock()
    return mgr


# ---------------------------------------------------------------------------
# write_frequency parsing from config file
# ---------------------------------------------------------------------------

class TestWriteFrequencyParsing:
    def test_string_async(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"apiKey": "k", "writeFrequency": "async"}))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.write_frequency == "async"

    def test_string_turn(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"apiKey": "k", "writeFrequency": "turn"}))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.write_frequency == "turn"

    def test_string_session(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"apiKey": "k", "writeFrequency": "session"}))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.write_frequency == "session"

    def test_integer_frequency(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"apiKey": "k", "writeFrequency": 5}))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.write_frequency == 5

    def test_integer_string_coerced(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"apiKey": "k", "writeFrequency": "3"}))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.write_frequency == 3

    def test_host_block_overrides_root(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "apiKey": "k",
            "writeFrequency": "turn",
            "hosts": {"hermes": {"writeFrequency": "session"}},
        }))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.write_frequency == "session"

    def test_defaults_to_async(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"apiKey": "k"}))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.write_frequency == "async"


# ---------------------------------------------------------------------------
# resolve_session_name with session_title
# ---------------------------------------------------------------------------

class TestResolveSessionNameTitle:
    def test_manual_override_beats_title(self):
        cfg = HonchoClientConfig(sessions={"/my/project": "manual-name"})
        result = cfg.resolve_session_name("/my/project", session_title="the-title")
        assert result == "manual-name"

    def test_title_beats_dirname(self):
        cfg = HonchoClientConfig()
        result = cfg.resolve_session_name("/some/dir", session_title="my-project")
        assert result == "my-project"

    def test_title_with_peer_prefix(self):
        cfg = HonchoClientConfig(peer_name="eri", session_peer_prefix=True)
        result = cfg.resolve_session_name("/some/dir", session_title="aeris")
        assert result == "eri-aeris"

    def test_title_sanitized(self):
        cfg = HonchoClientConfig()
        result = cfg.resolve_session_name("/some/dir", session_title="my project/name!")
        # trailing dashes stripped by .strip('-')
        assert result == "my-project-name"

    def test_title_all_invalid_chars_falls_back_to_dirname(self):
        cfg = HonchoClientConfig()
        result = cfg.resolve_session_name("/some/dir", session_title="!!! ###")
        # sanitized to empty → falls back to dirname
        assert result == "dir"

    def test_none_title_falls_back_to_dirname(self):
        cfg = HonchoClientConfig()
        result = cfg.resolve_session_name("/some/dir", session_title=None)
        assert result == "dir"

    def test_empty_title_falls_back_to_dirname(self):
        cfg = HonchoClientConfig()
        result = cfg.resolve_session_name("/some/dir", session_title="")
        assert result == "dir"

    def test_per_session_uses_session_id(self):
        cfg = HonchoClientConfig(session_strategy="per-session")
        result = cfg.resolve_session_name("/some/dir", session_id="20260309_175514_9797dd")
        assert result == "20260309_175514_9797dd"

    def test_per_session_with_peer_prefix(self):
        cfg = HonchoClientConfig(session_strategy="per-session", peer_name="eri", session_peer_prefix=True)
        result = cfg.resolve_session_name("/some/dir", session_id="20260309_175514_9797dd")
        assert result == "eri-20260309_175514_9797dd"

    def test_per_session_no_id_falls_back_to_dirname(self):
        cfg = HonchoClientConfig(session_strategy="per-session")
        result = cfg.resolve_session_name("/some/dir", session_id=None)
        assert result == "dir"

    def test_per_session_id_beats_title(self):
        # per-session: the run's session_id is authoritative; an (auto-)generated
        # title must NOT remap a live conversation onto a second Honcho session.
        cfg = HonchoClientConfig(session_strategy="per-session")
        result = cfg.resolve_session_name("/some/dir", session_title="my-title", session_id="20260309_175514_9797dd")
        assert result == "20260309_175514_9797dd"

    def test_per_session_id_beats_manual_map(self):
        # per-session: session_id also wins over a stale cwd map entry (e.g. the
        # desktop launching from a mapped home dir).
        cfg = HonchoClientConfig(session_strategy="per-session", sessions={"/some/dir": "pinned"})
        result = cfg.resolve_session_name("/some/dir", session_id="20260309_175514_9797dd")
        assert result == "20260309_175514_9797dd"

    def test_title_still_applies_for_non_per_session(self):
        # Outside per-session, /title still names the Honcho session.
        cfg = HonchoClientConfig(session_strategy="per-directory")
        result = cfg.resolve_session_name("/some/dir", session_title="my-title", session_id="20260309_175514_9797dd")
        assert result == "my-title"

    def test_gateway_key_beats_per_session_id(self):
        # Gateways keep per-chat isolation even in per-session.
        cfg = HonchoClientConfig(session_strategy="per-session")
        result = cfg.resolve_session_name("/some/dir", gateway_session_key="agent:main:telegram:dm:42", session_id="20260309_175514_9797dd")
        assert result == "agent-main-telegram-dm-42"

    def test_global_strategy_returns_workspace(self):
        cfg = HonchoClientConfig(session_strategy="global", workspace_id="my-workspace")
        result = cfg.resolve_session_name("/some/dir")
        assert result == "my-workspace"


# ---------------------------------------------------------------------------
# save() routing per write_frequency
# ---------------------------------------------------------------------------

class TestSaveRouting:
    def _make_session_with_message(self, mgr=None):
        sess = _make_session()
        sess.add_message("user", "hello")
        sess.add_message("assistant", "hi")
        if mgr:
            mgr._cache[sess.key] = sess
        return sess

    def test_turn_flushes_immediately(self):
        mgr = _make_manager(write_frequency="turn")
        sess = self._make_session_with_message(mgr)
        with patch.object(mgr, "_flush_session") as mock_flush:
            mgr.save(sess)
            mock_flush.assert_called_once_with(sess)

    def test_session_mode_does_not_flush(self):
        mgr = _make_manager(write_frequency="session")
        sess = self._make_session_with_message(mgr)
        with patch.object(mgr, "_flush_session") as mock_flush:
            mgr.save(sess)
            mock_flush.assert_not_called()

    def test_async_mode_enqueues(self):
        mgr = _make_manager(write_frequency="async")
        sess = self._make_session_with_message(mgr)
        with patch.object(mgr, "_flush_session") as mock_flush:
            mgr.save(sess)
            # flush_session should NOT be called synchronously
            mock_flush.assert_not_called()
        assert not mgr._async_queue.empty()

    def test_int_frequency_flushes_on_nth_turn(self):
        mgr = _make_manager(write_frequency=3)
        sess = self._make_session_with_message(mgr)
        with patch.object(mgr, "_flush_session") as mock_flush:
            mgr.save(sess)  # turn 1
            mgr.save(sess)  # turn 2
            assert mock_flush.call_count == 0
            mgr.save(sess)  # turn 3
            assert mock_flush.call_count == 1

    def test_int_frequency_skips_other_turns(self):
        mgr = _make_manager(write_frequency=5)
        sess = self._make_session_with_message(mgr)
        with patch.object(mgr, "_flush_session") as mock_flush:
            for _ in range(4):
                mgr.save(sess)
            assert mock_flush.call_count == 0
            mgr.save(sess)  # turn 5
            assert mock_flush.call_count == 1


# ---------------------------------------------------------------------------
# flush_all()
# ---------------------------------------------------------------------------

class TestFlushAll:
    def test_flushes_all_cached_sessions(self):
        mgr = _make_manager(write_frequency="session")
        s1 = _make_session(key="s1", honcho_session_id="s1")
        s2 = _make_session(key="s2", honcho_session_id="s2")
        s1.add_message("user", "a")
        s2.add_message("user", "b")
        mgr._cache = {"s1": s1, "s2": s2}

        with patch.object(mgr, "_flush_session") as mock_flush:
            mgr.flush_all()
            assert mock_flush.call_count == 2

    def test_flush_all_drains_async_queue(self):
        mgr = _make_manager(write_frequency="async")
        sess = _make_session()
        sess.add_message("user", "pending")

        with patch.object(mgr, "_flush_session") as mock_flush:
            # Put the item AFTER the mock is installed so the background
            # writer thread (if it dequeues before flush_all) still hits
            # the mock rather than the real _flush_session.
            mgr._async_queue.put(sess)
            mgr.flush_all()
            # Called at least once for the queued item
            assert mock_flush.call_count >= 1

    def test_flush_all_tolerates_errors(self):
        mgr = _make_manager(write_frequency="session")
        sess = _make_session()
        mgr._cache = {"key": sess}
        with patch.object(mgr, "_flush_session", side_effect=RuntimeError("oops")):
            # Should not raise
            mgr.flush_all()


# ---------------------------------------------------------------------------
# async writer thread lifecycle
# ---------------------------------------------------------------------------

class TestAsyncWriterThread:
    def test_thread_started_on_async_mode(self):
        mgr = _make_manager(write_frequency="async")
        assert mgr._async_thread is not None
        assert mgr._async_thread.is_alive()
        mgr.shutdown()

    def test_no_thread_for_turn_mode(self):
        mgr = _make_manager(write_frequency="turn")
        assert mgr._async_thread is None
        assert mgr._async_queue is None

    def test_shutdown_joins_thread(self):
        mgr = _make_manager(write_frequency="async")
        assert mgr._async_thread.is_alive()
        mgr.shutdown()
        assert not mgr._async_thread.is_alive()

    def test_async_writer_calls_flush(self):
        mgr = _make_manager(write_frequency="async")
        sess = _make_session()
        sess.add_message("user", "async msg")

        flushed = []
        flushed_event = threading.Event()

        def capture(session):
            flushed.append(session)
            flushed_event.set()
            return True

        mgr._flush_session = capture
        mgr._async_queue.put(sess)
        assert flushed_event.wait(timeout=10), "async writer never flushed"

        mgr.shutdown()
        assert len(flushed) == 1
        assert flushed[0] is sess

    def test_flush_all_uses_writer_barrier_without_duplicate_write(self):
        mgr = _make_manager(write_frequency="async")
        sess = _make_session()
        sess.add_message("user", "pending")
        mgr._cache[sess.key] = sess
        entered = threading.Event()
        release = threading.Event()
        remote_writes = []

        def controlled_flush(session):
            unsynced = [msg for msg in session.messages if not msg.get("_synced")]
            if not unsynced:
                return True
            entered.set()
            release.wait(timeout=2)
            remote_writes.append(list(unsynced))
            for msg in unsynced:
                msg["_synced"] = True
            return True

        mgr._flush_session = controlled_flush
        assert mgr._async_queue is not None
        mgr._async_queue.put(sess)
        assert entered.wait(timeout=1)
        flush_thread = threading.Thread(target=mgr.flush_all)
        flush_thread.start()
        release.set()
        flush_thread.join(timeout=2)
        mgr.shutdown()

        assert not flush_thread.is_alive()
        assert len(remote_writes) == 1

    def test_shutdown_sentinel_stops_loop(self):
        mgr = _make_manager(write_frequency="async")
        thread = mgr._async_thread
        mgr.shutdown()
        thread.join(timeout=10)
        assert not thread.is_alive()

    def test_shutdown_flushes_session_mode(self):
        """Manager shutdown preserves the provider's final-flush contract."""
        mgr = _make_manager(write_frequency="session")
        sess = _make_session()
        sess.add_message("user", "pending")
        mgr._cache[sess.key] = sess

        with patch.object(mgr, "_flush_session", return_value=True) as flush:
            mgr.shutdown()

        flush.assert_called_once_with(sess)

    def test_shutdown_joins_manager_prefetch_worker(self):
        mgr = _make_manager(write_frequency="turn")
        entered = threading.Event()
        release = threading.Event()

        def slow_context(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=2)
            return {"representation": "ready"}

        mgr.get_prefetch_context = slow_context
        mgr.prefetch_context("cli:test", "hello")
        assert entered.wait(timeout=1)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        try:
            mgr.shutdown()
        finally:
            release.set()
            timer.join(timeout=1)

        with mgr._background_threads_lock:
            assert not any(thread.is_alive() for thread in mgr._background_threads)

    def test_shutdown_waits_for_in_flight_async_write(self):
        mgr = _make_manager(write_frequency="async")
        assert mgr._config is not None
        mgr._config.timeout = 0.01
        sess = _make_session()
        sess.add_message("user", "pending")
        entered = threading.Event()
        release = threading.Event()

        def blocked_flush(session):
            assert session is sess
            entered.set()
            release.wait(timeout=2)
            return True

        mgr._flush_session = blocked_flush
        assert mgr._async_queue is not None
        mgr._async_queue.put(sess)
        assert entered.wait(timeout=1)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        try:
            mgr.shutdown()
        finally:
            release.set()
            timer.join(timeout=1)

        assert mgr._async_thread is not None
        assert not mgr._async_thread.is_alive()

    def test_save_enqueue_is_atomic_with_shutdown_sentinel(self):
        mgr = _make_manager(write_frequency="async")
        sess = _make_session()
        sess.add_message("user", "pending")
        entered = threading.Event()
        release = threading.Event()
        flushed = threading.Event()
        assert mgr._async_queue is not None
        original_put = mgr._async_queue.put

        def controlled_put(item, *args, **kwargs):
            if item is sess:
                entered.set()
                release.wait(timeout=2)
            return original_put(item, *args, **kwargs)

        mgr._async_queue.put = controlled_put
        mgr._flush_session = lambda session: flushed.set() or session is sess
        save_thread = threading.Thread(target=mgr.save, args=(sess,))
        save_thread.start()
        assert entered.wait(timeout=1)
        shutdown_thread = threading.Thread(target=mgr.shutdown)
        shutdown_thread.start()
        release.set()
        save_thread.join(timeout=1)
        shutdown_thread.join(timeout=2)

        assert not save_thread.is_alive()
        assert not shutdown_thread.is_alive()
        assert flushed.is_set()
        assert mgr._async_thread is not None
        assert not mgr._async_thread.is_alive()

    def test_manager_shutdown_timeout_is_retryable(self):
        mgr = _make_manager(write_frequency="turn")
        release = threading.Event()

        def blocked_worker() -> None:
            release.wait(timeout=2)

        worker = mgr._start_background_thread(
            name="blocked-prefetch",
            target=blocked_worker,
        )
        assert worker is not None
        mgr._join_timeout_seconds = lambda: 0.01

        with pytest.raises(RuntimeError, match="blocked-prefetch"):
            mgr.shutdown()

        assert not mgr._shutdown_complete
        release.set()
        worker.join(timeout=1)
        mgr.shutdown()
        assert mgr._shutdown_complete

    def test_shutdown_rejects_new_manager_workers(self):
        mgr = _make_manager(write_frequency="turn")
        mgr.shutdown()

        assert mgr._start_background_thread(name="late", target=lambda: None) is None

    def test_provider_shutdown_stops_async_writer(self):
        """The provider owns the manager and must stop its daemon writer."""
        mgr = _make_manager(write_frequency="async")
        provider = HonchoMemoryProvider()
        provider._manager = mgr
        provider._session_initialized = True
        thread = mgr._async_thread
        assert thread is not None

        try:
            provider.shutdown()
            assert not thread.is_alive()
        finally:
            # Keep the RED case from leaking its daemon into the test process.
            mgr.shutdown()

    def test_provider_shutdown_stops_manager_published_during_init(self):
        provider = HonchoMemoryProvider()
        provider._config = MagicMock(timeout=0.01)
        manager = MagicMock()
        provider._initializing_manager = manager
        entered = threading.Event()
        release = threading.Event()

        def blocked_init():
            entered.set()
            release.wait(timeout=2)

        provider._init_thread = provider._start_background_thread(
            target=blocked_init,
            name="honcho-session-init",
        )
        assert entered.wait(timeout=1)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        try:
            provider.shutdown()
        finally:
            release.set()
            timer.join(timeout=1)

        manager.shutdown.assert_called_once_with()
        assert provider._init_thread is not None
        assert not provider._init_thread.is_alive()

    def test_init_created_after_shutdown_is_stopped_before_publication(self):
        provider = HonchoMemoryProvider()
        provider._shutdown_event.set()
        cfg = MagicMock(context_tokens=1000)
        manager = MagicMock()

        with (
            patch(
                "plugins.memory.honcho.client.get_honcho_client",
                return_value=MagicMock(),
            ),
            patch(
                "plugins.memory.honcho.session.HonchoSessionManager",
                return_value=manager,
            ),
        ):
            provider._do_session_init(cfg, "test-session")

        manager.shutdown.assert_called_once_with()
        assert provider._manager is None
        assert provider._initializing_manager is None

    def test_provider_shutdown_rejects_new_workers(self):
        provider = HonchoMemoryProvider()
        provider.shutdown()

        assert provider._start_background_thread(target=lambda: None, name="late") is None

    def test_provider_retains_and_reports_failed_manager_shutdown(self):
        provider = HonchoMemoryProvider()
        manager = MagicMock()
        manager.shutdown.side_effect = [
            RuntimeError("blocked once"),
            RuntimeError("blocked twice"),
            None,
        ]
        provider._manager = manager
        provider._owned_managers.add(manager)

        with pytest.raises(RuntimeError, match="1 Honcho manager"):
            provider.shutdown()

        assert manager in provider._owned_managers
        assert provider._manager is manager
        provider.shutdown()
        assert manager not in provider._owned_managers
        assert provider._manager is None

    def test_provider_shutdown_timeout_covers_internal_retry(self):
        provider = HonchoMemoryProvider()
        provider._config = MagicMock(timeout=2.0)
        provider._dialectic_depth = 1

        # Provider workers: 5s. Manager: one flush plus two 8s attempts.
        assert provider.shutdown_timeout_seconds() == 29.0

        provider._owned_managers.update((MagicMock(), MagicMock()))
        # One flush plus two attempts for each of two retained managers.
        assert provider.shutdown_timeout_seconds() == 45.0

    def test_lazy_initialization_is_serialized(self):
        provider = HonchoMemoryProvider()
        provider._config = MagicMock()
        provider._lazy_init_kwargs = {}
        provider._lazy_init_session_id = "test"
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def initialize_once(*_args, **_kwargs):
            calls.append(1)
            entered.set()
            release.wait(timeout=2)
            provider._manager = MagicMock()
            provider._session_initialized = True

        provider._do_session_init = initialize_once
        first = threading.Thread(target=provider._ensure_session)
        second = threading.Thread(target=provider._ensure_session)
        first.start()
        assert entered.wait(timeout=1)
        second.start()
        release.set()
        first.join(timeout=1)
        second.join(timeout=1)

        assert not first.is_alive()
        assert not second.is_alive()
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# async retry on failure
# ---------------------------------------------------------------------------

class TestAsyncWriterRetry:
    def test_retries_once_on_failure(self):
        mgr = _make_manager(write_frequency="async")
        sess = _make_session()
        sess.add_message("user", "msg")

        call_count = [0]
        retry_done = threading.Event()

        def flaky_flush(session):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("network blip")
            retry_done.set()
            return True

        mgr._flush_session = flaky_flush

        with patch("time.sleep"):  # skip the 2s sleep in retry
            mgr._async_queue.put(sess)
            assert retry_done.wait(timeout=10), "async writer never retried"

        mgr.shutdown()
        assert call_count[0] == 2

    def test_drops_after_two_failures(self):
        mgr = _make_manager(write_frequency="async")
        sess = _make_session()
        sess.add_message("user", "msg")

        call_count = [0]
        retry_done = threading.Event()

        def always_fail(session):
            call_count[0] += 1
            if call_count[0] >= 2:
                retry_done.set()
            raise RuntimeError("always broken")

        mgr._flush_session = always_fail

        with patch("time.sleep"):
            mgr._async_queue.put(sess)
            assert retry_done.wait(timeout=10), "async writer never retried"

        mgr.shutdown()
        # Should have tried exactly twice (initial + one retry) and not crashed
        assert call_count[0] == 2
        assert not mgr._async_thread.is_alive()

    def test_retries_when_flush_reports_failure(self):
        mgr = _make_manager(write_frequency="async")
        sess = _make_session()
        sess.add_message("user", "msg")

        call_count = [0]
        retry_done = threading.Event()

        def fail_then_succeed(session):
            call_count[0] += 1
            if call_count[0] >= 2:
                retry_done.set()
            return call_count[0] > 1

        mgr._flush_session = fail_then_succeed

        with patch("time.sleep"):
            mgr._async_queue.put(sess)
            assert retry_done.wait(timeout=10), "async writer never retried"

        mgr.shutdown()
        assert call_count[0] == 2


class TestMemoryFileMigrationTargets:
    def test_soul_upload_targets_ai_peer(self, tmp_path):
        mgr = _make_manager(write_frequency="turn")
        session = _make_session(
            key="cli:test",
            user_peer_id="custom-user",
            assistant_peer_id="custom-ai",
            honcho_session_id="cli-test",
        )
        mgr._cache[session.key] = session

        user_peer = MagicMock(name="user-peer")
        ai_peer = MagicMock(name="ai-peer")
        mgr._peers_cache[session.user_peer_id] = user_peer
        mgr._peers_cache[session.assistant_peer_id] = ai_peer

        honcho_session = MagicMock()
        mgr._sessions_cache[session.honcho_session_id] = honcho_session

        (tmp_path / "MEMORY.md").write_text("memory facts", encoding="utf-8")
        (tmp_path / "USER.md").write_text("user profile", encoding="utf-8")
        (tmp_path / "SOUL.md").write_text("ai identity", encoding="utf-8")

        uploaded = mgr.migrate_memory_files(session.key, str(tmp_path))

        assert uploaded is True
        assert honcho_session.upload_file.call_count == 3

        peer_by_upload_name = {}
        for call_args in honcho_session.upload_file.call_args_list:
            payload = call_args.kwargs["file"]
            peer_by_upload_name[payload[0]] = call_args.kwargs["peer"]

        assert peer_by_upload_name["consolidated_memory.md"] is user_peer
        assert peer_by_upload_name["user_profile.md"] is user_peer
        assert peer_by_upload_name["agent_soul.md"] is ai_peer


# ---------------------------------------------------------------------------
# HonchoClientConfig dataclass defaults for new fields
# ---------------------------------------------------------------------------

class TestNewConfigFieldDefaults:
    def test_write_frequency_default(self):
        cfg = HonchoClientConfig()
        assert cfg.write_frequency == "async"

    def test_write_frequency_set(self):
        cfg = HonchoClientConfig(write_frequency="turn")
        assert cfg.write_frequency == "turn"


class TestPrefetchCacheAccessors:
    def test_set_and_pop_context_result(self):
        mgr = _make_manager(write_frequency="turn")
        payload = {"representation": "Known user", "card": "prefers concise replies"}

        mgr.set_context_result("cli:test", payload)

        assert mgr.pop_context_result("cli:test") == payload
        assert mgr.pop_context_result("cli:test") == {}

