"""Host-side write suppression for forked (non-primary) memory contexts.

Regression cover for the review on PR #94983. When ``checkpoint_required``
is armed the fork sites build their child agent with ``skip_memory=False`` so
the child can reach the checkpoint contract (``initialize()`` +
``on_pre_compress``). Turning the manager on, however, also wakes the ordinary
per-turn write/recall fan-outs (``on_turn_start``, ``sync_all`` → ``sync_turn``,
``prefetch_all``/``queue_prefetch_all``). Those must NOT run for a fork, or the
child leaks its harness prompt/output into the user's real memory — and the
old ``skip_memory=True`` guard existed precisely to stop that.

The suppression lives on the host (``MemoryManager``), keyed on the
``agent_context`` label captured at ``initialize_all`` time, so it does not
depend on each provider voluntarily honouring the flag. The checkpoint path
(``on_pre_compress``) is deliberately NOT suppressed — the contract needs it.
"""

from agent.memory_manager import MemoryManager
from agent.memory_provider import (
    PRE_COMPRESS_CHECKPOINT_API_VERSION,
    MemoryProvider,
)


class _RecordingProvider(MemoryProvider):
    """Records every fan-out the manager routes to it."""

    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

    def __init__(self, name="recording"):
        self._name = name
        self.turn_start_calls = []
        self.sync_turn_calls = []
        self.prefetch_calls = []
        self.queue_prefetch_calls = []
        self.pre_compress_calls = []

    @property
    def name(self):
        return self._name

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        return None

    def get_tool_schemas(self):
        return []

    def on_turn_start(self, turn_number, message, **kwargs):
        self.turn_start_calls.append((turn_number, message))

    def prefetch(self, query, *, session_id=""):
        self.prefetch_calls.append(query)
        return "recall"

    def queue_prefetch(self, query, *, session_id=""):
        self.queue_prefetch_calls.append(query)

    def sync_turn(self, user_content, assistant_content, *, session_id="", **kwargs):
        self.sync_turn_calls.append((user_content, assistant_content))

    def on_pre_compress(self, messages):
        self.pre_compress_calls.append(messages)
        return "checkpoint-saved"


def _manager_with(provider, *, agent_context):
    mgr = MemoryManager()
    mgr.add_provider(provider)
    mgr.initialize_all("sess-1", agent_context=agent_context)
    return mgr


def _drive_turn(mgr):
    mgr.on_turn_start(1, "user text")
    recall = mgr.prefetch_all("user text")
    mgr.sync_all("user text", "assistant text")
    mgr.queue_prefetch_all("user text")
    mgr.flush_pending(timeout=5)
    return recall


# -- Point 1: fork writes are host-suppressed -----------------------------


def test_subagent_context_suppresses_all_turn_writes_and_recall():
    provider = _RecordingProvider()
    mgr = _manager_with(provider, agent_context="subagent")

    recall = _drive_turn(mgr)

    assert provider.turn_start_calls == []
    assert provider.sync_turn_calls == []
    assert provider.prefetch_calls == []
    assert provider.queue_prefetch_calls == []
    assert recall == ""


def test_cron_and_flush_contexts_are_also_suppressed():
    for ctx in ("cron", "flush"):
        provider = _RecordingProvider()
        mgr = _manager_with(provider, agent_context=ctx)
        _drive_turn(mgr)
        assert provider.sync_turn_calls == [], ctx
        assert provider.turn_start_calls == [], ctx
        assert provider.queue_prefetch_calls == [], ctx


def test_primary_context_still_writes_and_recalls():
    provider = _RecordingProvider()
    mgr = _manager_with(provider, agent_context="primary")

    recall = _drive_turn(mgr)

    assert provider.turn_start_calls == [(1, "user text")]
    assert provider.sync_turn_calls == [("user text", "assistant text")]
    assert provider.queue_prefetch_calls == ["user text"]
    assert provider.prefetch_calls == ["user text"]
    assert recall == "recall"


def test_missing_agent_context_defaults_to_primary_writes():
    """A manager never told its context behaves as the historical primary."""
    provider = _RecordingProvider()
    mgr = MemoryManager()
    mgr.add_provider(provider)
    # No initialize_all / no agent_context — legacy construction path.
    _drive_turn(mgr)
    assert provider.sync_turn_calls == [("user text", "assistant text")]


# -- Point 3: the checkpoint path is NOT suppressed for a fork -------------


def test_subagent_checkpoint_on_pre_compress_still_saves():
    """A v2 checkpoint provider MUST still be driven under a fork context;
    the non-primary write skip must not swallow the checkpoint contract."""
    provider = _RecordingProvider()
    mgr = _manager_with(provider, agent_context="subagent")

    messages = [
        {"role": "user", "content": "evidence"},
        {"role": "assistant", "content": "reply"},
    ]
    summary = mgr.on_pre_compress(
        messages,
        require_checkpoint=True,
        checkpoint_api_version=PRE_COMPRESS_CHECKPOINT_API_VERSION,
    )

    # on_pre_compress ran (checkpoint actually saved) and require_checkpoint
    # was satisfied — no RuntimeError propagated.
    assert len(provider.pre_compress_calls) == 1
    assert summary == "checkpoint-saved"


# -- Point 2: parent_session_id reaches the provider initialize() ----------


def test_parent_session_id_reaches_provider_initialize():
    """The docs promise forks are initialized with parent_session_id so a
    provider can tell the fork shapes apart; drive it through a real
    ``AIAgent`` constructor and assert the label lands in initialize kwargs."""
    from unittest.mock import patch

    seen = {}

    class _InitRecordingProvider(MemoryProvider):
        pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION

        @property
        def name(self):
            return "recording"

        def is_available(self):
            return True

        def initialize(self, session_id, **kwargs):
            seen.clear()
            seen.update(kwargs)

        def get_tool_schemas(self):
            return []

    cfg = {
        "compression": {},
        "agent": {},
        "memory": {
            "provider": "recording",
            "memory_enabled": False,
            "user_profile_enabled": False,
        },
    }

    def _build(**extra):
        with (
            patch("hermes_cli.config.load_config", return_value=cfg),
            patch("hermes_cli.config.load_config_readonly", return_value=cfg),
            patch(
                "plugins.memory.load_memory_provider",
                return_value=_InitRecordingProvider(),
            ),
            patch(
                "agent.model_metadata.get_model_context_length",
                return_value=204_800,
            ),
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            from run_agent import AIAgent

            AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=False,
                **extra,
            )
        return seen

    kwargs = _build(
        memory_agent_context="subagent",
        parent_session_id="parent-session-xyz",
    )
    assert kwargs.get("parent_session_id") == "parent-session-xyz"
    assert kwargs.get("agent_context") == "subagent"

    # A primary agent with no parent simply omits the key (falsy → not threaded).
    kwargs = _build()
    assert "parent_session_id" not in kwargs or not kwargs["parent_session_id"]
