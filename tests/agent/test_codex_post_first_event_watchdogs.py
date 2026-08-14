from __future__ import annotations

import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx

from agent import chat_completion_helpers as cch
from agent import codex_runtime


def _agent(*, codex: bool, stale_timeout: float) -> MagicMock:
    agent = MagicMock()
    agent.api_mode = "codex_responses" if codex else "chat_completions"
    agent.provider = "openai-codex" if codex else "openai"
    agent.platform = "cli"
    agent.is_subagent = False
    agent._interrupt_requested = False
    agent._base_url_hostname = "chatgpt.com" if codex else "api.openai.com"
    agent._base_url_lower = (
        "https://chatgpt.com/backend-api/codex"
        if codex
        else "https://api.openai.com/v1"
    )
    agent._compute_non_stream_stale_timeout.return_value = stale_timeout
    agent._codex_silent_hang_hint.return_value = None
    agent._create_request_openai_client.return_value = MagicMock(name="client")
    agent._close_request_openai_client = MagicMock()
    return agent


def _env(*, ttfb: float, idle: float, hard: float = 2.0) -> dict[str, str]:
    return {
        "HERMES_CODEX_TTFB_TIMEOUT_SECONDS": str(ttfb),
        "HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS": str(idle),
        "HERMES_CODEX_HARD_TIMEOUT_SECONDS": str(hard),
    }


class CodexPostFirstEventWatchdogs(unittest.TestCase):
    def test_active_codex_sse_can_run_past_generic_stale_timeout(self) -> None:
        agent = _agent(codex=True, stale_timeout=0.05)
        aborted = threading.Event()
        expected = SimpleNamespace(id="completed")

        def abort(_client, *, reason):
            aborted.set()
            return 1

        def active_stream(*_args, **_kwargs):
            # Hold the event/stale decision lock across the first generic stale
            # polling deadline, then publish the first SSE before releasing it.
            # The watchdog must re-check the marker under this same lock.
            with agent._codex_stream_event_lock:
                time.sleep(0.35)
                codex_runtime._publish_codex_stream_event(agent)
            deadline = time.monotonic() + 0.35
            while time.monotonic() < deadline:
                codex_runtime._publish_codex_stream_event(agent)
                if aborted.wait(0.02):
                    raise httpx.ReadError("[Errno 32] Broken pipe")
            return expected

        agent._abort_request_openai_client.side_effect = abort
        agent._run_codex_stream.side_effect = active_stream

        with patch.dict(os.environ, _env(ttfb=0, idle=0.1), clear=False):
            response = cch.interruptible_api_call(
                agent, {"model": "gpt-test", "input": "x"}
            )

        self.assertIs(response, expected)
        self.assertFalse(aborted.is_set())

    def test_codex_sse_idle_still_uses_specialized_idle_kill(self) -> None:
        agent = _agent(codex=True, stale_timeout=10.0)
        aborted = threading.Event()

        def abort(_client, *, reason):
            aborted.set()
            return 1

        def idle_stream(*_args, **_kwargs):
            agent._codex_stream_last_event_ts = time.time()
            self.assertTrue(aborted.wait(3.0))
            raise httpx.ReadError("[Errno 32] Broken pipe")

        agent._abort_request_openai_client.side_effect = abort
        agent._run_codex_stream.side_effect = idle_stream

        with patch.dict(os.environ, _env(ttfb=0, idle=0.05), clear=False):
            with self.assertRaises(TimeoutError) as caught:
                cch.interruptible_api_call(
                    agent, {"model": "gpt-test", "input": "x"}
                )
        self.assertIn("codex_stream_idle_kill", str(caught.exception))

    def test_non_codex_without_response_still_uses_generic_stale_kill(self) -> None:
        agent = _agent(codex=False, stale_timeout=0.05)
        aborted = threading.Event()
        request_client = agent._create_request_openai_client.return_value

        def abort(_client, *, reason):
            aborted.set()
            return 1

        def blocked_create(**_kwargs):
            self.assertTrue(aborted.wait(3.0))
            raise httpx.ReadError("[Errno 32] Broken pipe")

        agent._abort_request_openai_client.side_effect = abort
        request_client.chat.completions.create.side_effect = blocked_create

        with patch.dict(os.environ, _env(ttfb=0, idle=0), clear=False):
            with self.assertRaises(TimeoutError) as caught:
                cch.interruptible_api_call(
                    agent, {"model": "gpt-test", "messages": []}
                )
        self.assertIn("stale_call_kill", str(caught.exception))

    def test_codex_without_any_sse_still_uses_ttfb_recovery(self) -> None:
        agent = _agent(codex=True, stale_timeout=10.0)
        aborted = threading.Event()

        def abort(_client, *, reason):
            aborted.set()
            return 1

        def silent_stream(*_args, **_kwargs):
            self.assertTrue(aborted.wait(3.0))
            raise httpx.ReadError("[Errno 32] Broken pipe")

        agent._abort_request_openai_client.side_effect = abort
        agent._run_codex_stream.side_effect = silent_stream

        with patch.dict(os.environ, _env(ttfb=0.05, idle=0), clear=False):
            with self.assertRaises(TimeoutError) as caught:
                cch.interruptible_api_call(
                    agent, {"model": "gpt-test", "input": "x"}
                )
        self.assertIn("codex_ttfb_kill", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
