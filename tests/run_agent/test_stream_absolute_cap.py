"""HERMES_STREAM_MAX_SECONDS: absolute wall-clock cap on a streaming call.

The stale watchdog only measures the gap since the LAST chunk, so a socket
that returns headers and then blocks forever (or dribbles a chunk just
under the stale threshold) never trips it and the call runs unbounded.
The absolute cap measures from call start, fires once, and force-closes
the request-local client exactly like the stale branch — landing the
worker in the existing retry/fallback path instead of raising from the
poll thread.
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from tests.run_agent.test_streaming import _make_stream_chunk


def _make_agent():
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://custom.example.com/v1",
        provider="custom",
        model="deepseek-v4-pro",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


class TestAbsoluteWallClockCap:
    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    @patch("run_agent.AIAgent._abort_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_cap_fires_when_stale_detector_cannot(
        self, mock_close, mock_create, mock_abort, mock_replace, monkeypatch,
    ):
        """Stale timeout far above test time + a stream that blocks after
        connecting → only the absolute cap can end the call; the worker's
        forced transport error then rides the normal retry to recovery."""
        monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "60")
        monkeypatch.setenv("HERMES_STREAM_MAX_SECONDS", "0.3")
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")

        unblock = threading.Event()

        class BlockedStream:
            response = SimpleNamespace(headers={})

            def __iter__(self):
                # Connected (headers back) but never a chunk: invisible to
                # the stale detector inside this test's lifetime.
                unblock.wait(timeout=5.0)
                raise httpx.ConnectError("connection dropped after abort")
                yield  # pragma: no cover — make this a generator

        retry_chunks = [
            _make_stream_chunk(content="recovered"),
            _make_stream_chunk(finish_reason="stop", model="test/model"),
        ]

        class RetryStream:
            response = SimpleNamespace(headers={})

            def __iter__(self):
                return iter(retry_chunks)

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            BlockedStream(),
            RetryStream(),
        ]
        mock_create.return_value = mock_client
        mock_abort.side_effect = lambda *a, **k: unblock.set()

        agent = _make_agent()
        response = agent._interruptible_streaming_api_call({})

        # The cap aborted the doomed first attempt and the retry recovered.
        assert response.choices[0].message.content == "recovered"
        assert mock_abort.call_count >= 1
        # Same thread-ownership rule as the stale branch (#67142/#70773):
        # the poll thread never replaces/closes the shared primary client.
        mock_replace.assert_not_called()

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    @patch("run_agent.AIAgent._abort_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_default_cap_does_not_disturb_fast_streams(
        self, mock_close, mock_create, mock_abort, mock_replace, monkeypatch,
    ):
        """With the 900s default cap, a normal fast stream completes with no
        abort — the watchdog is inert on the happy path."""
        monkeypatch.delenv("HERMES_STREAM_MAX_SECONDS", raising=False)

        chunks = [
            _make_stream_chunk(content="hello"),
            _make_stream_chunk(finish_reason="stop", model="test/model"),
        ]

        class FastStream:
            response = SimpleNamespace(headers={})

            def __iter__(self):
                return iter(chunks)

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = FastStream()
        mock_create.return_value = mock_client

        agent = _make_agent()
        response = agent._interruptible_streaming_api_call({})

        assert response.choices[0].message.content == "hello"
        assert mock_abort.call_count == 0
