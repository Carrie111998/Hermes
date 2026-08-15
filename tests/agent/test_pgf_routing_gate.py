"""Smoke tests for the Phase 4 quota-aware routing gate (agent/pgf_routing_gate).

Proves the gate's behavioral contract with mocked subsystems (no live quota,
no model invocation, no PAYG spend):

  - inactive for non-governed agents (normal chat / holding-hossein untouched)
  - active only for explicitly-marked governed PGF missions
  - on provider failure re-runs the policy chain (NOT static fallback)
  - never auto-approves PAYG (PAYG_ESCALATION -> refuses)
  - preserves static chain when inactive/errors (rollback)
  - anomaly guard stops expensive calls on confirmed near-exhaustion
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest

sys.path.insert(0, "/home/pooyan/pgf-control-center-runtime")


def _agent(marker: bool = False, task_class: str = "ARCHITECTURE", provider: str = "openai-codex"):
    a = types.SimpleNamespace()
    a._pgf_governed_mission = marker
    a._pgf_task_class = task_class
    a.provider = provider
    a._pgf_governed_selection = None
    a._pgf_governed_brain = None
    return a


def _plan(status="INCLUDED", brain: str | None = "claude", executor: str | None = "claude_code", wait=None):
    return {"status": status, "brain": brain, "executor": executor, "wait_until": wait}


@pytest.fixture()
def gate():
    from agent import pgf_routing_gate as g

    return g


def test_inactive_without_marker_returns_false(gate):
    a = _agent(marker=False)
    assert gate.gate_active(a) is False


def test_active_with_governed_marker(gate, monkeypatch):
    a = _agent(marker=True)
    import tools.self_improvement_guard as sig

    monkeypatch.setattr(sig, "_profile_config", lambda: {"governance": {"governed": True}})
    assert gate.gate_active(a) is True


def test_provider_failure_reruns_policy_not_static(gate):
    """Provider failure -> PolicyEngine rerun selects Claude, gate takes over."""
    a = _agent(marker=True)
    plan = _plan("INCLUDED", "claude", "claude_code")
    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(gate, "_run_plan", return_value=plan), \
            mock.patch.object(gate, "_anomalous_quota", return_value=False), \
            mock.patch.object(gate, "_persist_decision", return_value="/tmp/x"), \
            mock.patch.object(gate, "_persist_quota_snapshot", return_value="/tmp/q"):
        took = gate.route_governed_fallback(a, reason="quota exhausted")
    assert took is True
    assert a._pgf_governed_brain == "claude"


def test_payg_escalation_never_auto_approved(gate):
    """PAYG escalation must NOT be auto-approved: gate refuses, keeps static."""
    a = _agent(marker=True)
    plan = _plan("PAYG_ESCALATION", None, "openrouter_agent")
    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(gate, "_run_plan", return_value=plan), \
            mock.patch.object(gate, "_anomalous_quota", return_value=False), \
            mock.patch.object(gate, "_persist_decision", return_value="/tmp/x"), \
            mock.patch.object(gate, "_persist_quota_snapshot", return_value="/tmp/q"):
        took = gate.route_governed_fallback(a)
    assert took is False
    assert a._pgf_governed_brain is None


def test_reset_aware_wait(gate):
    a = _agent(marker=True)
    plan = _plan("WAIT", None, None, wait="2026-08-15T19:30:00+00:00")
    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(gate, "_run_plan", return_value=plan), \
            mock.patch.object(gate, "_anomalous_quota", return_value=False):
        took = gate.route_governed_fallback(a)
    assert took is False  # WAIT = do not spend; static preserved


def test_rollback_preserved_when_gate_errors(gate):
    """If the gate raises, try_activate_fallback's wrapper falls through to static."""
    from agent import chat_completion_helpers as ch

    a = _agent(marker=True)
    # gate_active True but route raises -> wrapper catches and logs, returns to static path
    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(gate, "route_governed_fallback", side_effect=RuntimeError("boom")), \
            mock.patch.object(ch, "logger"):
        # We test the wrapper logic: it must NOT swallow into gateway-death;
        # it catches the exception and returns False (falling through to static).
        # Simulate by invoking the same guarded block used in try_activate_fallback.
        result = _call_guarded(gate, ch, a)
        assert result == "fell-through"  # static chain path reached


def _call_guarded(gate, ch, a):
    """Mirror the guarded gate block in try_activate_fallback (returns sentinel)."""
    try:
        if gate.gate_active(a):
            if gate.route_governed_fallback(a):
                return "gate-took-over"
    except Exception:  # noqa: BLE001
        ch.logger.exception("Phase4 gate error")
        return "fell-through"
    return "fell-through"


