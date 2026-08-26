"""Host-side contract tests for the opt-in pre-compress checkpoint API (v2).

The contract has three parts:
- providers opt in by advertising ``pre_compress_checkpoint_api_version = 2``
  (v1 is the implicit historical best-effort contract with raw messages);
- ``MemoryManager`` exposes capability probing and a ``require_checkpoint``
  mode whose failure must propagate instead of being swallowed;
- the compression host normalizes messages to direct user/assistant evidence
  before handing them to v2+ providers.
"""

import pytest

from agent.conversation_compression import (
    CompressionCheckpointUnavailable,
    _checkpoint_blocked,
    _direct_messages_for_pre_compress_memory,
)
from agent.context_compressor import COMPRESSED_SUMMARY_METADATA_KEY
from agent.memory_manager import MemoryManager
from agent.memory_provider import (
    PRE_COMPRESS_CHECKPOINT_API_VERSION,
    MemoryProvider,
)


class _BaseStubProvider(MemoryProvider):
    def __init__(self, name="stub"):
        self._name = name
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

    def on_pre_compress(self, messages):
        self.pre_compress_calls.append(messages)
        return f"{self._name} context"


class _CheckpointProvider(_BaseStubProvider):
    pre_compress_checkpoint_api_version = PRE_COMPRESS_CHECKPOINT_API_VERSION


class _FailingCheckpointProvider(_CheckpointProvider):
    def on_pre_compress(self, messages):
        raise RuntimeError("durable store unreachable")


class _FailingLegacyProvider(_BaseStubProvider):
    def on_pre_compress(self, messages):
        raise RuntimeError("legacy best-effort failure")


def test_provider_base_class_defaults_to_implicit_historical_api_version_one():
    assert MemoryProvider.pre_compress_checkpoint_api_version == 1
    assert PRE_COMPRESS_CHECKPOINT_API_VERSION == 2


def test_v1_providers_receive_raw_messages_and_v2_receive_evidence():
    """The historical (v1) contract is untouched: raw message list.

    Only providers that opted into checkpoint API v2 receive the
    host-normalized evidence handoff.
    """
    manager = MemoryManager()
    legacy = _BaseStubProvider("legacy")
    manager.add_provider(legacy)
    raw = [
        {"role": "user", "content": "evidence"},
        {"role": "tool", "content": "tool output", "tool_call_id": "t1"},
    ]
    evidence = [{"role": "user", "content": "evidence"}]

    manager.on_pre_compress(raw, evidence_messages=evidence)
    assert legacy.pre_compress_calls == [raw]

    durable_manager = MemoryManager()
    durable = _CheckpointProvider("durable")
    durable_manager.add_provider(durable)
    durable_manager.on_pre_compress(raw, evidence_messages=evidence)
    assert durable.pre_compress_calls == [evidence]


def test_direct_messages_filter_keeps_only_direct_source_evidence():
    messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "durable user decision"},
        {"role": "assistant", "content": "direct assistant answer"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "content": "tool output", "tool_call_id": "t1"},
        {
            "role": "assistant",
            "content": "previous compaction summary",
            COMPRESSED_SUMMARY_METADATA_KEY: True,
        },
        "not-a-dict",
    ]

    direct = _direct_messages_for_pre_compress_memory(messages)

    assert [m["content"] for m in direct] == [
        "durable user decision",
        "direct assistant answer",
    ]


def test_direct_messages_filter_keeps_prose_of_tool_call_messages():
    """Assistant prose next to tool_calls is evidence; the payload is not."""
    messages = [
        {"role": "user", "content": "please scan the network"},
        {
            "role": "assistant",
            "content": "Scanning now — the last sweep found 26 hosts.",
            "tool_calls": [{"id": "t1", "function": {"name": "terminal"}}],
        },
        {"role": "assistant", "content": "   ", "tool_calls": [{"id": "t2"}]},
    ]

    direct = _direct_messages_for_pre_compress_memory(messages)

    assert [m["content"] for m in direct] == [
        "please scan the network",
        "Scanning now — the last sweep found 26 hosts.",
    ]
    assert all("tool_calls" not in m for m in direct)
    # The original message list is not mutated.
    assert messages[1]["tool_calls"]


