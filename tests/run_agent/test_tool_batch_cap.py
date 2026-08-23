"""Behavior-contract tests for the per-message tool-call batch cap (#93251).

An assistant turn can request an arbitrarily large parallel tool batch, and
when such a batch fails, EVERY result in it was lost ("Result unavailable"
for all N calls — #93251).  The fix denies overflow calls VISIBLY, per call,
at the single dispatch point: ``tools.max_tool_calls_per_batch`` caps how
many calls one assistant message may execute; the rest receive a denial
tool result (recoverable — re-issue next turn) instead of being executed
into whatever failure an oversized batch produces.

Contracts pinned here:

1. Every requested ``tool_call_id`` ends up with EXACTLY ONE result —
   admitted calls get real results, overflow calls get visible denials.
   This is the anti-silent-loss invariant itself.
2. Denied calls are NEVER dispatched — no side effects, no invocation.
3. The cap applies at the single dispatch point (sequential, concurrent,
   and segmented shapes all flow through it) AND as an entry backstop on
   the module-level executors that host code can call directly.
4. Denials are Hermes-authored runtime notices: plain text, never wrapped
   in the untrusted-data framing reserved for external tool output.
5. Config round-trip: ``tools.max_tool_calls_per_batch`` wins when valid;
   unset/invalid falls back to the default; values < 1 clamp to 1 (a cap
   of 0 would deny every call — misconfiguration, not feature).
6. Persistence fail-closed: if the incremental SessionDB flush of denial
   results reports failure, the turn stops rather than continuing from
   state that exists only in memory.

Mirrors the stub conventions of test_start_order_gate.py /
test_concurrent_interrupt.py.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)


CAP = 4


def _load_config_with(cap):
    return {"tools": {"max_tool_calls_per_batch": cap}}


def _make_agent(request=None):
    """Minimal AIAgent-like stub able to run the real executor paths."""
    import run_agent as _ra

    class _Stub:
        _interrupt_requested = False
        _interrupt_message = None
        _execution_thread_id = None
        _interrupt_thread_signal_pending = False
        log_prefix = ""
        quiet_mode = True
        verbose_logging = False
        log_prefix_chars = 200
        tool_progress_mode = "off"
        _checkpoint_mgr = MagicMock(enabled=False)
        tool_progress_callback = None
        tool_start_callback = None
        tool_complete_callback = None
        valid_tool_names = set()
        session_id = ""
        _current_turn_id = ""
        _current_api_request_id = ""
        _todo_store = MagicMock()
        _memory_store = MagicMock()
        _session_db = None
        enabled_toolsets = None
        disabled_toolsets = None
        _current_tool = None
        _print_fn = print

        def __init__(self):
            self.status_log = []
            self.flushed_snapshots = []
            self.dispatched_ids = []
            self._tool_worker_threads = set()
            self._tool_worker_threads_lock = __import__("threading").Lock()
            self._active_children = []
            self._active_children_lock = __import__("threading").Lock()

        def _emit_status(self, message):
            self.status_log.append(message)

        def _flush_messages_to_session_db(self, messages, conversation_history=None):
            self.flushed_snapshots.append(list(messages))
            return True

        def _touch_activity(self, desc):
            pass

        def _vprint(self, msg, force=False):
            pass

        def _safe_print(self, msg):
            pass

        def _should_emit_quiet_tool_messages(self):
            return False

        def _should_start_quiet_spinner(self):
            return False

        def _has_stream_consumers(self):
            return False

        def _tool_result_content_for_active_model(self, name, result):
            return result

        def _record_file_mutation_result(self, *a, **kw):
            pass

        def _apply_pending_steer_to_tool_results(self, *a, **kw):
            pass

        def _invoke_tool(self, name, args, task_id, call_id, **kw):
            # Concurrent-path dispatch funnel (tool_executor._execute).
            self.dispatched_ids.append(call_id)
            return json.dumps({"ok": name})

        def _record_sequential_dispatch(self, *args, **kwargs):
            # Sequential path funnel: run_agent.handle_function_call is the
            # module-level dispatcher the sequential executor routes through;
            # recording here keeps the dispatch ledger executor-agnostic.
            tool_call_id = kwargs.get("tool_call_id")
            self.dispatched_ids.append(tool_call_id)
            return json.dumps({"ok": "dispatched"})

    stub = _Stub()
    from agent.tool_executor import (
        execute_tool_calls_concurrent,
        execute_tool_calls_sequential,
    )

    stub._context_engine_tool_names = set()
    stub._memory_manager = None
    stub._subdirectory_hints = MagicMock()
    stub._subdirectory_hints.check_tool_call = lambda *a, **kw: None
    stub._append_guardrail_observation = lambda name, result, *a, **kw: result
    stub._guardrail_block_result = lambda d: json.dumps({"error": "blocked"})
    stub._tool_guardrails = MagicMock()
    stub._tool_guardrails.before_call = lambda name, args: MagicMock(allows_execution=True)
    # The sequential executor dispatches through the module-level funnel in
    # run_agent; route it at the stub so tests observe real dispatches.
    _hf_patch = patch(
        "run_agent.handle_function_call",
        side_effect=stub._record_sequential_dispatch,
    )
    _hf_patch.start()
    if request is not None:
        request.addfinalizer(_hf_patch.stop)
    stub._execute_tool_calls = _ra.AIAgent._execute_tool_calls.__get__(stub)
    stub._execute_tool_calls_concurrent = (
        lambda *a, **kw: execute_tool_calls_concurrent(stub, *a, **kw)
    )
    stub._execute_tool_calls_sequential = (
        lambda *a, **kw: execute_tool_calls_sequential(stub, *a, **kw)
    )
    return stub


def _tc(name="web_search", args=None, call_id=None):
    return SimpleNamespace(
        id=call_id or f"call_{name}_{id(object()):x}",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args or {})),
    )


def _oversized_batch(n=14):
    return [
        _tc("web_search", {"query": f"q{i}"}, call_id=f"ov_{i}")
        for i in range(n)
    ]


def _result_ids(messages):
    return [m.get("tool_call_id") for m in messages if m.get("role") == "tool"]


# ---------------------------------------------------------------------------
# Contract 1 + 2: every id answered exactly once; denied ids never dispatched
# ---------------------------------------------------------------------------

class TestOversizedBatchDeniesOverflow:
    def test_every_call_id_gets_exactly_one_result(self):
        agent = _make_agent()
        calls = _oversized_batch(14)
        assistant_message = SimpleNamespace(tool_calls=list(calls))

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(CAP),
        ):
            agent._execute_tool_calls(assistant_message, [], "task-cap")

        # The executors append onto the list passed in by the caller; the
        # denial flush snapshots capture the canonical transcript state.
        transcript = agent.flushed_snapshots[-1]
        ids = _result_ids(transcript)
        assert sorted(ids) == sorted(c.id for c in calls), (
            "silent loss regression: every requested tool_call_id must get "
            "exactly one result (real or visible denial)"
        )
        assert len(ids) == len(set(ids)), "a tool_call_id was answered twice"

    def test_overflow_calls_are_never_dispatched(self):
        agent = _make_agent()
        calls = _oversized_batch(14)
        assistant_message = SimpleNamespace(tool_calls=list(calls))

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(CAP),
        ):
            agent._execute_tool_calls(assistant_message, [], "task-cap")

        admitted = {c.id for c in calls[:CAP]}
        denied = {c.id for c in calls[CAP:]}
        assert set(agent.dispatched_ids) == admitted
        assert not (denied & set(agent.dispatched_ids)), (
            "denied calls must not execute — a denial is a visible refusal, "
            "not a silent drop and not an execution"
        )

    def test_denial_text_is_visible_actionable_and_unframed(self):
        agent = _make_agent()
        calls = _oversized_batch(6)
        assistant_message = SimpleNamespace(tool_calls=list(calls))

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(2),
        ):
            agent._execute_tool_calls(assistant_message, [], "task-cap")

        transcript = agent.flushed_snapshots[-1]
        denials = [
            m for m in transcript
            if m.get("role") == "tool" and m.get("tool_call_id") in {c.id for c in calls[2:]}
        ]
        assert len(denials) == 4
        for m in denials:
            text = m["content"]
            assert "NOT run" in text
            assert "tools.max_tool_calls_per_batch" in text
            assert "Re-issue" in text
            assert m.get("effect_disposition") == "none"
            # Hermes-authored notice, not external tool output: even a
            # web_search denial must not carry the injection-defense framing.
            assert "<untrusted_tool_result>" not in text

    def test_user_sees_a_status_per_denial(self):
        agent = _make_agent()
        assistant_message = SimpleNamespace(tool_calls=_oversized_batch(6))

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(CAP),
        ):
            agent._execute_tool_calls(assistant_message, [], "task-cap")

        assert len(agent.status_log) >= 2
        assert any("web_search" in s and "cap" in s.lower() for s in agent.status_log)


# ---------------------------------------------------------------------------
# Contract 3: the cap holds across every execution shape
# ---------------------------------------------------------------------------

class TestCapAcrossExecutionShapes:
    def test_under_limit_batch_runs_wholly_untouched(self):
        agent = _make_agent()
        calls = _oversized_batch(3)  # < CAP
        original_ids = [c.id for c in calls]
        assistant_message = SimpleNamespace(tool_calls=list(calls))

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(CAP),
        ):
            agent._execute_tool_calls(assistant_message, [], "task-under")

        assert [c.id for c in assistant_message.tool_calls] == original_ids
        assert set(agent.dispatched_ids) == set(original_ids)
        transcript = agent.flushed_snapshots[-1]
        assert all("denied" not in m["content"] for m in transcript)

    def test_single_call_path_still_works_at_cap_edge(self):
        agent = _make_agent()
        solo = [_tc("web_search", {}, call_id="solo_1")]
        assistant_message = SimpleNamespace(tool_calls=list(solo))

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(1),  # cap == batch size
        ):
            agent._execute_tool_calls(assistant_message, [], "task-solo")

        assert agent.dispatched_ids == ["solo_1"]
        transcript = agent.flushed_snapshots[-1]
        assert _result_ids(transcript) == ["solo_1"]

    def test_segmented_mixed_batch_keeps_admitted_emission_order(self):
        """Mixed safe/barrier batches truncate BEFORE segment planning, so the
        admitted prefix keeps the model's emission order across segments."""
        agent = _make_agent()
        calls = [
            _tc("web_search", {}, call_id="r1"),
            _tc("web_search", {}, call_id="r2"),
            _tc("read_file", {"path": "a.py"}, call_id="r3"),
            _tc("terminal", {"command": "echo hi"}, call_id="b1"),  # barrier
            _tc("web_search", {}, call_id="r4"),
            _tc("web_search", {}, call_id="r5"),
        ]

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(CAP),
        ):
            agent._execute_tool_calls(SimpleNamespace(tool_calls=calls), [], "task-mixed")

        # First four were admitted, the rest denied.
        assert set(agent.dispatched_ids) == {"r1", "r2", "r3", "b1"}
        # Denials land BEFORE the admitted results (same shape as the
        # invalid-tool-name path in conversation_loop): they are persisted
        # before any admitted call runs, so even a process-killing tool
        # (terminal restart) leaves denied ids answered in the store.
        transcript = agent.flushed_snapshots[-1]
        assert _result_ids(transcript) == ["r4", "r5", "r1", "r2", "r3", "b1"]


