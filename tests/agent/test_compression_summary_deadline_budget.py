"""The compression deadline must cover the summary the compressor asks for.

Symptom this guards: a session stops compacting forever. The compressor asks
for a handoff of up to ``_SUMMARY_TOKENS_CEILING`` (10K) output tokens, but the
auxiliary deadline was a flat 300 s regardless of that ask. A reasoning
summariser sustaining ~18 tok/s needs ~9 minutes for a 10K-token summary, so
every attempt exhausted the deadline, compression aborted, the transcript kept
growing past the model's context window, and each failure armed a longer
summary cooldown.

The contract asserted here is a relation, not a snapshot: whatever output the
compression prompt requests, the deadline handed to the provider must be able
to produce it at the slowest rate we still consider healthy.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.context_compressor import ContextCompressor

from agent.auxiliary_client import (
    _COMPRESSION_MIN_SUMMARY_TOKENS_PER_SECOND,
    _COMPRESSION_TIMEOUT_CEILING_SECONDS,
    _COMPRESSION_TIMEOUT_FLOOR_SECONDS,
    _compression_timeout_floor,
    async_call_llm,
    call_llm,
)

# A config-derived compression timeout well below any scaled floor, so the
# floor is what reaches the provider.
LOW_CONFIG_TIMEOUT = 120.0


def _seconds_needed(output_tokens: int) -> float:
    """Wall clock the slowest healthy summariser needs for that many tokens."""
    return output_tokens / _COMPRESSION_MIN_SUMMARY_TOKENS_PER_SECOND


def _client_sync():
    client = MagicMock()
    client.base_url = "https://api.openai.com/v1"
    client.chat.completions.create.return_value = {"ok": True}
    return client


def _client_async():
    client = MagicMock()
    client.base_url = "https://api.openai.com/v1"
    client.chat.completions.create = AsyncMock(return_value={"ok": True})
    return client


def _patches(client, *, task_timeout):
    return (
        patch("agent.auxiliary_client._resolve_task_provider_model",
              return_value=("openai-codex", "gpt-5.6", None, None, None)),
        patch("agent.auxiliary_client._get_cached_client",
              return_value=(client, "gpt-5.6")),
        patch("agent.auxiliary_client._validate_llm_response",
              side_effect=lambda resp, _task, **_kw: resp),
        patch("agent.auxiliary_client._get_task_timeout",
              return_value=task_timeout),
    )


class TestCompressionTimeoutFloorScaling:
    """The floor itself, as a pure function of the requested output size."""

    def test_unknown_budget_keeps_the_flat_floor(self):
        assert _compression_timeout_floor(None) == _COMPRESSION_TIMEOUT_FLOOR_SECONDS
        assert _compression_timeout_floor(0) == _COMPRESSION_TIMEOUT_FLOOR_SECONDS

    @pytest.mark.parametrize("budget", [2_000, 6_000, 10_000, 14_000])
    def test_floor_covers_generating_the_requested_output(self, budget):
        """The whole bug: a deadline smaller than the ask times out every time."""
        floor = _compression_timeout_floor(budget)
        assert floor >= _seconds_needed(budget), (
            f"a {budget}-token summary needs >= {_seconds_needed(budget):.0f}s of "
            f"generation but the deadline is {floor:.0f}s"
        )

    def test_floor_is_monotonic_in_the_requested_output(self):
        floors = [_compression_timeout_floor(b) for b in (1_000, 5_000, 10_000)]
        assert floors == sorted(floors)
        assert floors[0] < floors[-1], "a bigger ask must not get an equal deadline"

    def test_floor_never_drops_below_the_historical_flat_floor(self):
        """Prompt ingestion and pre-token reasoning still need the base budget."""
        assert _compression_timeout_floor(1) >= _COMPRESSION_TIMEOUT_FLOOR_SECONDS

    def test_pathological_budget_stays_bounded(self):
        assert (
            _compression_timeout_floor(10_000_000)
            == _COMPRESSION_TIMEOUT_CEILING_SECONDS
        )

    def test_garbage_budget_degrades_to_the_flat_floor(self):
        assert _compression_timeout_floor("lots") == _COMPRESSION_TIMEOUT_FLOOR_SECONDS
        assert _compression_timeout_floor(-5) == _COMPRESSION_TIMEOUT_FLOOR_SECONDS


class TestDeclaredBudgetReachesTheProvider:
    """``expected_output_tokens`` must actually widen the deadline on the wire."""

    def test_declared_budget_widens_the_deadline(self):
        client = _client_sync()
        budget = 10_000
        p1, p2, p3, p4 = _patches(client, task_timeout=LOW_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarise this"}],
                expected_output_tokens=budget,
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout >= _seconds_needed(budget)
        assert timeout > _COMPRESSION_TIMEOUT_FLOOR_SECONDS, (
            "a 10K-token ask must not be capped at the size-blind flat floor"
        )

    def test_undeclared_budget_keeps_prior_behavior(self):
        client = _client_sync()
        p1, p2, p3, p4 = _patches(client, task_timeout=LOW_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "x"}],
            )
        timeout = client.chat.completions.create.call_args.kwargs["timeout"]
        assert timeout == _COMPRESSION_TIMEOUT_FLOOR_SECONDS

    def test_explicit_per_call_timeout_still_wins(self):
        """An explicit deadline is a caller contract; scaling must not raise it."""
        client = _client_sync()
        p1, p2, p3, p4 = _patches(client, task_timeout=LOW_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "x"}],
                timeout=45.0,
                expected_output_tokens=10_000,
            )
        assert client.chat.completions.create.call_args.kwargs["timeout"] == 45.0

    def test_config_timeout_above_the_scaled_floor_is_kept(self):
        client = _client_sync()
        generous = _compression_timeout_floor(10_000) + 600.0
        p1, p2, p3, p4 = _patches(client, task_timeout=generous)
        with p1, p2, p3, p4:
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "x"}],
                expected_output_tokens=10_000,
            )
        assert client.chat.completions.create.call_args.kwargs["timeout"] == generous

    def test_other_tasks_are_not_scaled(self):
        """Only compression owns this floor; a declared budget must not leak."""
        client = _client_sync()
        p1, p2, p3, p4 = _patches(client, task_timeout=30.0)
        with p1, p2, p3, p4:
            call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "x"}],
                expected_output_tokens=10_000,
            )
        assert client.chat.completions.create.call_args.kwargs["timeout"] == 30.0

    def test_declared_budget_is_never_sent_on_the_wire(self):
        """It sizes the deadline only — it must not become a request field."""
        client = _client_sync()
        p1, p2, p3, p4 = _patches(client, task_timeout=LOW_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            call_llm(
                task="compression",
                messages=[{"role": "user", "content": "x"}],
                expected_output_tokens=10_000,
            )
        sent = client.chat.completions.create.call_args.kwargs
        assert "expected_output_tokens" not in sent
        assert sent.get("max_tokens") is None
        assert sent.get("max_completion_tokens") is None

    @pytest.mark.asyncio
    async def test_async_path_scales_identically(self):
        client = _client_async()
        budget = 10_000
        p1, p2, p3, p4 = _patches(client, task_timeout=LOW_CONFIG_TIMEOUT)
        with p1, p2, p3, p4:
            await async_call_llm(
                task="compression",
                messages=[{"role": "user", "content": "x"}],
                expected_output_tokens=budget,
            )
        sent = client.chat.completions.create.call_args.kwargs
        assert sent["timeout"] >= _seconds_needed(budget)
        assert "expected_output_tokens" not in sent


def _compressor(*, tail_mode="lean"):
    """Minimal compressor state for driving ``_generate_summary``."""
    c = ContextCompressor.__new__(ContextCompressor)
    c.protect_first_n = 2
    c.protect_last_n = 5
    c.tail_token_budget = 20_000
    c.tail_mode = tail_mode
    c.context_length = 272_000
    c.threshold_percent = 0.80
    c.threshold_tokens = 204_000
    c.summary_target_ratio = 0.20
    c.max_summary_tokens = 10_000
    c.quiet_mode = True
    c.compression_count = 0
    c.last_prompt_tokens = 0
    c._previous_summary = None
    c._ineffective_compression_count = 0
    c._verify_compaction_cleared_threshold = False
    c._summary_failure_cooldown_until = 0.0
    c.summary_model = None
    c.model = "test-model"
    c.provider = "test"
    c.base_url = "http://localhost"
    c.api_key = "test-key"
    c.api_mode = "chat_completions"
    return c


def _long_turns():
    """Enough content that the scaled budget is well above the minimum."""
    body = "deploy the strimzi runtime and record the outcome. " * 400
    return [
        {"role": "user", "content": f"turn {i}: {body}"}
        if i % 2 == 0
        else {"role": "assistant", "content": f"turn {i}: {body}"}
        for i in range(40)
    ]


class TestCompressorDeclaresWhatItAsksFor:
    """The prompt's ask and the declared deadline input must not drift apart."""

    @pytest.mark.parametrize("tail_mode", ["lean", "full"])
    def test_declared_budget_matches_the_prompt_target(self, tail_mode):
        compressor = _compressor(tail_mode=tail_mode)
        captured = {}

        def fake_call_llm(**kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "## Goal\nship it."
            return resp

        with patch("agent.context_compressor.call_llm", fake_call_llm):
            assert compressor._generate_summary(_long_turns()) is not None

        prompt = captured["messages"][0]["content"]
        target = re.search(r"Target ~(\d+) tokens", prompt)
        assert target, "summary prompt must state the output it asks for"
        assert captured["expected_output_tokens"] == int(target.group(1)), (
            "the deadline is sized from expected_output_tokens; if it does not "
            "match the tokens the prompt actually requests, the summary can "
            "outlive its own deadline on every attempt"
        )

    def test_declared_budget_buys_enough_time_to_produce_it(self):
        """End of the chain: what the compressor asks for is what it can wait for."""
        compressor = _compressor()
        captured = {}

        def fake_call_llm(**kwargs):
            captured.update(kwargs)
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = "## Goal\nship it."
            return resp

        with patch("agent.context_compressor.call_llm", fake_call_llm):
            compressor._generate_summary(_long_turns())

        declared = captured["expected_output_tokens"]
        assert declared > 0
        assert _compression_timeout_floor(declared) >= _seconds_needed(declared)
