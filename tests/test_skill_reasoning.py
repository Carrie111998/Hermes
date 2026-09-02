"""Tests for per-skill reasoning suggestion extraction.

Covers extracting `metadata.hermes.reasoning` from a skill's frontmatter,
validating against known effort levels, and the "no suggestion" default.
"""
import pytest

from tools.skills_tool import _skill_reasoning_effort


def test_extracts_xhigh_from_metadata_hermes():
    """A skill declaring metadata.hermes.reasoning: xhigh yields 'xhigh'."""
    frontmatter = {
        "name": "plan",
        "metadata": {"hermes": {"reasoning": "xhigh"}},
    }
    assert _skill_reasoning_effort(frontmatter) == "xhigh"


def test_extracts_medium_from_metadata_hermes():
    frontmatter = {
        "name": "brainstorming",
        "metadata": {"hermes": {"reasoning": "medium"}},
    }
    assert _skill_reasoning_effort(frontmatter) == "medium"


def test_no_metadata_returns_none():
    """Skills without the key contribute nothing — default remains."""
    assert _skill_reasoning_effort({"name": "plan"}) is None
    assert _skill_reasoning_effort({"name": "plan", "metadata": {}}) is None


def test_no_hermes_key_returns_none():
    assert (
        _skill_reasoning_effort(
            {"name": "plan", "metadata": {"tags": ["x"]}}
        )
        is None
    )


@pytest.mark.parametrize("bad", ["turbo", "very-high", "9", ""])
def test_invalid_effort_returns_none(bad):
    """Unknown/unsupported levels are ignored, never crash."""
    frontmatter = {"metadata": {"hermes": {"reasoning": bad}}}
    assert _skill_reasoning_effort(frontmatter) is None
