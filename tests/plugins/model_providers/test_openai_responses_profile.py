"""Unit tests for the openai-responses provider profile.

Pins the contract that:
1. The profile is discoverable and uses codex_responses mode.
2. build_api_kwargs_extras always emits retained_reasoning + compaction.
3. ResponsesApiTransport.build_kwargs merges those extras into extra_body
   when provider_profile is passed (the wiring teknium1 flagged as missing).
4. openai-codex (same api_mode) does not inject those fields.
5. Preflight preserves the extras for the wire body.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def openai_responses_profile():
    import model_tools  # noqa: F401 — trigger plugin discovery
    import providers

    profile = providers.get_provider_profile("openai-responses")
    assert profile is not None, "openai-responses provider profile must be registered"
    return profile


@pytest.fixture
def codex_transport():
    import agent.transports.codex  # noqa: F401
    from agent.transports import get_transport

    return get_transport("codex_responses")


class TestOpenAIResponsesProfile:
    def test_registered_with_codex_responses_mode(self, openai_responses_profile):
        assert openai_responses_profile.name == "openai-responses"
        assert openai_responses_profile.api_mode == "codex_responses"
        assert "api.openai.com" in (openai_responses_profile.base_url or "")

    def test_alias_lookup(self):
        import model_tools  # noqa: F401
        import providers

        assert providers.get_provider_profile("openai_responses").name == "openai-responses"

    def test_extras_emit_retained_reasoning_and_compaction(self, openai_responses_profile):
        extra_body, top_level = openai_responses_profile.build_api_kwargs_extras()
        assert extra_body["retained_reasoning"] is True
        assert extra_body["compaction"] is True
        assert top_level == {}

    def test_extras_ignore_reasoning_config(self, openai_responses_profile):
        """Reasoning effort stays on the transport; profile must not fork it."""
        extra_body, top_level = openai_responses_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            supports_reasoning=True,
        )
        assert extra_body == {"retained_reasoning": True, "compaction": True}
        assert "reasoning" not in extra_body
        assert "reasoning" not in top_level
        assert top_level == {}


class TestOpenAIResponsesTransportWiring:
    def test_build_kwargs_merges_profile_extras(
        self, codex_transport, openai_responses_profile
    ):
        kw = codex_transport.build_kwargs(
            model="gpt-5.6",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            provider_profile=openai_responses_profile,
            base_url="https://api.openai.com/v1",
        )
        extra = kw.get("extra_body") or {}
        assert extra.get("retained_reasoning") is True
        assert extra.get("compaction") is True

    def test_without_profile_extras_absent(self, codex_transport):
        kw = codex_transport.build_kwargs(
            model="gpt-5.6",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            base_url="https://api.openai.com/v1",
        )
        extra = kw.get("extra_body") or {}
        assert "retained_reasoning" not in extra
        assert "compaction" not in extra

    def test_openai_codex_profile_does_not_inject_responses_flags(self, codex_transport):
        import model_tools  # noqa: F401
        import providers

        codex_profile = providers.get_provider_profile("openai-codex")
        assert codex_profile is not None
        kw = codex_transport.build_kwargs(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            provider_profile=codex_profile,
            is_codex_backend=True,
            base_url="https://chatgpt.com/backend-api/codex",
        )
        extra = kw.get("extra_body") or {}
        assert "retained_reasoning" not in extra
        assert "compaction" not in extra

    def test_preflight_preserves_profile_extras(
        self, codex_transport, openai_responses_profile
    ):
        kw = codex_transport.build_kwargs(
            model="gpt-5.6",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            provider_profile=openai_responses_profile,
            base_url="https://api.openai.com/v1",
        )
        normalized = codex_transport.preflight_kwargs(kw)
        extra = normalized.get("extra_body") or {}
        assert extra.get("retained_reasoning") is True
        assert extra.get("compaction") is True

    def test_request_overrides_win_over_profile(
        self, codex_transport, openai_responses_profile
    ):
        """Parity with ChatCompletionsTransport: explicit overrides beat profile."""
        kw = codex_transport.build_kwargs(
            model="gpt-5.6",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            provider_profile=openai_responses_profile,
            base_url="https://api.openai.com/v1",
            request_overrides={"extra_body": {"retained_reasoning": False}},
        )
        # request_overrides does kwargs.update, so whole extra_body is replaced
        extra = kw.get("extra_body") or {}
        assert extra.get("retained_reasoning") is False

    def test_profile_extras_merge_with_xai_extra_body(
        self, codex_transport, openai_responses_profile
    ):
        """If extra_body already exists (xAI path), profile keys merge in."""
        # Force xAI path to seed extra_body with prompt_cache_key, while still
        # attaching the openai-responses profile (synthetic but pins merge).
        kw = codex_transport.build_kwargs(
            model="grok-4",
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            provider_profile=openai_responses_profile,
            is_xai_responses=True,
            session_id="sess-1",
            base_url="https://api.x.ai/v1",
        )
        extra = kw.get("extra_body") or {}
        assert "prompt_cache_key" in extra
        assert extra.get("retained_reasoning") is True
        assert extra.get("compaction") is True


class TestCodexHelpersPassProviderProfile:
    def test_build_api_kwargs_passes_profile_for_openai_responses(
        self, openai_responses_profile, monkeypatch
    ):
        """Regression: helpers must pass provider_profile into transport."""
        from agent.chat_completion_helpers import build_api_kwargs
        from agent.transports.codex import ResponsesApiTransport

        captured = {}

        def _capture_build_kwargs(self, model, messages, tools=None, **params):
            captured.clear()
            captured.update(params)
            captured["model"] = model
            return {"model": model, "input": [], "store": False}

        monkeypatch.setattr(
            ResponsesApiTransport, "build_kwargs", _capture_build_kwargs
        )

        class _Agent:
            api_mode = "codex_responses"
            provider = "openai-responses"
            model = "gpt-5.6"
            base_url = "https://api.openai.com/v1"
            _base_url_hostname = "api.openai.com"
            _base_url_lower = "https://api.openai.com/v1"
            reasoning_config = None
            session_id = "s1"
            max_tokens = None
            request_overrides = None
            tools = None
            log_prefix = ""
            _codex_reasoning_replay_enabled = True

            def _get_transport(self):
                return ResponsesApiTransport()

            def _prepare_messages_for_non_vision_model(self, messages):
                return messages

            def _resolved_api_call_timeout(self):
                return 120.0

            def _github_models_reasoning_extra_body(self):
                return None

            def _supports_reasoning_extra_body(self):
                return False

        result = build_api_kwargs(
            _Agent(), [{"role": "user", "content": "hi"}]
        )
        assert result is not None
        assert "provider_profile" in captured
        profile = captured["provider_profile"]
        assert profile is not None
        assert profile.name == openai_responses_profile.name
