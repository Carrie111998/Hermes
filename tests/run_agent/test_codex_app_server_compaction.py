import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.codex_runtime import _record_codex_app_server_compaction
from agent.conversation_compression import COMPACTION_DONE_STATUS, COMPACTION_STATUS, compress_context
from agent.transports.codex_app_server_session import TurnResult


class FakeCodexSession:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.closed = False

    def compact_thread(self):
        self.calls += 1
        return self.result

    def close(self):
        self.closed = True


class SlowCodexSession(FakeCodexSession):
    def __init__(self, result, touch_calls):
        super().__init__(result)
        self.touch_calls = touch_calls

    def compact_thread(self):
        self.calls += 1
        _wait_for_touch(self.touch_calls, "context compression in progress")
        return self.result


def _wait_for_touch(touch_calls, desc, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if desc in touch_calls:
            return
        time.sleep(0.01)
    pytest.fail(f"timed out waiting for touch {desc!r}; saw {touch_calls!r}")


class DummyAgent:
    def __init__(
        self,
        result,
        *,
        auto_compaction="native",
    ):
        self.api_mode = "codex_app_server"
        self.codex_app_server_auto_compaction = auto_compaction
        self.session_id = "hermes-session-1"
        self.platform = "cli"
        self._cached_system_prompt = "cached prompt"
        self._codex_session = FakeCodexSession(result)
        self.context_compressor = SimpleNamespace(
            compression_count=0,
            last_compression_rough_tokens=0,
            last_prompt_tokens=123,
            last_completion_tokens=45,
            awaiting_real_usage_after_compression=False,
        )
        self.statuses = []
        self.status_events = []
        self.status_callback = lambda kind, text: self.status_events.append((kind, text))
        self.warnings = []
        self.events = []
        self.built_prompts = []
        self.touch_calls = []
        self._compression_activity_heartbeat_interval = 0.1

    def _touch_activity(self, desc):
        self.touch_calls.append(desc)

    def _emit_status(self, message):
        self.statuses.append(message)
        self.status_callback("lifecycle", message)

    def _emit_warning(self, message):
        self.warnings.append(message)
        self.status_callback("warn", message)

    def _build_system_prompt(self, system_message):
        self.built_prompts.append(system_message)
        return "built prompt"

    def event_callback(self, name, payload):
        self.events.append((name, payload))


def test_codex_app_server_native_auto_mode_leaves_thread_compaction_to_codex():
    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="compact-turn-1")
    )
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        task_id="test",
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent._codex_session.calls == 0
    assert agent.context_compressor.compression_count == 0
    assert agent.events == []


def test_codex_app_server_compaction_heartbeat_refreshes_activity_while_waiting():
    agent = DummyAgent(
        TurnResult(
            thread_id="thread-1",
            turn_id="compact-turn-1",
            compacted=True,
        )
    )
    agent._codex_session = SlowCodexSession(
        agent._codex_session.result,
        agent.touch_calls,
    )
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        task_id="test",
        force=True,
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent._codex_session.calls == 1
    assert "context compression started" in agent.touch_calls
    assert "context compression in progress" in agent.touch_calls
    assert agent.touch_calls[-1] == "context compression completed"






@pytest.mark.parametrize(
    ("auto_compaction", "force"),
    [
        ("hermes", False),
        ("native", True),
        ("off", True),
    ],
)
def test_codex_app_server_without_thread_uses_builtin_compressor(
    auto_compaction,
    force,
):
    """Preflight/hygiene must not count a missing Codex thread as compaction."""
    agent = MagicMock()
    agent.api_mode = "codex_app_server"
    agent.codex_app_server_auto_compaction = auto_compaction
    agent._codex_session = None
    agent.session_id = "hermes-session-1"
    agent.platform = "cli"
    agent.model = "test/model"
    agent.provider = "test"
    agent.tools = []
    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    agent._memory_manager = None
    agent._session_db = None
    agent._memory_store = None
    agent._memory_enabled = False
    agent._user_profile_enabled = False
    agent._todo_store = MagicMock()
    agent._todo_store.format_for_injection.return_value = ""
    agent._cached_system_prompt = "cached prompt"
    agent.context_compressor = MagicMock()
    compressed = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
        {"role": "assistant", "content": "recent reply"},
    ]
    agent.context_compressor.compress.return_value = compressed
    agent.context_compressor.compression_count = 1
    agent.context_compressor.last_compression_rough_tokens = 0
    agent.context_compressor._last_compress_aborted = False
    agent.context_compressor._last_summary_error = None
    messages = [{"role": "user", "content": f"message {i}"} for i in range(8)]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        force=force,
    )

    assert returned == compressed
    assert prompt == "cached prompt"
    agent.context_compressor.compress.assert_called_once()


