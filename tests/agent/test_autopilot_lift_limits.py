"""Tests for autopilot limit-lifting (opt-in) — main cap + subagent cap-lift.

Pins Feature 2:
  * when autopilot + lift_limits, keep_budget_ahead lifts the MAIN cap high;
  * parent_lifts_subagent_cap returns the lifted per-subagent cap (not the
    config default) when the parent is in autopilot with lift_limits;
  * with lift_limits OFF (default) behavior is exactly today's (no lift);
  * subagent_max_iterations pins a specific lifted value when set.

Also proves the delegate_task resolution honors the lift by exercising the same
helper delegate_tool calls, so the subagent cap resolves to the lifted value.
"""

from __future__ import annotations

import types

from agent.autopilot import driver


class FakeBudget:
    def __init__(self, max_total):
        self.max_total = max_total
        self.used = 0

    @property
    def remaining(self):
        return max(0, self.max_total - self.used)


def make_agent(**overrides):
    a = types.SimpleNamespace()
    a.autopilot_mode = True
    a.iteration_budget = FakeBudget(90)
    a.max_iterations = 90
    a._api_call_count = 5
    for k, v in overrides.items():
        setattr(a, k, v)
    return a


# --------------------------------------------------------------------------- #
# main cap lift via keep_budget_ahead                                          #
# --------------------------------------------------------------------------- #
def test_main_cap_topped_up_without_lift(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_LIFT_LIMITS", raising=False)
    a = make_agent(_autopilot_lift_limits=False, max_iterations=30,
                   iteration_budget=FakeBudget(30))
    driver.keep_budget_ahead(a, headroom=50)
    # Without lift: bounded top-up (current 5 + 50 = 55), NOT the huge cap.
    assert a.max_iterations == 55
    assert a.iteration_budget.max_total == 55


def test_main_cap_lifted_high_with_lift():
    a = make_agent(_autopilot_lift_limits=True)
    driver.keep_budget_ahead(a, headroom=50)
    # With lift: the main cap jumps to the effectively-unbounded value.
    assert a.max_iterations >= driver._LIFTED_ITER_CAP
    assert a.iteration_budget.max_total >= driver._LIFTED_ITER_CAP


def test_main_cap_lift_via_env(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_LIFT_LIMITS", "1")
    a = make_agent()  # no attr -> env decides
    driver.keep_budget_ahead(a, headroom=50)
    assert a.max_iterations >= driver._LIFTED_ITER_CAP


def test_main_cap_lift_respects_user_continuation_cap():
    # Explicit user continuation cap reached -> keep_budget_ahead is a no-op,
    # even with lift on (the run winds down naturally).
    a = make_agent(_autopilot_lift_limits=True,
                   _autopilot_max_continuations=3,
                   _autopilot_continuations=3)
    driver.keep_budget_ahead(a, headroom=50)
    assert a.max_iterations == 90  # untouched


def test_no_lift_when_not_autopilot():
    a = make_agent(_autopilot_lift_limits=True)
    a.autopilot_mode = False
    driver.keep_budget_ahead(a, headroom=50)
    assert a.max_iterations == 90  # untouched


# --------------------------------------------------------------------------- #
# subagent cap-lift resolution                                                 #
# --------------------------------------------------------------------------- #
def test_subagent_cap_not_lifted_by_default(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_LIFT_LIMITS", raising=False)
    a = make_agent(_autopilot_lift_limits=False)
    # Off -> None, so delegate_task keeps the config cap (today's behavior).
    assert driver.parent_lifts_subagent_cap(a) is None


def test_subagent_cap_lifted_high_when_zero():
    a = make_agent(_autopilot_lift_limits=True,
                   _autopilot_subagent_max_iterations=0)
    # 0 = inherit default but lifted -> the very high default.
    assert driver.parent_lifts_subagent_cap(a) == driver._LIFTED_ITER_CAP


def test_subagent_cap_pinned_when_set():
    a = make_agent(_autopilot_lift_limits=True,
                   _autopilot_subagent_max_iterations=1234)
    assert driver.parent_lifts_subagent_cap(a) == 1234


def test_subagent_cap_none_when_not_autopilot():
    a = make_agent(_autopilot_lift_limits=True)
    a.autopilot_mode = False
    assert driver.parent_lifts_subagent_cap(a) is None


def test_subagent_cap_none_for_none_parent():
    assert driver.parent_lifts_subagent_cap(None) is None


def test_subagent_cap_via_env(monkeypatch):
    monkeypatch.setenv("AUTOPILOT_LIFT_LIMITS", "1")
    monkeypatch.setenv("AUTOPILOT_SUBAGENT_MAX_ITERATIONS", "777")
    a = make_agent()  # attrs absent -> env decides
    assert driver.parent_lifts_subagent_cap(a) == 777


# --------------------------------------------------------------------------- #
# delegate_task cap resolution honors the lift (off = exactly the config cap)  #
# --------------------------------------------------------------------------- #
def _resolve_delegate_cap(parent_agent, config_default):
    """Mirror delegate_tool's resolution: start at the config default, then
    apply the autopilot lift exactly as delegate_task does."""
    effective = config_default
    lifted = driver.parent_lifts_subagent_cap(parent_agent)
    if lifted and lifted > effective:
        effective = lifted
    return effective


def test_delegate_resolution_off_equals_config_default(monkeypatch):
    monkeypatch.delenv("AUTOPILOT_LIFT_LIMITS", raising=False)
    a = make_agent(_autopilot_lift_limits=False)
    # Off: the resolution returns exactly the config default (e.g. 250 or 50).
    assert _resolve_delegate_cap(a, 250) == 250
    assert _resolve_delegate_cap(a, 50) == 50


def test_delegate_resolution_lifts_above_config_default():
    a = make_agent(_autopilot_lift_limits=True,
                   _autopilot_subagent_max_iterations=0)
    # On: resolution returns the lifted value, not the 50/250 default.
    assert _resolve_delegate_cap(a, 250) == driver._LIFTED_ITER_CAP
    assert _resolve_delegate_cap(a, 50) == driver._LIFTED_ITER_CAP


def test_delegate_resolution_keeps_larger_config_default():
    # A pinned lift smaller than an already-large config default never shrinks it.
    a = make_agent(_autopilot_lift_limits=True,
                   _autopilot_subagent_max_iterations=100)
    assert _resolve_delegate_cap(a, 250) == 250  # config default already larger
    assert _resolve_delegate_cap(a, 50) == 100   # lift raises 50 -> 100
