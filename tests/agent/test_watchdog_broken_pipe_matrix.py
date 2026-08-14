from __future__ import annotations

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from agent import agent_runtime_helpers as runtime_helpers
from agent import chat_completion_helpers as cch


class BrokenPipeCausalityMatrix(unittest.TestCase):
    def agent(self):
        a = MagicMock()
        a.api_mode = "codex_responses"
        a.provider = "openai-codex"
        a.platform = "cli"
        a.is_subagent = False
        a._interrupt_requested = False
        a._base_url_hostname = "chatgpt.com"
        a._base_url_lower = "https://chatgpt.com/backend-api/codex"
        a._compute_non_stream_stale_timeout.return_value = 30.0
        a._codex_silent_hang_hint.return_value = None
        a._create_request_openai_client.return_value = MagicMock(name="client")
        a._close_request_openai_client = MagicMock()
        return a

    def run_watchdog_error(self, error, *, watchdog="ttfb"):
        a = self.agent()
        aborted = threading.Event()
        if watchdog == "stale":
            a._compute_non_stream_stale_timeout.return_value = 0.05
        def abort(_client, *, reason):
            aborted.set()
            return 1
        def stream(*_args, **_kwargs):
            if watchdog == "idle":
                a._codex_stream_last_event_ts = time.time()
            self.assertTrue(aborted.wait(3.0))
            raise error
        a._abort_request_openai_client.side_effect = abort
        a._run_codex_stream.side_effect = stream
        env = {
            "HERMES_CODEX_TTFB_TIMEOUT_SECONDS": "0.05" if watchdog == "ttfb" else "0",
            "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS": "0.05" if watchdog == "idle" else "0",
        }
        return a, env, lambda: cch.interruptible_api_call(a, {"model":"gpt-test","input":"x"})

    def test_ttfb_post_abort_readerror_broken_pipe_translates(self):
        original = httpx.ReadError("[Errno 32] Broken pipe")
        _a, env, call = self.run_watchdog_error(original)
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(TimeoutError) as caught:
                call()
        self.assertIs(caught.exception.__cause__, original)
        self.assertIn("codex_ttfb_kill", str(caught.exception))

    def test_idle_post_abort_readerror_broken_pipe_translates(self):
        original = httpx.ReadError("[Errno 32] Broken pipe")
        _a, env, call = self.run_watchdog_error(original, watchdog="idle")
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(TimeoutError) as caught:
                call()
        self.assertIs(caught.exception.__cause__, original)
        self.assertIn("codex_stream_idle_kill", str(caught.exception))

    def test_stale_post_abort_readerror_broken_pipe_translates(self):
        original = httpx.ReadError("[Errno 32] Broken pipe")
        _a, env, call = self.run_watchdog_error(original, watchdog="stale")
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(TimeoutError) as caught:
                call()
        self.assertIs(caught.exception.__cause__, original)
        self.assertIn("stale_call_kill", str(caught.exception))

    def test_broken_pipe_recorded_before_abort_helper_returns_translates(self):
        """shutdown may wake the worker before the abort helper returns."""
        a = self.agent()
        original = httpx.ReadError("[Errno 32] Broken pipe")
        shutdown_effect_seen = threading.Event()
        worker_error_timestamped = threading.Event()
        main_tid = threading.get_ident()
        real_monotonic = time.monotonic

        def monotonic():
            value = real_monotonic()
            if threading.get_ident() != main_tid:
                worker_error_timestamped.set()
            return value

        def abort(_client, *, reason):
            shutdown_effect_seen.set()
            self.assertTrue(worker_error_timestamped.wait(3.0))
            return 1

        def stream(*_args, **_kwargs):
            self.assertTrue(shutdown_effect_seen.wait(3.0))
            raise original

        a._abort_request_openai_client.side_effect = abort
        a._run_codex_stream.side_effect = stream
        with patch.object(cch.time, "monotonic", side_effect=monotonic), patch.dict(
            os.environ,
            {
                "HERMES_CODEX_TTFB_TIMEOUT_SECONDS": "0.05",
                "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS": "0",
            },
            clear=False,
        ):
            with self.assertRaises(TimeoutError) as caught:
                cch.interruptible_api_call(
                    a, {"model": "gpt-test", "input": "x"}
                )
        self.assertIs(caught.exception.__cause__, original)
        self.assertIn("codex_ttfb_kill", str(caught.exception))

    def test_force_close_counts_only_successful_shutdowns(self):
        failed = MagicMock()
        failed.shutdown.side_effect = OSError("already closed")
        succeeded = MagicMock()
        with patch.object(
            runtime_helpers,
            "_iter_pool_sockets",
            return_value=[failed, succeeded],
        ):
            count = runtime_helpers.force_close_tcp_sockets(object())
        self.assertEqual(count, 1)
        failed.shutdown.assert_called_once()
        succeeded.shutdown.assert_called_once()

    def test_active_only_shutdown_skips_idle_pool_socket(self):
        idle_socket = MagicMock()
        active_socket = MagicMock()
        idle_stream = SimpleNamespace(_sock=idle_socket)
        active_stream = SimpleNamespace(_sock=active_socket)
        idle_inner = SimpleNamespace(
            _connection=None,
            _network_stream=idle_stream,
            _stream=None,
        )
        active_inner = SimpleNamespace(
            _connection=None,
            _network_stream=active_stream,
            _stream=None,
        )
        idle_connection = SimpleNamespace(
            is_idle=lambda: True,
            _connection=idle_inner,
        )
        active_connection = SimpleNamespace(
            is_idle=lambda: False,
            _connection=active_inner,
        )
        pool = SimpleNamespace(
            _connections=[idle_connection, active_connection]
        )
        with patch.object(
            runtime_helpers,
            "_iter_httpx_pool_objects",
            return_value=[pool],
        ):
            count = runtime_helpers.force_close_tcp_sockets(
                object(), active_only=True
            )
        self.assertEqual(count, 1)
        idle_socket.shutdown.assert_not_called()
        active_socket.shutdown.assert_called_once()

    def test_openai_and_anthropic_abort_entrypoints_skip_idle_pool_socket(self):
        from run_agent import AIAgent

        def client_with_pool():
            idle_socket = MagicMock()
            active_socket = MagicMock()
            idle_connection = SimpleNamespace(
                is_idle=lambda: True,
                _connection=SimpleNamespace(
                    _connection=None,
                    _network_stream=SimpleNamespace(_sock=idle_socket),
                    _stream=None,
                ),
            )
            active_connection = SimpleNamespace(
                is_idle=lambda: False,
                _connection=SimpleNamespace(
                    _connection=None,
                    _network_stream=SimpleNamespace(_sock=active_socket),
                    _stream=None,
                ),
            )
            pool = SimpleNamespace(
                _connections=[idle_connection, active_connection]
            )
            http_client = SimpleNamespace(
                _transport=SimpleNamespace(_pool=pool),
                _mounts={},
            )
            return SimpleNamespace(_client=http_client), idle_socket, active_socket

        agent = AIAgent.__new__(AIAgent)
        setattr(agent, "provider", "test")
        setattr(agent, "base_url", "https://example.test/v1")
        setattr(agent, "model", "test-model")

        for transport in ("openai", "anthropic"):
            with self.subTest(transport=transport):
                client, idle_socket, active_socket = client_with_pool()
                if transport == "openai":
                    count = agent._abort_request_openai_client(
                        client, reason="test_abort"
                    )
                else:
                    count = agent._abort_request_anthropic_client(
                        client, reason="test_abort"
                    )
                self.assertEqual(count, 1)
                idle_socket.shutdown.assert_not_called()
                active_socket.shutdown.assert_called_once()

    def test_force_close_all_oserrors_reports_zero(self):
        failed = MagicMock()
        failed.shutdown.side_effect = OSError("already closed")
        with patch.object(
            runtime_helpers,
            "_iter_pool_sockets",
            return_value=[failed],
        ):
            count = runtime_helpers.force_close_tcp_sockets(object())
        self.assertEqual(count, 0)
        failed.shutdown.assert_called_once()

    def test_abort_without_socket_shutdown_preserves_readerror(self):
        a = self.agent()
        original = httpx.ReadError("[Errno 32] Broken pipe")
        aborted = threading.Event()

        def abort(_client, *, reason):
            aborted.set()
            return 0

        def stream(*_args, **_kwargs):
            self.assertTrue(aborted.wait(3.0))
            raise original

        a._abort_request_openai_client.side_effect = abort
        a._run_codex_stream.side_effect = stream
        with patch.dict(os.environ, {
            "HERMES_CODEX_TTFB_TIMEOUT_SECONDS":"0.05",
            "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS":"0",
        }, clear=False):
            with self.assertRaises(httpx.ReadError) as caught:
                cch.interruptible_api_call(a,{"model":"gpt-test","input":"x"})
        self.assertIs(caught.exception, original)

    def test_custom_readerror_with_broken_pipe_text_is_preserved(self):
        class ReadError(Exception):
            pass

        a = self.agent()
        original = ReadError("[Errno 32] Broken pipe")
        _a, env, call = self.run_watchdog_error(original)
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(ReadError) as caught:
                call()
        self.assertIs(caught.exception, original)

    def test_custom_httpx_prefixed_readerror_is_preserved(self):
        ReadError = type("ReadError", (Exception,), {"__module__": "httpx_fake"})
        original = ReadError("[Errno 32] Broken pipe")
        _a, env, call = self.run_watchdog_error(original)
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(ReadError) as caught:
                call()
        self.assertIs(caught.exception, original)

    def test_nonexact_case_or_whitespace_messages_are_preserved(self):
        for message in (
            "[errno 32] broken pipe",
            " [Errno 32] Broken pipe",
            "[Errno 32] Broken pipe ",
        ):
            with self.subTest(message=message):
                original = httpx.ReadError(message)
                _a, env, call = self.run_watchdog_error(original)
                with patch.dict(os.environ, env, clear=False):
                    with self.assertRaises(httpx.ReadError) as caught:
                        call()
                self.assertIs(caught.exception, original)

    def test_misleading_broken_pipe_text_is_preserved(self):
        a = self.agent()
        original = httpx.ReadError("proxy said broken pipe while validating")
        _a, env, call = self.run_watchdog_error(original)
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(httpx.ReadError) as caught:
                call()
        self.assertIs(caught.exception, original)

    def test_post_abort_other_readerror_is_preserved(self):
        original = httpx.ReadError("connection reset")
        _a, env, call = self.run_watchdog_error(original)
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(httpx.ReadError) as caught:
                call()
        self.assertIs(caught.exception, original)

    def test_post_abort_bare_brokenpipe_is_preserved(self):
        original = BrokenPipeError(32, "Broken pipe")
        _a, env, call = self.run_watchdog_error(original)
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(BrokenPipeError) as caught:
                call()
        self.assertIs(caught.exception, original)

    def test_interrupt_has_priority_when_watchdog_also_eligible(self):
        a = self.agent()
        dispatch_started = threading.Event()

        abort_seen = threading.Event()

        def stream(*_args, **_kwargs):
            dispatch_started.set()
            self.assertTrue(abort_seen.wait(3.0))
            raise httpx.ReadError("[Errno 32] Broken pipe")

        def abort(_client, *, reason):
            abort_seen.set()
            return 1

        a._run_codex_stream.side_effect = stream
        a._abort_request_openai_client.side_effect = abort
        timer = threading.Timer(
            0.02, setattr, args=(a, "_interrupt_requested", True)
        )
        timer.start()
        try:
            with patch.dict(os.environ, {
                "HERMES_CODEX_TTFB_TIMEOUT_SECONDS":"0.001",
                "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS":"0",
            }, clear=False):
                with self.assertRaises(InterruptedError):
                    cch.interruptible_api_call(a,{"model":"gpt-test","input":"x"})
        finally:
            timer.cancel()
        self.assertTrue(dispatch_started.is_set())
        a._abort_request_openai_client.assert_called_once()
        self.assertEqual(
            a._abort_request_openai_client.call_args.kwargs["reason"],
            "interrupt_abort",
        )

    def test_interrupt_after_worker_completion_still_has_priority(self):
        a = self.agent()
        response = object()
        a._run_codex_stream.return_value = response

        class CompletedThenInterruptedThread:
            def __init__(self, *, target, daemon):
                self._target = target
                self.daemon = daemon

            def start(self):
                self._target()
                a._interrupt_requested = True

            def is_alive(self):
                return False

        with patch.object(cch.threading, "Thread", CompletedThenInterruptedThread):
            with self.assertRaises(InterruptedError):
                cch.interruptible_api_call(
                    a, {"model": "gpt-test", "input": "x"}
                )
        a._run_codex_stream.assert_called_once()

    def test_response_completed_during_watchdog_join_is_returned(self):
        a = self.agent()
        aborted = threading.Event()
        response = object()

        def abort(_client, *, reason):
            aborted.set()
            return 1

        def stream(*_args, **_kwargs):
            self.assertTrue(aborted.wait(3.0))
            return response

        a._abort_request_openai_client.side_effect = abort
        a._run_codex_stream.side_effect = stream
        with patch.dict(os.environ, {
            "HERMES_CODEX_TTFB_TIMEOUT_SECONDS":"0.05",
            "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS":"0",
        }, clear=False):
            actual = cch.interruptible_api_call(
                a, {"model":"gpt-test","input":"x"}
            )
        self.assertIs(actual, response)

    def test_spontaneous_readerror_racing_later_watchdog_is_preserved(self):
        a = self.agent()
        original = httpx.ReadError("[Errno 32] Broken pipe")
        def stream(*_args, **_kwargs):
            raise original

        def slow_close(_client, *, reason):
            time.sleep(0.5)

        a._run_codex_stream.side_effect = stream
        a._close_request_openai_client.side_effect = slow_close
        with patch.dict(os.environ, {
            "HERMES_CODEX_TTFB_TIMEOUT_SECONDS":"0.05",
            "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS":"0",
        }, clear=False):
            with self.assertRaises(httpx.ReadError) as caught:
                cch.interruptible_api_call(a,{"model":"gpt-test","input":"x"})
        self.assertIs(caught.exception, original)

    def test_spontaneous_readerror_broken_pipe_is_preserved(self):
        a = self.agent()
        original = httpx.ReadError("[Errno 32] Broken pipe")
        a._run_codex_stream.side_effect = original
        with patch.dict(os.environ, {
            "HERMES_CODEX_TTFB_TIMEOUT_SECONDS":"30",
            "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS":"30",
        }, clear=False):
            with self.assertRaises(httpx.ReadError) as caught:
                cch.interruptible_api_call(a,{"model":"gpt-test","input":"x"})
        self.assertIs(caught.exception, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