@pytest.mark.parametrize("auto_compaction", ["native", "off"])
def test_codex_app_server_auto_without_thread_skips_builtin_compressor(
    auto_compaction,
):
    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="compact-turn-1"),
        auto_compaction=auto_compaction,
    )
    agent._codex_session = None
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent.context_compressor.compression_count == 0


def test_codex_app_server_off_mode_force_with_thread_uses_native_compaction():
    agent = DummyAgent(
        TurnResult(
            thread_id="thread-1",
            turn_id="compact-turn-1",
            compacted=True,
        ),
        auto_compaction="off",
    )
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        force=True,
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent._codex_session.calls == 1
    assert agent.context_compressor.compression_count == 1


def test_codex_app_server_compression_failure_preserves_bookkeeping():
    agent = DummyAgent(TurnResult(error="compact failed"))
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        force=True,
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent._codex_session.calls == 1
    assert agent.context_compressor.compression_count == 0
    assert agent.context_compressor.last_prompt_tokens == 123
    assert agent.warnings
    assert agent.touch_calls[0] == "context compression started"
    assert agent.touch_calls[-1] == "context compression failed"
    assert agent.status_events == [
        ("lifecycle", COMPACTION_STATUS),
        ("warn", "⚠ Codex app-server compaction failed: compact failed"),
        ("compacted", COMPACTION_DONE_STATUS),
    ]


def test_codex_app_server_compaction_requires_context_compaction_event():
    agent = DummyAgent(
        TurnResult(
            thread_id="thread-1",
            turn_id="compact-turn-1",
            compacted=False,
        )
    )
    messages = [{"role": "user", "content": "hi"}]

    returned, prompt = compress_context(
        agent,
        messages,
        "system",
        approx_tokens=100000,
        force=True,
    )

    assert returned is messages
    assert prompt == "cached prompt"
    assert agent.context_compressor.compression_count == 0
    assert agent.warnings == [
        "⚠ Codex app-server compaction failed: Codex completed the compact "
        "turn without the required contextCompaction event"
    ]
    assert agent.touch_calls[-1] == "context compression failed"


def test_forced_compaction_record_does_not_manufacture_missing_boundary():
    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="compact-turn-1")
    )

    assert _record_codex_app_server_compaction(
        agent,
        agent._codex_session.result,
        force=True,
    ) is False
    assert agent.context_compressor.compression_count == 0




def test_codex_native_boundary_clears_stale_hermes_fallback_streak():
    from unittest.mock import patch

    from agent.context_compressor import ContextCompressor

    with patch(
        "agent.context_compressor.get_model_context_length",
        return_value=100_000,
    ):
        compressor = ContextCompressor(model="test-model", quiet_mode=True)
    compressor._fallback_compression_streak = 1
    compressor._last_summary_fallback_used = True

    agent = DummyAgent(
        TurnResult(thread_id="thread-1", turn_id="normal-turn-1")
    )
    agent.context_compressor = compressor
    turn = TurnResult(
        thread_id="thread-1",
        turn_id="normal-turn-1",
        compacted=True,
    )

    assert _record_codex_app_server_compaction(agent, turn) is True
    assert compressor._fallback_compression_streak == 0
    assert compressor._verify_compaction_cleared_threshold is True
