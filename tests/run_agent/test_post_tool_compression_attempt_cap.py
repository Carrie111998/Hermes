"""Behavioral regression tests for the post-tool compression attempt cap.

The pre-API pressure gate, the overflow/413 error handlers, and the post-tool
compaction gate all share ``compression_attempts`` as a consecutive-failure
backstop, bounded by the resolved ``compression.max_attempts`` cap (default 3).
Provider-confirmed recovery starts a fresh interval during a long turn. Before
the fix the post-tool path neither checked nor incremented the counter, so a
long tool loop could compact after every tool response for the lifetime of
the turn.

These tests drive ``run_conversation()`` through real tool iterations with a
compressor that always demands compression and assert ``_compress_context``
fires at most ``max_compression_attempts`` times per turn — no source
inspection, only observable behavior.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.context_compressor import ContextCompressor
from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tool_call(i: int):
    return SimpleNamespace(
        id=f"call_{i}",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'),
    )


def _tool_response(i: int, *, prompt_tokens: int | None = None):
    msg = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call(i)],
    )
    choice = SimpleNamespace(message=msg, finish_reason="tool_calls")
    usage = None
    if prompt_tokens is not None:
        usage = SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=10,
            total_tokens=prompt_tokens + 10,
        )
    return SimpleNamespace(choices=[choice], model="test/model", usage=usage)


def _stop_response():
    msg = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _pressured_compressor() -> MagicMock:
    """A compressor that always reports context pressure after tools run.

    ``should_defer_preflight_to_real_usage`` returns True so the turn-start
    preflight and the pre-API pressure gate stand down — isolating the
    post-tool gate as the only compression site under test.
    """
    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 10_000
    compressor.context_length = 200_000
    compressor.last_prompt_tokens = 150_000
    compressor.should_compress.side_effect = (
        lambda prompt_tokens=None: (
            prompt_tokens
            if prompt_tokens is not None
            else compressor.last_prompt_tokens
        )
        >= compressor.threshold_tokens
    )
    compressor.should_defer_preflight_to_real_usage.return_value = True
    compressor.get_active_compression_failure_cooldown.return_value = None

    def _update_from_response(usage):
        compressor.last_prompt_tokens = usage.get("prompt_tokens", 0)
        compressor.awaiting_real_usage_after_compression = False

    compressor.update_from_response.side_effect = _update_from_response
    compressor.awaiting_real_usage_after_compression = False
    return compressor


@pytest.fixture()
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=10,
        )
    a.client = MagicMock()
    a._cached_system_prompt = "You are helpful."
    a._use_prompt_caching = False
    a._disable_streaming = True
    a.tool_delay = 0
    a.save_trajectories = False
    a.compression_enabled = True
    a.context_compressor = _pressured_compressor()
    return a


def _run_tool_loop(
    agent,
    n_tool_iterations: int,
    *,
    reported_prompt_tokens: int | list[int] | None = None,
    compaction_makes_progress: bool | set[int] = False,
):
    """Drive one turn: ``n_tool_iterations`` tool calls, then a stop."""
    if isinstance(reported_prompt_tokens, list):
        assert len(reported_prompt_tokens) == n_tool_iterations
        prompt_tokens_by_iteration = reported_prompt_tokens
    else:
        prompt_tokens_by_iteration = [reported_prompt_tokens] * n_tool_iterations
    responses = [
        _tool_response(i, prompt_tokens=prompt_tokens_by_iteration[i])
        for i in range(n_tool_iterations)
    ]
    responses.append(_stop_response())
    agent.client.chat.completions.create.side_effect = responses

    compress_calls = []

    def _fake_compress(messages, system_message, **_kwargs):
        compress_calls.append(len(messages))
        call_number = len(compress_calls)
        if compaction_makes_progress is True or (
            isinstance(compaction_makes_progress, set)
            and call_number in compaction_makes_progress
        ):
            agent.context_compressor.awaiting_real_usage_after_compression = True
        return messages, "compressed prompt"

    with (
        patch.object(agent, "_compress_context", side_effect=_fake_compress),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch(
            "run_agent.handle_function_call",
            lambda name, args, task_id=None, **kwargs: json.dumps({"ok": True}),
        ),
    ):
        result = agent.run_conversation("do a lot of tool work")

    return result, compress_calls


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPostToolCompressionAttemptCap:
    def test_post_tool_compression_capped_at_default_three(self, agent):
        """7 tool iterations under constant pressure → exactly 3 compactions.

        Before the fix the post-tool gate re-fired after every tool response
        (7 compactions here); the shared per-turn counter caps it at the
        resolved default of 3.
        """
        assert agent.max_compression_attempts == 3  # config default
        result, compress_calls = _run_tool_loop(agent, n_tool_iterations=7)

        assert result["completed"] is True
        assert len(compress_calls) == 3, (
            f"post-tool compression must stop at the per-turn cap (3), "
            f"got {len(compress_calls)} compactions"
        )


    def test_post_tool_compression_shares_counter_with_pre_api_gate(self, agent):
        """Pre-API compactions consume the same per-turn budget.

        Let the pre-API pressure gate fire once (defer disabled for the first
        check), then keep the pressure on through tool iterations: the
        combined total must still respect the cap.
        """
        # First pre-API check does not defer → pre-API gate fires once;
        # afterwards defer again so only the post-tool gate keeps firing.
        defers = iter([False])
        agent.context_compressor.should_defer_preflight_to_real_usage.side_effect = (
            lambda _t: next(defers, True)
        )
        result, compress_calls = _run_tool_loop(agent, n_tool_iterations=7)

        assert result["completed"] is True
        assert len(compress_calls) == 3, (
            "pre-API and post-tool compactions must share one per-turn "
            f"attempt budget, got {len(compress_calls)} total compactions"
        )

    def test_cap_is_per_turn_not_per_session(self, agent):
        """A fresh turn gets a fresh attempt budget."""
        _result, first = _run_tool_loop(agent, n_tool_iterations=5)
        agent.client.chat.completions.create.side_effect = None
        _result, second = _run_tool_loop(agent, n_tool_iterations=5)

        assert len(first) == 3
        assert len(second) == 3

    def test_effective_compaction_reopens_budget_during_long_turn(self, agent):
        """Provider-confirmed recovery makes the cap consecutive, not lifetime.

        The hgfast failure used three effective compactions early in one
        100-iteration tool turn. Each next provider call reported a prompt well
        below the threshold, but the cumulative counter stayed at three and
        disabled later compaction. A real below-threshold reading must reopen
        the retry budget for later context regrowth.
        """
        result, compress_calls = _run_tool_loop(
            agent,
            n_tool_iterations=7,
            reported_prompt_tokens=[
                150_000,
                5_000,
                150_000,
                5_000,
                150_000,
                5_000,
                150_000,
            ],
            compaction_makes_progress=True,
        )

        assert result["completed"] is True
        assert len(compress_calls) == 4

    def test_unrelated_low_usage_does_not_reopen_budget_after_noop(self, agent):
        result, compress_calls = _run_tool_loop(
            agent,
            n_tool_iterations=7,
            reported_prompt_tokens=[
                150_000,
                5_000,
                150_000,
                5_000,
                150_000,
                5_000,
                150_000,
            ],
            compaction_makes_progress=False,
        )

        assert result["completed"] is True
        assert len(compress_calls) == 3

    def test_host_consumes_plugin_compaction_verdict_latch_once(self, agent):
        """Plugins need not know about the host's dynamic one-shot latch."""
        agent.max_compression_attempts = 1
        agent.context_compressor.update_from_response.side_effect = (
            lambda usage: setattr(
                agent.context_compressor,
                "last_prompt_tokens",
                usage.get("prompt_tokens", 0),
            )
        )
        result, compress_calls = _run_tool_loop(
            agent,
            n_tool_iterations=5,
            reported_prompt_tokens=[
                150_000,
                5_000,
                150_000,
                5_000,
                150_000,
            ],
            # First attempt succeeds and arms the host latch. The second is a
            # no-op and must not let the following unrelated low reading reset
            # the cap a second time.
            compaction_makes_progress={1},
        )

        assert result["completed"] is True
        assert len(compress_calls) == 2
        assert agent.context_compressor.awaiting_real_usage_after_compression is False

    def test_real_compressor_observes_latch_before_host_consumes_it(self, agent):
        """The host must not erase the built-in compressor's fit baseline."""
        compressor = ContextCompressor(
            model="test/model",
            quiet_mode=True,
            config_context_length=200_000,
        )
        compressor.threshold_tokens = 10_000
        compressor.last_compression_rough_tokens = 90_000
        agent.context_compressor = compressor

        result, compress_calls = _run_tool_loop(
            agent,
            n_tool_iterations=2,
            reported_prompt_tokens=[150_000, 5_000],
            compaction_makes_progress={1},
        )

        assert result["completed"] is True
        assert len(compress_calls) == 1
        assert compressor.last_rough_tokens_when_real_prompt_fit == 90_000
        assert compressor.awaiting_real_usage_after_compression is False

    def test_zero_prompt_is_not_forwarded_to_plugin_or_used_to_reset_latch(self, agent):
        agent.max_compression_attempts = 1
        observed_plugin_prompts = []

        def _plugin_update(usage):
            prompt = usage["prompt_tokens"]
            observed_plugin_prompts.append(prompt)
            # This deliberately bad third-party engine consumes every update.
            # The host must protect it from zero/missing prompt payloads.
            agent.context_compressor.last_prompt_tokens = prompt
            agent.context_compressor.awaiting_real_usage_after_compression = False

        agent.context_compressor.update_from_response.side_effect = _plugin_update
        result, compress_calls = _run_tool_loop(
            agent,
            n_tool_iterations=4,
            reported_prompt_tokens=[150_000, 0, 5_000, 150_000],
            compaction_makes_progress={1},
        )

        assert result["completed"] is True
        # The zero-prompt response cannot reach the plug-in, clear its latch,
        # or reset the one-attempt budget.  The later real low prompt can.
        assert len(compress_calls) == 2
        assert 0 not in observed_plugin_prompts
        assert agent.context_compressor.awaiting_real_usage_after_compression is False
