"""Tests for gateway session hygiene — auto-compression of large sessions.

Verifies that the gateway detects pathologically large transcripts and
triggers auto-compression before running the agent.  (#628)

The hygiene system uses the SAME compression config as the agent:
  compression.threshold × model context length
so CLI and messaging platforms behave identically.
"""

import asyncio
import concurrent.futures
import importlib
import sys
import threading
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock

import pytest

from agent.model_metadata import estimate_messages_tokens_rough
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_history(n_messages: int, content_size: int = 100) -> list:
    """Build a fake transcript with n_messages user/assistant pairs."""
    history = []
    content = "x" * content_size
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": content, "timestamp": f"t{i}"})
    return history


def _make_large_history_tokens(target_tokens: int) -> list:
    """Build a history that estimates to roughly target_tokens tokens."""
    # estimate_messages_tokens_rough counts total chars in str(msg) // 4
    # Each msg dict has ~60 chars of overhead + content chars
    # So for N tokens we need roughly N * 4 total chars across all messages
    target_chars = target_tokens * 4
    # Each message as a dict string is roughly len(content) + 60 chars
    msg_overhead = 60
    # Use 50 messages with appropriately sized content
    n_msgs = 50
    content_size = max(10, (target_chars // n_msgs) - msg_overhead)
    return _make_history(n_msgs, content_size=content_size)


class HygieneCaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="hygiene-1")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


# ---------------------------------------------------------------------------
# Detection threshold tests (model-aware, unified with compression config)
# ---------------------------------------------------------------------------

class TestSessionHygieneThresholds:
    """Test that the threshold logic correctly identifies large sessions.

    Thresholds are derived from model context length × compression threshold,
    matching what the agent's ContextCompressor uses.
    """


    def test_under_threshold_no_trigger(self):
        """Session under threshold should not trigger, even with many messages."""
        # 250 short messages — lots of messages but well under token threshold
        history = _make_history(250, content_size=10)
        approx_tokens = estimate_messages_tokens_rough(history)

        # 200k model at 85% = 170k token threshold
        context_length = 200_000
        threshold_pct = 0.85
        compress_token_threshold = int(context_length * threshold_pct)

        needs_compress = approx_tokens >= compress_token_threshold
        assert not needs_compress, (
            f"250 short messages (~{approx_tokens} tokens) should NOT trigger "
            f"compression at {compress_token_threshold} token threshold"
        )

    def test_message_count_alone_does_not_trigger(self):
        """Message count alone should NOT trigger — only token count matters.

        The old system used an OR of token-count and message-count thresholds,
        which caused premature compression in tool-heavy sessions with 200+
        messages but low total tokens.
        """
        # 300 very short messages — old system would compress, new should not
        history = _make_history(300, content_size=10)
        approx_tokens = estimate_messages_tokens_rough(history)

        context_length = 200_000
        threshold_pct = 0.85
        compress_token_threshold = int(context_length * threshold_pct)

        # Token-based check only
        needs_compress = approx_tokens >= compress_token_threshold
        assert not needs_compress

    def test_threshold_scales_with_model(self):
        """Different models should have different compression thresholds."""
        # 128k model at 85% = 108,800 tokens
        small_model_threshold = int(128_000 * 0.85)
        # 200k model at 85% = 170,000 tokens
        large_model_threshold = int(200_000 * 0.85)
        # 1M model at 85% = 850,000 tokens
        huge_model_threshold = int(1_000_000 * 0.85)

        # A session at ~120k tokens:
        history = _make_large_history_tokens(120_000)
        approx_tokens = estimate_messages_tokens_rough(history)

        # Should trigger for 128k model
        assert approx_tokens >= small_model_threshold
        # Should NOT trigger for 200k model
        assert approx_tokens < large_model_threshold
        # Should NOT trigger for 1M model
        assert approx_tokens < huge_model_threshold


class TestSessionHygieneWarnThreshold:
    """Test the post-compression warning threshold (95% of context)."""

    def test_warn_when_still_large(self):
        """If compressed result is still above 95% of context, should warn."""
        context_length = 200_000
        warn_threshold = int(context_length * 0.95)  # 190k
        post_compress_tokens = 195_000
        assert post_compress_tokens >= warn_threshold

    def test_no_warn_when_under(self):
        """If compressed result is under 95% of context, no warning."""
        context_length = 200_000
        warn_threshold = int(context_length * 0.95)  # 190k
        post_compress_tokens = 150_000
        assert post_compress_tokens < warn_threshold


class TestEstimatedTokenThreshold:
    """Verify that hygiene thresholds are always below the model's context
    limit — for both actual and estimated token counts.

    Regression: a previous 1.4x multiplier on rough estimates pushed the
    threshold to 85% * 1.4 = 119% of context, which exceeded the model's
    limit and prevented hygiene from ever firing for ~200K models (GLM-5).
    The fix removed the multiplier entirely — the 85% threshold already
    provides ample headroom over the agent's 50% compressor.
    """

    def test_threshold_below_context_for_200k_model(self):
        """Hygiene threshold must always be below model context."""
        context_length = 200_000
        threshold = int(context_length * 0.85)
        assert threshold < context_length


    def test_overestimate_fires_early_but_safely(self):
        """If rough estimate is 50% inflated, hygiene fires at ~57% actual usage.

        That's between the agent's 50% threshold and the model's limit —
        safe and harmless.
        """
        context_length = 200_000
        threshold = int(context_length * 0.85)  # 170K
        # If actual tokens = 113K, rough estimate = 113K * 1.5 = 170K
        # Hygiene fires when estimate hits 170K, actual is ~113K = 57% of ctx
        actual_when_fires = threshold / 1.5
        assert actual_when_fires > context_length * 0.50, (
            "Early fire should still be above agent's 50% threshold"
        )
        assert actual_when_fires < context_length, (
            "Early fire must be well below model limit"
        )


class TestTokenEstimation:
    """Verify rough token estimation works as expected for hygiene checks."""


    def test_proportional_to_content(self):
        small = _make_history(10, content_size=100)
        large = _make_history(10, content_size=10_000)
        assert estimate_messages_tokens_rough(large) > estimate_messages_tokens_rough(small)


@pytest.mark.asyncio
async def test_session_hygiene_preserves_transcript_when_no_rotation(monkeypatch, tmp_path):
    """Regression for #21301: the hygiene agent is built without a session_db,
    so _compress_context cannot rotate. When it neither rotates NOR compacts
    in place, the transcript MUST be preserved — an unconditional
    rewrite_transcript() would replace the original messages with only the
    summary (permanent data loss). Mirrors the /compress guard (#44794)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class NonRotatingCompressAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "fake-session")
            self.compression_in_place = False  # not in-place either
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            type(self).last_instance = self

        def _compress_context(self, messages, *_args, **_kwargs):
            # No session_db → cannot rotate: session_id is UNCHANGED, and this
            # is a failure-to-rotate, not an in-place success.
            return ([{"role": "assistant", "content": "summary only"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = NonRotatingCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:17585",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "795544298")

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id="17585",
            user_id="12345",
        ),
        message_id="1",
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    # The transcript must NOT be rewritten — the original is preserved.
    runner.session_store.rewrite_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_session_hygiene_preserves_transcript_when_in_place_configured_but_no_db(monkeypatch, tmp_path):
    """Regression: when compression.in_place is True but the hygiene agent has
    no session_db, archive_and_compact cannot run — _last_compaction_in_place
    stays False.  The guard must read the *result* flag, not the *config* flag,
    otherwise the transcript is unconditionally rewritten with only the summary
    (permanent data loss identical to #21301)."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class InPlaceConfiguredAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "fake-session")
            self.compression_in_place = True
            self._last_compaction_in_place = False
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            type(self).last_instance = self

        def _compress_context(self, messages, *_args, **_kwargs):
            return ([{"role": "assistant", "content": "summary only"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = InPlaceConfiguredAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:group:-1001:17585",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "795544298")

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1001",
            chat_type="group",
            thread_id="17585",
            user_id="12345",
        ),
        message_id="1",
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    # The config says in_place=True, but the DB write failed (no session_db)
    # so _last_compaction_in_place is False. Transcript must NOT be rewritten.
    runner.session_store.rewrite_transcript.assert_not_called()


@pytest.mark.asyncio
async def test_session_hygiene_timeout_continues_to_agent_and_sets_cooldown(monkeypatch, tmp_path):
    """A timed-out SessionDB-bound worker cannot compact after the live turn starts.

    The worker pauses after its real durable acquire but before publishing the
    release hook, then remains alive in the auxiliary call. Timeout cancellation
    must wait for hook publication, release the old holder before continuing to
    the live agent even when the default executor is saturated, fence the late
    commit, and defer helper cleanup. Every barrier is unwound on assertion
    failure so the RED run is deterministic and leaves no orphan future.
    """
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    from hermes_state import SessionDB
    from run_agent import AIAgent as RealAIAgent

    worker_started = threading.Event()
    worker_finished = threading.Event()
    lock_acquired_before_hook = threading.Event()
    allow_acquire_return = threading.Event()
    cancel_attempted = threading.Event()
    cancel_results = []
    release_worker = threading.Event()
    cleanup_done = threading.Event()
    live_agent_started = threading.Event()
    teardown_started = threading.Event()
    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "sess-timeout"
    live_holder = "live-turn-holder"
    db.create_session(session_id, source="telegram")
    real_try_acquire = db.try_acquire_compression_lock

    def _gated_try_acquire(lock_session_id, holder, ttl_seconds=300.0):
        acquired = real_try_acquire(
            lock_session_id,
            holder,
            ttl_seconds=ttl_seconds,
        )
        if holder != live_holder and acquired:
            lock_acquired_before_hook.set()
            assert allow_acquire_return.wait(timeout=10)
        return acquired

    db.try_acquire_compression_lock = _gated_try_acquire
    archive_spy = MagicMock(wraps=db.archive_and_compact)
    db.archive_and_compact = archive_spy
    live_lock_attempts = []

    class SlowCompressAgent(RealAIAgent):
        last_instance = None

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            compressor = MagicMock()

            def _slow_summary(*_args, **_kwargs):
                worker_started.set()
                try:
                    assert release_worker.wait(timeout=10)
                    return [
                        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
                        {"role": "user", "content": "tail"},
                    ]
                finally:
                    worker_finished.set()

            compressor.compress.side_effect = _slow_summary
            compressor.compression_count = 1
            compressor.last_prompt_tokens = 0
            compressor.last_completion_tokens = 0
            compressor._last_summary_error = None
            compressor._last_compress_aborted = False
            compressor._last_aux_model_failure_model = None
            compressor._last_aux_model_failure_error = None
            compressor._last_compression_made_progress = True
            compressor._last_summary_fallback_used = False
            self.context_compressor = compressor
            self._compression_feasibility_checked = True
            self._cached_system_prompt = "sys"
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock(side_effect=cleanup_done.set)
            type(self).last_instance = self

    run_agent_module = sys.modules["run_agent"]
    monkeypatch.setattr(run_agent_module, "AIAgent", SlowCompressAgent)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        "  hygiene_timeout_seconds: 0.1\n"
        "  hygiene_failure_cooldown_seconds: 120\n"
    )

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner
    from agent.conversation_compression import CompressionCommitFence

    original_try_cancel = CompressionCommitFence.try_cancel_before_commit

    def _tracked_try_cancel(fence):
        result = original_try_cancel(fence)
        cancel_results.append(result)
        cancel_attempted.set()
        return result

    monkeypatch.setattr(
        CompressionCommitFence,
        "try_cancel_before_commit",
        _tracked_try_cancel,
    )

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-timeout",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = SimpleNamespace(_db=db)
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "test/model",
        {
            "api_key": "test-key",
            "base_url": "https://openrouter.ai/api/v1",
            "skip_context_files": True,
        },
    )

    async def _run_live_agent(**_kwargs):
        assert lock_acquired_before_hook.is_set()
        if not teardown_started.is_set():
            assert not worker_finished.is_set(), (
                "the hygiene worker finished before the live agent started; this "
                "does not exercise early cancellation lock release"
            )
            live_agent_started.set()
        live_lock_attempts.append(
            db.try_acquire_compression_lock(session_id, live_holder)
        )
        return {
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }

    runner._run_agent = AsyncMock(side_effect=_run_live_agent)

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
        message_id="1",
    )

    loop = asyncio.get_running_loop()
    saturated_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="hygiene-saturation-test",
    )
    real_run_in_executor = loop.run_in_executor

    def _route_default_executor(executor, func, *args):
        return real_run_in_executor(
            saturated_executor if executor is None else executor,
            func,
            *args,
        )

    monkeypatch.setattr(loop, "run_in_executor", _route_default_executor)

    async def _wait_for_event(event, timeout):
        deadline = loop.time() + timeout
        while not event.is_set():
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.001)
        return True

    handler_task = None
    try:
        handler_task = asyncio.create_task(runner._handle_message(event))
        assert await _wait_for_event(lock_acquired_before_hook, 15)
        assert await _wait_for_event(cancel_attempted, 10)
        assert cancel_results[0] is None
        assert runner._run_agent.await_count == 0
        allow_acquire_return.set()

        assert await _wait_for_event(live_agent_started, 2), (
            "holder-qualified release starved behind the blocked hygiene "
            "worker in the saturated default executor"
        )
        assert await _wait_for_event(worker_started, 2)
        assert not worker_finished.is_set()
        assert runner._run_agent.await_count == 1
        assert runner._hygiene_compression_failure_cooldowns["sess-timeout"] > time.time()
        timeout_warnings = [
            s for s in adapter.sent if "Context compression timed out" in s["content"]
        ]
        assert len(timeout_warnings) == 1
        archive_spy.assert_not_called()
        SlowCompressAgent.last_instance.close.assert_not_called()
        assert live_lock_attempts == [True], (
            "the timed-out hygiene worker still held the compression lock when "
            "the live agent started"
        )
        assert db.get_compression_lock_holder(session_id) == live_holder

        release_worker.set()
        assert await _wait_for_event(cleanup_done, 10)
        assert worker_finished.is_set()
        result = await asyncio.wait_for(handler_task, timeout=15)
        assert result == "ok"

        # The late worker observed cancellation at the commit fence, so it never
        # mutated the live session after the new turn began. Cleanup still ran once
        # it was safe to tear down the helper agent's clients/providers. Its
        # idempotent holder-qualified cleanup must not release the live turn's lock.
        archive_spy.assert_not_called()
        SlowCompressAgent.last_instance.close.assert_called_once()
        assert db.get_compression_lock_holder(session_id) == live_holder
    finally:
        teardown_started.set()
        allow_acquire_return.set()
        release_worker.set()
        if handler_task is not None:
            if not handler_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(handler_task), timeout=15)
                except Exception:
                    handler_task.cancel()
            try:
                await handler_task
            except (asyncio.CancelledError, Exception):
                pass
        GatewayRunner._shutdown_executor(runner)
        saturated_executor.shutdown(wait=True, cancel_futures=True)
        if db.get_compression_lock_holder(session_id) == live_holder:
            db.release_compression_lock(session_id, live_holder)
        db.close()


