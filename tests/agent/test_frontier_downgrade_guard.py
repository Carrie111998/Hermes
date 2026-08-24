"""HF-04 Layer A (IGN-197): fail-loud frontier check on the CLI/cron turn path.

Covers ``agent.frontier_guard`` — the shared, flag-gated requested-vs-served
model-class check wired into ``finalize_turn`` (the point the CLI/cron retry
loop has stopped retrying and the served model is final).

The classifier itself is IGN-195's deliverable; these tests install a stand-in
so the guard's own contract can be verified independently of where A1 lands it.
"""

import sys
import types

import pytest

from agent import frontier_guard as fg


FRONTIER = {"gpt-5.6-sol", "claude-opus-5", "gemini-3.6-pro"}
CHEAP = {"gemini-3.6-flash", "gpt-5-mini"}


def _stub_model_class(model):
    if model in FRONTIER:
        return "frontier"
    if model in CHEAP:
        return "cheap"
    return "unknown"


@pytest.fixture
def classifier(monkeypatch):
    """Install a stand-in for IGN-195's shared ``model_class``."""
    module = types.ModuleType("agent.model_classification")
    module.model_class = _stub_model_class
    monkeypatch.setitem(sys.modules, "agent.model_classification", module)
    return module


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, "1")


@pytest.fixture(autouse=True)
def _reset_latch(monkeypatch):
    monkeypatch.setattr(fg, "_classifier_missing_reported", False)


def test_inert_when_flag_off(monkeypatch, classifier):
    """Default-off rollback path: a real downgrade produces nothing."""
    monkeypatch.delenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, raising=False)
    result = {}
    assert (
        fg.check_frontier_downgrade(
            result,
            requested_model="gpt-5.6-sol",
            served_model="gemini-3.6-flash",
            frontier_required=True,
        )
        is None
    )
    assert "warnings" not in result


def test_inert_when_caller_did_not_require_frontier(flag_on, classifier):
    """Opt-in: callers that never asked for frontier are unaffected."""
    result = {}
    assert (
        fg.check_frontier_downgrade(
            result,
            requested_model="gpt-5.6-sol",
            served_model="gemini-3.6-flash",
            frontier_required=False,
        )
        is None
    )
    assert "warnings" not in result


def test_downgrade_is_reported_to_the_caller(flag_on, classifier):
    result = {}
    warning = fg.check_frontier_downgrade(
        result,
        requested_model="gpt-5.6-sol",
        served_model="gemini-3.6-flash",
        frontier_required=True,
    )
    assert warning["type"] == "frontier_downgrade"
    assert warning["requested_model"] == "gpt-5.6-sol"
    assert warning["served_model"] == "gemini-3.6-flash"
    assert warning["requested_class"] == "frontier"
    assert warning["served_class"] == "cheap"
    assert result["warnings"] == [warning]


def test_frontier_served_by_frontier_is_clean(flag_on, classifier):
    """A fallback that stays in-class is not a downgrade."""
    result = {}
    assert (
        fg.check_frontier_downgrade(
            result,
            requested_model="gpt-5.6-sol",
            served_model="claude-opus-5",
            frontier_required=True,
        )
        is None
    )
    assert "warnings" not in result


def test_non_frontier_request_cannot_be_downgraded(flag_on, classifier):
    result = {}
    assert (
        fg.check_frontier_downgrade(
            result,
            requested_model="gpt-5-mini",
            served_model="gemini-3.6-flash",
            frontier_required=True,
        )
        is None
    )


def test_missing_classifier_fails_loud_rather_than_silently_passing(
    monkeypatch, flag_on
):
    """A check that cannot run must not be indistinguishable from a clean one."""
    monkeypatch.setattr(fg, "_resolve_model_class", lambda: None)
    result = {}
    warning = fg.check_frontier_downgrade(
        result,
        requested_model="gpt-5.6-sol",
        served_model="gemini-3.6-flash",
        frontier_required=True,
    )
    assert warning["type"] == "frontier_check_unavailable"
    assert result["warnings"] == [warning]


def test_raising_classifier_is_reported_and_never_breaks_the_turn(
    monkeypatch, flag_on
):
    module = types.ModuleType("agent.model_classification")

    def _boom(model):
        raise RuntimeError("classifier exploded")

    module.model_class = _boom
    monkeypatch.setitem(sys.modules, "agent.model_classification", module)

    result = {}
    warning = fg.check_frontier_downgrade(
        result,
        requested_model="gpt-5.6-sol",
        served_model="gemini-3.6-flash",
        frontier_required=True,
    )
    assert warning["type"] == "frontier_check_unavailable"


def test_existing_warnings_are_appended_to_not_replaced(flag_on, classifier):
    pre_existing = {"type": "something_else"}
    result = {"warnings": [pre_existing]}
    fg.check_frontier_downgrade(
        result,
        requested_model="gpt-5.6-sol",
        served_model="gemini-3.6-flash",
        frontier_required=True,
    )
    assert result["warnings"][0] is pre_existing
    assert len(result["warnings"]) == 2


def test_flag_is_read_per_call_so_rollback_needs_no_restart(monkeypatch, classifier):
    """Instant rollback: flipping the env var takes effect on the next call."""
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, "1")
    assert fg.frontier_downgrade_check_enabled() is True
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, "0")
    assert fg.frontier_downgrade_check_enabled() is False
    result = {}
    assert (
        fg.check_frontier_downgrade(
            result,
            requested_model="gpt-5.6-sol",
            served_model="gemini-3.6-flash",
            frontier_required=True,
        )
        is None
    )


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_flag_values(monkeypatch, value):
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, value)
    assert fg.frontier_downgrade_check_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_falsey_flag_values(monkeypatch, value):
    monkeypatch.setenv(fg.FRONTIER_DOWNGRADE_CHECK_ENV, value)
    assert fg.frontier_downgrade_check_enabled() is False


def test_turn_path_threads_the_flag_end_to_end():
    """run_conversation -> finalize_turn carry the caller-supplied flag."""
    import inspect

    from agent.conversation_loop import run_conversation
    from agent.turn_finalizer import finalize_turn

    for fn in (run_conversation, finalize_turn):
        params = inspect.signature(fn).parameters
        assert "frontier_required" in params
        assert params["frontier_required"].default is False