def test_manager_advertises_checkpoint_capability_only_with_capable_provider():
    # The host allows one external provider per manager, so capability is
    # probed on two separate managers.
    legacy_manager = MemoryManager()
    legacy_manager.add_provider(_BaseStubProvider("legacy"))
    assert legacy_manager.supports_pre_compress_checkpoint(
        PRE_COMPRESS_CHECKPOINT_API_VERSION
    ) is False

    durable_manager = MemoryManager()
    durable_manager.add_provider(_CheckpointProvider("durable"))
    assert durable_manager.supports_pre_compress_checkpoint(
        PRE_COMPRESS_CHECKPOINT_API_VERSION
    ) is True


def test_manager_require_checkpoint_raises_without_capable_provider():
    manager = MemoryManager()
    manager.add_provider(_BaseStubProvider("legacy"))

    with pytest.raises(RuntimeError, match="pre-compress checkpoint"):
        manager.on_pre_compress(
            [{"role": "user", "content": "evidence"}],
            require_checkpoint=True,
            checkpoint_api_version=PRE_COMPRESS_CHECKPOINT_API_VERSION,
        )


def test_manager_require_checkpoint_propagates_checkpoint_provider_failure():
    manager = MemoryManager()
    manager.add_provider(_FailingCheckpointProvider("durable"))

    with pytest.raises(RuntimeError, match="durable store unreachable"):
        manager.on_pre_compress(
            [{"role": "user", "content": "evidence"}],
            require_checkpoint=True,
            checkpoint_api_version=PRE_COMPRESS_CHECKPOINT_API_VERSION,
        )


def test_manager_require_checkpoint_succeeds_and_returns_provider_context():
    manager = MemoryManager()
    durable = _CheckpointProvider("durable")
    manager.add_provider(durable)

    combined = manager.on_pre_compress(
        [{"role": "user", "content": "evidence"}],
        require_checkpoint=True,
        checkpoint_api_version=PRE_COMPRESS_CHECKPOINT_API_VERSION,
    )

    assert "durable context" in combined
    assert durable.pre_compress_calls


def test_manager_best_effort_mode_keeps_historical_swallow_semantics():
    manager = MemoryManager()
    manager.add_provider(_FailingLegacyProvider("legacy"))

    combined = manager.on_pre_compress([{"role": "user", "content": "evidence"}])

    assert combined == ""


def test_checkpoint_blocked_error_is_prefixed_and_typed():
    error = _checkpoint_blocked("no active provider")
    assert isinstance(error, CompressionCheckpointUnavailable)
    assert str(error).startswith("BLOCKED_MISSING_PREREQUISITE:")
    assert "no active provider" in str(error)


def test_compressed_summary_marker_survives_restart_via_resume_history(tmp_path):
    """The persistent marker reaches the resumed model history — and only it.

    ``get_messages_as_conversation`` keeps its existing marker-free contract;
    the resume path carries ``_compressed_summary`` so checkpoint providers
    keep excluding derivative summaries after a process restart.
    """
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1", source="cli")
    db.append_message("s1", "user", "durable user evidence")
    db.append_message(
        "s1", "assistant", "derivative summary", _compressed_summary=True
    )

    reopened = SessionDB(tmp_path / "state.db")
    model_history, _display = reopened.get_resume_conversations("s1")
    by_content = {m.get("content"): m for m in model_history}
    assert by_content["derivative summary"].get("_compressed_summary") is True
    assert "_compressed_summary" not in by_content["durable user evidence"]

    plain = reopened.get_messages_as_conversation("s1")
    assert all("_compressed_summary" not in m for m in plain)


