"""Tests for upstream 5xx fallback: aux tasks must fall back on server errors.

Regression context: an OpenCode Go endpoint returned HTTP 500
("Internal server error") for every muse-spark request. 500s survived the
transient-retry path but were not classified as a fallback-worthy failure,
so ``should_fallback`` stayed False and user-configured
``auxiliary.<task>.fallback_chain`` entries were never consulted — auxiliary
tasks (compression, title generation, approval) failed outright instead of
rerouting to a healthy provider.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.auxiliary_client import _is_server_error, call_llm


def _make_500_err():
    """An APIStatusError-shaped exception like openai raises on HTTP 500."""
    class _Err500(Exception):
        status_code = 500
    return _Err500("Error code: 500 - {'type': 'error', 'error': "
                   "{'message': 'Internal server error'}}")


def _make_503_err():
    class _Err503(Exception):
        status_code = 503
    return _Err503("Error code: 503 - Service Unavailable")


class TestIsServerError:
    def test_500_status_code_detected(self):
        assert _is_server_error(_make_500_err()) is True

    def test_503_status_code_detected(self):
        assert _is_server_error(_make_503_err()) is True

    def test_plain_exception_with_message_detected(self):
        # Some providers surface 5xx as bare exceptions without status_code.
        exc = Exception("upstream returned Internal server error")
        assert _is_server_error(exc) is True

    def test_4xx_not_matched(self):
        exc = Exception("Error code: 400 - bad request")
        exc.status_code = 400
        assert _is_server_error(exc) is False

    def test_unrelated_exception_not_matched(self):
        assert _is_server_error(Exception("something else went wrong")) is False

    def test_auth_error_body_not_shadowed(self):
        # A 5xx whose message mentions auth must NOT be force-matched here;
        # the caller classifies auth first via _is_auth_error.
        exc = Exception("internal error while validating credentials")
        exc.status_code = 502
        assert _is_server_error(exc) is True  # matched, but caller checks auth first
        assert "auth" not in ("internal server error", )


class TestExplicitProvider500FallsBackToChain:
    """HTTP 500 on an explicit aux provider must consult fallback_chain.

    Mirrors TestAuxiliaryFallbackLayering.test_explicit_provider_rate_limit_
    triggers_fallback (#52228), extended to 5xx responses.
    """

    def test_explicit_provider_500_triggers_configured_chain(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _make_500_err()

        fallback_client = MagicMock()
        fallback_client.chat.completions.create.return_value = MagicMock(choices=[
            MagicMock(message=MagicMock(content="from fallback chain"))
        ])

        with patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "muse-spark-1.2-contributor")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("opencode-go", "muse-spark-1.2-contributor",
                                 None, None, None)), \
             patch("agent.auxiliary_client._try_configured_fallback_chain",
                   return_value=(fallback_client, "deepseek-v4-flash",
                                 "fallback_chain[0](commandcode)")) as mock_chain, \
             patch("agent.auxiliary_client._try_main_agent_model_fallback") as mock_main:
            result = call_llm(
                task="title_generation",
                messages=[{"role": "user", "content": "title this"}],
            )

        mock_chain.assert_called()
        assert fallback_client.chat.completions.create.called
        assert result.choices[0].message.content == "from fallback chain"
        mock_main.assert_not_called()

    def test_explicit_provider_503_triggers_configured_chain(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _make_503_err()

        fallback_client = MagicMock()
        fallback_client.chat.completions.create.return_value = MagicMock(choices=[
            MagicMock(message=MagicMock(content="recovered"))
        ])

        with patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "m")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("opencode-go", "m", None, None, None)), \
             patch("agent.auxiliary_client._try_configured_fallback_chain",
                   return_value=(fallback_client, "fb-model",
                                 "fallback_chain[0](commandcode)")) as mock_chain, \
             patch("agent.auxiliary_client._try_main_agent_model_fallback"):
            result = call_llm(
                task="compression",
                messages=[{"role": "user", "content": "summarize"}],
            )

        mock_chain.assert_called()
        assert result.choices[0].message.content == "recovered"

    def test_500_with_no_chain_reaches_main_model_safety_net(self, monkeypatch):
        """When no chain is configured, 500 still falls back to main model."""
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")

        primary_client = MagicMock()
        primary_client.chat.completions.create.side_effect = _make_500_err()

        main_client = MagicMock()
        main_client.chat.completions.create.return_value = MagicMock(choices=[
            MagicMock(message=MagicMock(content="main saved it"))
        ])

        with patch("agent.auxiliary_client._get_cached_client",
                   return_value=(primary_client, "m")), \
             patch("agent.auxiliary_client._resolve_task_provider_model",
                   return_value=("opencode-go", "m", None, None, None)), \
             patch("agent.auxiliary_client._try_configured_fallback_chain",
                   return_value=(None, None, "")), \
             patch("agent.auxiliary_client._try_main_agent_model_fallback",
                   return_value=(main_client, "stealth/ox-alpha", "main-agent(openrouter)")) as mock_main:
            result = call_llm(
                task="approval",
                messages=[{"role": "user", "content": "approve?"}],
            )

        mock_main.assert_called()
        assert result.choices[0].message.content == "main saved it"
