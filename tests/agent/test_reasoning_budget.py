from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.reasoning_budget import (
    ReasoningBudgetConfig,
    ReasoningBudgetTracker,
    begin_reasoning_budget_turn,
    set_reasoning_prompt_tokens,
    track_reasoning_delta,
)
from agent.think_scrubber import StreamingThinkScrubber


def test_absolute_budget_warns_once_at_token_threshold() -> None:
    tracker = ReasoningBudgetTracker(
        ReasoningBudgetConfig(
            warn_after_tokens=4,
            context_ratio=0,
            ratio_min_tokens=0,
            nudge_next_turn=False,
        )
    )
    tracker.set_prompt_tokens(1_000)

    for _ in range(12):
        assert tracker.add_delta("a") is None

    warning = tracker.add_delta("a")

    assert warning is not None
    assert warning.reason == "absolute"
    assert warning.reasoning_tokens == 4
    assert tracker.add_delta("more reasoning") is None


def test_ratio_budget_warns_before_absolute_threshold() -> None:
    tracker = ReasoningBudgetTracker(
        ReasoningBudgetConfig(
            warn_after_tokens=100,
            context_ratio=2,
            ratio_min_tokens=4,
            nudge_next_turn=False,
        )
    )
    tracker.set_prompt_tokens(2)

    warning = tracker.add_delta("a" * 13)

    assert warning is not None
    assert warning.reason == "ratio"
    assert warning.reasoning_tokens == 4
    assert warning.prompt_tokens == 2


def test_ratio_budget_waits_for_minimum_output_floor() -> None:
    tracker = ReasoningBudgetTracker(
        ReasoningBudgetConfig(
            warn_after_tokens=100,
            context_ratio=1,
            ratio_min_tokens=5,
            nudge_next_turn=False,
        )
    )
    tracker.set_prompt_tokens(1)

    assert tracker.add_delta("a" * 13) is None
    assert tracker.add_delta("a" * 4).reason == "ratio"


def test_zero_absolute_budget_disables_absolute_and_ratio_warnings() -> None:
    tracker = ReasoningBudgetTracker(
        ReasoningBudgetConfig(
            warn_after_tokens=0,
            context_ratio=1,
            ratio_min_tokens=1,
            nudge_next_turn=True,
        )
    )
    tracker.set_prompt_tokens(1)

    assert tracker.add_delta("a" * 1_000) is None


def test_config_reads_agent_mapping_and_clamps_invalid_values() -> None:
    config = ReasoningBudgetConfig.from_mapping(
        {
            "reasoning_warn_after_tokens": "64",
            "reasoning_warn_context_ratio": "3.5",
            "reasoning_warn_ratio_min_tokens": -10,
            "reasoning_warn_nudge": "true",
        }
    )

    assert config == ReasoningBudgetConfig(
        warn_after_tokens=64,
        context_ratio=3.5,
        ratio_min_tokens=0,
        nudge_next_turn=True,
    )


def test_runtime_defaults_match_config_defaults() -> None:
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert ReasoningBudgetConfig.from_mapping(
        DEFAULT_CONFIG["agent"]
    ) == ReasoningBudgetConfig()


def test_agent_helpers_stage_and_consume_one_shot_nudge() -> None:
    agent = SimpleNamespace(
        _reasoning_budget_tracker=ReasoningBudgetTracker(
            ReasoningBudgetConfig(
                warn_after_tokens=4,
                context_ratio=0,
                ratio_min_tokens=0,
                nudge_next_turn=True,
            )
        ),
        _pending_reasoning_budget_nudge=False,
    )
    set_reasoning_prompt_tokens(agent, 20)

    warning_text = track_reasoning_delta(agent, "a" * 13)

    assert warning_text is not None
    assert "~4 reasoning tokens" in warning_text
    assert "~20-token request context" in warning_text
    assert agent._pending_reasoning_budget_nudge is True

    nudge = begin_reasoning_budget_turn(agent)

    assert "Conclude directly or make the planned tool call now" in nudge
    assert agent._pending_reasoning_budget_nudge is False
    assert agent._reasoning_budget_tracker.reasoning_tokens == 0
    assert begin_reasoning_budget_turn(agent) == ""


def test_shared_reasoning_sink_emits_budget_warning() -> None:
    from run_agent import AIAgent

    agent = object.__new__(AIAgent)
    agent._reasoning_budget_tracker = ReasoningBudgetTracker(
        ReasoningBudgetConfig(
            warn_after_tokens=4,
            context_ratio=0,
            ratio_min_tokens=0,
            nudge_next_turn=False,
        )
    )
    agent._pending_reasoning_budget_nudge = False
    agent._stream_writer_tls = None
    agent.reasoning_callback = MagicMock()
    agent._emit_warning = MagicMock()

    agent._fire_reasoning_delta("a" * 13)

    agent.reasoning_callback.assert_called_once_with("a" * 13)
    agent._emit_warning.assert_called_once()
    assert "~4 reasoning tokens" in agent._emit_warning.call_args.args[0]


def test_inline_think_stream_counts_suppressed_reasoning() -> None:
    agent = SimpleNamespace(
        _reasoning_budget_tracker=ReasoningBudgetTracker(
            ReasoningBudgetConfig(
                warn_after_tokens=4,
                context_ratio=0,
                ratio_min_tokens=0,
                nudge_next_turn=False,
            )
        ),
        _pending_reasoning_budget_nudge=False,
    )
    warnings: list[str] = []
    scrubber = StreamingThinkScrubber(
        on_reasoning_delta=lambda text: warnings.append(
            track_reasoning_delta(agent, text) or ""
        )
    )

    visible = "".join(
        scrubber.feed(delta)
        for delta in ("<think>", "a" * 13, "</think>", "done")
    )

    assert visible == "done"
    assert len([warning for warning in warnings if warning]) == 1
    assert "~4 reasoning tokens" in next(w for w in warnings if w)