def test_compressed_summary_column_is_added_to_legacy_databases(tmp_path):
    """Pre-upgrade databases gain the marker column via declarative reconcile.

    ``_init_schema()`` diffs live columns against SCHEMA_SQL on every
    writable open and ADDs whatever is missing, so a database created
    before this feature must accept marker writes after a plain reopen.
    """
    import sqlite3

    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    SessionDB(db_path)

    # Simulate a pre-upgrade database: the marker column does not exist.
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE messages DROP COLUMN _compressed_summary")
    conn.commit()
    legacy_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(messages)")
    }
    conn.close()
    assert "_compressed_summary" not in legacy_cols

    upgraded = SessionDB(db_path)
    upgraded.create_session("legacy", source="cli")
    upgraded.append_message(
        "legacy", "assistant", "derivative summary", _compressed_summary=True
    )

    model_history, _display = upgraded.get_resume_conversations("legacy")
    assert model_history[-1].get("_compressed_summary") is True


def test_native_responses_compaction_is_suppressed_when_checkpoint_required():
    """checkpoint_required must keep ``context_management`` off the wire.

    Server-side native compaction is a lossy boundary the provider owns; no
    pre-compress checkpoint can run before it, so the gate suppresses the
    payload while ordinary checkpoint-aware Hermes compression stays
    available.
    """
    from types import SimpleNamespace

    from agent.native_compaction import native_compaction_context_management

    def agent(checkpoint_required):
        return SimpleNamespace(
            model="gpt-5.6",
            base_url="https://api.openai.com/v1",
            codex_responses_native_compaction=True,
            compression_enabled=True,
            compression_checkpoint_required=checkpoint_required,
            codex_responses_compact_threshold=0.8,
            context_compressor=None,
        )

    assert native_compaction_context_management(
        agent(False), is_codex_backend=True
    )
    assert (
        native_compaction_context_management(agent(True), is_codex_backend=True)
        is None
    )


def test_codex_app_server_turn_fails_closed_before_codex_can_compact():
    """checkpoint_required + app-server must never reach ``run_turn()``.

    The codex agent compacts its own thread; once ``run_turn()`` executes, a
    codex-owned compaction may already have happened with no checkpoint. The
    turn entrypoint must raise first — the session is never even created.
    """
    from types import SimpleNamespace

    from agent.codex_runtime import run_codex_app_server_turn

    class _ExplodingSession:
        def run_turn(self, *args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("run_turn() must not be reached")

    agent = SimpleNamespace(
        api_mode="codex_app_server",
        compression_checkpoint_required=True,
        _codex_session=_ExplodingSession(),
    )

    with pytest.raises(CompressionCheckpointUnavailable, match="codex_app_server"):
        run_codex_app_server_turn(
            agent,
            user_message="hello",
            original_user_message="hello",
            messages=[],
            effective_task_id="t1",
        )


def test_agent_init_refuses_checkpoint_required_on_codex_app_server():
    """The incompatible configuration must fail closed at init time.

    In the default "native" auto-compaction mode Hermes never initiates the
    compaction, so the compress_context() guard alone cannot cover native
    turns — init_agent has to refuse before a turn exists.
    """
    from agent.agent_init import (
        _refuse_checkpoint_required_on_codex_app_server,
    )

    with pytest.raises(RuntimeError, match="BLOCKED_MISSING_PREREQUISITE"):
        _refuse_checkpoint_required_on_codex_app_server(True, "codex_app_server")

    # Every other combination stays permitted.
    _refuse_checkpoint_required_on_codex_app_server(True, "chat_completions")
    _refuse_checkpoint_required_on_codex_app_server(True, "codex_responses")
    _refuse_checkpoint_required_on_codex_app_server(False, "codex_app_server")
    _refuse_checkpoint_required_on_codex_app_server(False, None)


def test_turn_finalizer_never_micro_compacts_while_checkpoint_gate_armed(
    monkeypatch,
):
    """Micro-compaction is a lossy rewrite authority with no checkpoint hook.

    Even if a live agent's compressor has ``_micro_compact_enabled`` flipped
    on (agent init forces it off under the gate, but it is plain mutable
    state), the post-turn finalizer must refuse to call ``_micro_compact()``
    while ``compression_checkpoint_required`` is armed — otherwise assistant
    evidence is absorbed into a rolling summary that the checkpoint filter
    later excludes, and the evidence never reaches the durable provider.
    """
    from tests.agent.test_turn_finalizer_final_response_persistence import (
        FakeAgent,
    )
    from agent.turn_finalizer import finalize_turn

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])

    class _RecordingCompressor:
        _micro_compact_enabled = True

        def __init__(self):
            self.calls = 0

        def _micro_compact(self, messages):
            self.calls += 1
            return list(messages)

    def _run(checkpoint_required: bool):
        agent = FakeAgent()
        compressor = _RecordingCompressor()
        agent.context_compressor = compressor
        agent.compression_checkpoint_required = checkpoint_required
        finalize_turn(
            agent,
            final_response="Done.",
            api_call_count=1,
            interrupted=False,
            failed=False,
            messages=[
                {"role": "user", "content": "do it"},
                {"role": "assistant", "content": "Done."},
            ],
            conversation_history=[],
            effective_task_id="task",
            turn_id="turn",
            user_message="do it",
            original_user_message="do it",
            _should_review_memory=False,
            _turn_exit_reason="completed",
        )
        return compressor.calls

    # Gate armed: micro-compaction never runs.
    assert _run(checkpoint_required=True) == 0

    # Gate off: micro-compaction remains reachable — sabotage control proving
    # this harness genuinely exercises the call site (the finalizer swallows
    # compressor exceptions, so a call counter is the observable signal).
    assert _run(checkpoint_required=False) == 1


