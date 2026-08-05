"""Inbound message handlers methods for ``TelegramAdapter``.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the same mechanical mixin lift
that produced ``gateway/authz_mixin.py`` and the Telegram authorization
mixin (PR #75742). This mixin holds the c7 cluster: the PTB update handlers that turn raw inbound updates into gateway ``MessageEvent``s: text/command/location/media/sticker handling, lazy forum-command registration, and the effective-message resolver for channel posts.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
Class attributes (``_SPLIT_THRESHOLD``) stay on ``TelegramAdapter`` and
resolve via ``self.*`` / ``cls.*`` through the MRO, exactly as before the
lift, and ``InboundHandlersMixin`` precedes ``BasePlatformAdapter`` in the bases.

``logger`` is bound by explicit name so records emitted from these methods
keep the logger name ``"plugins.platforms.telegram.adapter"``. ``Message``
is imported under the same ``ImportError`` guard the adapter uses, falling
back to ``Any``; like the adapter, this module does not enable postponed
annotation evaluation.
"""

import json
import logging
import os
import re
from typing import Any, Optional

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_IMAGE_DOCUMENT_TYPES,
    SUPPORTED_VIDEO_TYPES,
    _TEXT_INJECT_EXTENSIONS,
)

try:
    from telegram import Message, Update
    from telegram.ext import ContextTypes
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    Message = Any
    Update = Any

    # Mock ContextTypes so type annotations using ContextTypes.DEFAULT_TYPE
    # don't crash during class definition when the library isn't installed.
    class _MockContextTypes:
        DEFAULT_TYPE = Any
    ContextTypes = _MockContextTypes

logger = logging.getLogger("plugins.platforms.telegram.adapter")

# Telegram image-type tables. Moved with the media handler that is the
# only consumer of these constants (module-level in adapter.py).
_TELEGRAM_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_TELEGRAM_IMAGE_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_TELEGRAM_IMAGE_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

