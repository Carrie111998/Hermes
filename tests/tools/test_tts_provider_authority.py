"""Regression tests for model-boundary TTS provider authority.

Programmatic callers may select a provider, but model-generated tool arguments
must not override ``tts.provider``. Platform hints are advisory, so enforcement
belongs in the registered schema and handler.
"""

import pytest

from tools import tts_tool
from tools.registry import registry


class _ProviderProbe(Exception):
    def __init__(self, provider: str):
        super().__init__(provider)
        self.provider = provider


def test_programmatic_provider_override_remains_supported(monkeypatch):
    """Trusted Python callers retain upstream's explicit provider-selection API."""
    seen = []
    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {"provider": "xai"})
    monkeypatch.setattr(
        tts_tool,
        "_resolve_max_text_length",
        lambda provider, _config: seen.append(provider) or 4096,
    )
    monkeypatch.setattr(tts_tool, "_split_text_for_tts", lambda _text, _limit: [])

    tts_tool.text_to_speech_tool("hello", provider="openai")

    assert seen == ["openai"]


def test_inner_dispatch_preserves_programmatic_provider_override(monkeypatch):
    """The explicit provider must reach synthesis rather than reverting to config."""
    monkeypatch.setattr(
        tts_tool,
        "_resolve_max_text_length",
        lambda provider, _config: (_ for _ in ()).throw(_ProviderProbe(provider)),
    )

    with pytest.raises(_ProviderProbe) as caught:
        tts_tool._text_to_speech_single(
            "hello",
            provider="openai",
            tts_config_override={"provider": "xai"},
        )

    assert caught.value.provider == "openai"


def test_registered_handler_discards_requested_provider(monkeypatch):
    """Even a rogue payload must not cross the model-facing handler boundary."""
    seen = {}
    monkeypatch.setattr(
        tts_tool,
        "text_to_speech_tool",
        lambda **kwargs: seen.update(kwargs) or "{}",
    )

    entry = registry.get_entry("text_to_speech")
    assert entry is not None
    entry.handler({"text": "hello", "provider": "openai"})

    assert seen["provider"] is None


def test_registered_model_schema_cannot_request_provider_override():
    """Provider selection belongs to config, not model-generated tool arguments."""
    entry = registry.get_entry("text_to_speech")
    assert entry is not None
    assert "provider" not in entry.schema["parameters"]["properties"]
