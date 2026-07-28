"""OpenAI-compatible TTS response-format fallback behavior."""

from unittest.mock import MagicMock, patch

import pytest

from tools.tts_tool import _generate_openai_tts


class _FormatError(Exception):
    status_code = 422


def _client_with_responses(*responses):
    client = MagicMock()
    client.audio.speech.create.side_effect = responses
    return client


def test_retries_unsupported_opus_as_mp3(tmp_path):
    output_path = tmp_path / "voice.ogg"
    response = MagicMock()
    client = _client_with_responses(
        _FormatError("response_format must be mp3, flac, wav, or pcm"),
        response,
    )

    with patch("tools.tts_tool._import_openai_client", return_value=lambda **_: client):
        result = _generate_openai_tts(
            "hello",
            str(output_path),
            {},
            api_key="test-key",
            base_url="http://tts.example/v1",
        )

    assert result == str(output_path)
    assert [
        call.kwargs["response_format"]
        for call in client.audio.speech.create.call_args_list
    ] == [
        "opus",
        "mp3",
    ]
    first_headers = client.audio.speech.create.call_args_list[0].kwargs["extra_headers"]
    retry_headers = client.audio.speech.create.call_args_list[1].kwargs["extra_headers"]
    assert first_headers["x-idempotency-key"] != retry_headers["x-idempotency-key"]
    response.stream_to_file.assert_called_once_with(str(output_path))


@pytest.mark.parametrize(
    "error",
    [
        _FormatError("voice is unsupported"),
        type("_ServerError", (Exception,), {"status_code": 500})(
            "response_format failed"
        ),
    ],
)
def test_does_not_retry_unrelated_errors(tmp_path, error):
    client = _client_with_responses(error)

    with patch("tools.tts_tool._import_openai_client", return_value=lambda **_: client):
        with pytest.raises(type(error), match=str(error)):
            _generate_openai_tts(
                "hello",
                str(tmp_path / "voice.ogg"),
                {},
                api_key="test-key",
                base_url="http://tts.example/v1",
            )

    client.audio.speech.create.assert_called_once()
