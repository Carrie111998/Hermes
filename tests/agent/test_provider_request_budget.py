from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.provider_request_budget import (
    ProviderRequestBudget,
    ProviderRequestBudgetExceeded,
    capture_provider_request_reservation,
    parse_provider_request_limit,
    reserve_provider_request,
    reset_provider_request_budget,
)


def test_disabled_budget_is_a_noop_without_a_finite_remaining_count():
    budget = ProviderRequestBudget()

    assert budget.reserve(reason="normal") == 0
    assert budget.reserve(reason="retry") == 0
    assert budget.enabled is False
    assert budget.used == 0
    assert budget.remaining is None


def test_enabled_budget_raises_before_exceeding_the_limit():
    budget = ProviderRequestBudget(2)

    assert budget.reserve(reason="normal") == 1
    assert budget.reserve(reason="fallback") == 2

    with pytest.raises(
        ProviderRequestBudgetExceeded,
        match=r"provider request budget exhausted \(2/2\).*retry",
    ):
        budget.reserve(reason="retry")

    assert budget.enabled is True
    assert budget.used == 2
    assert budget.remaining == 0


def test_provider_request_limit_parser_accepts_only_positive_integer_like_values():
    assert parse_provider_request_limit(3) == 3
    assert parse_provider_request_limit("4") == 4
    assert parse_provider_request_limit(0) == 0
    assert parse_provider_request_limit(-1) == 0
    assert parse_provider_request_limit(True) == 0
    assert parse_provider_request_limit(3.5) == 0
    assert parse_provider_request_limit("3.5") == 0
    assert parse_provider_request_limit("invalid") == 0
    assert parse_provider_request_limit(None) == 0


def test_concurrent_reservations_never_exceed_the_limit():
    budget = ProviderRequestBudget(3)

    def reserve(index: int) -> bool:
        try:
            budget.reserve(reason=f"worker-{index}")
        except ProviderRequestBudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(reserve, range(10)))

    assert outcomes.count(True) == 3
    assert outcomes.count(False) == 7
    assert budget.used == 3
    assert budget.remaining == 0


def test_reservation_helper_covers_only_the_main_agent():
    main = SimpleNamespace(
        provider="openai",
        platform="cli",
        is_subagent=False,
        provider_request_budget=ProviderRequestBudget(1),
    )
    assert reserve_provider_request(main, reason="normal") == 1

    for excluded in (
        SimpleNamespace(
            provider="moa",
            platform="cli",
            is_subagent=False,
            provider_request_budget=ProviderRequestBudget(1),
        ),
        SimpleNamespace(
            provider="openai",
            platform="subagent",
            is_subagent=True,
            provider_request_budget=ProviderRequestBudget(1),
        ),
        SimpleNamespace(
            provider="openai",
            platform="cli",
            is_subagent=False,
            _delegate_depth=1,
            provider_request_budget=ProviderRequestBudget(1),
        ),
        SimpleNamespace(
            provider="openai",
            platform="cli",
            is_subagent=False,
            _memory_write_context="background_review",
            provider_request_budget=ProviderRequestBudget(1),
        ),
        SimpleNamespace(
            provider="openai",
            platform="curator",
            is_subagent=False,
            _memory_write_origin="background_review",
            provider_request_budget=ProviderRequestBudget(1),
        ),
        SimpleNamespace(
            provider="openai",
            platform="cli",
            is_subagent=False,
            _memory_write_origin="background_review",
            provider_request_budget=ProviderRequestBudget(1),
        ),
        SimpleNamespace(
            provider="openai",
            platform="cli",
            is_subagent=False,
            _persist_disabled=True,
            provider_request_budget=ProviderRequestBudget(1),
        ),
        SimpleNamespace(
            provider="openai",
            platform="cli",
            is_subagent=False,
            _provider_request_budget_exempt=True,
            provider_request_budget=ProviderRequestBudget(1),
        ),
    ):
        assert reserve_provider_request(excluded, reason="excluded") == 0
        assert excluded.provider_request_budget.used == 0


def test_reset_rebuilds_a_fresh_budget_from_the_agent_limit():
    class Agent:
        max_provider_requests_per_turn = 1

    agent = Agent()
    first = reset_provider_request_budget(agent)
    first.reserve(reason="normal")

    second = reset_provider_request_budget(agent)

    assert second is agent.provider_request_budget
    assert second is not first
    assert second.max_total == 1
    assert second.used == 0


def test_captured_reservation_never_charges_a_later_turn():
    agent = SimpleNamespace(
        provider="openai",
        platform="cli",
        is_subagent=False,
        max_provider_requests_per_turn=1,
    )
    first = reset_provider_request_budget(agent)
    reserve_first_turn = capture_provider_request_reservation(agent)
    second = reset_provider_request_budget(agent)

    reserve_first_turn(reason="late worker")

    assert first.used == 1
    assert second.used == 0


def test_default_config_disables_the_provider_request_budget():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["agent"]["max_provider_requests_per_turn"] == 0


def test_observability_does_not_misclassify_request_exhaustion_as_iteration_limit():
    from hermes_cli.observability.shared_metrics_contract import task_terminal_state

    assert task_terminal_state(
        {"turn_exit_reason": "provider_request_budget_exhausted"}
    ) == ("failed", "provider_request_limit", "system_aborted")


def test_agent_initializes_the_budget_from_config():
    from run_agent import AIAgent

    config = {"agent": {"max_provider_requests_per_turn": "2"}}
    with (
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.load_config_readonly", return_value=config),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent.max_provider_requests_per_turn == 2
    assert agent.provider_request_budget.max_total == 2
    assert agent.provider_request_budget.used == 0


def test_new_user_turn_resets_the_provider_request_budget():
    from run_agent import AIAgent

    config = {"agent": {"max_provider_requests_per_turn": 1}}
    with (
        patch("run_agent.OpenAI"),
        patch("hermes_cli.config.load_config", return_value=config),
        patch("hermes_cli.config.load_config_readonly", return_value=config),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    previous = agent.provider_request_budget
    previous.reserve(reason="previous-turn")
    agent._interruptible_api_call = MagicMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="done", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=None,
        )
    )

    result = agent.run_conversation("hello")

    assert result["final_response"] == "done"
    assert agent.provider_request_budget is not previous
    assert agent.provider_request_budget.max_total == 1
    assert agent.provider_request_budget.used == 0
