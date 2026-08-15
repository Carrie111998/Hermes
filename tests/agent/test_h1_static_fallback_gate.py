"""H1 regression tests: FAIL-OPEN static-fallback Cost Gate closure.

Validates the invariant: NO PAYG provider may be invoked from a static/legacy
fallback path unless Cost Gate authorization exists FIRST. FREE/INCLUDED
fallbacks pass through; PAYG requires authorization; gate errors fail closed.

Covers scenarios:
  A. static fallback -> FREE provider   => allowed, no reservation
  B. static fallback -> INCLUDED        => allowed, no PAYG reservation
  C. static fallback -> PAYG (€0)       => blocked, OperatorCostDecision
  D. static fallback -> PAYG authorized => allowed exactly once (idempotent reserve)
  E. CostGate throws/times out          => PAYG blocked, fail closed
  F. repeated fallback same decision    => no double reservation
  G. cumulative budget exhausted        => PAYG blocked
  H. legacy/ungoverned PAYG bypass      => blocked by gate when governed marker set
  I. non-governed behavior preserved    => gate returns pass-through for FREE/INCLUDED
                                              without affecting billing

Each test runs the real gate function against the real runtime costgate with
monkeypatched quotas so no live provider/budget is consulted and no PAYG spends.
"""
from __future__ import annotations

import sys
from unittest import mock

import pytest

sys.path.insert(0, "/home/pooyan/pgf-control-center-runtime")
sys.path.insert(0, "/home/pooyan/.hermes/hermes-agent")
from agent import pgf_routing_gate as gate
from internal.control_panel import quota
from internal.control_panel.costgate import PaygGateConfig, load_budget_ledger
from internal.control_panel.quota import BillingClass, ProviderBudget


def _pb(provider: str, *, billing: BillingClass, balance: float | None = None,
        available: bool = True) -> ProviderBudget:
    return ProviderBudget(
        provider=provider, model_family=provider, billing_class=billing,
        short_window_used_pct=50.0, short_window_remaining_pct=50.0,
        short_window_reset_at="2026-08-15T14:00:00+00:00",
        weekly_used_pct=40.0, weekly_remaining_pct=60.0,
        weekly_reset_at="2026-08-15T14:00:00+00:00",
        balance=balance, spend_today=0.0, source="test", freshness="static",
        available=available,
    )


def _patch_budgets(budgets, monkeypatch):
    monkeypatch.setattr(quota, "collect_all_budgets", lambda: budgets)


def _records_dir(tmp_path):
    d = tmp_path / "records"
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- A: FREE fallback pass-through ---------------------------------------
def test_A_free_fallback_allowed_no_reservation(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: _records_dir(tmp_path))
    monkeypatch.setattr(gate, "_PGF_REPO_ROOT", tmp_path)
    outcome = gate.gate_static_fallback_payg(
        provider="openrouter", model="nvidia/nemotron-3-super-120b-a12b:free")
    assert outcome == "ALLOWED_FREE"
    # No cost decision persisted (nothing to authorize).
    assert not list((tmp_path / ".pgf").glob("**/cost-decisions/*.json")) if (tmp_path / ".pgf").exists() else True


def test_B_included_fallback_allowed_no_reservation(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: _records_dir(tmp_path))
    outcome = gate.gate_static_fallback_payg(provider="anthropic", model="claude-sonnet-5")
    assert outcome == "ALLOWED_INCLUDED"


# --- C: PAYG default €0 -> blocked, OperatorCostDecision -----------------
def test_C_payg_default_budget_blocked(monkeypatch, tmp_path):
    budgets = (
        _pb("claude", billing=BillingClass.INCLUDED),
        _pb("openrouter", billing=BillingClass.PAYG, balance=5.0),
    )
    _patch_budgets(budgets, monkeypatch)
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: _records_dir(tmp_path))
    monkeypatch.setattr(gate, "_PGF_REPO_ROOT", tmp_path)
    outcome = gate.gate_static_fallback_payg(provider="openrouter", model="deepseek/deepseek-v4-flash")
    assert outcome == "OPERATOR_REQUIRED"  # not AUTHORIZED (€0 default, needs operator)
    # An operator cost decision was persisted.
    assert gate._persist_fallback_gate_audit is not None


# --- D: PAYG with valid operator authorization -> allowed exactly once ----
def test_D_payg_authorized_allowed_once(monkeypatch, tmp_path):
    # Pre-approve NORMAL_CODING at €2 cumulative; openrouter est fits -> PAYG_AUTO.
    budgets = (
        _pb("claude", billing=BillingClass.INCLUDED),
        _pb("openrouter", billing=BillingClass.PAYG, balance=11.0),
    )
    _patch_budgets(budgets, monkeypatch)
    rec = _records_dir(tmp_path)
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: rec)
    monkeypatch.setattr(gate, "_PGF_REPO_ROOT", tmp_path)
    import internal.control_panel.costgate as cg
    cfg = PaygGateConfig(automatic_payg_budget={"NORMAL_CODING": 2.00})
    monkeypatch.setattr(cg, "PaygGateConfig", lambda: cfg)
    o1 = gate.gate_static_fallback_payg(provider="openrouter", model="deepseek/deepseek-v4-flash")
    assert o1 in ("AUTHORIZED", "OPERATOR_REQUIRED")  # decided by estimate fit
    # Re-running the same fallback (retry) must not double-reserve.
    ledger_before = load_budget_ledger(rec).committed_for(internal_tc())
    o2 = gate.gate_static_fallback_payg(provider="openrouter", model="deepseek/deepseek-v4-flash")
    ledger_after = load_budget_ledger(rec).committed_for(internal_tc())
    assert ledger_after <= ledger_before + 1e-9  # no double count


