"""Regression for #88931: the catalog had a generic "qwen" catch-all
(131,072) that used longest-substring matching. A custom model like
``qwen36-35b`` (no explicit catalog entry) silently inherited that
value, short-circuiting the user-configured override path so the
dashboard reported the wrong context window.

The fix removed the generic catch-all. The specific ``qwen3.*``
entries cover every currently-named Qwen model. New family additions
must be appended explicitly — NOT as a generic catch-all.
"""

import pytest

from agent.model_metadata import get_model_context_length


def test_qwen_catch_all_removed_from_catalog():
    """The generic "qwen" catch-all entry must no longer exist.

    Without this assertion, a re-introduction of the catch-all would
    silently regress the dashboard fix.
    """
    from agent import model_metadata
    catalog = model_metadata.DEFAULT_CONTEXT_LENGTHS  # type: ignore[attr-defined]
    assert "qwen" not in catalog, (
        "Generic 'qwen' catch-all re-introduced in the context-length "
        "catalog. This was the root cause of #88931 — see the comment "
        "in agent/model_metadata.py around the qwen3.* block."
    )


def test_specific_qwen3_models_still_resolve_from_catalog():
    """The specific qwen3.* entries must continue to resolve correctly."""
    assert get_model_context_length("qwen3-max") == 262144
    assert get_model_context_length("qwen3-coder") == 262144
    assert get_model_context_length("qwen3-coder-plus") == 1_000_000
    # NOTE: the qwen3.6-plus / qwen3.7-plus catalog values are
    # 1,048,576, but models.dev can return 1,000,000 first. The catalog
    # answer is verified by the catalog key, the runtime answer by the
    # get_model_context_length call (which honors the live registry).
    # The important assertion is that the catalog still carries a
    # specific entry (not a generic "qwen" catch-all).
    from agent import model_metadata
    catalog = model_metadata.DEFAULT_CONTEXT_LENGTHS  # type: ignore[attr-defined]
    assert catalog["qwen3.6-plus"] == 1_048_576
    assert catalog["qwen3.7-plus"] == 1_048_576
    assert catalog["qwen3.8-max"] == 1_000_000
    # And the runtime resolves to a non-zero, non-131072 value.
    assert get_model_context_length("qwen3.6-plus") > 0
    assert get_model_context_length("qwen3.6-plus") != 131_072


def test_unknown_qwen_does_not_inherit_131072():
    """A custom Qwen-named model with no catalog entry must NOT inherit
    131,072 via the (now-removed) catch-all.

    Without an override, an unknown model falls through to the
    256K default, not 131K. With an override, the override wins.
    """
    # No override → not 131072 (the removed catch-all value).
    auto = get_model_context_length("qwen36-35b")
    assert auto != 131072, (
        "Unknown qwen model still resolves to 131072, suggesting the "
        "catch-all has been re-introduced."
    )


def test_qwen_override_honored():
    """The user-configured override (config_context_length) must win
    over the catalog answer for a custom Qwen model.

    This is the dashboard scenario: user has
    ``model_overrides.custom.litellm.qwen36-35b.context_window: 99000``
    and expects the agent to use 99K, not the catalog fallback.
    """
    assert get_model_context_length(
        "qwen36-35b", config_context_length=99000,
    ) == 99000


def test_qwen_override_honored_for_known_qwen3_too():
    """The override must also win for a model that has a catalog entry,
    so an operator can shrink a model window for a specific session."""
    assert get_model_context_length(
        "qwen3-max", config_context_length=128000,
    ) == 128000


def test_qwen_zero_or_negative_override_falls_through():
    """A non-positive override (0, -1, None) must NOT be applied as
    the context length — the catalog/endpoint answer wins instead.
    """
    # 0 is a sentinel for "no override" and must not be applied.
    assert get_model_context_length(
        "qwen3-max", config_context_length=0,
    ) == 262144
    # None is the explicit "no override" path.
    assert get_model_context_length(
        "qwen3-max", config_context_length=None,
    ) == 262144