def test_agent_init_suppresses_micro_compaction_under_checkpoint_gate():
    """checkpoint_required forces micro-compaction off at init.

    Both keys can be enabled together in config; the gate must win so every
    lossy rewrite passes through the checkpoint-aware batch compressor.
    """
    import inspect

    from agent import agent_init

    source = inspect.getsource(agent_init)
    # The suppression must happen before the compressor attribute assignment.
    suppress_idx = source.find(
        "if compression_checkpoint_required and compression_micro_compact:"
    )
    assign_idx = source.find("_cc._micro_compact_enabled = compression_micro_compact")
    assert suppress_idx != -1, (
        "init_agent must suppress micro-compaction when checkpoint_required"
    )
    assert assign_idx != -1
    assert suppress_idx < assign_idx


# --- Context-engine compaction authority (engine_compacts_outside_compress) --


def _engine_stubs():
    """Build engine stubs against the CURRENTLY imported ContextEngine: sibling
    suites purge ``agent.*`` from sys.modules, and stubs bound to a stale ABC
    would fail isinstance for the wrong reason."""
    from agent.context_engine import ContextEngine

    class _CompressOnlyEngine(ContextEngine):
        """The real third-party shape: should_compress() + compress() only."""

        @property
        def name(self) -> str:
            return "compress-only"

        def update_from_response(self, usage):
            return None

        def should_compress(self, prompt_tokens=None):
            return False

        def compress(
            self,
            messages,
            current_tokens=None,
            focus_topic=None,
            force=False,
            memory_context="",
        ):
            return messages

    class _TurnCompleteEngine(_CompressOnlyEngine):
        """Also takes the post-turn hook, which no checkpoint precedes."""

        def on_turn_complete(self, messages, usage=None, **kwargs):
            return None

    return _CompressOnlyEngine, _TurnCompleteEngine


def test_engine_declaration_defaults_to_undeclared():
    """The declaration is tri-state and inherits ``None``, not a bool: False
    would make every pre-existing engine an implicit waiver, True would refuse
    engines the host can prove safe."""
    from agent.context_engine import ContextEngine

    compress_only, _turn_complete = _engine_stubs()
    assert ContextEngine.compacts_outside_compress is None
    assert compress_only().compacts_outside_compress is None


def test_undeclared_engine_overriding_on_turn_complete_is_refused():
    """An undeclared engine overriding ``on_turn_complete`` fails closed; the
    verdict is the same ``__func__``-vs-ABC-default check the loop performs."""
    from agent.context_engine import engine_compacts_outside_compress

    _compress_only, turn_complete = _engine_stubs()
    unsafe, reason = engine_compacts_outside_compress(turn_complete())
    assert unsafe is True
    assert "on_turn_complete" in reason


