"""Telegram video notes should reuse the existing STT path.

Round Telegram video messages are short voice-like clips with visual context.
They should remain video attachments for visual analysis, while their mp4 audio
track is also eligible for the normal transcription pipeline.
"""

from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import _event_media_is_stt_input


def test_telegram_video_note_video_attachment_is_stt_input():
    event = MessageEvent(
        text="",
        message_type=MessageType.VIDEO,
        media_urls=["/tmp/video_note.mp4"],
        media_types=["video/mp4"],
        metadata={"telegram_video_note": True},
    )

    assert _event_media_is_stt_input(event, 0) is True


def test_plain_video_attachment_is_not_automatic_stt_input():
    event = MessageEvent(
        text="",
        message_type=MessageType.VIDEO,
        media_urls=["/tmp/video.mp4"],
        media_types=["video/mp4"],
    )

    assert _event_media_is_stt_input(event, 0) is False
