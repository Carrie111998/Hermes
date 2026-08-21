"""Tests for the per-session cumulative token budget guard (#91713)."""

from types import SimpleNamespace

import pytest

from agent import session_budget as sb


def _agent(total=0, aux=0, cap=None, action="abort", calls=0, warned=False):
    return SimpleNamespace(
        session_total_tokens=total,
        session_aux_tokens_for_budget=aux,
        session_budget_tokens=cap,
        session_budget_action=action,
        session_api_calls=calls,
        _session_budget_warned=warned,
    )


# ── normalize_budget_tokens ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        (0, None),          # 0 = unlimited
        (-5, None),         # negative = unlimited
        (True, None),       # bool rejected (YAML `true`)
        (False, None),
        ("nope", None),
        (5_000_000, 5_000_000),
        ("5000000", 5_000_000),
        (100.0, 100),
    ],
)
def test_normalize_budget_tokens(value, expected):
    assert sb.normalize_budget_tokens(value) == expected


# ── normalize_budget_action ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "abort"),
        ("abort", "abort"),
        ("warn", "warn"),
        ("WARN", "warn"),
        (" warn ", "warn"),
        ("garbage", "abort"),   # unknown → safe default (abort)
        (123, "abort"),
    ],
)
def test_normalize_budget_action(value, expected):
    assert sb.normalize_budget_action(value) == expected


# ── used / remaining / exceeded ────────────────────────────────────────────

def test_used_includes_aux_forks():
    # The whole point of the aux side-counter: aux spend counts toward the cap
    # even though it is not part of session_total_tokens.
    assert sb.budget_used_tokens(_agent(total=1000, aux=500)) == 1500


def test_remaining_none_when_unlimited():
    assert sb.budget_remaining_tokens(_agent(total=999, cap=None)) is None
    assert sb.budget_remaining_tokens(_agent(total=999, cap=0)) is None


def test_remaining_counts_down_and_floors_at_zero():
    assert sb.budget_remaining_tokens(_agent(total=400, aux=100, cap=1000)) == 500
    # Over budget floors at 0, never negative.
    assert sb.budget_remaining_tokens(_agent(total=1200, cap=1000)) == 0


def test_exceeded_false_when_unlimited():
    assert sb.budget_exceeded(_agent(total=10**9, cap=None)) is False


def test_exceeded_is_inclusive_at_cap():
    assert sb.budget_exceeded(_agent(total=999, cap=1000)) is False
    assert sb.budget_exceeded(_agent(total=1000, cap=1000)) is True   # >= cap
    assert sb.budget_exceeded(_agent(total=900, aux=100, cap=1000)) is True


def test_exhausted_message_reports_calls_and_totals():
    msg = sb.budget_exhausted_message(_agent(total=4000, aux=1000, cap=5000, calls=42))
    assert "42 calls" in msg
    assert "5,000" in msg
    assert "5,000/5,000" in msg  # used == cap here
    # Recovery guidance must not imply live in-session mutation (config only,
    # takes effect next session).
    assert "next session" in msg


# ── evaluate_breach: the enforcement decision (abort / warn-once / none) ────

def test_evaluate_breach_none_when_under_or_unlimited():
    assert sb.evaluate_breach(_agent(total=500, cap=1000)) is None
    assert sb.evaluate_breach(_agent(total=10**9, cap=None)) is None
    assert sb.evaluate_breach(_agent(total=10**9, cap=0)) is None


def test_evaluate_breach_abort_every_time():
    ag = _agent(total=1000, cap=1000, action="abort")
    assert sb.evaluate_breach(ag) == "abort"
    assert sb.evaluate_breach(ag) == "abort"  # abort is not one-shot


def test_evaluate_breach_warn_is_one_shot_and_latches():
    ag = _agent(total=1000, cap=1000, action="warn")
    assert ag._session_budget_warned is False
    assert sb.evaluate_breach(ag) == "warn"      # first breach warns
    assert ag._session_budget_warned is True      # latch set
    assert sb.evaluate_breach(ag) is None         # subsequent breaches: continue silently


def test_evaluate_breach_counts_aux_toward_cap():
    # Breach reached only once aux is included.
    assert sb.evaluate_breach(_agent(total=900, aux=0, cap=1000)) is None
    assert sb.evaluate_breach(_agent(total=900, aux=100, cap=1000)) == "abort"


# ── config defaults wiring ─────────────────────────────────────────────────

def test_config_defaults_present_and_off_by_default():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    agent_cfg = DEFAULT_CONFIG["agent"]
    assert agent_cfg["session_budget_tokens"] is None  # unlimited by default
    assert agent_cfg["session_budget_action"] == "abort"


# ── aux-fork wiring (background review feeds the budget counter) ────────────

def test_background_review_usage_feeds_budget_counter():
    from agent.background_review import _record_review_usage_to_parent

    parent = SimpleNamespace(
        _session_db=None,       # no DB: budget must still register
        session_id=None,
        session_aux_tokens_for_budget=0,
    )
    _record_review_usage_to_parent(
        parent, {"input_tokens": 700, "output_tokens": 300}
    )
    assert parent.session_aux_tokens_for_budget == 1000

    # Accumulates across forks.
    _record_review_usage_to_parent(
        parent, {"input_tokens": 250, "output_tokens": 250}
    )
    assert parent.session_aux_tokens_for_budget == 1500
