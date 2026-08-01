"""Deterministic vertical-slice tests for zero-chunk stall recovery."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.provider_health_probe import ProbeOutcome
from agent.provider_stall import ProviderStalledError
from hermes_cli.timeouts import ProviderStallRecoveryConfig


def _chunk(content: str, *, finish_reason: str | None = None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                delta=SimpleNamespace(
                    content=content,
                    tool_calls=None,
                    reasoning_content=None,
                    reasoning=None,
                ),
                finish_reason=finish_reason,
            )
        ],
        model="test-model",
        usage=None,
    )


class _BlockingStream:
    def __init__(self, aborted: threading.Event, *, late_chunk: str | None = None):
        self.aborted = aborted
        self.late_chunk = late_chunk
        self._late_sent = False

    def __iter__(self):
        return self

    def __next__(self):
        assert self.aborted.wait(5), "watchdog did not abort the stalled stream"
        if self.late_chunk is not None and not self._late_sent:
            self._late_sent = True
            return _chunk(self.late_chunk, finish_reason="stop")
        raise ConnectionError("request-local transport aborted")

    def close(self):
        return None


class _GateStream:
    def __init__(self, release_chunk: threading.Event):
        self.release_chunk = release_chunk
        self._sent = False

    def __iter__(self):
        return self

    def __next__(self):
        if self._sent:
            raise StopIteration
        assert self.release_chunk.wait(5), "test did not release provider chunk"
        self._sent = True
        return _chunk("original", finish_reason="stop")

    def close(self):
        return None


def _client(stream):
    client = MagicMock()
    client.chat.completions.create.return_value = stream
    return client


@pytest.fixture
def agent():
    from run_agent import AIAgent

    value = AIAgent(
        api_key="test-key",
        base_url="https://provider.invalid/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    value.api_mode = "chat_completions"
    value._interrupt_requested = False
    return value


def _install_policy(monkeypatch, *, enabled=True, probe=True, retries=1):
    config = ProviderStallRecoveryConfig(
        enabled=enabled,
        health_probe_enabled=probe,
        health_probe_timeout_seconds=1.0,
        same_provider_retries=retries,
    )
    monkeypatch.setattr(
        "agent.chat_completion_helpers.get_provider_stall_recovery_config",
        lambda: config,
    )
    monkeypatch.setattr(
        "agent.chat_completion_helpers.get_provider_stale_timeout",
        lambda provider, model: 0.001,
    )


def test_first_zero_chunk_stall_probes_cancels_and_retries_with_fresh_client(
    agent, monkeypatch
):
    _install_policy(monkeypatch)
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    first_aborted = threading.Event()
    first_client = _client(_BlockingStream(first_aborted))
    second_client = _client(iter([_chunk("recovered", finish_reason="stop")]))
    agent._create_request_openai_client = MagicMock(
        side_effect=[first_client, second_client]
    )
    agent._abort_request_openai_client = MagicMock(
        side_effect=lambda client, reason: first_aborted.set()
    )
    statuses: list[str] = []
    agent._buffer_status = statuses.append
    probe = MagicMock(
        return_value=ProbeOutcome(
            status="reachable", http_status=401, detail="endpoint returned HTTP 401"
        )
    )
    monkeypatch.setattr(
        "agent.chat_completion_helpers.probe_provider_endpoint", probe
    )

    response = agent._interruptible_streaming_api_call({"model": "test/model"})

    assert response.choices[0].message.content == "recovered"
    assert probe.call_count == 1
    assert agent._create_request_openai_client.call_count == 2
    assert first_client is not second_client
    assert first_aborted.is_set()
    assert any(
        status
        == "Provider endpoint reachable but generation produced no chunks; reconnecting once."
        for status in statuses
    )


def test_chunk_arriving_during_probe_prevents_cancellation(agent, monkeypatch):
    _install_policy(monkeypatch)
    probe_started = threading.Event()
    release_chunk = threading.Event()
    chunk_received = threading.Event()
    release_probe = threading.Event()
    client = _client(_GateStream(release_chunk))
    agent._create_request_openai_client = MagicMock(return_value=client)
    agent._abort_request_openai_client = MagicMock()
    original_touch = agent._touch_activity

    def touch(detail):
        if detail == "receiving stream response":
            chunk_received.set()
        return original_touch(detail)

    agent._touch_activity = touch

    def probe(**kwargs):
        probe_started.set()
        assert release_probe.wait(5), "test did not release probe"
        return ProbeOutcome(status="reachable", http_status=200, detail="HTTP 200")

    monkeypatch.setattr("agent.chat_completion_helpers.probe_provider_endpoint", probe)

    def coordinate():
        assert probe_started.wait(5), "watchdog did not start probe"
        release_chunk.set()
        assert chunk_received.wait(5), "provider chunk was not accepted during probe"
        release_probe.set()

    coordinator = threading.Thread(target=coordinate)
    coordinator.start()
    response = agent._interruptible_streaming_api_call({"model": "test/model"})
    coordinator.join(5)

    assert not coordinator.is_alive()
    assert response.choices[0].message.content == "original"
    agent._abort_request_openai_client.assert_not_called()
    assert agent._create_request_openai_client.call_count == 1


def test_late_chunks_from_cancelled_stall_attempt_are_discarded(agent, monkeypatch):
    _install_policy(monkeypatch, probe=False)
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    first_aborted = threading.Event()
    clients = [
        _client(_BlockingStream(first_aborted, late_chunk="late")),
        _client(iter([_chunk("winner", finish_reason="stop")])),
    ]
    agent._create_request_openai_client = MagicMock(side_effect=clients)
    agent._abort_request_openai_client = MagicMock(
        side_effect=lambda client, reason: first_aborted.set()
    )
    deltas: list[str] = []
    agent._fire_stream_delta = deltas.append

    response = agent._interruptible_streaming_api_call({"model": "test/model"})

    assert response.choices[0].message.content == "winner"
    assert "late" not in "".join(deltas)
    assert "winner" in "".join(deltas)
    assert agent._create_request_openai_client.call_count == 2


def test_zero_chunk_stall_with_retries_disabled_is_immediately_typed(
    agent, monkeypatch
):
    _install_policy(monkeypatch, retries=0)
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "5")
    aborted = threading.Event()
    agent._create_request_openai_client = MagicMock(
        return_value=_client(_BlockingStream(aborted))
    )
    agent._abort_request_openai_client = MagicMock(
        side_effect=lambda client, reason: aborted.set()
    )
    monkeypatch.setattr(
        "agent.chat_completion_helpers.probe_provider_endpoint",
        lambda **kwargs: ProbeOutcome(status="unreachable", detail="ConnectError"),
    )

    with pytest.raises(ProviderStalledError) as caught:
        agent._interruptible_streaming_api_call({"model": "test/model"})

    assert caught.value.attempt == 1
    assert caught.value.probe.status == "unreachable"
    assert agent._create_request_openai_client.call_count == 1


def test_second_zero_chunk_stall_is_typed_without_generic_retry(
    agent, monkeypatch
):
    _install_policy(monkeypatch, retries=1)
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "5")
    first_aborted = threading.Event()
    second_aborted = threading.Event()
    clients = [
        _client(_BlockingStream(first_aborted)),
        _client(_BlockingStream(second_aborted)),
    ]
    agent._create_request_openai_client = MagicMock(side_effect=clients)

    def abort(client, reason):
        (first_aborted if client is clients[0] else second_aborted).set()

    agent._abort_request_openai_client = MagicMock(side_effect=abort)
    monkeypatch.setattr(
        "agent.chat_completion_helpers.probe_provider_endpoint",
        lambda **kwargs: ProbeOutcome(status="reachable", http_status=200),
    )

    with pytest.raises(ProviderStalledError) as caught:
        agent._interruptible_streaming_api_call({"model": "test/model"})

    assert caught.value.attempt == 2
    assert agent._create_request_openai_client.call_count == 2


def test_recovery_disabled_preserves_generic_transient_retry_behavior(
    agent, monkeypatch
):
    _install_policy(monkeypatch, enabled=False)
    monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
    aborted = threading.Event()
    agent._create_request_openai_client = MagicMock(
        return_value=_client(_BlockingStream(aborted))
    )
    agent._abort_request_openai_client = MagicMock(
        side_effect=lambda client, reason: aborted.set()
    )
    probe = MagicMock()
    monkeypatch.setattr(
        "agent.chat_completion_helpers.probe_provider_endpoint", probe
    )

    with pytest.raises(ConnectionError):
        agent._interruptible_streaming_api_call({"model": "test/model"})

    probe.assert_not_called()
    assert agent._create_request_openai_client.call_count == 1