class TestDirectExecutorBackstop:
    def test_module_level_concurrent_executor_rejects_oversized_batch(self):
        """Host code can call the module-level executors directly, bypassing
        AIAgent._execute_tool_calls — the backstop must deny there too."""
        from agent.tool_executor import execute_tool_calls_concurrent

        agent = _make_agent()
        calls = _oversized_batch(9)
        assistant_message = SimpleNamespace(tool_calls=list(calls))
        messages: list = []

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(CAP),
        ):
            execute_tool_calls_concurrent(
                agent, assistant_message, messages, "task-backstop",
            )

        assert set(agent.dispatched_ids) == {c.id for c in calls[:CAP]}
        assert sorted(_result_ids(messages)) == sorted(c.id for c in calls)
        assert [c.id for c in assistant_message.tool_calls] == [
            c.id for c in calls[:CAP]
        ], "executor backstop must truncate so denied ids cannot be re-run"

    def test_module_level_sequential_executor_rejects_oversized_batch(self):
        from agent.tool_executor import execute_tool_calls_sequential

        agent = _make_agent()
        calls = _oversized_batch(7)
        assistant_message = SimpleNamespace(tool_calls=list(calls))
        messages: list = []

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(2),
        ):
            execute_tool_calls_sequential(
                agent, assistant_message, messages, "task-backstop-seq",
            )

        assert set(agent.dispatched_ids) == {c.id for c in calls[:2]}
        assert sorted(_result_ids(messages)) == sorted(c.id for c in calls)

    def test_concurrent_backstop_fails_closed_when_denial_flush_fails(self):
        """If denial persistence fails at the backstop, admitted calls must
        NOT run from in-memory-only state (same rule as the caller)."""
        from agent.tool_executor import execute_tool_calls_concurrent

        agent = _make_agent()
        agent._flush_messages_to_session_db = lambda *a, **kw: False
        calls = _oversized_batch(6)
        assistant_message = SimpleNamespace(tool_calls=list(calls))
        messages: list = []

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(CAP),
        ):
            execute_tool_calls_concurrent(
                agent, assistant_message, messages, "task-backstop-flush",
            )

        assert getattr(agent, "_incremental_persistence_failed") is True
        assert agent.dispatched_ids == [], (
            "no tool may run when the canonical store rejected the denial flush"
        )


