"""Test deterministic_empty guard behavior on local/zero-cost endpoints.

Regression test for #89213: deterministic_empty() should fail open when
the streak has no known cost, mirroring the behavior of the cost-aware
budget guard. Local/self-hosted endpoints have no charges to avoid, so
skipping retries has no upside.
"""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from agent.empty_response_guard import (
    DEFAULT_EMPTY_RETRY_BUDGET,
    deterministic_empty,
    empty_retry_budget,
    record_empty_attempt,
    reset_guard_state,
)


@pytest.fixture
def mock_agent():
    """Create a mock agent with empty guard enabled."""
    agent = Mock()
    agent._empty_guard_enabled = True
    agent._empty_guard_cost_threshold_usd = Decimal("0.25")
    agent._empty_content_retries = 0
    agent.provider = "custom"
    agent.model = "local-model"
    return agent


@pytest.fixture
def mock_response_zero_cost():
    """Mock response with usage present but no cost estimate (local endpoint)."""
    resp = Mock()
    resp.usage = Mock()
    resp.usage.prompt_tokens = 1000
    resp.usage.completion_tokens = 0
    resp.model = "local-model"
    # No cost data for local endpoints
    return resp


@pytest.fixture
def mock_response_with_cost():
    """Mock response with usage and known cost (paid endpoint)."""
    resp = Mock()
    resp.usage = Mock()
    resp.usage.prompt_tokens = 100000  # Large context
    resp.usage.completion_tokens = 0
    resp.model = "gpt-4"
    # Will have cost estimate in real scenario
    return resp


def test_deterministic_empty_fails_open_on_zero_cost_streak(mock_agent, mock_response_zero_cost):
    """deterministic_empty() returns False when streak has no known cost.
    
    This is the core fix for #89213: local endpoints with transient empties
    should keep their full retry budget, not be treated as deterministic
    failures after 2 attempts.
    """
    # Reset state
    reset_guard_state(mock_agent)
    
    # Simulate two consecutive empty attempts from a local endpoint
    for i in range(2):
        mock_agent._empty_content_retries = i + 1
        record_empty_attempt(
            mock_agent,
            finish_reason="stop",
            response=mock_response_zero_cost,
        )
    
    # Should NOT be deterministic (cost is None)
    assert not deterministic_empty(mock_agent)
    
    # Budget should remain at the full default
    budget = empty_retry_budget(mock_agent, mock_response_zero_cost)
    assert budget == DEFAULT_EMPTY_RETRY_BUDGET


def test_deterministic_empty_detects_paid_endpoint_pattern(mock_agent):
    """deterministic_empty() still detects true deterministic failures on paid routes.
    
    When cost IS known and the pattern matches, the guard should fire.
    """
    # Reset state
    reset_guard_state(mock_agent)
    
    # Simulate two empties with known cost
    for i in range(2):
        mock_agent._empty_content_retries = i + 1
        # Inject cost manually (in real usage, _estimate_attempt_cost provides this)
        if not hasattr(mock_agent, "_empty_streak_cost_usd"):
            mock_agent._empty_streak_cost_usd = Decimal("0")
        mock_agent._empty_streak_cost_usd += Decimal("0.50")
        
        record_empty_attempt(
            mock_agent,
            finish_reason="stop",
            response=mock_response_with_cost,
        )
    
    # Should be deterministic (cost is known and > 0)
    assert deterministic_empty(mock_agent)


def test_deterministic_empty_requires_at_least_two_attempts(mock_agent, mock_response_zero_cost):
    """deterministic_empty() never fires on the first attempt."""
    reset_guard_state(mock_agent)
    
    mock_agent._empty_content_retries = 1
    record_empty_attempt(
        mock_agent,
        finish_reason="stop",
        response=mock_response_zero_cost,
    )
    
    # Not deterministic after just 1 attempt
    assert not deterministic_empty(mock_agent)


def test_cost_aware_budget_already_fails_open_on_zero_cost(mock_agent, mock_response_zero_cost):
    """Verify empty_retry_budget already handles zero-cost correctly.
    
    This test documents the existing correct behavior in the cost-aware
    guard, which deterministic_empty should mirror.
    """
    reset_guard_state(mock_agent)
    
    # No cost accumulation on local endpoint
    budget = empty_retry_budget(mock_agent, mock_response_zero_cost)
    
    # Should keep full budget (cost is None)
    assert budget == DEFAULT_EMPTY_RETRY_BUDGET


def test_guard_disabled_skips_deterministic_check(mock_agent, mock_response_zero_cost):
    """When guard is disabled, deterministic_empty always returns False."""
    reset_guard_state(mock_agent)
    mock_agent._empty_guard_enabled = False
    
    # Simulate pattern that would normally trigger
    for i in range(2):
        mock_agent._empty_content_retries = i + 1
        if not hasattr(mock_agent, "_empty_streak_cost_usd"):
            mock_agent._empty_streak_cost_usd = Decimal("1.00")
        record_empty_attempt(
            mock_agent,
            finish_reason="stop",
            response=mock_response_zero_cost,
        )
    
    # Should NOT be deterministic (guard disabled)
    assert not deterministic_empty(mock_agent)


def test_mixed_signature_prevents_deterministic_detection(mock_agent, mock_response_zero_cost):
    """Different finish_reasons break deterministic detection."""
    reset_guard_state(mock_agent)
    
    # First attempt with "stop"
    mock_agent._empty_content_retries = 1
    if not hasattr(mock_agent, "_empty_streak_cost_usd"):
        mock_agent._empty_streak_cost_usd = Decimal("0.50")
    record_empty_attempt(
        mock_agent,
        finish_reason="stop",
        response=mock_response_zero_cost,
    )
    
    # Second attempt with "length"
    mock_agent._empty_content_retries = 2
    mock_agent._empty_streak_cost_usd += Decimal("0.50")
    record_empty_attempt(
        mock_agent,
        finish_reason="length",
        response=mock_response_zero_cost,
    )
    
    # Should NOT be deterministic (signatures differ)
    assert not deterministic_empty(mock_agent)