def test_compress_only_engine_resolves_safe():
    """compress()-only engines resolve safe, also with the hooks that do not
    count: ``prune_tool_results_only`` (gated at its call site),
    ``select_context`` (request-only), ``on_session_end`` (return discarded)."""
    from agent.context_engine import engine_compacts_outside_compress

    compress_only, _turn_complete = _engine_stubs()
    unsafe, _reason = engine_compacts_outside_compress(compress_only())
    assert unsafe is False

    class _GatedHookEngine(compress_only):
        def prune_tool_results_only(self, messages, current_tokens=None):
            return messages, 0

        def select_context(self, request_messages, **kwargs):
            return request_messages

        def on_session_end(self, session_id, messages):
            return None

    unsafe, _reason = engine_compacts_outside_compress(_GatedHookEngine())
    assert unsafe is False

    # No engine installed is not an engine that compacts.
    assert engine_compacts_outside_compress(None)[0] is False


def test_non_context_engine_object_is_refused():
    """A non-ContextEngine object in the engine slot (the directory loader has
    no isinstance gate) is refused rather than hook-inferred."""
    from types import SimpleNamespace

    from agent.context_engine import (
        ContextEngine,
        engine_compacts_outside_compress,
    )

    unsafe, reason = engine_compacts_outside_compress(SimpleNamespace())
    assert unsafe is True
    assert "ContextEngine" in reason

    # A duck borrowing the ABC's default hooks matches every __func__ identity,
    # so hook inference alone would clear it — the isinstance rule must not.
    class _BorrowedDefaultsDuck:
        name = "duck"
        on_turn_complete = ContextEngine.on_turn_complete

        def should_compress(self, prompt_tokens=None):
            return False

        def compress(self, messages, **kwargs):
            return messages

    assert engine_compacts_outside_compress(_BorrowedDefaultsDuck())[0] is True


def test_explicit_declaration_overrides_inference():
    """A declaration wins over inference in both directions: a pure observer
    keeps ``on_turn_complete``; an engine with its own scheduler (invisible to
    inference) can declare True."""
    from agent.context_engine import engine_compacts_outside_compress

    compress_only, turn_complete = _engine_stubs()

    class _DeclaredObserver(turn_complete):
        compacts_outside_compress = False

    class _DeclaredScheduler(compress_only):
        compacts_outside_compress = True

    assert engine_compacts_outside_compress(_DeclaredObserver())[0] is False
    assert engine_compacts_outside_compress(_DeclaredScheduler())[0] is True

    # Truthy-but-not-True is not a declaration: plugin engines and MagicMocks
    # answer getattr with truthy auto-attributes.
    class _AutoAttributeEngine(turn_complete):
        compacts_outside_compress = 1

    assert engine_compacts_outside_compress(_AutoAttributeEngine())[0] is True


# --- Proactive tool-result prune (agent/conversation_loop.py) -------------


class _CountingPruneCompressor:
    """Built-in-shaped compressor double: counts instead of asserting, because
    the call site swallows exceptions. Publishes ``would_proactively_prune``
    with a configured trigger by default (a suppression counts only when a
    prune would have run)."""

    def __init__(self, proactive_prune_tokens: int = 48_000):
        self.calls = 0
        self.proactive_prune_tokens = proactive_prune_tokens

    def would_proactively_prune(self, current_tokens=None):
        if self.proactive_prune_tokens <= 0:
            return False
        if current_tokens is not None and current_tokens < self.proactive_prune_tokens:
            return False
        return True

    def prune_tool_results_only(self, messages, current_tokens=None):
        self.calls += 1
        return list(messages) + [{"role": "system", "content": "pruned"}], 3


def _prune_agent(checkpoint_required: bool):
    from types import SimpleNamespace

    return SimpleNamespace(compression_checkpoint_required=checkpoint_required)


