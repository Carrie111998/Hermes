"""Locale-aware spoken-text normalization regressions for issue #80136."""

from pathlib import Path

import pytest

from tools.tts_text_normalize import prepare_spoken_text
from tools.tts_tool import (
    _load_tts_config,
    _prepare_spoken_text_for_tts,
    _resolve_tts_locale,
    _strip_markdown_for_tts,
)


class TestLocalizedSymbolExpansion:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("Il fait 44 % d'humidité.", "Il fait 44 pour cent d'humidité."),
            ("Il fait 28,2 °C.", "Il fait 28,2 degrés Celsius."),
            ("Vent à 12 km/h.", "Vent à 12 kilomètres par heure."),
            ("Entre 11,5-17,2 °C.", "Entre 11,5 à 17,2 degrés Celsius."),
        ],
    )
    def test_french_expansions(self, source, expected):
        assert prepare_spoken_text(source, max_chars=None, locale="fr-FR") == expected

    def test_default_remains_english_for_backward_compatibility(self):
        assert prepare_spoken_text("44 % at 28 °C", max_chars=None) == (
            "44 percent at 28 degrees Celsius"
        )

    def test_unbundled_non_english_locale_never_injects_english(self):
        spoken = prepare_spoken_text(
            "44 % a 28 °C y 12 km/h", max_chars=None, locale="es-ES"
        )

        assert "percent" not in spoken
        assert "degrees" not in spoken
        assert "kilometres per hour" not in spoken
        assert "44 %" in spoken
        assert "28 °C" in spoken
        assert "12 km/h" in spoken


class TestTtsLocaleResolution:
    def test_provider_language_wins(self):
        config = {
            "provider": "openai",
            "language": "de-DE",
            "openai": {"language": " fr_fr "},
        }

        assert _resolve_tts_locale("openai", config) == "fr-FR"

    def test_global_language_precedes_voice_inference(self):
        config = {
            "provider": "edge",
            "language": "de-DE",
            "edge": {"voice": "fr-FR-HenriNeural"},
        }

        assert _resolve_tts_locale("edge", config) == "de-DE"

    @pytest.mark.parametrize(
        ("provider", "voice", "expected"),
        [
            ("edge", "fr-FR-HenriNeural", "fr-FR"),
            ("piper", "fr_FR-siwis-medium", "fr-FR"),
            ("piper", "/voices/de_DE-thorsten-medium.onnx", "de-DE"),
        ],
    )
    def test_locale_is_inferred_from_structured_voice_names(
        self, provider, voice, expected
    ):
        config = {"provider": provider, provider: {"voice": voice}}

        assert _resolve_tts_locale(provider, config) == expected

    def test_opaque_voice_keeps_english_compatibility_fallback(self):
        config = {"provider": "openai", "openai": {"voice": "alloy"}}

        assert _resolve_tts_locale("openai", config) == "en"

    def test_explicit_auto_leaves_symbols_to_provider(self):
        config = {
            "provider": "edge",
            "edge": {"language": "auto", "voice": "fr-FR-HenriNeural"},
        }

        assert _resolve_tts_locale("edge", config) == "und"


class TestLocalePropagation:
    def test_config_aware_streaming_cleaner_uses_voice_locale(self):
        config = {
            "provider": "edge",
            "edge": {"voice": "fr-FR-HenriNeural"},
        }

        assert _strip_markdown_for_tts("**44 %**", tts_config=config) == (
            "44 pour cent"
        )

    def test_real_config_load_reaches_provider_dispatch(self, tmp_path, monkeypatch):
        from hermes_constants import get_config_path
        from tools import tts_tool

        config_path = Path(get_config_path())
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            "tts:\n"
            "  provider: edge\n"
            "  edge:\n"
            "    voice: fr-FR-HenriNeural\n",
            encoding="utf-8",
        )

        config = _load_tts_config()
        assert _prepare_spoken_text_for_tts(
            "44 % à 28 °C",
            max_chars=None,
            tts_config=config,
        ) == "44 pour cent à 28 degrés Celsius"

        captured = {}

        async def fake_edge(text, output_path, _tts_config):
            captured["text"] = text
            Path(output_path).write_bytes(b"ID3fake-audio")
            return output_path

        monkeypatch.setattr(tts_tool, "_import_edge_tts", lambda: object())
        monkeypatch.setattr(tts_tool, "_generate_edge_tts", fake_edge)

        result = tts_tool.text_to_speech_tool(
            "44 % à 28 °C",
            output_path=str(tmp_path / "speech.mp3"),
        )

        assert '"success": true' in result
        assert captured["text"] == "44 pour cent à 28 degrés Celsius"