class InboundHandlersMixin:
    """Inbound message handlers cluster lifted verbatim from ``TelegramAdapter``."""

    async def _ensure_forum_commands(self, message) -> None:
        """Lazy-register bot commands for forum supergroups.

        Forum topics don't inherit AllGroupChats scope — Telegram resolves
        via BotCommandScopeChat(chat_id).  Register on first message so the
        command menu works in topic views.
        """
        async with self._forum_lock:
            try:
                chat = getattr(message, "chat", None)
                if not chat or not getattr(chat, "is_forum", False):
                    return
                chat_id = int(chat.id)
                if chat_id in self._forum_command_registered:
                    return
                from telegram import BotCommand, BotCommandScopeChat
                from hermes_cli.commands import telegram_menu_commands, telegram_menu_max_commands
                menu_commands, _ = telegram_menu_commands(max_commands=telegram_menu_max_commands())
                bot_commands = [BotCommand(name, desc) for name, desc in menu_commands]
                await self._bot.set_my_commands(bot_commands, scope=BotCommandScopeChat(chat_id=chat_id))
                self._forum_command_registered.add(chat_id)
                logger.info("[%s] Lazy-registered %d commands for forum chat %s", self.name, len(bot_commands), chat_id)
            except Exception as e:
                logger.warning("[%s] Forum command lazy-registration failed: %s", self.name, _redact_telegram_error_text(e))

    def _effective_update_message(self, update: Update) -> Optional[Message]:
        """Return the message-like payload for normal messages and channel posts.

        Telegram exposes channel broadcasts as ``update.channel_post`` rather
        than ``update.message``.  MessageHandler filters can still dispatch
        those updates, so handlers must use ``effective_message`` to avoid
        consuming channel posts without ever building a gateway event.
        """
        return getattr(update, "effective_message", None) or getattr(update, "message", None)

    async def _handle_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming text messages.

        Telegram clients split long messages into multiple updates.  Buffer
        rapid successive text messages from the same user/chat and aggregate
        them into a single MessageEvent before dispatching.
        """
        msg = self._effective_update_message(update)
        if not msg or not msg.text:
            return
        # Early user-level auth check: reject unauthorized users before any
        # text batching, observe-buffer persistence, event building, or response
        # generation. This prevents removed/blocked users from injecting prompts
        # into the agent path or the observed transcript context (#40863).
        if not self._is_user_authorized_from_message(msg):
            logger.warning(
                "[Telegram] Blocked unauthorized user %s in chat %s",
                getattr(getattr(msg, "from_user", None), "id", None),
                getattr(getattr(msg, "chat", None), "id", None),
            )
            return
        if not self._should_process_message(msg):
            if self._should_observe_unmentioned_group_message(msg):
                self._observe_unmentioned_group_message(msg, MessageType.TEXT, update_id=update.update_id)
            return
        await self._ensure_forum_commands(update.message)

        event = self._build_message_event(msg, MessageType.TEXT, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        await self._cache_replied_media(msg, event)
        event = self._apply_telegram_group_observe_attribution(event)
        self._enqueue_text_event(event)

    async def _handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming command messages."""
        msg = self._effective_update_message(update)
        if not msg or not msg.text:
            return
        if not self._should_process_message(msg, is_command=True):
            return
        if not self._is_user_authorized_from_message(msg):
            logger.warning(
                "[Telegram] Blocked unauthorized user %s in chat %s",
                getattr(getattr(msg, "from_user", None), "id", None),
                getattr(getattr(msg, "chat", None), "id", None),
            )
            return
        await self._ensure_forum_commands(msg)

        event = self._build_message_event(msg, MessageType.COMMAND, update_id=update.update_id)
        event.text = self._clean_bot_trigger_text(event.text)
        await self._cache_replied_media(msg, event)
        event = self._apply_telegram_group_observe_attribution(event)
        # Telegram clients split messages above 4096 chars into multiple
        # updates.  A long command paste (e.g. ``/queue <huge prompt>``)
        # arrives as a COMMAND chunk near the limit followed by plain TEXT
        # continuation chunk(s).  Dispatching the command immediately would
        # orphan the continuation, which then lands as a separate message and
        # interrupts the running agent.  Route near-limit command chunks
        # through the same text-batching pipeline so continuations merge in
        # before dispatch; short commands (/stop, /approve, ...) keep the
        # immediate path and are never delayed.
        if len(event.text or "") >= self._SPLIT_THRESHOLD:
            self._enqueue_text_event(event)
            return
        await self.handle_message(event)

    async def _handle_location_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming location/venue pin messages."""
        msg = self._effective_update_message(update)
        if not msg:
            return
        if not self._is_user_authorized_from_message(msg):
            logger.warning(
                "[Telegram] Blocked unauthorized user %s in chat %s",
                getattr(getattr(msg, "from_user", None), "id", None),
                getattr(getattr(msg, "chat", None), "id", None),
            )
            return
        if not self._should_process_message(msg):
            if self._should_observe_unmentioned_group_message(msg):
                self._observe_unmentioned_group_message(msg, MessageType.LOCATION, update_id=update.update_id)
            return

        venue = getattr(msg, "venue", None)
        location = getattr(venue, "location", None) if venue else getattr(msg, "location", None)

        if not location:
            return

        lat = getattr(location, "latitude", None)
        lon = getattr(location, "longitude", None)
        if lat is None or lon is None:
            return

        # Build a text message with coordinates and context
        parts = ["[The user shared a location pin.]"]
        if venue:
            title = getattr(venue, "title", None)
            address = getattr(venue, "address", None)
            if title:
                parts.append(f"Venue: {title}")
            if address:
                parts.append(f"Address: {address}")
        parts.append(f"latitude: {lat}")
        parts.append(f"longitude: {lon}")
        parts.append(f"Map: https://www.google.com/maps/search/?api=1&query={lat},{lon}")
        parts.append("Ask what they'd like to find nearby (restaurants, cafes, etc.) and any preferences.")

        event = self._build_message_event(msg, MessageType.LOCATION, update_id=update.update_id)
        event.text = "\n".join(parts)
        event = self._apply_telegram_group_observe_attribution(event)
        await self.handle_message(event)

    async def _handle_media_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle incoming media messages, downloading images to local cache."""
        if not update.message:
            return
        if not self._is_user_authorized_from_message(update.message):
            logger.info(
                "[Telegram] Blocked media from unauthorized user %s in chat %s",
                getattr(getattr(update.message, "from_user", None), "id", None),
                getattr(getattr(update.message, "chat", None), "id", None),
            )
            return
        if not self._should_process_message(update.message):
            if self._should_observe_unmentioned_group_message(update.message):
                _m = update.message
                _observe_type = self._media_message_type(_m)
                _event = self._build_message_event(_m, _observe_type, update_id=update.update_id)
                if _m.caption:
                    _event.text = self._clean_bot_trigger_text(_m.caption)
                await self._cache_observed_media(_m, _event)
                self._observe_unmentioned_group_message(
                    _m, _event.message_type, update_id=update.update_id, event=_event
                )
            return

        msg = update.message

        msg_type = self._media_message_type(msg)

        event = self._build_message_event(msg, msg_type, update_id=update.update_id)
        
        # Add caption as text
        if msg.caption:
            event.text = self._clean_bot_trigger_text(msg.caption)
        
        # Handle stickers: describe via vision tool with caching
        if msg.sticker:
            await self._handle_sticker(msg, event)
            event = self._apply_telegram_group_observe_attribution(event)
            await self.handle_message(event)
            return

        # Apply observe attribution after caption is set; sticker is handled above
        # because _handle_sticker overwrites event.text with its vision description.
        event = self._apply_telegram_group_observe_attribution(event)

        # Download photo to local image cache so the vision tool can access it
        # even after Telegram's ephemeral file URLs expire (~1 hour).
        if msg.photo:
            try:
                # msg.photo is a list of PhotoSize sorted by size; take the largest
                photo = msg.photo[-1]
                file_obj = await photo.get_file()
                # Download the image bytes directly into memory
                image_bytes = await file_obj.download_as_bytearray()
                # Determine extension from the file path if available
                ext = ".jpg"
                if file_obj.file_path:
                    for candidate in [".png", ".webp", ".gif", ".jpeg", ".jpg"]:
                        if file_obj.file_path.lower().endswith(candidate):
                            ext = candidate
                            break
                # Save to local cache (for vision tool access)
                cached_path = cache_image_from_bytes(bytes(image_bytes), ext=ext)
                event.media_urls = [cached_path]
                event.media_types = [f"image/{ext.lstrip('.')}" ]
                logger.info("[Telegram] Cached user photo at %s", cached_path)
                media_group_id = getattr(msg, "media_group_id", None)
                if media_group_id:
                    await self._queue_media_group_event(str(media_group_id), event)
                else:
                    batch_key = self._photo_batch_key(event, msg)
                    self._enqueue_photo_event(batch_key, event)
                return

            except Exception as e:
                logger.warning("[Telegram] Failed to cache photo: %s", _redact_telegram_error_text(e), exc_info=True)
                await self._surface_media_cache_failure(msg, event, "photo", e)

        # Download voice/audio messages to cache for STT transcription
        if msg.voice:
            try:
                allowed, note = self._telegram_media_size_allowed(msg.voice, "voice message")
                if not allowed:
                    event.text = self._append_observed_note(event.text, note or "")
                    logger.info("[Telegram] Skipped oversized user voice (size=%s)", getattr(msg.voice, "file_size", None))
                    await self.handle_message(event)
                    return
                file_obj = await msg.voice.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = cache_audio_from_bytes(bytes(audio_bytes), ext=".ogg")
                event.media_urls = [cached_path]
                event.media_types = ["audio/ogg"]
                logger.info("[Telegram] Cached user voice at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache voice: %s", _redact_telegram_error_text(e), exc_info=True)
                await self._surface_media_cache_failure(msg, event, "voice message", e)
        elif msg.audio:
            try:
                allowed, note = self._telegram_media_size_allowed(msg.audio, "audio file")
                if not allowed:
                    event.text = self._append_observed_note(event.text, note or "")
                    logger.info("[Telegram] Skipped oversized user audio (size=%s)", getattr(msg.audio, "file_size", None))
                    await self.handle_message(event)
                    return
                file_obj = await msg.audio.get_file()
                audio_bytes = await file_obj.download_as_bytearray()
                cached_path = cache_audio_from_bytes(bytes(audio_bytes), ext=".mp3")
                event.media_urls = [cached_path]
                event.media_types = ["audio/mp3"]
                logger.info("[Telegram] Cached user audio at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache audio: %s", _redact_telegram_error_text(e), exc_info=True)
                await self._surface_media_cache_failure(msg, event, "audio file", e)

        elif msg.video:
            try:
                allowed, note = self._telegram_media_size_allowed(msg.video, "video file")
                if not allowed:
                    event.text = self._append_observed_note(event.text, note or "")
                    logger.info("[Telegram] Skipped oversized user video (size=%s)", getattr(msg.video, "file_size", None))
                    await self.handle_message(event)
                    return
                file_obj = await msg.video.get_file()
                video_bytes = await file_obj.download_as_bytearray()
                ext = ".mp4"
                if getattr(file_obj, "file_path", None):
                    for candidate in SUPPORTED_VIDEO_TYPES:
                        if file_obj.file_path.lower().endswith(candidate):
                            ext = candidate
                            break
                cached_path = cache_video_from_bytes(bytes(video_bytes), ext=ext)
                event.media_urls = [cached_path]
                event.media_types = [SUPPORTED_VIDEO_TYPES.get(ext, "video/mp4")]
                logger.info("[Telegram] Cached user video at %s", cached_path)
            except Exception as e:
                logger.warning("[Telegram] Failed to cache video: %s", _redact_telegram_error_text(e), exc_info=True)
                await self._surface_media_cache_failure(msg, event, "video file", e)

        # Download document files to cache for agent processing
        elif msg.document:
            doc = msg.document
            try:
                # Determine file extension
                ext = ""
                original_filename = doc.file_name or ""
                if original_filename:
                    _, ext = os.path.splitext(original_filename)
                    ext = ext.lower()

                # Normalize mime_type for robust comparisons (some clients send
                # uppercase like "IMAGE/PNG").
                doc_mime = (doc.mime_type or "").lower()

                # If no extension from filename, reverse-lookup from MIME type
                if not ext and doc_mime:
                    ext = _TELEGRAM_IMAGE_MIME_TO_EXT.get(doc_mime, "")
                    if not ext:
                        mime_to_ext = {v: k for k, v in SUPPORTED_DOCUMENT_TYPES.items()}
                        ext = mime_to_ext.get(doc_mime, "")

                # Check file size early so image documents cannot bypass the
                # document size limit by taking the image path.
                if not doc.file_size or doc.file_size > self._max_doc_bytes:
                    limit_mb = self._max_doc_bytes // (1024 * 1024)
                    event.text = (
                        "The document is too large or its size could not be verified. "
                        f"Maximum: {limit_mb} MB."
                    )
                    logger.info("[Telegram] Document too large: %s bytes", doc.file_size)
                    await self.handle_message(event)
                    return

                # Telegram may deliver screenshots/photos as documents. If the
                # payload is actually an image, route it through the image cache
                # and batching path instead of rejecting it as a document.
                if ext in _TELEGRAM_IMAGE_EXTENSIONS or doc_mime.startswith("image/"):
                    file_obj = await doc.get_file()
                    image_bytes = await file_obj.download_as_bytearray()
                    image_ext = ext if ext in _TELEGRAM_IMAGE_EXTENSIONS else _TELEGRAM_IMAGE_MIME_TO_EXT.get(doc_mime, ".jpg")
                    try:
                        cached_path = cache_image_from_bytes(bytes(image_bytes), ext=image_ext)
                    except ValueError as e:
                        logger.warning("[Telegram] Failed to cache image document: %s", _redact_telegram_error_text(e), exc_info=True)
                        event.text = (
                            f"Image document '{original_filename or doc_mime or ext or 'unknown'}' "
                            "could not be read as an image."
                        )
                        await self.handle_message(event)
                        return

                    event.message_type = MessageType.PHOTO
                    event.media_urls = [cached_path]
                    event.media_types = [doc_mime if doc_mime.startswith("image/") else _TELEGRAM_IMAGE_EXT_TO_MIME.get(image_ext, "image/jpeg")]
                    logger.info("[Telegram] Cached user image-document at %s", cached_path)

                    media_group_id = getattr(msg, "media_group_id", None)
                    if media_group_id:
                        await self._queue_media_group_event(str(media_group_id), event)
                    else:
                        batch_key = self._photo_batch_key(event, msg)
                        self._enqueue_photo_event(batch_key, event)
                    return

                if not ext and doc.mime_type:
                    video_mime_to_ext = {v: k for k, v in SUPPORTED_VIDEO_TYPES.items()}
                    ext = video_mime_to_ext.get(doc.mime_type, "")

                if not ext and doc.mime_type:
                    # SUPPORTED_IMAGE_DOCUMENT_TYPES has duplicate values (.jpg + .jpeg
                    # both map to image/jpeg); keep the first ext we encounter.
                    image_mime_to_ext: dict[str, str] = {}
                    for _ext, _mime in SUPPORTED_IMAGE_DOCUMENT_TYPES.items():
                        image_mime_to_ext.setdefault(_mime, _ext)
                    ext = image_mime_to_ext.get(doc.mime_type, "")

                if ext in SUPPORTED_VIDEO_TYPES:
                    file_obj = await doc.get_file()
                    video_bytes = await file_obj.download_as_bytearray()
                    cached_path = cache_video_from_bytes(bytes(video_bytes), ext=ext)
                    event.media_urls = [cached_path]
                    event.media_types = [SUPPORTED_VIDEO_TYPES[ext]]
                    event.message_type = MessageType.VIDEO
                    logger.info("[Telegram] Cached user video document at %s", cached_path)
                    await self.handle_message(event)
                    return

                # NOTE: image-document handling is performed earlier in this
                # function (ext in _TELEGRAM_IMAGE_EXTENSIONS or image/* mime),
                # which returns before reaching here.  Any subsequent
                # ext-in-SUPPORTED_IMAGE_DOCUMENT_TYPES branch would be dead
                # code — the extension sets are identical.

                # Download and cache. Any file type is accepted — authorization
                # to message the agent is the gate, not the file extension.
                # Known types keep their precise MIME; unknown types are tagged
                # application/octet-stream so the agent reaches for terminal tools.
                file_obj = await doc.get_file()
                doc_bytes = await file_obj.download_as_bytearray()
                raw_bytes = bytes(doc_bytes)
                from gateway.platforms.base import cache_media_bytes

                cached = cache_media_bytes(
                    raw_bytes,
                    filename=original_filename or f"document{ext or '.bin'}",
                    mime_type=doc_mime,
                )
                if cached is None:
                    event.text = (
                        f"Document '{original_filename or doc_mime or ext or 'unknown'}' "
                        "could not be cached."
                    )
                    await self.handle_message(event)
                    return
                event.media_urls = [cached.path]
                event.media_types = [cached.media_type]
                if cached.kind == "audio":
                    event.message_type = MessageType.AUDIO
                logger.info(
                    "[Telegram] Cached user %s at %s (%s)",
                    cached.kind,
                    cached.path,
                    cached.media_type,
                )

                # For text-readable files, inject content into event.text (capped
                # at 100 KB). Gate on a text-like extension/MIME — NOT a blind
                # UTF-8 decode, since binary formats (PDF/zip/docx) can have
                # decodable ASCII headers. Binary files are surfaced as a cached
                # path only (run.py emits a path-pointing context note).
                MAX_TEXT_INJECT_BYTES = 100 * 1024
                _is_text = ext in _TEXT_INJECT_EXTENSIONS or (doc_mime or "").startswith("text/")
                if _is_text and len(raw_bytes) <= MAX_TEXT_INJECT_BYTES:
                    try:
                        text_content = raw_bytes.decode("utf-8")
                        display_name = original_filename or f"document{ext or '.txt'}"
                        display_name = re.sub(r'[^\w.\- ]', '_', display_name)
                        injection = f"[Content of {display_name}]:\n{text_content}"
                        if event.text:
                            event.text = f"{injection}\n\n{event.text}"
                        else:
                            event.text = injection
                    except UnicodeDecodeError:
                        # Binary file — agent has the cached path and can use
                        # terminal/read_file against it. No inline injection.
                        pass

            except Exception as e:
                logger.warning("[Telegram] Failed to cache document: %s", _redact_telegram_error_text(e), exc_info=True)
                await self._surface_media_cache_failure(
                    msg, event, "attachment", e,
                    display_name=getattr(doc, "file_name", None) or None,
                )

        media_group_id = getattr(msg, "media_group_id", None)
        if media_group_id:
            await self._queue_media_group_event(str(media_group_id), event)
            return

        await self.handle_message(event)

    async def _handle_sticker(self, msg: Message, event: "MessageEvent") -> None:
        """
        Describe a Telegram sticker via vision analysis, with caching.

        For static stickers (WEBP), we download, analyze with vision, and cache
        the description by file_unique_id. For animated/video stickers, we inject
        a placeholder noting the emoji.
        """
        from gateway.sticker_cache import (
            get_cached_description,
            cache_sticker_description,
            build_sticker_injection,
            build_animated_sticker_injection,
            STICKER_VISION_PROMPT,
        )

        sticker = msg.sticker
        emoji = sticker.emoji or ""
        set_name = sticker.set_name or ""

        # Animated and video stickers can't be analyzed as static images
        if sticker.is_animated or sticker.is_video:
            event.text = build_animated_sticker_injection(emoji)
            return

        # Check the cache first
        cached = get_cached_description(sticker.file_unique_id)
        if cached:
            event.text = build_sticker_injection(
                cached["description"], cached.get("emoji", emoji), cached.get("set_name", set_name)
            )
            logger.info("[Telegram] Sticker cache hit: %s", sticker.file_unique_id)
            return

        # Cache miss -- download and analyze
        try:
            file_obj = await sticker.get_file()
            image_bytes = await file_obj.download_as_bytearray()
            cached_path = cache_image_from_bytes(bytes(image_bytes), ext=".webp")
            logger.info("[Telegram] Analyzing sticker at %s", cached_path)

            from tools.vision_tools import vision_analyze_tool
            result_json = await vision_analyze_tool(
                image_url=cached_path,
                user_prompt=STICKER_VISION_PROMPT,
            )
            result = json.loads(result_json)

            if result.get("success"):
                description = result.get("analysis", "a sticker")
                cache_sticker_description(sticker.file_unique_id, description, emoji, set_name)
                event.text = build_sticker_injection(description, emoji, set_name)
            else:
                # Vision failed -- use emoji as fallback
                event.text = build_sticker_injection(
                    f"a sticker with emoji {emoji}" if emoji else "a sticker",
                    emoji, set_name,
                )
        except Exception as e:
            logger.warning("[Telegram] Sticker analysis error: %s", _redact_telegram_error_text(e), exc_info=True)
            event.text = build_sticker_injection(
                f"a sticker with emoji {emoji}" if emoji else "a sticker",
                emoji, set_name,
            )


from plugins.platforms.telegram.adapter import _redact_telegram_error_text



# Media-cache helpers are delegated through the adapter module at call
# time so runtime patching of
# ``plugins.platforms.telegram.adapter.cache_image_from_bytes`` /
# ``cache_audio_from_bytes`` / ``cache_video_from_bytes`` (existing
# gateway tests do exactly that) keeps affecting the lifted handlers.
# The adapter module object is resolved lazily to stay circular-import
# safe: the adapter imports this mixin at module top, and by the time
# these helpers run the adapter module is fully loaded.
from plugins.platforms.telegram import adapter as _adapter_mod


def cache_image_from_bytes(*args, **kwargs):
    return _adapter_mod.cache_image_from_bytes(*args, **kwargs)


def cache_audio_from_bytes(*args, **kwargs):
    return _adapter_mod.cache_audio_from_bytes(*args, **kwargs)


def cache_video_from_bytes(*args, **kwargs):
    return _adapter_mod.cache_video_from_bytes(*args, **kwargs)
