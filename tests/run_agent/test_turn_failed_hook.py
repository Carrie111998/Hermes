"""Tests for the ``turn_failed`` plugin hook emitted by ``finalize_turn``.

Phase-0 agent-observability (T1): the turn finalizer resolves a 13-value
``_turn_exit_reason`` and logs a "Turn ended: reason=..." diagnostic, but there
was no programmatic hook for downstream observability. This adds ``turn_failed``,
fired ONLY for non-clean turn exits:

  * any error / exhaustion / guardrail reason, OR
  * ``last_msg_role == "tool"`` (the agent stopped mid-work — the
    ``protocol_violation`` / ``breads-pc`` premature-stop class).

A healthy ``text_response(finish_reason=stop)`` exit with
``last_msg_role != "tool"`` must NOT fire it, and neither must a deliberate
user ``/stop`` (``interrupted`` True) — that is a clean exit, not a failure.

The classification lives in the pure guard ``_should_emit_turn_failed`` so it is
deterministic and testable without a full agent. A small integration-style test
drives the real ``finalize_turn`` path with a mocked ``invoke_hook`` to confirm
the emit wiring + kwargs.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import patch

import pytest


sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

from hermes_cli.plugins import VALID_HOOKS
from agent.turn_finalizer import _should_emit_turn_failed, finalize_turn


# --------------------------------------------------------------------------- #
# Hook registration
# --------------------------------------------------------------------------- #
def test_turn_failed_in_valid_hooks():
    assert "turn_failed" in VALID_HOOKS


# --------------------------------------------------------------------------- #
# Pure guard: _should_emit_turn_failed(reason, last_msg_role, interrupted)
# --------------------------------------------------------------------------- #
def test_guard_fires_on_protocol_violation_shaped_premature_stop():
    # Worker stopped mid-work: last message is a tool result. This is the
    # protocol_violation class — fire regardless of reason text.
    assert (
        _should_emit_turn_failed("text_response(finish_reason=stop)", "tool", False)
        is True
    )


def test_guard_fires_on_breads_pc_premature_text_response_with_pending_tool():
    # breads-pc: premature text_response while a tool call is still pending,
    # i.e. last_msg_role == "tool".
    assert (
        _should_emit_turn_failed(
            "text_response(finish_reason=tool_calls)", "tool", False
        )
        is True
    )


def test_guard_fires_on_error_and_exhaustion_reasons():
    for reason in (
        "max_iterations_reached(50/50)",
        "empty_response_exhausted",
        "api_request_error",
        "guardrail_triggered",
    ):
        assert _should_emit_turn_failed(reason, "assistant", False) is True, reason


def test_guard_does_not_fire_on_healthy_completion():
    # Healthy: text_response(...) AND last message is NOT a tool result.
    assert (
        _should_emit_turn_failed(
            "text_response(finish_reason=stop)", "assistant", False
        )
        is False
    )
    assert (
        _should_emit_turn_failed("text_response(finish_reason=stop)", None, False)
        is False
    )


def test_guard_does_not_fire_on_user_interrupt():
    # A deliberate user /stop is a clean exit, not a failure. The interrupt
    # flag suppresses the hook regardless of reason text or last_msg_role —
    # including the interrupted_by_user reason (not a text_response(...)) and a
    # mid-tool stop, both of which would otherwise trip the reason/tool arms.
    for reason, role in (
        ("interrupted_by_user", "assistant"),
        ("interrupted_by_user", "tool"),
        ("text_response(finish_reason=stop)", "tool"),
        ("api_request_error", "assistant"),
    ):
        assert (
            _should_emit_turn_failed(reason, role, True) is False
        ), (reason, role)


# --------------------------------------------------------------------------- #
# Integration: real finalize_turn path with a mocked invoke_hook
# --------------------------------------------------------------------------- #
def _make_agent():
    budget = SimpleNamespace(remaining=10, used=3, max_total=50)
    return SimpleNamespace(
        model="anthropic/claude-x",
        provider="anthropic",
        base_url="https://example.test",
        session_id="sess-123",
        max_iterations=50,
        iteration_budget=budget,
        quiet_mode=True,
        platform="cli",
        # Token / cost accounting referenced when assembling the result dict.
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status="ok",
        session_cost_source="estimate",
        context_compressor=SimpleNamespace(last_prompt_tokens=0),
        _tool_guardrail_halt_decision=None,
        _response_was_previewed=False,
        _interrupt_message=None,
        _stream_callback=None,
        _skill_nudge_interval=0,
        _iters_since_skill=0,
        valid_tool_names=set(),
        # finalize_turn calls these — stub them as no-ops.
        _emit_status=lambda *a, **k: None,
        _safe_print=lambda *a, **k: None,
        _save_trajectory=lambda *a, **k: None,
        _cleanup_task_resources=lambda *a, **k: None,
        _drop_trailing_empty_response_scaffolding=lambda *a, **k: None,
        _persist_session=lambda *a, **k: None,
        _file_mutation_verifier_enabled=lambda: False,
        _turn_completion_explainer_enabled=lambda: False,
        _turn_failed_file_mutations={},
        _drain_pending_steer=lambda *a, **k: None,
        clear_interrupt=lambda *a, **k: None,
        _sync_external_memory_for_turn=lambda *a, **k: None,
        _spawn_background_review=lambda *a, **k: None,
    )


def _capture_turn_failed_calls(monkeypatch_hook):
    """Patch invoke_hook so we record turn_failed kwargs; other hooks no-op."""
    calls = []

    def _fake_invoke_hook(name, **kwargs):
        if name == "turn_failed":
            calls.append(kwargs)
        return []

    return calls, _fake_invoke_hook


def test_finalize_fires_turn_failed_on_pending_tool_premature_stop():
    agent = _make_agent()
    messages = [
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "kanban_complete"}}],
        },
        {"role": "tool", "content": "ok"},
    ]
    calls, fake = _capture_turn_failed_calls(None)
    with patch("hermes_cli.plugins.invoke_hook", side_effect=fake):
        finalize_turn(
            agent,
            final_response="partial",
            api_call_count=5,
            interrupted=False,
            failed=False,
            messages=messages,
            conversation_history=[],
            effective_task_id="task-9",
            turn_id="turn-1",
            user_message="do the thing",
            original_user_message="do the thing",
            _should_review_memory=False,
            _turn_exit_reason="text_response(finish_reason=stop)",
        )
    assert len(calls) == 1, "turn_failed should fire exactly once on pending-tool stop"
    kw = calls[0]
    assert kw["reason"] == "text_response(finish_reason=stop)"
    assert kw["last_msg_role"] == "tool"
    assert kw["model"] == "anthropic/claude-x"
    assert kw["session_id"] == "sess-123"
    assert kw["api_calls"] == 5
    assert kw["response_len"] == len("partial")
    assert kw["turn_id"] == "turn-1"
    assert kw["interrupted"] is False
    assert "tool_turns" in kw


def test_finalize_does_not_fire_turn_failed_on_user_interrupt_mid_tool():
    # A user /stop while a tool result is pending: finalize appends a synthetic
    # assistant close (so last_msg_role flips to "assistant"), and the interrupt
    # flag suppresses the hook regardless. This is the clean-stop path that must
    # NOT surface as a failure signal.
    agent = _make_agent()
    messages = [
        {"role": "user", "content": "do the thing"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "kanban_complete"}}],
        },
        {"role": "tool", "content": "ok"},
    ]
    calls, fake = _capture_turn_failed_calls(None)
    with patch("hermes_cli.plugins.invoke_hook", side_effect=fake):
        finalize_turn(
            agent,
            final_response="partial",
            api_call_count=5,
            interrupted=True,
            failed=False,
            messages=messages,
            conversation_history=[],
            effective_task_id="task-9",
            turn_id="turn-3",
            user_message="do the thing",
            original_user_message="do the thing",
            _should_review_memory=False,
            _turn_exit_reason="interrupted_by_user",
        )
    assert calls == [], "turn_failed must NOT fire on a deliberate user interrupt"


def test_finalize_does_not_fire_turn_failed_on_healthy_completion():
    agent = _make_agent()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Done."},
    ]
    calls, fake = _capture_turn_failed_calls(None)
    with patch("hermes_cli.plugins.invoke_hook", side_effect=fake):
        finalize_turn(
            agent,
            final_response="Done.",
            api_call_count=2,
            interrupted=False,
            failed=False,
            messages=messages,
            conversation_history=[],
            effective_task_id="task-9",
            turn_id="turn-2",
            user_message="hi",
            original_user_message="hi",
            _should_review_memory=False,
            _turn_exit_reason="text_response(finish_reason=stop)",
        )
    assert calls == [], "turn_failed must NOT fire on a healthy completed turn"


# --------------------------------------------------------------------------- #
# Bypass exits: paths that return straight out of run_conversation and never
# reach finalize_turn (the gap the finalizer-only hook could not observe).
# --------------------------------------------------------------------------- #
def _unfinalized(result, agent=None, **kw):
    """Run the sweep with invoke_hook captured; return the recorded calls."""
    from agent import turn_finalizer

    calls, fake = _capture_turn_failed_calls(None)
    agent = agent if agent is not None else _make_agent()
    agent._turn_failed_emitted = False
    with patch("hermes_cli.plugins.invoke_hook", fake):
        turn_finalizer.emit_turn_failed_for_unfinalized_exit(agent, result, **kw)
    return calls


def test_bypass_sweep_fires_on_invalid_response_exhaustion():
    """The reviewer's named path: invalid-response terminal return.

    Dict shape mirrors the real terminal return in
    ``agent/conversation_loop.py`` for "Invalid API response after N retries",
    which persists the session and returns directly — never reaching
    ``finalize_turn``.
    """
    msg = "Invalid API response after 5 retries: empty content"
    calls = _unfinalized(
        {
            "final_response": msg,
            "messages": [{"role": "assistant", "content": ""}],
            "completed": False,
            "api_calls": 5,
            "error": msg,
            "failed": True,
        },
        turn_id="task-abc",
    )
    assert len(calls) == 1, "invalid-response exhaustion must emit turn_failed"
    assert msg in calls[0]["reason"]
    assert calls[0]["interrupted"] is False
    assert calls[0]["api_calls"] == 5
    assert calls[0]["turn_id"] == "task-abc"


def test_bypass_sweep_fires_on_context_overflow_without_failed_key():
    """Second named path, and the reason ``completed`` is the predicate.

    Context-overflow / compaction-disabled returns set ``completed: False`` but
    NOT ``failed``. Keying on ``failed`` would silently miss them — 11 of the
    terminal returns have no ``failed`` key at all.
    """
    calls = _unfinalized(
        {
            "final_response": "Context length exceeded: max compression reached",
            "messages": [{"role": "tool", "content": "..."}],
            "completed": False,
            "api_calls": 2,
        }
    )
    assert len(calls) == 1
    assert "Context length exceeded" in calls[0]["reason"]
    assert calls[0]["last_msg_role"] == "tool"


def test_bypass_sweep_silent_on_user_interrupt():
    """A user /stop is a clean exit — matches the _should_emit_turn_failed gate."""
    assert (
        _unfinalized(
            {
                "final_response": "Interrupted by user",
                "messages": [],
                "completed": False,
                "interrupted": True,
            }
        )
        == []
    )


def test_bypass_sweep_silent_on_healthy_completion():
    assert (
        _unfinalized(
            {
                "final_response": "here you go",
                "messages": [{"role": "assistant", "content": "here you go"}],
                "completed": True,
                "api_calls": 1,
            }
        )
        == []
    )


@pytest.mark.parametrize("result", [None, "not-a-dict", {}, {"completed": None}])
def test_bypass_sweep_silent_on_non_terminal_shapes(result):
    """Never emit on a shape the sweep cannot positively identify as failed."""
    assert _unfinalized(result) == []


def test_bypass_sweep_does_not_double_fire_after_finalize_turn():
    """The per-turn latch is what makes the sweep safe to run unconditionally."""
    from agent import turn_finalizer

    agent = _make_agent()
    agent._turn_failed_emitted = False
    calls, fake = _capture_turn_failed_calls(None)
    with patch("hermes_cli.plugins.invoke_hook", fake):
        # Stand in for finalize_turn having already emitted this turn.
        assert turn_finalizer.emit_turn_failed(agent, reason="api_error") is True
        # The sweep then sees an already-emitted turn and stays quiet.
        assert (
            turn_finalizer.emit_turn_failed_for_unfinalized_exit(
                agent, {"completed": False, "final_response": "x", "messages": []}
            )
            is False
        )
    assert len(calls) == 1, "hook must fire at most once per turn"


def test_emit_turn_failed_survives_a_raising_observer():
    """A broken plugin must not break turn teardown."""
    from agent import turn_finalizer

    agent = _make_agent()
    agent._turn_failed_emitted = False

    def _boom(name, **kwargs):
        raise RuntimeError("observer exploded")

    with patch("hermes_cli.plugins.invoke_hook", _boom):
        assert turn_finalizer.emit_turn_failed(agent, reason="api_error") is True
    # Latch still set, so a later sweep does not retry a broken observer.
    assert agent._turn_failed_emitted is True


def test_every_terminal_return_in_run_conversation_sets_completed():
    """Source invariant the sweep depends on.

    The sweep identifies a bypass exit by ``completed: False`` rather than by
    instrumenting each site, so a future terminal return that omits
    ``completed`` would be invisible. Assert the contract at the source: any
    ``return {...}`` carrying ``final_response`` also carries ``completed``.
    """
    import ast
    from pathlib import Path

    src = Path("agent/conversation_loop.py")
    if not src.exists():  # pragma: no cover - layout guard
        pytest.skip("conversation_loop.py not at the expected path")

    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == "run_conversation"
        ),
        None,
    )
    assert fn is not None, "run_conversation not found"

    offenders = []
    checked = 0
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)):
            continue
        keys = {k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if "final_response" not in keys:
            continue
        checked += 1
        if "completed" not in keys:
            offenders.append(node.lineno)

    assert checked > 0, "expected to find terminal result returns"
    assert not offenders, (
        "terminal return(s) carry final_response without completed, so the "
        f"turn_failed bypass sweep cannot see them: lines {offenders}"
    )