# ---------------------------------------------------------------------------
# Contract 5: config resolution
# ---------------------------------------------------------------------------

class TestCapResolution:
    def test_unset_config_falls_back_to_default(self):
        from agent.tool_dispatch_helpers import (
            _DEFAULT_MAX_TOOL_CALLS_PER_BATCH,
            resolve_max_tool_calls_per_batch,
        )

        with patch("hermes_cli.config.load_config", return_value={}):
            assert resolve_max_tool_calls_per_batch() == _DEFAULT_MAX_TOOL_CALLS_PER_BATCH

    def test_valid_config_wins(self):
        from agent.tool_dispatch_helpers import resolve_max_tool_calls_per_batch

        with patch(
            "hermes_cli.config.load_config",
            return_value={"tools": {"max_tool_calls_per_batch": 2}},
        ):
            assert resolve_max_tool_calls_per_batch() == 2

    def test_invalid_config_falls_back_to_default(self):
        from agent.tool_dispatch_helpers import (
            _DEFAULT_MAX_TOOL_CALLS_PER_BATCH,
            resolve_max_tool_calls_per_batch,
        )

        with patch(
            "hermes_cli.config.load_config",
            return_value={"tools": {"max_tool_calls_per_batch": "lots"}},
        ):
            assert resolve_max_tool_calls_per_batch() == _DEFAULT_MAX_TOOL_CALLS_PER_BATCH

    def test_zero_or_negative_clamps_to_one(self):
        from agent.tool_dispatch_helpers import resolve_max_tool_calls_per_batch

        for bad in (0, -3):
            with patch(
                "hermes_cli.config.load_config",
                return_value={"tools": {"max_tool_calls_per_batch": bad}},
            ):
                assert resolve_max_tool_calls_per_batch() == 1


