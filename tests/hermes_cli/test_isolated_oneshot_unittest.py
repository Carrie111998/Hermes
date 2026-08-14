from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _response(content="OK", model="fake/actual"):
    message = SimpleNamespace(
        role="assistant", content=content, tool_calls=None, reasoning=None, refusal=None
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop", index=0)],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
        model=model,
        id="fake-id",
    )


def _stream_chunk(content="STREAM_OK", model="fake/actual"):
    delta = SimpleNamespace(
        role="assistant",
        content=content,
        reasoning=None,
        reasoning_content=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason="stop", index=0)],
        usage=SimpleNamespace(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
        model=model,
        id="fake-stream-id",
    )


class _FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        return None


class _FakeStreamingClient:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.error:
            raise self.error
        return _FakeStream([_stream_chunk()])

    def close(self):
        return None


def _snapshot(root: str):
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in Path(root).rglob("*")
        if p.is_file()
    }


class IsolatedOneShotContractTests(unittest.TestCase):
    def _agent(self):
        from hermes_cli.bootstrap_policy import BootstrapPolicy, set_policy
        set_policy(BootstrapPolicy.ISOLATED_ONESHOT)
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="FAKE_SENTINEL_KEY",
            base_url="http://127.0.0.1:9/v1",
            provider="openrouter",
            api_mode="chat_completions",
            model="fake/requested",
            enabled_toolsets=[],
            quiet_mode=True,
            platform="cli",
            session_db=None,
            fallback_model=None,
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            persist_session=False,
            minimal_system_prompt=True,
            api_max_attempts=1,
            isolated_runtime=True,
        )
        agent._disable_streaming = True
        return agent

    def test_success_and_failure_matrix_is_single_attempt_and_zero_write(self):
        import agent.chat_completion_helpers as helpers
        import agent.model_metadata as metadata

        cases = [
            None,
            RuntimeError("simulated 400 Bad Request"),
            RuntimeError("simulated 401 Unauthorized"),
            RuntimeError("simulated 404 Not Found"),
            RuntimeError("simulated 429 Too Many Requests"),
            RuntimeError("simulated 500 Internal Server Error"),
            TimeoutError("simulated timeout"),
            OSError("simulated transport exception"),
            RuntimeError("simulated malformed provider response"),
        ]
        for failure in cases:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as home:
                calls = []

                def dispatch(agent, api_kwargs, *, make_client):
                    calls.append(dict(api_kwargs))
                    if failure:
                        raise failure
                    return _response()

                with patch.dict(os.environ, {"HERMES_HOME": home}, clear=False), patch.object(
                    helpers, "_dispatch_nonstreaming_api_request", side_effect=dispatch
                ), patch.object(
                    metadata.requests,
                    "get",
                    side_effect=AssertionError("metadata network attempted"),
                ):
                    before = _snapshot(home)
                    agent = self._agent()
                    result = agent.run_conversation("FAKE_PROMPT_SENTINEL")
                    agent.close()
                    after = _snapshot(home)
                self.assertEqual(len(calls), 1)
                self.assertEqual(before, after)
                self.assertEqual(agent.enabled_toolsets, [])
                self.assertEqual(agent._fallback_chain, [])
                self.assertTrue(agent._persist_disabled)
                if failure is None:
                    self.assertEqual(result["response_model"], "fake/actual")

    def test_streaming_retry_budget_is_zero(self):
        agent = self._agent()
        self.assertEqual(agent._api_max_retries, 1)
        self.assertTrue(agent._fallback_disabled)
        self.assertTrue(agent._isolated_runtime)
        agent.close()

    def test_streaming_success_and_failure_are_single_attempt_zero_write(self):
        import agent.model_metadata as metadata

        for failure in (None, ConnectionError("simulated stream failure")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as home:
                client = _FakeStreamingClient(error=failure)
                with patch.dict(os.environ, {"HERMES_HOME": home}, clear=False), patch.object(
                    metadata.requests,
                    "get",
                    side_effect=AssertionError("metadata network attempted"),
                ):
                    before = _snapshot(home)
                    agent = self._agent()
                    agent.client = client
                    agent._create_request_openai_client = lambda **_kwargs: client
                    agent.run_conversation("FAKE_STREAM_PROMPT")
                    agent.close()
                    after = _snapshot(home)
                self.assertEqual(len(client.calls), 1)
                self.assertEqual(before, after)
                self.assertEqual(client.calls[0].get("tools"), None)
                self.assertNotIn("tool_choice", client.calls[0])


if __name__ == "__main__":
    unittest.main()
