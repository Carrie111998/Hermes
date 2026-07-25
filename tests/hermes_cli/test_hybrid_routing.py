"""Unit tests for the hybrid local/cloud routing classifier.

Covers the pure decision logic in ``hermes_cli.hybrid_routing`` — the
SIMPLE→local / COMPLEX→cloud heuristics, the enable/configured gates, and the
``/local``//``/cloud`` force override. No agent construction or network.
"""
from __future__ import annotations

import pytest

from hermes_cli.hybrid_routing import (
    CLOUD,
    LOCAL,
    classify_complexity,
    decide_route,
    is_local_configured,
    is_routing_enabled,
)


def _routing_cfg(**overrides):
    """A fully-configured routing block (routing enabled, local ready)."""
    cfg = {
        "enabled": True,
        "local": {"provider": "lmstudio", "model": "qwen3-4b", "base_url": "http://localhost:1234/v1"},
        "complexity": {
            "max_prompt_chars": 1500,
            "max_prompt_tokens": 400,
            "cloud_keywords": ["refactor", "debug", "analyze"],
            "escalate_on_images": True,
            "escalate_on_code_fence": True,
        },
    }
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------
# classify_complexity
# --------------------------------------------------------------------------

def test_short_plain_prompt_is_local():
    assert classify_complexity("what time is it in Tokyo?", routing_cfg=_routing_cfg()) == LOCAL


def test_long_prompt_escalates_to_cloud():
    long_prompt = "x" * 1600  # exceeds max_prompt_chars (1500)
    assert classify_complexity(long_prompt, routing_cfg=_routing_cfg()) == CLOUD


def test_token_threshold_escalates_to_cloud():
    # Under the char cap but over the token cap: use a small token limit.
    cfg = _routing_cfg()
    cfg["complexity"]["max_prompt_chars"] = 100000
    cfg["complexity"]["max_prompt_tokens"] = 5  # ~20 chars
    assert classify_complexity("this prompt is definitely longer than twenty chars", routing_cfg=cfg) == CLOUD


def test_keyword_escalates_to_cloud():
    assert classify_complexity("please refactor this function", routing_cfg=_routing_cfg()) == CLOUD


def test_keyword_match_is_case_insensitive():
    assert classify_complexity("Can you DEBUG my code", routing_cfg=_routing_cfg()) == CLOUD


def test_code_fence_escalates_to_cloud():
    prompt = "fix this:\n```python\nprint(1)\n```"
    assert classify_complexity(prompt, routing_cfg=_routing_cfg()) == CLOUD


def test_images_escalate_to_cloud():
    assert classify_complexity("what is this?", has_images=True, routing_cfg=_routing_cfg()) == CLOUD


def test_images_ignored_when_disabled():
    cfg = _routing_cfg()
    cfg["complexity"]["escalate_on_images"] = False
    assert classify_complexity("what is this?", has_images=True, routing_cfg=cfg) == LOCAL


def test_code_fence_ignored_when_disabled():
    cfg = _routing_cfg()
    cfg["complexity"]["escalate_on_code_fence"] = False
    prompt = "look:\n```\nx\n```"
    assert classify_complexity(prompt, routing_cfg=cfg) == LOCAL


def test_empty_prompt_is_local():
    assert classify_complexity("", routing_cfg=_routing_cfg()) == LOCAL


def test_none_prompt_does_not_raise():
    assert classify_complexity(None, routing_cfg=_routing_cfg()) == LOCAL  # type: ignore[arg-type]


def test_malformed_complexity_config_uses_defaults():
    # cloud_keywords as a string (not a list) → falls back to built-in defaults,
    # which include "refactor".
    cfg = _routing_cfg()
    cfg["complexity"]["cloud_keywords"] = "not-a-list"
    assert classify_complexity("please refactor this", routing_cfg=cfg) == CLOUD


def test_missing_complexity_block_uses_defaults():
    cfg = {"enabled": True, "local": {"model": "m"}}
    assert classify_complexity("hi", routing_cfg=cfg) == LOCAL
    assert classify_complexity("please debug this", routing_cfg=cfg) == CLOUD


# --------------------------------------------------------------------------
# gates: is_routing_enabled / is_local_configured
# --------------------------------------------------------------------------

def test_is_routing_enabled():
    assert is_routing_enabled(_routing_cfg()) is True
    assert is_routing_enabled({"enabled": False}) is False
    assert is_routing_enabled({}) is False
    assert is_routing_enabled(None) is False


def test_is_local_configured_requires_model():
    assert is_local_configured(_routing_cfg()) is True
    assert is_local_configured({"local": {"base_url": "http://x", "model": ""}}) is False
    assert is_local_configured({"local": {"model": "  "}}) is False
    assert is_local_configured({"local": {}}) is False
    assert is_local_configured({}) is False
    assert is_local_configured(None) is False


# --------------------------------------------------------------------------
# decide_route
# --------------------------------------------------------------------------

def test_decide_route_disabled_is_cloud():
    cfg = _routing_cfg(enabled=False)
    assert decide_route("hi", cfg) == CLOUD


def test_decide_route_unconfigured_local_is_cloud():
    cfg = _routing_cfg(local={"provider": "", "model": "", "base_url": ""})
    assert decide_route("hi", cfg) == CLOUD


def test_decide_route_none_config_is_cloud():
    assert decide_route("hi", None) == CLOUD


def test_decide_route_simple_prompt_goes_local():
    assert decide_route("hello there", _routing_cfg()) == LOCAL


def test_decide_route_complex_prompt_goes_cloud():
    assert decide_route("refactor the parser", _routing_cfg()) == CLOUD


def test_decide_route_force_local_overrides_classifier():
    # A prompt that would classify as CLOUD, forced to LOCAL.
    assert decide_route("please debug this", _routing_cfg(), force=LOCAL) == LOCAL


def test_decide_route_force_cloud_overrides_classifier():
    # A prompt that would classify as LOCAL, forced to CLOUD.
    assert decide_route("hi", _routing_cfg(), force=CLOUD) == CLOUD


def test_decide_route_force_local_blocked_when_local_unconfigured():
    # The unconfigured-local guard wins even over an explicit force → CLOUD,
    # so a pinned-local turn never dispatches a blank model.
    cfg = _routing_cfg(local={"provider": "", "model": "", "base_url": ""})
    assert decide_route("hi", cfg, force=LOCAL) == CLOUD


def test_decide_route_images_go_cloud():
    assert decide_route("what's this", _routing_cfg(), has_images=True) == CLOUD


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
