"""Regression coverage for Hindsight + GitHub Copilot embedded mode.

The final test intentionally crosses into Hindsight's real provider factory and
request/session construction path. The network/runtime boundary is faked, but
``github-copilot`` itself is not mocked, so an unsupported-provider regression
cannot be hidden by a fake ``HindsightEmbedded`` wrapper.
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
from types import SimpleNamespace

import pytest

from plugins.memory.hindsight import (
    HindsightMemoryProvider,
    _build_embedded_profile_env,
    _ensure_hindsight_copilot_runtime_version,
    _prepare_embedded_copilot_auth,
)


def test_config_schema_exposes_copilot_provider():
    schema = HindsightMemoryProvider().get_config_schema()
    field = next(item for item in schema if item.get("key") == "llm_provider")
    assert "copilot" in field["choices"]


def test_embedded_profile_env_uses_upstream_github_copilot_provider():
    env = _build_embedded_profile_env(
        {
            "llm_provider": "copilot",
            "llm_model": "gpt-5.6-terra",
            # A stale URL/key from the old OpenAI-compatible implementation must
            # not leak into Hindsight's native Copilot provider.
            "llm_base_url": "https://api.githubcopilot.com",
            "llm_api_key": "stale-exchanged-api-token",
        }
    )

    assert env["HINDSIGHT_API_LLM_PROVIDER"] == "github-copilot"
    assert env["HINDSIGHT_API_LLM_MODEL"] == "gpt-5.6-terra"
    assert "HINDSIGHT_API_LLM_API_KEY" not in env
    assert "HINDSIGHT_API_LLM_BASE_URL" not in env


def test_prepare_embedded_copilot_auth_exports_raw_github_token(monkeypatch):
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        "hermes_cli.copilot_auth.resolve_copilot_token",
        lambda: ("ghu_raw_test_token", "gh auth token"),
    )

    source = _prepare_embedded_copilot_auth()

    assert source == "gh auth token"
    assert os.environ["COPILOT_GITHUB_TOKEN"] == "ghu_raw_test_token"


def test_copilot_runtime_rejects_hindsight_before_native_provider(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.9.1")

    with pytest.raises(RuntimeError, match=r"hindsight-all>=0\.9\.2"):
        _ensure_hindsight_copilot_runtime_version()


def test_get_client_uses_native_github_copilot_provider_without_openai_credentials(monkeypatch):
    captured = {}

    class FakeHindsightEmbedded:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("plugins.memory.hindsight._check_local_runtime", lambda: (True, ""))
    monkeypatch.setattr(
        "plugins.memory.hindsight._ensure_hindsight_copilot_runtime_version",
        lambda: None,
    )
    monkeypatch.setattr("plugins.memory.hindsight._prepare_embedded_copilot_auth", lambda: "test")
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *args, **kwargs: None)
    monkeypatch.setitem(
        sys.modules,
        "hindsight",
        SimpleNamespace(HindsightEmbedded=FakeHindsightEmbedded),
    )

    provider = HindsightMemoryProvider()
    provider._mode = "local_embedded"
    provider._config = {
        "profile": "hermes",
        "llm_provider": "copilot",
        "llm_model": "gpt-5.6-terra",
        "llm_api_key": "must-not-be-forwarded",
        "llm_base_url": "https://api.githubcopilot.com",
    }
    provider._llm_base_url = "https://api.githubcopilot.com"

    provider._get_client()

    assert captured["llm_provider"] == "github-copilot"
    assert captured["llm_model"] == "gpt-5.6-terra"
    assert "llm_api_key" not in captured
    assert "llm_base_url" not in captured


@pytest.mark.asyncio
async def test_upstream_hindsight_copilot_provider_builds_real_request_path(monkeypatch):
    """Exercise Hindsight's actual provider factory through session creation.

    ``hindsight-all`` is optional in the main Hermes test environment, so this
    integration-path test skips only when that optional runtime is absent. In
    local-embedded/Copilot installs, setup requires Hindsight >= 0.9.2 and this
    test executes in full.
    """

    config_module = pytest.importorskip("hindsight_api.config")
    wrapper_module = pytest.importorskip("hindsight_api.engine.llm_wrapper")
    provider_module = pytest.importorskip(
        "hindsight_api.engine.providers.github_copilot_llm"
    )
    session_events = pytest.importorskip("copilot.session_events")

    events = [
        SimpleNamespace(
            data=session_events.AssistantMessageData(
                content="ok",
                message_id="message-1",
                tool_requests=None,
            )
        ),
        SimpleNamespace(
            data=session_events.AssistantUsageData(
                model="gpt-5.6-terra",
                input_tokens=5,
                output_tokens=1,
                cache_read_tokens=0,
                reasoning_tokens=0,
                finish_reason="stop",
            )
        ),
    ]

    class FakeSession:
        session_id = "session-1"

        def __init__(self, on_event):
            self.on_event = on_event

        async def send_and_wait(self, _prompt, *, timeout):
            for event in events:
                self.on_event(event)
            return events[0]

        async def disconnect(self):
            return None

        async def abort(self):
            return None

    class FakeClient:
        def __init__(self):
            self.create_kwargs = None

        async def create_session(self, **kwargs):
            self.create_kwargs = kwargs
            return FakeSession(kwargs["on_event"])

        async def delete_session(self, _session_id):
            return None

    class FakeRuntime:
        runtime_url = ""
        ref_count = 1

        def __init__(self):
            self.client = FakeClient()

        async def ensure_started(self):
            return None

        async def get_client(self):
            return self.client

        async def invalidate(self, _expected_client, reason):
            return None

        async def stop(self):
            return None

    runtime = FakeRuntime()
    monkeypatch.setattr(provider_module, "_acquire_runtime", lambda _url: runtime)

    assert config_module.PROVIDER_DEFAULT_MODELS["github-copilot"] == "gpt-5.6-terra"
    assert wrapper_module.requires_api_key("github-copilot") is False

    llm = wrapper_module.create_llm_provider(
        provider="github-copilot",
        api_key="",
        base_url="",
        model="gpt-5.6-terra",
        reasoning_effort=None,
    )
    result = await llm.call(
        messages=[{"role": "user", "content": "Say ok."}],
        max_retries=0,
    )

    assert result == "ok"
    assert isinstance(llm, provider_module.GitHubCopilotLLM)
    assert runtime.client.create_kwargs["model"] == "gpt-5.6-terra"
    assert runtime.client.create_kwargs["enable_file_hooks"] is False
    assert runtime.client.create_kwargs["enable_config_discovery"] is False
    assert runtime.client.create_kwargs["skip_custom_instructions"] is True
