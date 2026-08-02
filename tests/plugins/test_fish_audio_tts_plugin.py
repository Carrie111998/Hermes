from __future__ import annotations

import importlib
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from plugins.fish_audio_tts import core


def test_settings_reads_fish_api_key_without_exposing_it(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-secret")
    cfg = core.settings({"fishaudio": {"model": "s2-pro", "reference_id": "voice-1"}})
    assert cfg.api_key == "test-secret"
    assert cfg.model == "s2-pro"
    assert cfg.reference_id == "voice-1"
    payload = core.status_payload({"fishaudio": {"model": "s2-pro"}})
    assert payload["available"] is True
    assert "test-secret" not in json.dumps(payload)


def test_settings_supports_hermes_fish_audio_alias(monkeypatch):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-secret")
    cfg = core.settings({"fish_audio": {"format": "wav", "speed": 1.5}})
    assert cfg.output_format == "wav"
    assert cfg.speed == 1.5


def test_synthesize_posts_official_json_request(monkeypatch, tmp_path):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-secret")
    captured = {}

    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b"audio-bytes"

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(core, "urlopen", fake_urlopen)
    destination = tmp_path / "speech.mp3"
    result = core.synthesize_text(
        "hello",
        output_path=destination,
        voice="voice-1",
        model="s2-pro",
        speed=1.2,
    )
    assert result["ok"] is True
    assert destination.read_bytes() == b"audio-bytes"
    assert captured["url"] == "https://api.fish.audio/v1/tts"
    assert captured["headers"]["Authorization"] == "Bearer test-secret"
    assert captured["headers"]["Model"] == "s2-pro"
    assert captured["body"]["reference_id"] == "voice-1"
    assert captured["body"]["prosody"]["speed"] == 1.2


def test_synthesize_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="API key is missing"):
        core.synthesize_text("hello", output_path=tmp_path / "x.mp3")


def test_http_error_does_not_include_authorization(monkeypatch, tmp_path):
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-secret")

    def fake_urlopen(*args, **kwargs):
        raise HTTPError("https://api.fish.audio/v1/tts", 401, "bad", {}, None)

    monkeypatch.setattr(core, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as exc:
        core.synthesize_text("hello", output_path=tmp_path / "x.mp3")
    assert "test-secret" not in str(exc.value)
    assert "HTTP 401" in str(exc.value)


class FakePluginContext:
    def __init__(self) -> None:
        self.tools = {}
        self.tts_providers = {}
        self.cli_commands = {}

    def register_tts_provider(self, provider) -> None:
        self.tts_providers[provider.name] = provider

    def register_tool(self, name, handler, **kwargs) -> None:
        self.tools[name] = {"handler": handler, "kwargs": kwargs}

    def register_cli_command(self, name, **kwargs) -> None:
        self.cli_commands[name] = kwargs


def test_synthesize_handler_accepts_tool_argument_mapping(monkeypatch) -> None:
    plugin = importlib.import_module("plugins.fish_audio_tts")
    captured = {}

    def fake_synthesize_text(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "file_path": "test.mp3"}

    monkeypatch.setattr(plugin, "synthesize_text", fake_synthesize_text)
    result = plugin._synthesize_handler(
        {
            "text": "こんにちは、ボブにゃん！",
            "output_path": "test.mp3",
            "voice": "hakua",
            "model": "s2.1-pro-free",
            "format": "mp3",
            "speed": 1.1,
        }
    )

    assert json.loads(result)["ok"] is True
    assert captured == {
        "text": "こんにちは、ボブにゃん！",
        "output_path": "test.mp3",
        "voice": "hakua",
        "model": "s2.1-pro-free",
        "output_format": "mp3",
        "speed": 1.1,
    }