def test_proactive_prune_is_suppressed_when_checkpoint_required():
    """The gate reaches the prune and the transcript survives intact:
    suppression, not refusal."""
    from agent.conversation_loop import _proactive_tool_result_prune

    compressor = _CountingPruneCompressor()
    messages = [{"role": "user", "content": "scan"}]
    agent = _prune_agent(True)

    result = _proactive_tool_result_prune(agent, compressor, messages, 400_000)

    assert compressor.calls == 0
    assert result is messages
    assert agent._checkpoint_gate_suppression_count == 1


def test_proactive_prune_still_runs_when_gate_is_off():
    """Sabotage control: gate off, the harness really reaches the prune."""
    from agent.conversation_loop import _proactive_tool_result_prune

    compressor = _CountingPruneCompressor()
    messages = [{"role": "user", "content": "scan"}]

    result = _proactive_tool_result_prune(
        _prune_agent(False), compressor, messages, 400_000
    )

    assert compressor.calls == 1
    assert result is not messages
    assert result[-1]["content"] == "pruned"


def test_engine_override_of_prune_is_suppressed_too():
    """One call site covers the built-in and every engine overriding the hook."""
    from agent.conversation_loop import _proactive_tool_result_prune

    compress_only, _turn_complete = _engine_stubs()

    class _PruningEngine(compress_only):
        calls = 0

        def prune_tool_results_only(self, messages, current_tokens=None):
            type(self).calls += 1
            return list(messages), 3

    engine = _PruningEngine()
    messages = [{"role": "user", "content": "scan"}]
    agent = _prune_agent(True)

    assert _proactive_tool_result_prune(agent, engine, messages, 400_000) is messages
    assert _PruningEngine.calls == 0
    # An overriding engine owns its trigger policy; the host would have
    # dispatched the hook, so this suppression IS reported.
    assert agent._checkpoint_gate_suppression_count == 1

    # Same engine, gate off: the override is reached (sabotage control).
    _proactive_tool_result_prune(_prune_agent(False), engine, messages, 400_000)
    assert _PruningEngine.calls == 1


def test_prune_suppression_logs_once_and_names_the_availability_consequence(
    caplog, monkeypatch,
):
    """One warning per process, naming the trade: with compaction fail-closed,
    a checkpoint-provider outage halts the session."""
    import logging

    from agent import conversation_loop
    from agent.conversation_loop import _proactive_tool_result_prune

    monkeypatch.setattr(
        conversation_loop, "_checkpoint_gate_warned", set(), raising=True
    )
    compressor = _CountingPruneCompressor()
    agent = _prune_agent(True)

    with caplog.at_level(logging.WARNING, logger=conversation_loop.__name__):
        for _ in range(2):
            _proactive_tool_result_prune(agent, compressor, [], 400_000)

    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "checkpoint_required" in r.getMessage()
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "tool-result prune" in message
    assert "halt" in message
    # Suppression itself is re-evaluated every call — only the log is deduped.
    assert agent._checkpoint_gate_suppression_count == 2


def _prune_warnings(caplog):
    import logging

    return [
        r for r in caplog.records
        if r.levelno >= logging.WARNING
        and "tool-result prune" in r.getMessage()
    ]


def test_disabled_prune_is_not_reported_as_a_suppressed_authority(
    caplog, monkeypatch,
):
    """The shipping default (``proactive_prune_tokens: 0``) suppresses nothing,
    so it must report nothing."""
    import logging

    from agent import conversation_loop
    from agent.conversation_loop import _proactive_tool_result_prune

    monkeypatch.setattr(
        conversation_loop, "_checkpoint_gate_warned", set(), raising=True
    )
    compressor = _CountingPruneCompressor(proactive_prune_tokens=0)
    messages = [{"role": "user", "content": "scan"}]
    agent = _prune_agent(True)

    with caplog.at_level(logging.WARNING, logger=conversation_loop.__name__):
        result = _proactive_tool_result_prune(agent, compressor, messages, 400_000)

    # Still suppressed: the prune is not reached and the transcript is intact.
    assert compressor.calls == 0
    assert result is messages
    # ...but nothing was withheld, so nothing is reported.
    assert getattr(agent, "_checkpoint_gate_suppression_count", 0) == 0
    assert _prune_warnings(caplog) == []