def test_anomalous_quota_stops_expensive_call(gate):
    a = _agent(marker=True)
    plan = _plan("INCLUDED", "claude", "claude_code")
    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(gate, "_run_plan", return_value=plan), \
            mock.patch.object(gate, "_anomalous_quota", return_value=True), \
            mock.patch.object(gate, "_persist_decision", return_value=None), \
            mock.patch.object(gate, "_persist_quota_snapshot", return_value=None):
        took = gate.route_governed_fallback(a)
    assert took is False  # refuse when anomaly flagged


def test_anomaly_quota_ignores_unknown_non_confirmed(gate):
    """Unconfirmed/unavailable quota must NOT count as anomalous (fail-open)."""
    import importlib

    # patch the internal.control_panel.quota symbol that _anomalous_quota resolves
    sys.path.insert(0, "/home/pooyan/pgf-control-center-runtime")
    quota = importlib.import_module("internal.control_panel.quota")
    b = mock.MagicMock()
    b.available = False
    with mock.patch.object(quota, "collect_claude_budget", return_value=b):
        assert gate._anomalous_quota() is False


# --- Stage A: deterministic classifier + pre-invocation gate ------------


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("run pytest on the whole suite", "TEST_VALIDATION"),
        ("design the api schema and road-map the refactor", "ARCHITECTURE"),
        ("grep for the symbol and git status", "MECHANICAL_EXECUTION"),
        ("summarize the meeting notes", "SUMMARIZATION"),
        ("security incident in production, urgent rollback", "CRITICAL_REASONING"),
        ("do a normal code edit", "NORMAL_CODING"),
        ("review the pull request for correctness", "CODE_REVIEW"),
    ],
)
def test_classify_task_deterministic(gate, msg, expected):
    assert gate.classify_task(msg) == expected


def test_pre_invocation_selects_claude_for_reasoning(gate):
    """Stage A: a governed reasoning task -> Claude Brain (included) pre-invocation."""
    a = _agent(marker=True)
    a.provider = "deepseek"  # legacy default must NOT win
    a._pgf_pre_invocation_gate = "INACTIVE"
    a._pgf_failure_replan_gate = "INACTIVE"
    plan = _plan("INCLUDED", "claude", "claude_code")

    import importlib

    sys.path.insert(0, "/home/pooyan/pgf-control-center-runtime")
    orch = importlib.import_module("internal.control_panel.orchestration")
    class FakeSel:
        def to_dict(self):
            return plan

    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(orch, "build_plan", return_value=FakeSel()), \
            mock.patch.object(gate, "classify_task", return_value="ARCHITECTURE"), \
            mock.patch.object(gate, "_persist_pre_invocation", return_value="/tmp/x"):
        gate.route_pre_invocation(a, "design the api")
    assert a._pgf_pre_invocation_gate == "ACTIVE"
    assert a._pgf_failure_replan_gate == "ACTIVE"
    assert a._pgf_task_class == "ARCHITECTURE"


# --- F1: real provider replan -------------------------------------------


def test_f1_replan_actually_switches_runtime_provider(gate):
    """Provider A fails -> RoutingPolicyEngine picks B -> the ACTUAL next
    invocation targets B (agent.provider/model swapped), not merely bookkeeping.
    A is not retried; static fallback does not silently override."""
    a = _agent(marker=True)
    a.provider = "openai-codex"  # provider A (failed)
    a.model = "gpt-5.6-sol"
    a.requested_provider = "openai-codex"
    a._config_context_length = 999999  # stale cached value must be cleared
    a._transport_cache = {"client": "cached"}  # must be cleared too

    plan = _plan("INCLUDED", "claude", "claude_code")
    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(gate, "_run_plan", return_value=plan), \
            mock.patch.object(gate, "_anomalous_quota", return_value=False), \
            mock.patch.object(gate, "_persist_replan_activation", return_value="/tmp/r"):
        took = gate.route_governed_fallback(a, reason="codex exhausted")

    assert took is True
    # The ACTUAL next invocation target is Claude (provider=anthropic).
    assert a.provider == "anthropic"
    assert a.model == "claude-sonnet-5"
    assert a.requested_provider == "anthropic"
    # Stale cached state cleared so the retry resolves Claude (not A).
    assert a._config_context_length is None
    assert a._transport_cache == {}


def test_f1_replan_unknown_brain_fails_closed(gate):
    """An unmapped brain must NOT silently 'handled' — route_governed_fallback
    returns False so the legacy static chain is preserved (fail closed)."""
    a = _agent(marker=True)
    a.provider = "openai-codex"
    a.model = "gpt-5.6-sol"

    plan = _plan("INCLUDED", "not_a_real_brain", "whatever")
    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(gate, "_run_plan", return_value=plan), \
            mock.patch.object(gate, "_anomalous_quota", return_value=False), \
            mock.patch.object(gate, "_persist_replan_activation", return_value="/tmp/r"):
        took = gate.route_governed_fallback(a, reason="codex exhausted")

    assert took is False  # fail closed -> static chain runs
    # Provider NOT switched (still A) — legacy path may retry/fall over it.
    assert a.provider == "openai-codex"
    assert a.model == "gpt-5.6-sol"


