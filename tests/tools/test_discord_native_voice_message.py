"""Tests for Discord native voice-message container validation (feature V1)."""

import pytest

from plugins.platforms.discord.voice_message import (
    DEFAULT_MAX_BYTES,
    VoiceAttachment,
    VoiceMessageError,
    enforce_single_audio,
    is_native_ogg,
    validate_native_voice_attachment,
    voice_attachment_policy,
)


# ---------------------------------------------------------------------------
# validate_native_voice_attachment
# ---------------------------------------------------------------------------

def test_native_ogg_audio_ogg_accepted():
    att = VoiceAttachment(
        filename="voice-message.ogg", size_bytes=4096, content_type="audio/ogg"
    )
    assert validate_native_voice_attachment(att) == "voice-message.ogg"


def test_native_ogg_application_ogg_accepted():
    att = VoiceAttachment(
        filename="voice-message.ogg", size_bytes=4096, content_type="application/ogg"
    )
    assert validate_native_voice_attachment(att) == "voice-message.ogg"


def test_native_ogg_without_content_type_accepted():
    att = VoiceAttachment(filename="voice-message.ogg", size_bytes=4096, content_type=None)
    assert validate_native_voice_attachment(att) == "voice-message.ogg"


def test_mp3_rejected():
    att = VoiceAttachment(filename="recording.mp3", size_bytes=4096, content_type="audio/mpeg")
    with pytest.raises(VoiceMessageError):
        validate_native_voice_attachment(att)


def test_wav_rejected():
    att = VoiceAttachment(filename="recording.wav", size_bytes=4096, content_type="audio/wav")
    with pytest.raises(VoiceMessageError):
        validate_native_voice_attachment(att)


def test_mp3_renamed_to_ogg_rejected():
    # An MP3 cannot be renamed + labeled as a native voice message.
    att = VoiceAttachment(filename="recording.ogg", size_bytes=4096, content_type="audio/mpeg")
    with pytest.raises(VoiceMessageError):
        validate_native_voice_attachment(att)


def test_validate_error_is_value_error():
    att = VoiceAttachment(filename="recording.mp3", size_bytes=4096, content_type="audio/mpeg")
    with pytest.raises(ValueError):
        validate_native_voice_attachment(att)


# ---------------------------------------------------------------------------
# is_native_ogg
# ---------------------------------------------------------------------------

def test_is_native_ogg_lowercase():
    assert is_native_ogg("voice.ogg") is True


def test_is_native_ogg_case_insensitive():
    assert is_native_ogg("voice.OGG") is True
    assert is_native_ogg("voice.Ogg") is True


def test_is_native_ogg_rejects_other_extensions():
    assert is_native_ogg("voice.mp3") is False
    assert is_native_ogg("voice.wav") is False
    assert is_native_ogg("voice") is False
    assert is_native_ogg("voice.ogg.mp3") is False


# ---------------------------------------------------------------------------
# voice_attachment_policy
# ---------------------------------------------------------------------------

def test_policy_native_for_valid_ogg():
    att = VoiceAttachment(filename="voice.ogg", size_bytes=1024, content_type="audio/ogg")
    assert voice_attachment_policy(att) == "native"


def test_policy_native_without_size():
    att = VoiceAttachment(filename="voice.ogg", size_bytes=None, content_type="audio/ogg")
    assert voice_attachment_policy(att) == "native"


def test_policy_fallback_mp3_to_normal_attachment():
    att = VoiceAttachment(filename="recording.mp3", size_bytes=1024, content_type="audio/mpeg")
    assert voice_attachment_policy(att) == "normal_attachment"


def test_policy_fallback_wav_to_normal_attachment():
    att = VoiceAttachment(filename="recording.wav", size_bytes=1024, content_type="audio/wav")
    assert voice_attachment_policy(att) == "normal_attachment"


def test_policy_fallback_renamed_mp3_to_normal_attachment():
    # Not silently dropped and not labeled native: falls back to a normal attachment.
    att = VoiceAttachment(filename="recording.ogg", size_bytes=1024, content_type="audio/mpeg")
    assert voice_attachment_policy(att) == "normal_attachment"


def test_policy_fallback_no_content_type_non_ogg():
    att = VoiceAttachment(filename="clip.mp4", size_bytes=1024, content_type=None)
    assert voice_attachment_policy(att) == "normal_attachment"


def test_policy_raises_when_over_default_max():
    att = VoiceAttachment(
        filename="voice.ogg",
        size_bytes=DEFAULT_MAX_BYTES + 1,
        content_type="audio/ogg",
    )
    with pytest.raises(VoiceMessageError):
        voice_attachment_policy(att)


def test_policy_raises_when_over_custom_max():
    att = VoiceAttachment(filename="voice.ogg", size_bytes=2048, content_type="audio/ogg")
    with pytest.raises(VoiceMessageError):
        voice_attachment_policy(att, max_bytes=1024)


def test_policy_at_max_is_allowed():
    att = VoiceAttachment(filename="voice.ogg", size_bytes=DEFAULT_MAX_BYTES, content_type="audio/ogg")
    assert voice_attachment_policy(att) == "native"


def test_policy_oversized_non_native_also_raises():
    # The size bound is enforced regardless of native-ness.
    att = VoiceAttachment(filename="recording.mp3", size_bytes=DEFAULT_MAX_BYTES + 1, content_type="audio/mpeg")
    with pytest.raises(VoiceMessageError):
        voice_attachment_policy(att)


# ---------------------------------------------------------------------------
# enforce_single_audio
# ---------------------------------------------------------------------------

def test_single_audio_allowed():
    attachments = [
        VoiceAttachment(filename="voice.ogg", size_bytes=1024, content_type="audio/ogg"),
    ]
    enforce_single_audio(attachments)  # no raise


def test_single_audio_with_non_audio_attachment_allowed():
    attachments = [
        VoiceAttachment(filename="voice.ogg", size_bytes=1024, content_type="audio/ogg"),
        VoiceAttachment(filename="photo.png", size_bytes=1024, content_type="image/png"),
    ]
    enforce_single_audio(attachments)  # no raise


def test_no_audio_attachment_allowed():
    enforce_single_audio([])  # no raise
    enforce_single_audio(
        [VoiceAttachment(filename="doc.pdf", size_bytes=1024, content_type="application/pdf")]
    )  # no raise


def test_two_audio_attachments_raise():
    attachments = [
        VoiceAttachment(filename="a.mp3", size_bytes=1024, content_type="audio/mpeg"),
        VoiceAttachment(filename="b.wav", size_bytes=1024, content_type="audio/wav"),
    ]
    with pytest.raises(VoiceMessageError):
        enforce_single_audio(attachments)


def test_two_native_ogg_attachments_raise():
    attachments = [
        VoiceAttachment(filename="a.ogg", size_bytes=1024, content_type="audio/ogg"),
        VoiceAttachment(filename="b.ogg", size_bytes=1024, content_type="audio/ogg"),
    ]
    with pytest.raises(VoiceMessageError):
        enforce_single_audio(attachments)


def test_missing_content_type_not_counted_as_audio():
    # Attachment with no content type cannot be identified as audio; allowed.
    attachments = [
        VoiceAttachment(filename="a.ogg", size_bytes=1024, content_type=None),
        VoiceAttachment(filename="b.mp3", size_bytes=1024, content_type="audio/mpeg"),
    ]
    enforce_single_audio(attachments)  # no raise
