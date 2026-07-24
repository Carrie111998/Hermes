"""Live context gauge must climb smoothly during a turn, not jump.

Regression for the "40% then suddenly 100%" symptom: previously the
status bar only read ``context_compressor.last_prompt_tokens``, which is
updated solely when a real API call returns usage (turn boundary). During a
long streamed reply, while tools run, or across the compression-transition
turn, that value is STALE, so the gauge froze then snapped.

While a turn is in flight (``_prompt_start_time`` is set) we now estimate
the live window occupancy from ``agent.messages`` and take the max of the
two readings, so the bar rises smoothly as messages accumulate.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import cli as cli_mod
from cli import HermesCLI


def _make_cli(model: str = "tencent/hy3:free"):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.model = model
    cli_obj.session_start = datetime.now() - timedelta(minutes=14, seconds=32)
    cli_obj.conversation_history = [{"role": "user", "content": "hi"}]
    cli_obj.agent = None
    cli_obj._prompt_start_time = None
    return cli_obj


def _attach_agent_with_stale_and_messages(
    cli_obj,
    *,
    stale_context_tokens: int,
    context_length: int,
    live_estimate: int,
    messages=None,
):
    cli_obj.agent = SimpleNamespace(
        model=cli_obj.model,
        provider="nous" if cli_obj.model.startswith("tencent/") else None,
        base_url="",
        session_input_tokens=stale_context_tokens,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_prompt_tokens=stale_context_tokens,
        session_completion_tokens=0,
        session_total_tokens=stale_context_tokens,
        session_api_calls=1,
        get_rate_limit_state=lambda: None,
        messages=messages if messages is not None else [],
        _cached_system_prompt="You are a helpful assistant." * 50,
        tools=None,
        context_compressor=SimpleNamespace(
            last_prompt_tokens=stale_context_tokens,
            context_length=context_length,
            compression_count=0,
        ),
    )
    return cli_obj


class TestLiveContextGauge:
    def test_gauge_uses_live_estimate_when_turn_in_flight(self):
        """During a turn the gauge reflects the live message estimate, not the
        stale last_prompt_tokens."""
        cli_obj = _make_cli()
        cli_obj._prompt_start_time = __import__("time").time()
        # Stale compressor reading: 40K / 200K = 20%
        # Live estimate of accumulated messages: 80K / 200K = 40%
        _attach_agent_with_stale_and_messages(
            cli_obj,
            stale_context_tokens=40_000,
            context_length=200_000,
            live_estimate=80_000,
            messages=[{"role": "user", "content": "x"}] * 40,
        )
        with patch(
            "agent.model_metadata.estimate_request_tokens_rough",
            return_value=80_000,
        ):
            snap = cli_obj._get_status_bar_snapshot()
        # Must reflect the live 40%, NOT the stale 20%.
        assert snap["context_tokens"] == 80_000
        assert snap["context_percent"] == 40

    def test_gauge_falls_back_to_stale_when_no_turn(self):
        """When no turn is in flight, the gauge uses the real measured value
        (last_prompt_tokens) and does NOT invent a live estimate."""
        cli_obj = _make_cli()
        cli_obj._prompt_start_time = None  # no active turn
        _attach_agent_with_stale_and_messages(
            cli_obj,
            stale_context_tokens=40_000,
            context_length=200_000,
            live_estimate=80_000,
            messages=[{"role": "user", "content": "x"}] * 40,
        )
        with patch(
            "agent.model_metadata.estimate_request_tokens_rough",
            return_value=80_000,
        ):
            snap = cli_obj._get_status_bar_snapshot()
        # No turn -> must stay at the stale 20%, not jump to 40%.
        assert snap["context_tokens"] == 40_000
        assert snap["context_percent"] == 20

    def test_gauge_max_not_min_avoids_downward_jump(self):
        """Live estimate lower than the stale measured value must NOT pull the
        gauge down (prevents spurious downward snaps)."""
        cli_obj = _make_cli()
        cli_obj._prompt_start_time = __import__("time").time()
        _attach_agent_with_stale_and_messages(
            cli_obj,
            stale_context_tokens=80_000,
            context_length=200_000,
            live_estimate=40_000,  # live lower than stale
            messages=[{"role": "user", "content": "x"}] * 20,
        )
        with patch(
            "agent.model_metadata.estimate_request_tokens_rough",
            return_value=40_000,
        ):
            snap = cli_obj._get_status_bar_snapshot()
        # max(80K, 40K) -> stays at 80K / 40%.
        assert snap["context_tokens"] == 80_000
        assert snap["context_percent"] == 40

    def test_gauge_handles_missing_messages_gracefully(self):
        """If agent.messages is missing/empty mid-turn, fall back to the
        stale reading instead of crashing."""
        cli_obj = _make_cli()
        cli_obj._prompt_start_time = __import__("time").time()
        _attach_agent_with_stale_and_messages(
            cli_obj,
            stale_context_tokens=40_000,
            context_length=200_000,
            live_estimate=0,
            messages=None,  # signals "no messages attr useable"
        )
        # Force messages to be falsy without throwing.
        cli_obj.agent.messages = []
        with patch(
            "agent.model_metadata.estimate_request_tokens_rough",
            return_value=0,
        ):
            snap = cli_obj._get_status_bar_snapshot()
        assert snap["context_tokens"] == 40_000
        assert snap["context_percent"] == 20