def test_compression_commit_fence_closes_lock_hook_publication_race():
    """Timeout cancellation cannot win while lock cleanup is unpublished."""
    from agent.conversation_compression import CompressionCommitFence

    fence = CompressionCommitFence()
    released = threading.Event()

    assert fence.begin_lock_setup() is True
    assert fence.try_cancel_before_commit() is None
    assert fence.register_cancelled_lock_release(released.set) is False
    fence.finish_lock_setup()

    assert fence.try_cancel_before_commit() is True
    fence.release_cancelled_compression_lock()
    assert released.is_set()

    # If cancellation wins before setup starts, the worker is fenced from
    # acquiring a durable lock and therefore needs no late release hook.
    early_cancel = CompressionCommitFence()
    assert early_cancel.try_cancel_before_commit() is True
    assert early_cancel.begin_lock_setup() is False


@pytest.mark.asyncio
async def test_session_hygiene_forces_in_place_compaction_with_bound_session_db(
    monkeypatch, tmp_path
):
    """Regression for #60947: gateway hygiene should not rely on
    helper-agent session rotation to shrink a live gateway transcript.

    The hygiene pass runs before the user turn and already owns the gateway
    session binding, so it should force in-place compaction and bind the
    compressor to the gateway SessionDB. Otherwise a helper can return a
    summary without rotating/compacting, the guard preserves the original
    transcript, and the same oversized session is reloaded on every turn.
    """
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    stored_system_prompt = (
        "You are Hermes.\n\n"
        "<memory_provider_context>\n"
        "Pinboard provider instructions\n"
        "</memory_provider_context>"
    )
    fake_db = MagicMock()
    async_session_db = SimpleNamespace(
        _db=fake_db,
        get_session=AsyncMock(
            return_value={
                "system_prompt": stored_system_prompt,
            }
        ),
    )

    class FakeInPlaceCompressAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.platform = kwargs.get("platform")
            self.session_id = kwargs.get("session_id", "fake-session")
            self._session_db = kwargs.get("session_db")
            self._cached_system_prompt = None
            self.compression_in_place = False
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            type(self).last_instance = self

        def _compress_context(self, messages, *_args, **_kwargs):
            assert self.compression_in_place is True
            assert self._session_db is fake_db
            assert self.platform == "gateway_hygiene"
            assert self._cached_system_prompt == stored_system_prompt
            self._last_compaction_in_place = True
            return ([{"role": "assistant", "content": "compressed in place"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeInPlaceCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:private:12345",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="private",
    )
    runner.session_store.load_transcript.return_value = _make_history(12, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = async_session_db
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="private",
            user_id="12345",
        ),
        message_id="1",
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    agent = FakeInPlaceCompressAgent.last_instance
    assert agent is not None
    async_session_db.get_session.assert_awaited_once_with("sess-1")
    agent.context_compressor.bind_session_state.assert_called_once_with(fake_db, "sess-1")
    # In-place compaction already persisted via archive_and_compact() —
    # rewrite_transcript would replace_messages(active_only=False) and DELETE
    # the just-archived rows (#61145). The hygiene handler must skip it.
    runner.session_store.rewrite_transcript.assert_not_called()
    runner._run_agent.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_hygiene_honors_configurable_hard_message_limit(
    monkeypatch, tmp_path
):
    """compression.hygiene_hard_message_limit overrides the default.

    Regression for user-reported fix: a gateway session with a small
    transcript (12 messages) should not hit hygiene compression by default,
    but WILL when the user lowers the hard-limit to 10.  Verifies the new
    config key is actually read and applied at the force-compress gate.
    """
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    class FakeCompressAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.model = kwargs.get("model")
            self.session_id = kwargs.get("session_id", "fake-session")
            self._print_fn = None
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()
            type(self).last_instance = self

        def _compress_context(self, messages, *_args, **_kwargs):
            self.session_id = f"{self.session_id}_compressed"
            return ([{"role": "assistant", "content": "compressed"}], None)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FakeCompressAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    # Write config.yaml with lowered hard-limit
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        "  hygiene_hard_message_limit: 10\n"
    )

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:private:12345",
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="private",
    )
    # 12 messages: below default → no compression without override,
    # but above the configured limit of 10 → should compress.
    runner.session_store.load_transcript.return_value = _make_history(12, content_size=40)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    # Pick a context length large enough that the token-based threshold
    # won't trigger for 12 short messages — hard-limit must be the ONLY
    # thing firing compression.
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 1_000_000,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="private",
            user_id="12345",
        ),
        message_id="1",
    )

    result = await runner._handle_message(event)

    assert result == "ok"
    # The compression agent was instantiated → hard-limit fired on the
    # configured value (10), not the hardcoded 400 default.
    assert FakeCompressAgent.last_instance is not None, (
        "Expected hygiene compression to fire when message count (12) "
        "exceeds configured hygiene_hard_message_limit (10)"
    )


# ---------------------------------------------------------------------------
# Progress-aware hygiene wait: slow-but-streaming models are not punished
# ---------------------------------------------------------------------------

def _make_progress_runner(monkeypatch, tmp_path, agent_cls, cfg_text):
    """Shared scaffolding for the progress-aware hygiene wait tests."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(cfg_text, encoding="utf-8")

    gateway_run = importlib.import_module("gateway.run")
    GatewayRunner = gateway_run.GatewayRunner

    adapter = HygieneCaptureAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")}
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-progress",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = _make_history(6, content_size=400)
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"})
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )

    event = MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
        message_id="1",
    )
    return runner, adapter, event


