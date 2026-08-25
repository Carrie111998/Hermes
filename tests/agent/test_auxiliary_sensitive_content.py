"""Privacy-boundary tests for sensitive auxiliary model calls."""

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import auxiliary_client, relay_llm


_CANARY = "PRIVATE-TRANSCRIPT-CANARY-7f42"


class _CanaryTransportError(Exception):
    pass


def _response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _rendered_logs(records) -> str:
    formatter = logging.Formatter("%(levelname)s %(message)s")
    return "\n".join(formatter.format(record) for record in records)


def _pin_resolution(monkeypatch, client) -> None:
    monkeypatch.setattr(
        auxiliary_client,
        "_resolve_task_provider_model",
        lambda *_args, **_kwargs: (
            "selected-provider",
            "selected-model",
            None,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_get_cached_client",
        lambda *_args, **_kwargs: (client, "selected-model"),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_is_transient_transport_error",
        lambda exc: isinstance(exc, _CanaryTransportError),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_is_connection_error",
        lambda exc: isinstance(exc, _CanaryTransportError),
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_evict_cached_client_instance",
        lambda _client: None,
    )


def test_sensitive_sync_call_redacts_logs_bypasses_relay_and_stays_on_provider(
    monkeypatch,
    caplog,
):
    primary = MagicMock()
    primary.base_url = "https://selected.example/v1"
    primary.chat.completions.create.side_effect = _CanaryTransportError(
        f"provider echoed {_CANARY}"
    )
    fallback = MagicMock()
    fallback.chat.completions.create.return_value = _response("fallback")
    _pin_resolution(monkeypatch, primary)
    monkeypatch.setattr(auxiliary_client, "_transient_retry_count", lambda: 1)
    monkeypatch.setattr(auxiliary_client.time, "sleep", lambda _delay: None)
    relay_call = MagicMock(side_effect=AssertionError("Relay captured request"))
    monkeypatch.setattr(relay_llm, "execute_current", relay_call)
    configured_fallback = MagicMock(
        return_value=(fallback, "fallback-model", "fallback-provider")
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_try_configured_fallback_chain",
        configured_fallback,
    )

    with caplog.at_level(logging.DEBUG, logger="agent.auxiliary_client"):
        with pytest.raises(_CanaryTransportError):
            auxiliary_client.call_llm(
                task="privacy_test",
                messages=[{"role": "user", "content": _CANARY}],
                sensitive_content=True,
                allow_provider_fallback=False,
            )

    assert primary.chat.completions.create.call_count == 2
    fallback.chat.completions.create.assert_not_called()
    configured_fallback.assert_not_called()
    relay_call.assert_not_called()
    assert _CANARY not in _rendered_logs(caplog.records)
    assert any("details redacted" in record.getMessage() for record in caplog.records)
    assert auxiliary_client._RELAY_AUX_CALL_CONTEXT.get() is None


@pytest.mark.asyncio
async def test_sensitive_async_call_redacts_logs_bypasses_relay_and_stays_on_provider(
    monkeypatch,
    caplog,
):
    primary = MagicMock()
    primary.base_url = "https://selected.example/v1"
    primary.chat.completions.create = AsyncMock(
        side_effect=_CanaryTransportError(f"provider echoed {_CANARY}")
    )
    fallback = MagicMock()
    fallback.chat.completions.create = AsyncMock(return_value=_response("fallback"))
    _pin_resolution(monkeypatch, primary)
    relay_call = AsyncMock(side_effect=AssertionError("Relay captured request"))
    monkeypatch.setattr(relay_llm, "execute_current_async", relay_call)
    configured_fallback = MagicMock(
        return_value=(MagicMock(), "fallback-model", "fallback-provider")
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_try_configured_fallback_chain",
        configured_fallback,
    )
    monkeypatch.setattr(
        auxiliary_client,
        "_to_async_client",
        lambda *_args, **_kwargs: (fallback, "fallback-model"),
    )

    with caplog.at_level(logging.DEBUG, logger="agent.auxiliary_client"):
        with pytest.raises(_CanaryTransportError):
            await auxiliary_client.async_call_llm(
                task="privacy_test",
                messages=[{"role": "user", "content": _CANARY}],
                sensitive_content=True,
                allow_provider_fallback=False,
            )

    assert primary.chat.completions.create.await_count == 2
    fallback.chat.completions.create.assert_not_awaited()
    configured_fallback.assert_not_called()
    relay_call.assert_not_awaited()
    assert _CANARY not in _rendered_logs(caplog.records)
    assert any("details redacted" in record.getMessage() for record in caplog.records)
    assert auxiliary_client._RELAY_AUX_CALL_CONTEXT.get() is None


def test_sensitive_context_reaches_codex_timeout_thread(monkeypatch, caplog):
    closed = threading.Event()

    class _EmptyStream:
        def __iter__(self):
            return iter(())

        def close(self):
            return None

    class _SlowResponses:
        def create(self, **_kwargs):
            time.sleep(0.05)
            return _EmptyStream()

    def close_client():
        closed.set()
        raise RuntimeError(f"close echoed {_CANARY}")

    client = SimpleNamespace(responses=_SlowResponses(), close=close_client)
    adapter = auxiliary_client._CodexCompletionsAdapter(client, "selected-model")
    monkeypatch.setattr(
        auxiliary_client,
        "_evict_cached_client_instance",
        lambda _client: None,
    )

    @auxiliary_client._relay_auxiliary_call
    def run(*, sensitive_content=False):
        return adapter.create(
            messages=[{"role": "user", "content": _CANARY}],
            timeout=0.01,
        )

    with caplog.at_level(logging.DEBUG, logger="agent.auxiliary_client"):
        with pytest.raises(TimeoutError):
            run(sensitive_content=True)

    assert closed.wait(0.5)
    assert _CANARY not in _rendered_logs(caplog.records)
    assert any(
        "client close during timeout failed" in record.getMessage()
        for record in caplog.records
    )


def test_sensitive_provider_error_is_not_persisted_in_pool_context():
    error = _CanaryTransportError(f"provider echoed {_CANARY}")
    error.status_code = 429

    @auxiliary_client._relay_auxiliary_call
    def capture(*, sensitive_content=False):
        return auxiliary_client._pool_error_context(error)

    assert capture(sensitive_content=True) == {"status_code": 429}
    assert capture() == {
        "message": f"provider echoed {_CANARY}",
        "status_code": 429,
    }


def test_sensitive_context_redacts_nested_anthropic_provider_error(caplog):
    class _UnavailableStream:
        def __enter__(self):
            raise RuntimeError(f"stream not supported; provider echoed {_CANARY}")

        def __exit__(self, _exc_type, _exc, _tb):
            return False

    class _Messages:
        def stream(self, **_kwargs):
            return _UnavailableStream()

        def create(self, **_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                ),
            )

    adapter = auxiliary_client._AnthropicCompletionsAdapter(
        SimpleNamespace(messages=_Messages()),
        "claude-sonnet-4-6",
    )

    @auxiliary_client._relay_auxiliary_call
    def run(*, sensitive_content=False):
        return adapter.create(
            model="claude-sonnet-4-6",
            messages=[{"role": "user", "content": _CANARY}],
        )

    with caplog.at_level(logging.DEBUG, logger="agent.anthropic_adapter"):
        response = run(sensitive_content=True)

    assert response.choices[0].message.content == "ok"
    assert _CANARY not in _rendered_logs(caplog.records)
    assert any("details redacted" in record.getMessage() for record in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="agent.anthropic_adapter"):
        run()

    assert _CANARY in _rendered_logs(caplog.records)


@pytest.mark.asyncio
async def test_sensitive_context_redacts_nested_gemini_sse_content(caplog):
    from agent.gemini_native_adapter import _iter_sse_events

    class _Response:
        def iter_text(self):
            return iter(
                [
                    f"data: malformed provider echo {_CANARY}\n",
                    'data: {"candidates": []}\n',
                ]
            )

    @auxiliary_client._relay_auxiliary_call_async
    async def run(*, sensitive_content=False):
        return await asyncio.to_thread(lambda: list(_iter_sse_events(_Response())))

    with caplog.at_level(logging.DEBUG, logger="agent.gemini_native_adapter"):
        assert await run(sensitive_content=True) == [{"candidates": []}]

    assert _CANARY not in _rendered_logs(caplog.records)
    assert any("details redacted" in record.getMessage() for record in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="agent.gemini_native_adapter"):
        await run()

    assert _CANARY in _rendered_logs(caplog.records)
