"""#70773: the shared OpenAI-wire client must never be pool-closed from a
non-owner thread.

A custom OpenAI-compatible provider reproduced the #29507/#67142 TLS-FD →
SQLite corruption shape on v0.18.2: the streaming stale watchdog (poll
thread) called ``_replace_primary_openai_client(reason=
"stale_stream_pool_cleanup")``, which hard-closed the old shared client's
connection pool while worker threads from previous stale-killed attempts
were still unwinding their SSL BIOs. The kernel recycled a just-released
TLS FD onto ``kanban.db`` and the unwinding TLS flush wrote a 24-byte
application-data record over the SQLite header.

Fix (mirrors the #67142 Anthropic ownership discipline):

1. The three in-request cleanup sites (stale watchdog, mid-tool retry,
   stream retry) no longer touch the shared client at all — the
   request-local client is closed via ``_close_request_client_once`` and
   the shared client is replaced lazily by ``_ensure_primary_openai_client``
   on the next request, on the requesting thread.
2. ``_replace_primary_openai_client`` (still used by credential rotation,
   refresh, and dead-connection cleanup) retires the old client instead of
   hard-closing it: sockets are ``shutdown()`` (FD-safe from any thread),
   FD release is deferred to GC so it cannot happen under a borrowing
   thread's live SSL BIO.
"""
import socket
import struct
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


class TestStaleWatchdogNeverClosesSharedClient:
    """The stale watchdog / retry cleanups must not rebuild (and therefore
    must not close) the shared OpenAI client from inside a request."""

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    @patch("run_agent.AIAgent._abort_request_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_stale_stream_kill_does_not_replace_primary(
        self, mock_close, mock_create, mock_abort, mock_replace, monkeypatch,
    ):
        """Stale-stream watchdog fires → request-local client is aborted, the
        shared primary is never replaced (poll thread must not close it)."""
        monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", "0.05")
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")

        unblock = threading.Event()

        class StaleThenDeadStream:
            response = SimpleNamespace(headers={})

            def __iter__(self):
                # Yield nothing; block until the watchdog aborts the request
                # client, then surface the transport error.
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
            StaleThenDeadStream(),
            RetryStream(),
        ]
        mock_create.return_value = mock_client
        mock_abort.side_effect = lambda *a, **k: unblock.set()

        agent = _make_agent()
        response = agent._interruptible_streaming_api_call({})

        assert response.choices[0].message.content == "recovered"
        # The watchdog aborted the request-local client from the poll thread…
        assert mock_abort.call_count >= 1
        # …and NEVER replaced/closed the shared primary client (#70773).
        mock_replace.assert_not_called()

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_stream_retry_cleanup_does_not_replace_primary(
        self, mock_close, mock_create, mock_replace, monkeypatch,
    ):
        """Connection drop before first delta → retry path closes only the
        request-local client; the shared primary is left alone."""
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "1")

        attempts = {"n": 0}

        def _pick_stream(*a, **kw):
            attempts["n"] += 1
            if attempts["n"] == 1:
                def _dead():
                    raise httpx.ConnectError("connection reset by peer")
                    yield  # pragma: no cover
                return _dead()
            return iter([
                _make_stream_chunk(content="ok"),
                _make_stream_chunk(finish_reason="stop", model="test/model"),
            ])

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _pick_stream
        mock_create.return_value = mock_client

        agent = _make_agent()
        response = agent._interruptible_streaming_api_call({})

        assert attempts["n"] == 2
        assert response.choices[0].message.content == "ok"
        # Request-local cleanup ran; shared client untouched (#70773).
        assert mock_close.call_count >= 1
        mock_replace.assert_not_called()

    @pytest.mark.filterwarnings(
        "ignore::pytest.PytestUnhandledThreadExceptionWarning"
    )
    @patch("run_agent.AIAgent._replace_primary_openai_client")
    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_mid_tool_partial_cleanup_does_not_replace_primary(
        self, mock_close, mock_create, mock_replace, monkeypatch,
    ):
        """Connection drop mid tool-call preserves a partial and closes only
        the request-local client; the shared primary is left alone."""
        from tests.run_agent.test_streaming import _make_tool_call_delta

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")

        attempts = {"n": 0}

        def _first_stream():
            yield _make_stream_chunk(content="Working: ")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_1", name="terminal"),
            ])
            raise httpx.RemoteProtocolError("peer closed connection")

        def _second_stream():
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_1", name="terminal"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments='{"command": "ls"}'),
            ])
            yield _make_stream_chunk(finish_reason="tool_calls")

        def _pick_stream(*a, **kw):
            attempts["n"] += 1
            return _first_stream() if attempts["n"] == 1 else _second_stream()

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _pick_stream
        mock_create.return_value = mock_client

        agent = _make_agent()
        response = agent._interruptible_streaming_api_call({})

        assert attempts["n"] == 1
        assert response.choices[0].message.tool_calls is None
        assert getattr(response, "_stream_no_retry", False) is True
        assert "Stream stalled" in (response.choices[0].message.content or "")
        mock_replace.assert_not_called()


