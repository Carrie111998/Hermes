"""OpenRouter speech-to-text provider behavior."""

from unittest.mock import patch

from tools import transcription_tools as stt


class TestOpenRouterStt:
    def test_explicit_provider_requires_openrouter_key(self):
        with patch.object(stt, "_HAS_OPENAI", True), patch.object(
            stt, "_resolve_provider_key", return_value="or-key"
        ) as resolve:
            assert stt._get_provider({"enabled": True, "provider": "openrouter"}) == "openrouter"
        resolve.assert_called_with("OPENROUTER_API_KEY", "openrouter")

    def test_dispatch_reuses_openai_compatible_transcription_with_selected_model(self, tmp_path):
        audio = tmp_path / "speech.webm"
        audio.write_bytes(b"audio")
        config = {
            "enabled": True,
            "provider": "openrouter",
            "openrouter": {"model": "openai/gpt-4o-mini-transcribe"},
        }
        with patch.object(stt, "_load_stt_config", return_value=config), patch.object(
            stt, "_get_provider", return_value="openrouter"
        ), patch.object(stt, "_resolve_provider_key", return_value="or-key"), patch.object(
            stt,
            "_transcribe_openai",
            return_value={"success": True, "transcript": "hello", "provider": "openrouter"},
        ) as transcribe:
            result = stt._transcribe_prepared_audio(str(audio), model=None)

        assert result["transcript"] == "hello"
        transcribe.assert_called_once_with(
            str(audio),
            "openai/gpt-4o-mini-transcribe",
            api_key="or-key",
            base_url="https://openrouter.ai/api/v1",
            provider_label="openrouter",
        )
