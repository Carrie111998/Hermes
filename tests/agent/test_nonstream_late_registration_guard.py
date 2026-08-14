"""Regression tests for request-client registration racing terminal watchdogs.

A watchdog or interrupt may win while the worker is still constructing its
request-local client. Once that terminal transition wins, a subsequently
registered client must be aborted before provider dispatch, and owner-thread
cleanup must still run.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from agent import chat_completion_helpers as cch


class NonstreamLateRegistrationGuardTests(unittest.TestCase):
    def _make_codex_agent(self) -> MagicMock:
        agent = MagicMock()
        agent.api_mode = "codex_responses"
        agent.provider = "openai-codex"
        agent.platform = "cli"
        agent.is_subagent = False
        agent._interrupt_requested = False
        agent._base_url_hostname = "chatgpt.com"
        agent._base_url_lower = "https://chatgpt.com/backend-api/codex"
        agent._compute_non_stream_stale_timeout.return_value = 30.0
        agent._codex_silent_hang_hint.return_value = None
        agent._create_request_openai_client = MagicMock()
        agent._abort_request_openai_client = MagicMock()
        agent._close_request_openai_client = MagicMock()
        agent._run_codex_stream = MagicMock(return_value=object())
        return agent

    @staticmethod
    def _wait_for(predicate, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("condition was not reached before timeout")

    def _run_registration_blocked_until_after_watchdog(
        self,
        *,
        watchdog: str,
    ) -> tuple[MagicMock, MagicMock]:
        agent = self._make_codex_agent()
        client = MagicMock(name=f"{watchdog}_request_client")
        release_registration = threading.Event()
        creation_started = threading.Event()

        def create_client(**_kwargs):
            creation_started.set()
            if watchdog == "sse_idle":
                agent._codex_stream_last_event_ts = time.time()
            self.assertTrue(release_registration.wait(3.0))
            return client

        agent._create_request_openai_client.side_effect = create_client

        def release_after_terminal_transition(message):
            # Both watchdog branches touch activity only after committing the
            # terminal marker under request_client_lock.
            if "killed after" in str(message):
                release_registration.set()

        agent._touch_activity.side_effect = release_after_terminal_transition
        env = {
            "HERMES_CODEX_TTFB_TIMEOUT_SECONDS": "0.001",
            "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS": "0.001",
        }

        try:
            with patch.dict(os.environ, env, clear=False):
                with self.assertRaises(TimeoutError):
                    cch.interruptible_api_call(
                        agent, {"model": "gpt-test", "input": "hi"}
                    )
        finally:
            release_registration.set()

        self.assertTrue(creation_started.is_set())
        self._wait_for(lambda: agent._close_request_openai_client.called)
        return agent, client

    def test_ttfb_watchdog_before_registration_blocks_late_dispatch_and_cleans_up(self):
        agent, client = self._run_registration_blocked_until_after_watchdog(
            watchdog="ttfb"
        )

        agent._run_codex_stream.assert_not_called()
        agent._abort_request_openai_client.assert_called_once_with(
            client, reason="codex_ttfb_kill"
        )
        agent._close_request_openai_client.assert_called_once_with(
            client, reason="request_error_cleanup"
        )

    def test_sse_idle_watchdog_before_registration_blocks_late_dispatch(self):
        agent, client = self._run_registration_blocked_until_after_watchdog(
            watchdog="sse_idle"
        )

        agent._run_codex_stream.assert_not_called()
        agent._abort_request_openai_client.assert_called_once_with(
            client, reason="codex_stream_idle_kill"
        )

    def test_interrupt_before_registration_blocks_late_dispatch_and_cleans_up(self):
        agent = self._make_codex_agent()
        client = MagicMock(name="interrupted_request_client")
        release_registration = threading.Event()
        creation_started = threading.Event()

        def create_client(**_kwargs):
            creation_started.set()
            self.assertTrue(release_registration.wait(3.0))
            return client

        agent._create_request_openai_client.side_effect = create_client
        # The worker blocks in client creation. The first poll commits the
        # interrupt terminal marker; the caller then raises and releases the
        # worker in the finally below.
        agent._interrupt_requested = True
        try:
            with patch.dict(
                os.environ,
                {
                    "HERMES_CODEX_TTFB_TIMEOUT_SECONDS": "0",
                    "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS": "0",
                },
                clear=False,
            ):
                with self.assertRaises(InterruptedError):
                    cch.interruptible_api_call(
                        agent, {"model": "gpt-test", "input": "hi"}
                    )
        finally:
            release_registration.set()

        self.assertTrue(creation_started.is_set())
        self._wait_for(lambda: agent._close_request_openai_client.called)
        agent._run_codex_stream.assert_not_called()
        agent._abort_request_openai_client.assert_called_once_with(
            client, reason="interrupt_abort"
        )
        agent._close_request_openai_client.assert_called_once_with(
            client, reason="request_error_cleanup"
        )

    def test_registration_before_ttfb_watchdog_aborts_registered_client(self):
        agent = self._make_codex_agent()
        client = MagicMock(name="registered_request_client")
        request_entered = threading.Event()
        abort_observed = threading.Event()

        agent._create_request_openai_client.return_value = client

        def run_stream(*_args, **_kwargs):
            request_entered.set()
            self.assertTrue(abort_observed.wait(3.0))
            raise RuntimeError("aborted")

        def abort_client(actual_client, *, reason):
            self.assertIs(actual_client, client)
            self.assertEqual(reason, "codex_ttfb_kill")
            abort_observed.set()

        agent._run_codex_stream.side_effect = run_stream
        agent._abort_request_openai_client.side_effect = abort_client

        with patch.dict(
            os.environ,
            {
                "HERMES_CODEX_TTFB_TIMEOUT_SECONDS": "0.05",
                "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS": "0",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                cch.interruptible_api_call(
                    agent, {"model": "gpt-test", "input": "hi"}
                )

        self.assertTrue(request_entered.is_set())
        agent._abort_request_openai_client.assert_called_once_with(
            client, reason="codex_ttfb_kill"
        )
        agent._close_request_openai_client.assert_called_once_with(
            client, reason="request_error_cleanup"
        )


if __name__ == "__main__":
    unittest.main()
