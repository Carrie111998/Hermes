"""Regression tests for the generic no-first-byte TTFB watchdog.

A provider can accept a connection without emitting a stream event.  The
stale-stream detector is deliberately scaled for reasoning models, so this
watchdog supplies a separate cutoff for retrying a dead connection.

The tests import the production resolvers and state transition helper rather
than reproducing their timeout formulas here.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from agent import chat_completion_helpers as helpers
from agent.chat_completion_helpers import (
    _derive_stream_stale_timeout,
    _set_stream_ttfb_window,
    interruptible_streaming_api_call,
    resolve_stream_ttfb_timeout,
    ttfb_kill_should_fire,
)


CLOUD_URL = "https://api.openai.com/v1"


class TestTtfbResolution:
    def test_cloud_default_is_120s(self):
        assert resolve_stream_ttfb_timeout(CLOUD_URL, 0, 600.0) == 120.0

    def test_local_endpoint_disables_watchdog(self):
        assert resolve_stream_ttfb_timeout(
            "http://localhost:11434", 0, float("inf")
        ) == float("inf")
        assert resolve_stream_ttfb_timeout(
            "http://127.0.0.1:8080", 0, float("inf")
        ) == float("inf")

    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            (None, 120.0),
            ("", 120.0),
            ("  ", 120.0),
            ("not-a-number", 120.0),
            ("nan", 120.0),
            ("0", float("inf")),
            ("-1", float("inf")),
            ("inf", float("inf")),
            ("-inf", float("inf")),
            ("45", 45.0),
        ],
    )
    def test_ttfb_setting_is_normalised_without_nan(self, env_value, expected):
        actual = resolve_stream_ttfb_timeout(
            CLOUD_URL,
            est_tokens=0,
            stale_timeout=600.0,
            env_value=env_value,
        )
        assert actual == expected

    def test_large_context_scaling_is_preserved(self):
        assert resolve_stream_ttfb_timeout(CLOUD_URL, 60_000, 600.0) == 240.0
        assert resolve_stream_ttfb_timeout(CLOUD_URL, 150_000, 600.0) == 300.0

    @pytest.mark.parametrize(
        ("est_tokens", "stale_timeout", "expected"),
        [
            (0, 90.0, 89.0),
            (60_000, 240.0, 239.0),
            (150_000, 300.0, 299.0),
        ],
    )
    def test_ttfb_stays_strictly_before_finite_stale_deadline(
        self, est_tokens, stale_timeout, expected
    ):
        actual = resolve_stream_ttfb_timeout(
            CLOUD_URL, est_tokens, stale_timeout
        )
        assert actual == expected
        assert actual < stale_timeout

    @pytest.mark.parametrize("stale_timeout", [1.0, 0.5, 0.0, -1.0])
    def test_finite_small_stale_deadline_disables_ttfb(self, stale_timeout):
        assert resolve_stream_ttfb_timeout(
            CLOUD_URL, 0, stale_timeout
        ) == float("inf")

    def test_infinite_stale_keeps_cloud_ttfb_finite(self):
        assert resolve_stream_ttfb_timeout(
            CLOUD_URL, 0, float("inf")
        ) == 120.0

    def test_non_finite_stale_is_not_allowed_to_arm_ttfb(self):
        assert resolve_stream_ttfb_timeout(
            CLOUD_URL, 0, float("nan")
        ) == float("inf")
        assert resolve_stream_ttfb_timeout(
            CLOUD_URL, 0, float("-inf")
        ) == float("inf")

    def test_reasoning_floor_belongs_to_stale_not_ttfb(self, monkeypatch):
        monkeypatch.setattr(
            helpers, "get_provider_stale_timeout", lambda *_args: None
        )
        agent = SimpleNamespace(
            provider="nvidia",
            model="nvidia/nemotron-3-ultra-550b-a55b",
            base_url=CLOUD_URL,
        )

        stale_timeout = _derive_stream_stale_timeout(
            agent, {"model": agent.model}
        )
        assert stale_timeout == 600.0

        ttfb_timeout = resolve_stream_ttfb_timeout(
            CLOUD_URL, 0, stale_timeout
        )
        assert 0 < ttfb_timeout < stale_timeout


class TestTtfbKillPredicate:
    def test_no_first_byte_past_cutoff_fires(self):
        assert ttfb_kill_should_fire(False, 121.0, 120.0) is True

    def test_no_first_byte_before_cutoff_waits(self):
        assert ttfb_kill_should_fire(False, 119.0, 120.0) is False

    def test_first_byte_seen_disarms(self):
        assert ttfb_kill_should_fire(True, 500.0, 120.0) is False

    @pytest.mark.parametrize(
        "timeout", ["", "not-a-number", "nan", 0.0, -1.0, float("inf"), float("nan")]
    )
    def test_garbage_or_disabled_timeout_never_fires(self, timeout):
        assert ttfb_kill_should_fire(False, 9999.0, timeout) is False

    def test_normal_string_timeout_is_defensive_and_arms(self):
        assert ttfb_kill_should_fire(False, 46.0, "45") is True

    def test_kill_reset_suppresses_repeat_until_next_attempt(self):
        assert ttfb_kill_should_fire(False, 121.0, 120.0) is True
        assert ttfb_kill_should_fire(True, 1.0, 120.0) is False
        assert ttfb_kill_should_fire(False, 121.0, 120.0) is True


def test_set_stream_ttfb_window_updates_state_and_timestamp():
    first_event_seen = {"yes": True}
    last_chunk_time = {"t": 1.0}

    _set_stream_ttfb_window(
        first_event_seen,
        last_chunk_time,
        first_event_seen=False,
        now=42.5,
    )

    assert first_event_seen == {"yes": False}
    assert last_chunk_time == {"t": 42.5}


class _DeterministicClock:
    """Monotonic-by-call clock for the worker/poll-loop harness."""

    def __init__(self):
        self._lock = threading.Lock()
        self._value = 1_000.0

    def time(self):
        with self._lock:
            self._value += 0.1
            return self._value


class _AbortableProviderStream:
    def __init__(self, *, gate=None, chunk=None):
        self._gate = gate
        self._chunk = chunk
        self.closed = False

    def __iter__(self):
        if self._gate is not None:
            self._gate.wait()
        if self.closed:
            raise ConnectionError("provider stream closed")
        if self._chunk is not None:
            yield self._chunk

    def close(self):
        self.closed = True
        if self._gate is not None:
            self._gate.set()


class _FakeOpenAiClient:
    def __init__(self, stream):
        self.stream = stream
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: self.stream,
            )
        )


def _make_stream_watchdog_agent(clients, aborts, closes):
    agent = SimpleNamespace(
        api_mode="chat_completions",
        provider="openai",
        model="test-model",
        base_url=CLOUD_URL,
        platform="cli",
        session_id="",
        is_subagent=False,
        _fallback_index=0,
        _interrupt_requested=False,
        _consecutive_stale_streams=0,
        reasoning_callback=None,
        stream_delta_callback=None,
        interim_assistant_callback=None,
        show_commentary=True,
        _disable_streaming=False,
        _current_api_request_id=None,
    )
    agent._touch_activity = lambda *_args: None
    agent._buffer_status = lambda *_args: None
    agent._emit_wait_notice = lambda *_args: None
    agent._emit_stream_start = lambda: None
    agent._emit_stream_end = lambda **_kwargs: None
    agent._emit_stream_drop = lambda **_kwargs: None
    agent._fire_stream_delta = lambda *_args: None
    agent._fire_reasoning_delta = lambda *_args: None
    agent._fire_tool_gen_started = lambda *_args: None
    agent._has_stream_consumers = lambda: False
    agent._stream_diag_init = lambda: {}
    agent._stream_diag_capture_response = lambda *_args: None
    agent._capture_rate_limits = lambda *_args: None
    agent._capture_credits = lambda *_args: None
    agent._check_openrouter_cache_status = lambda *_args: None
    agent._is_provider_stream_parse_error = lambda *_args: False
    agent._log_stream_retry = lambda **_kwargs: None

    # The list is populated with clients before this callback is installed;
    # use a separate cursor so client creation remains deterministic.
    create_cursor = {"index": 0}

    def create_request_client(**_kwargs):
        client = clients[create_cursor["index"]]
        create_cursor["index"] += 1
        return client

    agent._create_request_openai_client = create_request_client

    def abort_request_client(client, *, reason=None):
        aborts.append(reason)
        client.stream.close()

    def close_request_client(client, *, reason=None):
        closes.append(reason)
        client.stream.close()

    agent._abort_request_openai_client = abort_request_client
    agent._close_request_openai_client = close_request_client
    return agent


def test_ttfb_kill_retries_with_fresh_window_and_counts_stale(monkeypatch):
    """The real generic entry point fences one killed attempt and retries once."""
    first_gate = threading.Event()
    completed_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content="recovered",
                    reasoning_content=None,
                    reasoning=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )
        ],
        model="test-model",
        usage=None,
    )
    streams = [
        _AbortableProviderStream(gate=first_gate),
        _AbortableProviderStream(chunk=completed_chunk),
    ]
    clients = [_FakeOpenAiClient(stream) for stream in streams]
    aborts = []
    closes = []
    agent = _make_stream_watchdog_agent(clients, aborts, closes)

    clock = _DeterministicClock()
    monkeypatch.setattr(helpers.time, "time", clock.time)
    monkeypatch.setattr(
        helpers, "get_provider_stale_timeout", lambda *_args: None
    )
    monkeypatch.setenv("HERMES_STREAM_TTFB_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")
    monkeypatch.setattr(helpers, "claim_stream_writer", lambda _agent: object())
    monkeypatch.setattr(
        helpers, "stream_writer_is_current", lambda _agent, _token: True
    )

    window_calls = []
    real_set_window = helpers._set_stream_ttfb_window

    def record_window(*args, **kwargs):
        window_calls.append(
            (kwargs["first_event_seen"], kwargs["now"])
        )
        return real_set_window(*args, **kwargs)

    monkeypatch.setattr(helpers, "_set_stream_ttfb_window", record_window)
    stale_bumps = []
    real_bump = helpers._bump_stale_streak

    def record_stale_bump(current_agent):
        stale_bumps.append(True)
        return real_bump(current_agent)

    monkeypatch.setattr(helpers, "_bump_stale_streak", record_stale_bump)

    response = interruptible_streaming_api_call(
        agent, {"model": "test-model", "messages": []}
    )

    assert response.choices[0].message.content == "recovered"
    assert len(clients) == 2
    assert aborts == ["stream_ttfb_kill"]
    assert stale_bumps == [True]
    assert [seen for seen, _now in window_calls] == [False, True, False]
    assert window_calls[2][1] > window_calls[1][1]
    # The first attempt was cancelled once; it cannot be killed again while
    # its forced close unwinds, and the second attempt gets the fresh window.
    assert aborts.count("stream_ttfb_kill") == 1


def test_stale_giveup_rejects_next_call_before_opening_client(monkeypatch):
    class Agent:
        _consecutive_stale_streams = 5
        api_mode = "chat_completions"
        provider = "openai"
        platform = "cli"
        _interrupt_requested = False

        def _create_request_openai_client(self, **_kwargs):
            raise AssertionError("give-up must happen before client creation")

    monkeypatch.setenv("HERMES_STREAM_STALE_GIVEUP", "5")

    with pytest.raises(RuntimeError, match="5 consecutive stale attempts"):
        interruptible_streaming_api_call(Agent(), {"model": "test-model"})
