"""OpenAI-wire stream cleanup must NOT close/replace the shared OpenAI client
from the stale/interrupt/retry watchdog — the request-local client (#29507)
is the only thing that may be torn down from a request (#70773).

#67142 fixed this exact bug class for the Anthropic path (the stale watchdog
closing the shared _anthropic_client from the poll/stranger thread released a
live TLS FD that the kernel recycled onto a SQLite header, corrupting the DB).
But the OpenAI-wire path kept calling _replace_primary_openai_client() at three
cleanup sites (stale_stream / stream_retry / stream_mid_tool_retry), which swaps
self.client and closes the *old shared pool* — from the watchdog thread for the
stale site, and from a request worker for the retry sites — while sibling/
prior-attempt workers may still hold sockets from that pool. #70773 is the
production forensic report of that OpenAI-wire instance corrupting kanban.db.

Since the fix, every OpenAI-wire stream runs on a per-request client that the
watchdog aborts (stranger thread) or the owning worker closes — the shared
client is never closed from inside a request. These tests assert
_replace_primary_openai_client is never called from any of the three cleanup
sites, while the request-local abort/close still fires (no #28161 hang).

Fixes #70773. Extends #67142.
"""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_openai_agent(**kwargs):
    from run_agent import AIAgent

    defaults = dict(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    defaults.update(kwargs)
    agent = AIAgent(**defaults)
    # OpenAI-wire (chat_completions) — the custom-provider path in #70773.
    agent.api_mode = "chat_completions"
    return agent


def _chunk(content=None, tool_calls=None, finish_reason=None, model=None):
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=None,
        reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=None)


def _tool_call_delta(index=0, tc_id=None, name=None, arguments=None):
    func = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=tc_id, function=func)


class _Stream:
    """Minimal OpenAI-wire stream: an iterable with a .response (for diag
    header snapshotting). Deliberately NOT a MagicMock so it exposes no
    ``choices`` attribute (which would trip the non-iterator fast path)."""

    def __init__(self, chunks_or_gen):
        self.response = SimpleNamespace(headers={})
        self._src = chunks_or_gen

    def __iter__(self):
        src = self._src
        return src() if callable(src) else iter(src)


def _good_chunks():
    return [
        _chunk(content="ok final"),
        _chunk(finish_reason="stop", model="test/model"),
    ]


class TestOpenAIWireStreamPoolCleanup:
    """OpenAI-wire cleanup must never close/replace the shared OpenAI client;
    only the request-local client is torn down (#70773 / #67142 / #29507)."""

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    @patch("run_agent.AIAgent._abort_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    def test_stream_retry_does_not_replace_shared_client(
        self, mock_create, mock_close, mock_abort, mock_replace, monkeypatch
    ):
        """Transient ConnectError on a fresh stream → retry on a new
        request-local client; the shared client is NEVER replaced (that was the
        stream_retry_pool_cleanup FD-recycle vector), and the worker closes its
        own request client from its own thread."""
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")

        agent = _make_openai_agent()
        mock_client = MagicMock()
        mock_create.return_value = mock_client

        attempt = [0]

        def _create_side_effect(**kwargs):
            attempt[0] += 1
            if attempt[0] == 1:
                raise httpx.ConnectError("connection reset by peer")
            return _Stream(_good_chunks())

        mock_client.chat.completions.create.side_effect = _create_side_effect

        agent._interrupt_requested = False
        response = agent._interruptible_streaming_api_call({})

        assert response is not None
        assert attempt[0] == 2  # failed once, retried, succeeded
        # THE fix: shared OpenAI client never swapped/closed from the retry site.
        mock_replace.assert_not_called()
        # The stale request client is closed by its owning worker thread.
        mock_close.assert_called()

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    @patch("run_agent.AIAgent._abort_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    def test_stale_stream_aborts_request_client_not_shared(
        self, mock_create, mock_close, mock_abort, mock_replace, monkeypatch
    ):
        """Stale-stream outer-poll detector → aborts the request-local client's
        socket from the poll (stranger) thread and retries; it must NEVER call
        _replace_primary_openai_client (the #70773 corruption vector: closing
        the shared pool from the watchdog thread while a worker unwinds)."""
        monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.1")
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")

        agent = _make_openai_agent()
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        unblock = threading.Event()
        attempt = [0]

        def _create_side_effect(**kwargs):
            attempt[0] += 1
            if attempt[0] == 1:
                def _blocking_gen():
                    # Yields nothing → trips the stale detector; unblocks only
                    # when the poll thread aborts the request client's socket.
                    unblock.wait(timeout=5.0)
                    raise httpx.ConnectError("connection dropped after abort")
                    yield  # make this a generator
                return _Stream(_blocking_gen)
            return _Stream(_good_chunks())

        mock_client.chat.completions.create.side_effect = _create_side_effect
        # Poll thread aborts the request-local client's socket (not close() on
        # the shared client); simulate the shutdown waking the blocked read.
        mock_abort.side_effect = lambda *a, **k: unblock.set()

        agent._interrupt_requested = False
        response = agent._interruptible_streaming_api_call({})

        assert response is not None
        assert attempt[0] >= 2  # stale-killed once, then retried
        mock_replace.assert_not_called()
        # The poll (stranger) thread aborted the request-local client's socket.
        assert mock_abort.called

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    @patch("run_agent.AIAgent._abort_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    def test_mid_tool_retry_does_not_replace_shared_client(
        self, mock_create, mock_close, mock_abort, mock_replace, monkeypatch
    ):
        """A mid-tool-call transient drop (deltas already sent + tool in-flight)
        triggers the stream_mid_tool_retry cleanup + silent retry. That site
        must NOT replace the shared OpenAI client either (#70773)."""
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")

        agent = _make_openai_agent()
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        attempt = [0]

        def _create_side_effect(**kwargs):
            attempt[0] += 1
            if attempt[0] == 1:
                def _gen():
                    # Deliver a tool-call delta (marks a tool in-flight and
                    # sets deltas_were_sent), then drop the connection.
                    yield _chunk(
                        tool_calls=[
                            _tool_call_delta(index=0, tc_id="call_1", name="terminal")
                        ]
                    )
                    raise httpx.RemoteProtocolError("peer closed connection")
                return _Stream(_gen)
            return _Stream(_good_chunks())

        mock_client.chat.completions.create.side_effect = _create_side_effect

        agent._interrupt_requested = False
        agent._interruptible_streaming_api_call({})

        assert attempt[0] == 2  # mid-tool drop, then silent retry
        mock_replace.assert_not_called()
        mock_close.assert_called()