# ---------------------------------------------------------------------------
# Contract 6: persistence of denials is canonical, fail-closed
# ---------------------------------------------------------------------------

class TestDenialPersistence:
    def test_denials_are_flushed_before_any_execution(self):
        agent = _make_agent()
        assistant_message = SimpleNamespace(tool_calls=_oversized_batch(6))
        seen_sizes = []

        original_flush = agent._flush_messages_to_session_db

        def _recording_flush(messages, conversation_history=None):
            seen_sizes.append(len(_result_ids(messages)))
            return original_flush(messages, conversation_history)

        agent._flush_messages_to_session_db = _recording_flush

        with patch(
            "hermes_cli.config.load_config",
            return_value=_load_config_with(CAP),
        ):
            agent._execute_tool_calls(assistant_message, [], "task-flush")

        # The first flush snapshot must already contain all 2 denial results —
        # persisted BEFORE the admitted calls ran, so a crash mid-batch never
        # leaves denied ids unanswered in the canonical store.
        assert seen_sizes[0] == 2

    def test_flush_failure_sets_fail_closed_flag(self):
        from agent.tool_dispatch_helpers import append_denied_batch_results

        agent = SimpleNamespace(
            _emit_status=lambda m: None,
            _flush_messages_to_session_db=lambda *a, **kw: False,
        )
        messages: list = []
        denied = _oversized_batch(2)

        append_denied_batch_results(agent, messages, denied, limit=CAP)

        assert getattr(agent, "_incremental_persistence_failed") is True
        assert getattr(agent, "_last_persistence_error_cause") == "unknown"

    def test_flush_exception_classifies_and_flags(self):
        from agent.tool_dispatch_helpers import append_denied_batch_results

        def _boom(*a, **kw):
            raise RuntimeError("db locked")

        agent = SimpleNamespace(_emit_status=lambda m: None, _flush_messages_to_session_db=_boom)
        messages: list = []

        append_denied_batch_results(agent, messages, _oversized_batch(1), limit=CAP)

        assert getattr(agent, "_incremental_persistence_failed") is True
        cause = getattr(agent, "_last_persistence_error_cause")
        assert cause and cause != "unknown"


# ---------------------------------------------------------------------------
# Split primitive
# ---------------------------------------------------------------------------

class TestSplitBatchOverflow:
    def test_split_preserves_emission_order(self):
        from agent.tool_dispatch_helpers import split_batch_overflow

        calls = [SimpleNamespace(id=f"c{i}") for i in range(5)]
        admitted, denied = split_batch_overflow(calls, 3)
        assert [c.id for c in admitted] == ["c0", "c1", "c2"]
        assert [c.id for c in denied] == ["c3", "c4"]

    def test_split_none_and_empty_are_safe(self):
        from agent.tool_dispatch_helpers import split_batch_overflow

        assert split_batch_overflow(None, 4) == ([], [])
        assert split_batch_overflow([], 4) == ([], [])
