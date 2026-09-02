"""Tests for the active-skill reasoning registry.

A skill that is viewed during a turn becomes the "active" skill for
reasoning resolution. This module tests that we record the active skill
and its optional reasoning suggestion, and that the most-recent view wins.
"""
import pytest

from tools import skills_tool as st


@pytest.fixture(autouse=True)
def _clean_registry():
    st.reset_active_skill_reasoning()
    yield
    st.reset_active_skill_reasoning()


def _record(name, frontmatter):
    """Simulate a served skill_view recording its reasoning suggestion."""
    reasoning = st._skill_reasoning_effort(frontmatter)
    st.record_active_skill("task-1", name, reasoning)


def test_records_skill_with_reasoning():
    _record("plan", {"metadata": {"hermes": {"reasoning": "xhigh"}}})
    assert st.active_skill_reasoning("task-1") == ("plan", "xhigh")


def test_records_skill_without_reasoning():
    _record("fetch", {"name": "fetch"})
    assert st.active_skill_reasoning("task-1") == ("fetch", None)


def test_no_active_skill_returns_none():
    assert st.active_skill_reasoning("task-1") is None


def test_most_recent_view_wins():
    _record("plan", {"metadata": {"hermes": {"reasoning": "xhigh"}}})
    _record("brainstorm", {"metadata": {"hermes": {"reasoning": "high"}}})
    assert st.active_skill_reasoning("task-1") == ("brainstorm", "high")


def test_registry_is_per_task():
    _record("plan", {"metadata": {"hermes": {"reasoning": "xhigh"}}})
    assert st.active_skill_reasoning("task-2") is None