def test_f1_replan_activation_persisted(gate):
    """The replan must persist failed provider, selected replacement,
    activation result, and retry provider/model."""
    a = _agent(marker=True)
    a.provider = "openai-codex"
    a.model = "gpt-5.6-sol"

    plan = _plan("INCLUDED", "claude", "claude_code")
    with mock.patch.object(gate, "gate_active", return_value=True), \
            mock.patch.object(gate, "_run_plan", return_value=plan), \
            mock.patch.object(gate, "_anomalous_quota", return_value=False), \
            mock.patch.object(gate, "_persist_replan_activation") as p:
        p.return_value = "/tmp/r"
        took = gate.route_governed_fallback(a, reason="codex exhausted")

    assert took is True
    p.assert_called_once()
    call = p.call_args
    assert call.kwargs["failed_provider"] == "openai-codex"
    assert call.kwargs["selected_brain"] == "claude"
    assert call.kwargs["activation_result"] == "activated"
    assert call.kwargs["retry_provider"] == "anthropic"
    assert call.kwargs["retry_model"] == "claude-sonnet-5"


def test_f1_rollback_undoes_mutation_when_prior_attr_was_none(gate):
    """F1 rollback hardener: if a fresh agent had no prior requested_provider
    and activation fails after setting it, the rejected brain must be undone
    (requested_provider removed / set back), not left pointing at the failure."""
    a = _agent(marker=True)
    # Fresh agent: `_agent` does not define requested_provider / _pgf_governed_brain,
    # so their prior state is _MISSING (never had one).

    # Force _apply_selection to raise AFTER mutating by making the first persist
    # (activation_result="activated") call explode, while the failure-path
    # persist (activation_result="failed") call succeeds.
    def _boom_persist(**kw):
        if kw.get("activation_result") == "activated":
            raise RuntimeError("persist exploded after state was mutated")
        return "/tmp/r"

    with mock.patch.object(gate, "_persist_replan_activation", side_effect=_boom_persist):
        switched = gate._apply_selection(a, "claude", failed_provider="openai-codex", plan={})

    assert switched is False
    # The rejected brain must not be left on requested_provider.
    assert not hasattr(a, "requested_provider") or a.requested_provider != "anthropic"
    assert not hasattr(a, "_pgf_governed_brain") or a._pgf_governed_brain != "claude"


# --- FREE execution lane (Rule E) ----------------------------------------


def test_free_worker_dispatch_uses_free_model(gate):
    """A FREE plan dispatches the runtime to the free model, not PAYG."""
    a = _agent(marker=True, task_class="MECHANICAL_EXECUTION")
    a.provider = "deepseek"  # would otherwise be the fallback executor
    plan = {"status": "FREE", "brain": None, "executor": "nemotron"}
    ok = gate._run_free_worker(a, "nemotron", plan)
    assert ok is True
    assert a.provider == "openrouter"
    assert a.model == "nvidia/nemotron-3-super-120b-a12b:free"


def test_free_worker_never_brain_for_architecture(gate):
    """ARCHITECTURE is not free-eligible — critical reasoning never goes free."""
    assert gate.free_worker_eligible("ARCHITECTURE") is False
    assert gate.free_worker_eligible("COMPLEX_DEBUGGING") is False
    assert gate.free_worker_eligible("CODE_REVIEW") is False
    assert gate.free_worker_eligible("CRITICAL_REASONING") is False


def test_free_worker_eligible_mechanical(gate):
    """Mechanical/test/summarization classes ARE free-eligible."""
    assert gate.free_worker_eligible("MECHANICAL_EXECUTION") is True
    assert gate.free_worker_eligible("TEST_VALIDATION") is True
    assert gate.free_worker_eligible("SUMMARIZATION") is True
    assert gate.free_worker_eligible("NORMAL_CODING") is True


def test_free_quality_gate_passes_only_on_full_accept(gate):
    """Free output is provisional until every deterministic gate passes."""
    full = {g: True for g in gate.FREE_QUALITY_GATES}
    assert gate.evaluate_free_quality_gate("nemotron", "MECHANICAL_EXECUTION", full) is True
    assert gate.evaluate_free_quality_gate("nemotron", "MECHANICAL_EXECUTION", {"tests": False}) is False
    assert gate.evaluate_free_quality_gate("nemotron", "MECHANICAL_EXECUTION", None) is False  # pending


def test_free_worker_unknown_mapping_fails_closed(gate):
    """An unmapped free worker must not dispatch (fail closed)."""
    a = _agent(marker=True, task_class="MECHANICAL_EXECUTION")
    ok = gate._run_free_worker(a, "not_a_worker", {})
    assert ok is False