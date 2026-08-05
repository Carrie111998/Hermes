"""Observed/replied-media caching methods for ``TelegramAdapter``.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the same mechanical mixin lift
that produced ``gateway/authz_mixin.py`` and the Telegram authorization
mixin (PR #75742). This mixin holds the c5 cluster: classifying Telegram media messages and downloading observed or replied-to attachments into the local media cache (bounded by ``_max_doc_bytes``), with surface-failure notices on both ends.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
Class attributes (none (``_max_doc_bytes`` is an instance attribute)) stay on ``TelegramAdapter`` and
resolve via ``self.*`` / ``cls.*`` through the MRO, exactly as before the
lift, and ``MediaCacheMixin`` precedes ``BasePlatformAdapter`` in the bases.

``logger`` is bound by explicit name so records emitted from these methods
keep the logger name ``"plugins.platforms.telegram.adapter"``. ``Message``
is imported under the same ``ImportError`` guard the adapter uses, falling
back to ``Any``; like the adapter, this module does not enable postponed
annotation evaluation.
"""

import logging
import os
from typing import Any, Optional

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
)

try:
    from telegram import Message
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    Message = Any

logger = logging.getLogger("plugins.platforms.telegram.adapter")

class MediaCacheMixin:
    """Observed/replied-media caching cluster lifted verbatim from ``TelegramAdapter``."""

    def _media_message_type(self, msg: Message) -> MessageType:
        """Classify a Telegram media message into a MessageType."""
        if msg.sticker:
            return MessageType.STICKER
        if msg.photo:
            return MessageType.PHOTO
        if msg.video:
            return MessageType.VIDEO
        if msg.audio:
            return MessageType.AUDIO
        if msg.voice:
            return MessageType.VOICE
        return MessageType.DOCUMENT

    async def _cache_observed_media(self, msg: Message, event: MessageEvent) -> None:
        """Cache an unmentioned group attachment and annotate the observed text.

        Passive group traffic, so downloads are bounded by the same
        ``_max_doc_bytes`` limit as the addressed document path. Oversized or
        unsupported attachments are noted in the transcript without downloading.
        """
        from gateway.platforms.base import cache_media_bytes

        source, filename, mime, kind = self._observed_media_source(msg)
        if source is None:
            return

        max_bytes = getattr(self, "_max_doc_bytes", 20 * 1024 * 1024)
        file_size = getattr(source, "file_size", None)
        try:
            size = int(file_size or 0)
        except (TypeError, ValueError):
            size = 0
        if not (0 < size <= max_bytes):
            limit_mb = max_bytes // (1024 * 1024)
            event.text = self._append_observed_note(
                event.text,
                f"[Observed Telegram attachment too large or unverifiable. Maximum: {limit_mb} MB.]",
            )
            logger.info("[Telegram] Observed group attachment skipped (size=%s)", file_size)
            return

        try:
            file_obj = await source.get_file()
            data = bytes(await file_obj.download_as_bytearray())
            if not filename:
                filename = os.path.basename(getattr(file_obj, "file_path", "") or "")
            cached = cache_media_bytes(data, filename=filename, mime_type=mime, default_kind=kind)
        except Exception as exc:
            logger.warning("[Telegram] Failed to cache observed group media: %s", _redact_telegram_error_text(exc), exc_info=True)
            return

        if cached is None:
            # Only reachable for images that fail validation now — any other
            # file type is always cached (authorization is the gate, not the
            # extension).
            event.text = self._append_observed_note(
                event.text, "[Observed Telegram attachment could not be read, not cached.]"
            )
            return

        event.media_urls = [cached.path]
        event.media_types = [cached.media_type]
        if cached.kind == "image":
            event.message_type = MessageType.PHOTO
        elif cached.kind == "video":
            event.message_type = MessageType.VIDEO
        elif cached.kind == "audio":
            event.message_type = MessageType.AUDIO
        event.text = self._append_observed_note(event.text, cached.context_note())
        logger.info("[Telegram] Cached observed group %s at %s", cached.kind, cached.path)

    async def _cache_replied_media(self, msg: Any, event: MessageEvent) -> None:
        """Cache media from the message this turn replies to, if any."""
        from gateway.platforms.base import cache_media_bytes

        reply_msg = getattr(msg, "reply_to_message", None)
        if reply_msg is None:
            return
        source, filename, mime, kind = self._observed_media_source(reply_msg)
        if source is None:
            return

        max_bytes = getattr(self, "_max_doc_bytes", 20 * 1024 * 1024)
        file_size = getattr(source, "file_size", None)
        try:
            size = int(file_size or 0)
        except (TypeError, ValueError):
            size = 0
        if not (0 < size <= max_bytes):
            return

        try:
            file_obj = await source.get_file()
            data = bytes(await file_obj.download_as_bytearray())
            if not filename:
                filename = os.path.basename(getattr(file_obj, "file_path", "") or "")
            cached = cache_media_bytes(data, filename=filename, mime_type=mime, default_kind=kind)
        except Exception as exc:
            logger.warning("[Telegram] Failed to cache replied-to media: %s", _redact_telegram_error_text(exc), exc_info=True)
            return

        if cached is None:
            return

        event.media_urls.append(cached.path)
        event.media_types.append(cached.media_type)
        if len(event.media_urls) == 1:
            if cached.kind == "image":
                event.message_type = MessageType.PHOTO
            elif cached.kind == "video":
                event.message_type = MessageType.VIDEO
            elif cached.kind == "audio":
                event.message_type = MessageType.AUDIO
        event.text = self._append_observed_note(
            event.text,
            f"[Replied-to {cached.kind} '{cached.display_name}' saved at: {cached.path}]",
        )
        logger.info("[Telegram] Cached replied-to %s at %s", cached.kind, cached.path)

    def _observed_media_source(self, msg: Message):
        """Return (telegram_file_source, filename, mime, default_kind) or Nones."""
        if msg.photo:
            return msg.photo[-1], "", "", "image"
        if msg.video:
            return msg.video, "", "video/mp4", "video"
        if msg.voice:
            return msg.voice, "voice.ogg", "audio/ogg", "audio"
        if msg.audio:
            return msg.audio, getattr(msg.audio, "file_name", "") or "", "", "audio"
        if msg.document:
            doc = msg.document
            return doc, doc.file_name or "", (doc.mime_type or "").lower(), None
        return None, "", "", None

    @staticmethod
    def _append_observed_note(existing: Optional[str], note: str) -> str:
        if not note:
            return existing or ""
        if not existing:
            return note
        return f"{existing}\n\n{note}"

    async def _surface_media_cache_failure(
        self,
        msg: Message,
        event: MessageEvent,
        kind: str,
        exc: Exception,
        display_name: Optional[str] = None,
    ) -> None:
        """Surface a failed media download/cache on BOTH ends instead of swallowing it.

        When download_as_bytearray()/cache_*_from_bytes() raises (typically a
        transient httpx.ConnectError to Telegram's CDN), the attachment never
        made it into event.media_urls. Without this, the handler falls through
        and dispatches an empty turn: the user thinks the file was delivered,
        the agent sees nothing, and the only record is a buried log warning.

        This (1) replies to the user in Telegram so they know to retry, and
        (2) appends an agent-visible notice to event.text via the existing
        observed-note channel so the agent knows an attachment was attempted
        and failed — never a silent empty turn. No new event fields (the
        structured-event refactor is out of scope per #23045).
        """
        named = f" ({display_name})" if display_name else ""
        try:
            await msg.reply_text(
                f"\u26a0\ufe0f Couldn't download your {kind}{named} "
                f"({exc.__class__.__name__}). Please try sending it again."
            )
        except Exception as reply_err:
            logger.warning(
                "[Telegram] Failed to notify user about %s cache failure: %s",
                kind,
                reply_err,
                exc_info=True,
            )
        agent_note = (
            f"[The user attempted to send a {kind}{named} but it could not be "
            f"downloaded ({exc.__class__.__name__}); they have been asked to retry.]"
        )
        event.text = self._append_observed_note(event.text, agent_note)


from plugins.platforms.telegram.adapter import _redact_telegram_error_text