def test_prune_below_its_trigger_is_not_reported_as_a_suppressed_authority(
    caplog, monkeypatch,
):
    """Configured but below the trigger is equally a non-event: the withheld
    call would have returned the input untouched."""
    import logging

    from agent import conversation_loop
    from agent.conversation_loop import _proactive_tool_result_prune

    monkeypatch.setattr(
        conversation_loop, "_checkpoint_gate_warned", set(), raising=True
    )
    compressor = _CountingPruneCompressor(proactive_prune_tokens=400_000)
    agent = _prune_agent(True)

    with caplog.at_level(logging.WARNING, logger=conversation_loop.__name__):
        _proactive_tool_result_prune(agent, compressor, [], 399_999)

    assert getattr(agent, "_checkpoint_gate_suppression_count", 0) == 0
    assert _prune_warnings(caplog) == []

    # One token more and the same compressor IS a suppressed authority: the
    # threshold, not the harness, decided above.
    with caplog.at_level(logging.WARNING, logger=conversation_loop.__name__):
        _proactive_tool_result_prune(agent, compressor, [], 400_000)

    assert agent._checkpoint_gate_suppression_count == 1
    assert len(_prune_warnings(caplog)) == 1


def test_would_proactively_prune_is_the_compressors_own_precondition():
    """The predicate is answered by the real ``ContextCompressor`` (not a
    double), so the host's report cannot drift from the compressor's trigger."""
    from unittest.mock import patch

    from agent.context_compressor import ContextCompressor
    from agent.conversation_loop import _prune_would_have_run

    with patch(
        "agent.context_compressor.get_model_context_length", return_value=1_000_000
    ):
        default = ContextCompressor(model="test", quiet_mode=True)
        configured = ContextCompressor(
            model="test", quiet_mode=True, proactive_prune_tokens=48_000
        )

    # Shipping default: opt-in, so off.
    assert default.proactive_prune_tokens == 0
    assert default.would_proactively_prune(400_000) is False
    assert _prune_would_have_run(default, 400_000) is False

    assert configured.would_proactively_prune(47_999) is False
    assert configured.would_proactively_prune(48_000) is True
    assert _prune_would_have_run(configured, 47_999) is False
    assert _prune_would_have_run(configured, 48_000) is True

    # An unknown token count cannot rule the prune out; the prune proceeds on None.
    assert configured.would_proactively_prune(None) is True


def test_prune_hookless_and_broken_predicate_shapes_are_handled():
    """No hook = no authority; a raising predicate reads as "would have run"
    (the call was withheld) rather than silencing the report."""
    from types import SimpleNamespace

    from agent.conversation_loop import _prune_would_have_run

    assert _prune_would_have_run(SimpleNamespace(), 400_000) is False

    class _BrokenPredicate:
        def would_proactively_prune(self, current_tokens=None):
            raise RuntimeError("engine blew up")

        def prune_tool_results_only(self, messages, current_tokens=None):
            return messages, 0

    assert _prune_would_have_run(_BrokenPredicate(), 400_000) is True


def test_engine_that_never_overrode_the_prune_hook_is_not_a_suppressed_authority():
    """The inherited ABC no-op is not an authority the gate withheld; the
    ``__func__`` identity check tells occupancy from inheritance."""
    from agent.conversation_loop import _prune_would_have_run

    compress_only, _turn_complete = _engine_stubs()

    class _InheritsTheDefault(compress_only):
        pass

    class _OccupiesTheHook(compress_only):
        def prune_tool_results_only(self, messages, current_tokens=None):
            return list(messages), 1

    assert _prune_would_have_run(_InheritsTheDefault(), 400_000) is False
    assert _prune_would_have_run(_OccupiesTheHook(), 400_000) is True
