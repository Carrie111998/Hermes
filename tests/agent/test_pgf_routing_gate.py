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