"""E2E: the Z.AI Coding overload adaptive backoff must actually run before
provider fallback fires.

`zai_coding_overload_retry_ceiling()` raises the retry ceiling for the narrow
Z.AI Coding Plan GLM-5.2 overload 429 shape precisely so the long backoff
schedule (30/60/90/120s) executes. But `overloaded` also counts as a transport
failure, and the eager-fallback gate used a hardcoded `retry_count >= 2` for
ALL transport failures — so Hermes switched to the fallback chain at retry 2
(a few seconds in) and the raised ceiling + adaptive schedule were dead code.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from run_agent import AIAgent

_ZAI_BASE = "https://api.z.ai/api/coding/paas/v4"


class _ZaiOverloadError(Exception):
    """The exact shape `is_zai_coding_overload_error` detects: 429 + code 1305."""

    status_code = 429

    def __init__(self):
        super().__init__(
            '{"error": {"code": 1305, "message": '
            '"The service may be temporarily overloaded. Please try again later."}}'
        )


def _make_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url=_ZAI_BASE,
            model="glm-5.2",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "openai", "model": "gpt-4o"}],
        )
    agent.client = MagicMock()
    agent.client.base_url = _ZAI_BASE
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._persist_session = lambda *a, **k: None
    agent._save_trajectory = lambda *a, **k: None
    agent._fallback_activated = False
    return agent


class TestZaiOverloadRunsBackoffBeforeFallback:
    def test_overload_retries_past_threshold_two_before_fallback(self, monkeypatch):
        """With a fallback chain configured, a Z.AI overload must NOT switch
        providers at retry 2 — the adaptive long backoff owns those retries.
        The loop keeps retrying the primary until its raised ceiling
        (zai_coding_overload_retry_ceiling, 8) is reached; only then may the
        fallback chain activate. Without the fix the fallback fires at
        retry_count == 2 and the primary never sees attempt 3+.
        """
        from agent import conversation_loop as _conv
        from agent.retry_utils import zai_coding_overload_retry_ceiling

        agent = _make_agent()
        ceiling = zai_coding_overload_retry_ceiling()
        assert ceiling > 2, "precondition: the ZAI ceiling raises the default budget"

        attempts = {"primary": 0, "fallback": 0}
        overload = _ZaiOverloadError()

        def _create(*args, **kwargs):
            url = str(agent.base_url)
            if "api.z.ai/api/coding/paas/v4" in url:
                attempts["primary"] += 1
                raise overload
            attempts["fallback"] += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="fallback reply", tool_calls=None),
                    finish_reason="stop",
                )],
                usage=None,
            )

        agent.client.chat.completions.create.side_effect = _create

        def _fake_activate_fallback(reason=None):
            # Minimal stand-in for the real swap: mark the fallback active
            # and move the dispatch marker so the next create() succeeds.
            agent._fallback_index += 1
            agent._fallback_activated = True
            agent.base_url = "https://api.openai.com/v1"
            agent.model = "gpt-4o"
            return True

        agent._try_activate_fallback = _fake_activate_fallback

        monkeypatch.setattr(_conv, "jittered_backoff", lambda *a, **k: 0.0)
        monkeypatch.setattr(
            _conv, "adaptive_rate_limit_backoff",
            lambda attempt, **k: (0.0, "zai_coding_overload_long"),
        )

        result = agent.run_conversation("hello")

        # The primary provider was retried to its raised ceiling, not cut off
        # at the transport threshold of 2.
        assert attempts["primary"] >= ceiling, (
            f"primary retried only {attempts['primary']} times — the fallback "
            "gate cut the adaptive Z.AI backoff schedule short"
        )
        assert attempts["fallback"] == 1
        assert result["completed"] is True
        assert result["final_response"] == "fallback reply"

    def test_ordinary_transport_failure_still_falls_back_at_two(self, monkeypatch):
        """The threshold change is scoped to the Z.AI overload shape: a plain
        timeout/transport failure still switches providers after 2 retries."""
        from agent import conversation_loop as _conv

        agent = _make_agent()
        attempts = {"primary": 0, "fallback": 0}

        class _TimeoutError(Exception):
            pass

        def _create(*args, **kwargs):
            url = str(agent.base_url)
            if "api.z.ai" in url:
                attempts["primary"] += 1
                raise _TimeoutError("Connection to provider timed out.")
            attempts["fallback"] += 1
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="fallback reply", tool_calls=None),
                    finish_reason="stop",
                )],
                usage=None,
            )

        agent.client.chat.completions.create.side_effect = _create

        def _fake_activate_fallback(reason=None):
            agent._fallback_index += 1
            agent._fallback_activated = True
            agent.base_url = "https://api.openai.com/v1"
            agent.model = "gpt-4o"
            return True

        agent._try_activate_fallback = _fake_activate_fallback

        monkeypatch.setattr(_conv, "jittered_backoff", lambda *a, **k: 0.0)

        result = agent.run_conversation("hello")

        # Original behavior preserved: primary sees at most 3 attempts
        # (initial + 2 retries), then the fallback takes over.
        assert 1 <= attempts["primary"] <= 3
        assert attempts["fallback"] == 1
        assert result["completed"] is True

    def test_no_fallback_configured_overload_exhausts_at_ceiling(self, monkeypatch):
        """Without a fallback chain the loop must keep retrying to the raised
        ceiling rather than surfacing an error at the default budget."""
        from agent import conversation_loop as _conv
        from agent.retry_utils import zai_coding_overload_retry_ceiling

        agent = _make_agent()
        agent._fallback_chain = []
        agent.fallback_model = None
        ceiling = zai_coding_overload_retry_ceiling()

        attempts = {"n": 0}

        def _create(*args, **kwargs):
            attempts["n"] += 1
            raise _ZaiOverloadError()

        agent.client.chat.completions.create.side_effect = _create

        monkeypatch.setattr(_conv, "jittered_backoff", lambda *a, **k: 0.0)
        monkeypatch.setattr(
            _conv, "adaptive_rate_limit_backoff",
            lambda attempt, **k: (0.0, "zai_coding_overload_long"),
        )

        result = agent.run_conversation("hello")

        assert attempts["n"] >= ceiling
        assert result["failed"] is True