class TestReplacePrimaryRetiresInsteadOfClosing:
    """_replace_primary_openai_client (credential rotation / refresh /
    dead-connection cleanup) must retire the old shared client — shutdown
    sockets, defer FD release to GC — never hard-close its pool."""

    def test_replace_retires_old_client_without_close(self):
        agent = _make_agent()

        old_client = MagicMock()
        agent.client = old_client
        agent._client_kwargs = {
            "api_key": "test-key",
            "base_url": "https://custom.example.com/v1",
        }

        shutdown_calls = []
        with patch.object(
            agent, "_force_close_tcp_sockets",
            side_effect=lambda c: shutdown_calls.append(c) or 1,
        ):
            with patch("run_agent.OpenAI", MagicMock()):
                ok = agent._replace_primary_openai_client(reason="test_rotate")

        assert ok
        # Sockets were shut down (FD-safe from any thread)…
        assert shutdown_calls == [old_client]
        # …but the old client's pool was NOT hard-closed: no thread owns a
        # replaced shared client, so nobody may release its FDs (#70773).
        old_client.close.assert_not_called()

    def test_retire_helper_never_calls_close(self):
        agent = _make_agent()
        client = MagicMock()

        with patch.object(agent, "_force_close_tcp_sockets", return_value=2):
            agent._retire_shared_openai_client(client, reason="unit_test")

        client.close.assert_not_called()

    def test_retire_helper_none_is_noop(self):
        agent = _make_agent()
        # Must not raise.
        agent._retire_shared_openai_client(None, reason="unit_test")


class TestRealSocketTerminationSignals:
    """Drive real incomplete chunked reads through the wrapper."""

    @pytest.mark.parametrize("termination", ["fin", "rst"])
    def test_partial_sse_fin_or_rst_does_not_retry(
        self, termination, monkeypatch,
    ):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            listener.close()
            pytest.skip("sandbox forbids loopback bind for the RST probe")
        listener.listen(1)
        listener.settimeout(0.2)
        port = listener.getsockname()[1]
        attempts = []
        server_errors = []
        stop = threading.Event()
        delta_seen = threading.Event()

        payload = (
            b'data: {"id":"chatcmpl-real","object":"chat.completion.chunk",'
            b'"created":1,"model":"test/model","choices":[{"index":0,'
            b'"delta":{"role":"assistant","content":"partial over socket"},'
            b'"finish_reason":null}]}\n\n'
        )
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Transfer-Encoding: chunked\r\n"
            b"Connection: close\r\n\r\n"
            + f"{len(payload):X}\r\n".encode()
            + payload
            + b"\r\n"
        )

        def _serve():
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                attempts.append(1)
                try:
                    peer.settimeout(2)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += peer.recv(4096)
                    headers, body = request.split(b"\r\n\r\n", 1)
                    content_length = 0
                    for line in headers.split(b"\r\n")[1:]:
                        name, _, value = line.partition(b":")
                        if name.lower() == b"content-length":
                            content_length = int(value.strip())
                            break
                    while len(body) < content_length:
                        body += peer.recv(4096)
                    peer.sendall(response)
                    delta_seen.wait(timeout=2)
                    if termination == "rst":
                        peer.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_LINGER,
                            struct.pack("ii", 1, 0),
                        )
                    else:
                        peer.shutdown(socket.SHUT_WR)
                except Exception as exc:  # pragma: no cover - diagnostic only
                    server_errors.append(exc)
                finally:
                    peer.close()

        server = threading.Thread(target=_serve, daemon=True)
        server.start()

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        monkeypatch.setenv("no_proxy", "127.0.0.1")
        from openai import OpenAI

        client = OpenAI(
            api_key="test-key",
            base_url=f"http://127.0.0.1:{port}/v1",
            max_retries=0,
        )
        agent = _make_agent()
        agent._create_request_openai_client = lambda **_kwargs: client
        agent._close_request_openai_client = (
            lambda request_client, **_kwargs: request_client.close()
        )
        agent.stream_delta_callback = lambda _text: delta_seen.set()

        try:
            wrapper_response = agent._interruptible_streaming_api_call({
                "model": "test/model",
                "messages": [{"role": "user", "content": "test"}],
            })
        finally:
            stop.set()
            listener.close()
            server.join(timeout=2)

        assert server_errors == []
        assert len(attempts) == 1
        assert wrapper_response.choices[0].message.content == "partial over socket"
        assert wrapper_response.choices[0].message.tool_calls is None
        assert getattr(wrapper_response, "_stream_no_retry", False) is True
