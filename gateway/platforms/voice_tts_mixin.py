"""Auto-TTS decision and voice-message delivery for platform adapters.

Extracted verbatim from ``gateway/platforms/base.py`` (godfile decomposition
wave 1, shard s3, cluster c16: ``_should_auto_tts_for_chat``, ``send_voice``, ``prepare_tts_text``, ``play_tts``).  The mixin is a base of
``BasePlatformAdapter``; the ``_auto_tts_*`` instance state set by ``BasePlatformAdapter.__init__``
and the ``send``/``name`` members stay on the adapter and resolve via MRO.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional

# Same logger object as ``gateway.platforms.base`` (logging keeps a
# name-keyed singleton registry), so log records keep the historical
# ``gateway.platforms.base`` name.
logger = logging.getLogger("gateway.platforms.base")


class VoiceTtsMixin:
    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        """Whether auto-TTS on voice input should fire for ``chat_id``.

        Decision layers (Issue #16007):
          1. Explicit ``/voice on`` or ``/voice tts`` → always fire (even if
             ``voice.auto_tts`` is False).
          2. Explicit ``/voice off`` → never fire.
          3. Fall back to the global ``voice.auto_tts`` config default.
        """
        if chat_id in self._auto_tts_enabled_chats:
            return True
        if chat_id in self._auto_tts_disabled_chats:
            return False
        return bool(self._auto_tts_default)


    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """
        Send an audio file as a native voice message via the platform API.

        Override in subclasses to send audio as voice bubbles (Telegram)
        or file attachments (Discord). Default falls back to a friendly
        notice — never echo the local audio_path into chat, since it is a
        host filesystem path that would leak the Hermes home layout.
        """
        # audio_path is intentionally NOT included in the chat text — it is a
        # host-local path that leaks filesystem layout. The path is logged for
        # operator diagnostics instead.
        logger.warning(
            "[%s] send_voice fallback: native audio send unavailable for %s",
            self.name, audio_path,
        )
        text = "⚠️ Couldn't deliver the audio attachment."
        if caption:
            text = f"{caption}\n{text}"
        return await self.send(chat_id=chat_id, content=text, reply_to=reply_to, metadata=metadata)

    def prepare_tts_text(self, text: str) -> str:
        """Prepare a spoken script for TTS.

        Auto-TTS should not feed raw chat Markdown, ``<think>`` reasoning
        blocks, or compact symbols to the speech provider.  It should receive
        a transcript-like script: reasoning blocks removed, headings and
        bullets flattened into sentence pauses, and units like ``°C``
        expanded to words such as ``degrees Celsius``.
        """
        try:
            from tools.tts_text_normalize import prepare_spoken_text
            return prepare_spoken_text(text, max_chars=4000)
        except Exception:
            # Keep auto-TTS best-effort if the normalizer ever fails.
            text = re.sub(r'<think[\s>].*?</think>', ' ', text, flags=re.DOTALL)
            return re.sub(r'[*_`#\[\]()]', '', text)[:4000].strip()

    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        **kwargs,
    ) -> SendResult:
        """
        Play auto-TTS audio for voice replies.

        Override in subclasses for invisible playback (e.g. Web UI).
        Default falls back to send_voice (shows audio player).
        """
        return await self.send_voice(chat_id=chat_id, audio_path=audio_path, **kwargs)

