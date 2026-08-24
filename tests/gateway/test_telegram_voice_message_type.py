"""Telegram voice notes must tag MessageEvent.message_type = VOICE (#92165).

The inbound ``if msg.voice:`` branch cached the audio but never set
``event.message_type``, unlike its sibling photo/video/audio branches.
The outgoing auto-TTS reply gate (``_should_send_voice_reply``, checked
in ``gateway/platforms/base.py`` and ``gateway/run.py``) matches on
``message_type == MessageType.VOICE`` — so a Telegram voice note never
triggered a spoken reply, silently.

Harness mirrors ``test_telegram_documents.py`` (real adapter instance,
caches redirected to tmp_path, events captured via a mocked
``handle_message``).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    from plugins.platforms.telegram.adapter import TelegramAdapter

    monkeypatch.setattr(
        "gateway.platforms.base.AUDIO_CACHE_DIR", tmp_path / "audio_cache"
    )
    config = PlatformConfig(enabled=True, token="fake-token")
    a = TelegramAdapter(config)
    a.handle_message = AsyncMock()
    a._is_callback_user_authorized = lambda user_id, **_kw: True
    return a


def _voice_message(file_size=1024):
    voice = MagicMock()
    voice.file_size = file_size
    file_obj = AsyncMock()
    file_obj.download_as_bytearray = AsyncMock(return_value=bytearray(b"ogg"))
    file_obj.file_path = "voice/file.ogg"
    voice.get_file = AsyncMock(return_value=file_obj)

    msg = MagicMock()
    msg.message_id = 7
    msg.text = ""
    msg.caption = None
    msg.date = None
    msg.photo = None
    msg.video = None
    msg.audio = None
    msg.voice = voice
    msg.sticker = None
    msg.document = None
    msg.media_group_id = None
    msg.chat = MagicMock()
    msg.chat.id = 100
    msg.chat.type = "private"
    msg.chat.title = None
    msg.chat.full_name = "Tester"
    msg.from_user = MagicMock()
    msg.from_user.id = 1
    msg.from_user.full_name = "Tester"
    msg.message_thread_id = None
    msg.reply_text = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_voice_note_sets_message_type_voice(adapter):
    update = MagicMock()
    update.message = _voice_message()

    await adapter._handle_media_message(update, MagicMock())

    assert adapter.handle_message.await_count >= 1
    event = adapter.handle_message.call_args[0][0]
    assert event.media_types == ["audio/ogg"]
    assert event.message_type == MessageType.VOICE, (
        "auto-TTS reply gate matches message_type == VOICE (#92165)"
    )