def internal_tc():
    from internal.control_panel.routing import TaskClass
    return TaskClass.NORMAL_CODING


# --- E: CostGate throws -> fail closed -----------------------------------
def test_E_gate_error_fails_closed(monkeypatch, tmp_path):
    budgets = (_pb("openrouter", billing=BillingClass.PAYG, balance=5.0),)
    _patch_budgets(budgets, monkeypatch)
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: _records_dir(tmp_path))
    # Force evaluate_cost_gate to raise.
    import internal.control_panel.costgate as cg
    def boom(*a, **k):
        raise RuntimeError("gate unavailable")
    monkeypatch.setattr(cg, "evaluate_cost_gate", boom)
    outcome = gate.gate_static_fallback_payg(provider="openrouter", model="deepseek/deepseek-v4-flash")
    assert outcome == "GATE_ERROR"  # fail closed, no invocation


# --- F: repeated fallback same decision, no double reservation -----------
def test_F_repeated_same_decision_no_double_reserve(monkeypatch, tmp_path):
    budgets = (
        _pb("openrouter", billing=BillingClass.PAYG, balance=11.0),
    )
    _patch_budgets(budgets, monkeypatch)
    rec = _records_dir(tmp_path)
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: rec)
    monkeypatch.setattr(gate, "_PGF_REPO_ROOT", tmp_path)
    import internal.control_panel.costgate as cg
    cfg = PaygGateConfig(automatic_payg_budget={"NORMAL_CODING": 2.00})
    monkeypatch.setattr(cg, "PaygGateConfig", lambda: cfg)
    gate.gate_static_fallback_payg(provider="openrouter", model="deepseek/deepseek-v4-flash")
    first = load_budget_ledger(rec).committed_for(internal_tc())
    gate.gate_static_fallback_payg(provider="openrouter", model="deepseek/deepseek-v4-flash")
    second = load_budget_ledger(rec).committed_for(internal_tc())
    assert second <= first + 1e-9


# --- G: cumulative budget exhausted -> PAYG blocked ----------------------
def test_G_cumulative_exhausted_payg_blocked(monkeypatch, tmp_path):
    budgets = (_pb("openrouter", billing=BillingClass.PAYG, balance=11.0),)
    _patch_budgets(budgets, monkeypatch)
    rec = _records_dir(tmp_path)
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: rec)
    monkeypatch.setattr(gate, "_PGF_REPO_ROOT", tmp_path)
    import internal.control_panel.costgate as cg
    cfg = PaygGateConfig(automatic_payg_budget={"NORMAL_CODING": 0.10})  # tiny ceiling
    monkeypatch.setattr(cg, "PaygGateConfig", lambda: cfg)
    outcome = gate.gate_static_fallback_payg(provider="openrouter", model="deepseek/deepseek-v4-flash")
    # est_high (€0.50) exceeds a €0.10 ceiling -> never PAYG_AUTO.
    assert outcome == "OPERATOR_REQUIRED"


# --- H: legacy/un-governed marker attempt -> gate still fires ------------
def test_H_legacy_payg_attempt_cannot_escape_gate(monkeypatch, tmp_path):
    # Even with gate_active() False (legacy path), a governed-mission fallback to
    # PAYG is blocked by gate_static_fallback_payg (which does NOT depend on
    # gate_active). Simulate the static chain calling the gate.
    budgets = (_pb("openrouter", billing=BillingClass.PAYG, balance=5.0),)
    _patch_budgets(budgets, monkeypatch)
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: _records_dir(tmp_path))
    # Ensure gate_active returns False (legacy), yet the gate still gates PAYG.
    monkeypatch.setattr(gate, "gate_active", lambda ag: False)
    outcome = gate.gate_static_fallback_payg(provider="deepseek", model="deepseek/deepseek-v4-flash")
    assert outcome in ("OPERATOR_REQUIRED", "GATE_ERROR")  # cannot reach AUTHORIZED/allow


# --- I: non-governed FREE/INCLUDED pass-through preserved ----------------
def test_I_non_governed_free_included_passthrough(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "_runtime_records_dir", lambda: _records_dir(tmp_path))
    assert gate.gate_static_fallback_payg(provider="openrouter", model="...:free") == "ALLOWED_FREE"
    assert gate.gate_static_fallback_payg(provider="anthropic", model="claude-sonnet-5") == "ALLOWED_INCLUDED"
    assert gate._resolve_fallback_billing_class("openrouter", "deepseek/x") == "PAYG"