"""Tests for the DeepInfra TTS provider.

``_generate_deepinfra_tts`` is a thin shim that resolves credentials/model
then delegates to ``_generate_openai_tts``. These tests pin language
propagation, provider isolation, and the no-hardcoded-fallback contract;
shared infrastructure (catalog fetch + tag filter) is covered in
``tests/hermes_cli/test_api_key_providers.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolation(monkeypatch):
    import hermes_cli.models as _models_mod
    monkeypatch.setattr(_models_mod, "_deepinfra_catalog_cache", {})
    monkeypatch.setenv("DEEPINFRA_API_KEY", "test-key")
    yield


def test_raises_when_no_model_resolvable(monkeypatch, tmp_path):
    """No-fallback contract: empty config + unreachable catalog → ValueError."""
    import urllib.request
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(Exception("offline")),
    )
    from tools.tts_tool import _generate_deepinfra_tts
    with pytest.raises(ValueError, match="No DeepInfra TTS model available"):
        _generate_deepinfra_tts("hi", str(tmp_path / "out.mp3"), {})


def test_forwards_language_hint_to_deepinfra_request(monkeypatch, tmp_path):
    from tools import tts_tool

    response = MagicMock()
    client = MagicMock()
    client.audio.speech.create.return_value = response
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(tts_tool, "_import_openai_client", lambda: client_factory)

    output_path = tmp_path / "out.mp3"
    tts_tool._generate_deepinfra_tts(
        "Olá",
        str(output_path),
        {
            "provider": "deepinfra",
            "deepinfra": {
                "model": "inworld-ai/realtime-tts-2",
                "voice": "Heitor",
                "language": "PT_BR",
            },
        },
    )

    create_kwargs = client.audio.speech.create.call_args.kwargs
    assert create_kwargs["extra_body"] == {"language": "PT_BR"}


def test_deepinfra_does_not_inherit_openai_language(monkeypatch, tmp_path):
    from tools import tts_tool

    client = MagicMock()
    client_factory = MagicMock(return_value=client)
    monkeypatch.setattr(tts_tool, "_import_openai_client", lambda: client_factory)

    tts_tool._generate_deepinfra_tts(
        "Olá",
        str(tmp_path / "out.mp3"),
        {
            "openai": {"language": "en"},
            "deepinfra": {"model": "inworld-ai/realtime-tts-2"},
        },
    )

    create_kwargs = client.audio.speech.create.call_args.kwargs
    assert "extra_body" not in create_kwargs


def test_requirements_follow_explicit_deepinfra_provider(monkeypatch):
    from tools import tts_tool

    monkeypatch.setattr(
        tts_tool,
        "_load_tts_config",
        lambda: {"provider": "deepinfra", "deepinfra": {}},
    )
    monkeypatch.setattr(tts_tool, "_import_openai_client", lambda: object)

    assert tts_tool.check_tts_requirements() is True


def test_unselected_cloud_credentials_do_not_expose_edge_tool(monkeypatch):
    from tools import tts_tool

    monkeypatch.setattr(tts_tool, "_load_tts_config", lambda: {})
    monkeypatch.setattr(tts_tool, "_import_edge_tts", MagicMock(side_effect=ImportError))
    monkeypatch.setenv("OPENAI_API_KEY", "unselected-key")

    assert tts_tool.check_tts_requirements() is False
