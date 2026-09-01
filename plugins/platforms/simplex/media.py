"""Inbound transfer state and outbound media egress for SimpleX."""

from __future__ import annotations

import asyncio
import base64
import copy
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

from gateway.platforms.base import SendResult
from plugins.platforms.simplex.protocol import (
    response_error as _response_error,
    response_type as _response_type,
)

logger = logging.getLogger(__name__)


class SimplexMediaMixin:
    @staticmethod
    def _normalize_chat_item_wrapper(payload: dict) -> dict:
        """Normalize SimpleX AChatItem payload variants to {chatInfo, chatItem}.

        Depending on daemon version and event type, the chat item wrapper
        arrives in one of several shapes:

        * ``{"chatInfo": ..., "chatItem": ...}`` — already normalized
          (the usual ``newChatItems`` array element).
        * ``{"type": "newChatItem", "chatItem": {"chatInfo": ...,
          "chatItem": ...}}`` — the singular event nests the AChatItem one
          level down.
        * ``{"chatInfo": ..., "item": ...}`` — some responses name the item
          field ``item`` instead of ``chatItem``.

        ``_handle_chat_item`` only reads ``chatInfo``/``chatItem`` at the top
        level, so anything not normalized here would be silently dropped.
        """
        if not isinstance(payload, dict):
            return {}

        nested = payload.get("chatItem")
        if isinstance(nested, dict):
            # Nested AChatItem: {type: newChatItem, chatItem: {chatInfo, chatItem}}
            if isinstance(nested.get("chatInfo"), dict) and isinstance(
                nested.get("chatItem"), dict
            ):
                return nested
            # Already normalized: {chatInfo: ..., chatItem: {content/meta/...}}
            if isinstance(payload.get("chatInfo"), dict):
                return payload

        if isinstance(payload.get("chatInfo"), dict) and isinstance(
            payload.get("item"), dict
        ):
            return {"chatInfo": payload["chatInfo"], "chatItem": payload["item"]}

        return payload

    @staticmethod
    def _file_id_from_wrapper(wrapper: dict) -> Optional[int]:
        normalized = SimplexMediaMixin._normalize_chat_item_wrapper(wrapper)
        inner = normalized.get("chatItem", {}) if normalized else {}
        file_info = inner.get("file", {}) if isinstance(inner, dict) else {}
        raw_id = file_info.get("fileId") if isinstance(file_info, dict) else None
        try:
            return int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _item_id_from_wrapper(wrapper: dict) -> Optional[str]:
        normalized = SimplexMediaMixin._normalize_chat_item_wrapper(wrapper)
        inner = normalized.get("chatItem", {}) if normalized else {}
        meta = inner.get("meta", {}) if isinstance(inner, dict) else {}
        item_id = meta.get("itemId") if isinstance(meta, dict) else None
        return str(item_id) if item_id is not None else None

    def _file_sender_context(
        self, wrapper: dict
    ) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Return ``(user_id, chat_type, chat_id)`` for an attachment event."""
        normalized = self._normalize_chat_item_wrapper(wrapper)
        chat_info = normalized.get("chatInfo", {}) if normalized else {}
        inner = normalized.get("chatItem", {}) if normalized else {}
        chat_dir = inner.get("chatDir", {}) if isinstance(inner, dict) else {}
        chat_type = chat_info.get("type") if isinstance(chat_info, dict) else None
        if chat_type == "direct":
            contact = chat_info.get("contact", {}) or {}
            contact_id = contact.get("contactId")
            user_id = str(contact_id) if contact_id is not None else None
            return user_id, "dm", user_id
        if chat_type == "group":
            group = chat_info.get("groupInfo", {}) or {}
            group_id = group.get("groupId")
            member = chat_dir.get("groupMember", {}) if isinstance(chat_dir, dict) else {}
            member_contact_id = (
                member.get("memberContactId") if isinstance(member, dict) else None
            )
            member_id = member.get("memberId") if isinstance(member, dict) else None
            stable_member_id = (
                member_contact_id if member_contact_id is not None else member_id
            )
            user_id = (
                str(stable_member_id) if stable_member_id is not None else None
            )
            chat_id = f"group:{group_id}" if group_id is not None else None
            if (
                group_id is None
                or not self.group_allow_from
                or (
                    "*" not in self.group_allow_from
                    and str(group_id) not in self.group_allow_from
                )
            ):
                return user_id, "group", None
            return user_id, "group", chat_id
        return None, None, None

    def _file_sender_is_authorized(self, wrapper: dict) -> bool:
        user_id, chat_type, chat_id = self._file_sender_context(wrapper)
        if not user_id or not chat_id:
            return False
        return self._is_sender_authorized(user_id, chat_type, chat_id) is True

    def _cancel_file_timeout(self, file_id: int) -> None:
        task = self._file_transfer_tasks.pop(file_id, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _prune_terminal_files(self) -> None:
        now = time.monotonic()
        for file_id, terminal in list(self._terminal_file_transfers.items()):
            if float(terminal.get("expires_at", 0.0)) <= now:
                self._terminal_file_transfers.pop(file_id, None)
        while len(self._terminal_file_transfers) > 4096:
            self._terminal_file_transfers.pop(next(iter(self._terminal_file_transfers)))

    def _mark_file_terminal(self, file_id: int, reason: str) -> Optional[str]:
        """Remember a terminal transfer long enough to reject late duplicates."""
        self._file_receive_started.discard(file_id)
        target = self._file_receive_targets.pop(file_id, None)
        prior = self._terminal_file_transfers.get(file_id, {})
        if target is None:
            target = prior.get("target")
        self._terminal_file_transfers[file_id] = {
            "target": target,
            "reason": reason,
            "expires_at": time.monotonic() + 86400.0,
        }
        self._prune_terminal_files()
        return target

    def _terminal_file_target(self, file_id: int) -> Optional[str]:
        self._prune_terminal_files()
        terminal = self._terminal_file_transfers.get(file_id)
        if terminal:
            return terminal.get("target")
        return self._file_receive_targets.get(file_id)

    def _cleanup_owned_media_path(self, path: str) -> None:
        """Remove only an exact temporary path minted by this adapter."""
        normalized = os.path.abspath(path)
        task = self._owned_media_cleanup_tasks.pop(normalized, None)
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        try:
            if os.path.isfile(normalized) or os.path.islink(normalized):
                os.remove(normalized)
        except OSError as exc:
            self._diagnostics["media_cleanup_failures"] += 1
            logger.warning(
                "SimpleX: failed to remove owned temporary media %s (%s)",
                os.path.basename(normalized),
                type(exc).__name__,
            )

    async def _expire_owned_media_path(self, path: str) -> None:
        try:
            await asyncio.sleep(self._media_cleanup_timeout)
            self._cleanup_owned_media_path(path)
        except asyncio.CancelledError:
            return

    def _schedule_owned_media_cleanup(self, path: str, *, reset: bool = False) -> None:
        """Install a TTL backstop for an adapter-created media path."""
        normalized = os.path.abspath(path)
        existing = self._owned_media_cleanup_tasks.get(normalized)
        if existing and not existing.done():
            if not reset:
                return
            existing.cancel()
        task = asyncio.create_task(self._expire_owned_media_path(normalized))
        self._owned_media_cleanup_tasks[normalized] = task

        def _done(done: asyncio.Task) -> None:
            if self._owned_media_cleanup_tasks.get(normalized) is done:
                self._owned_media_cleanup_tasks.pop(normalized, None)
            if not done.cancelled():
                try:
                    done.result()
                except Exception:
                    logger.exception("SimpleX: temporary media cleanup failed")

        task.add_done_callback(_done)

    def _track_pending_file(self, file_id: int, wrapper: dict) -> None:
        self._pending_file_transfers[file_id] = wrapper
        existing = self._file_transfer_tasks.get(file_id)
        if existing and not existing.done():
            return
        task = asyncio.create_task(self._expire_file_transfer(file_id))
        self._file_transfer_tasks[file_id] = task

        def _done(done: asyncio.Task) -> None:
            if self._file_transfer_tasks.get(file_id) is done:
                self._file_transfer_tasks.pop(file_id, None)
            if not done.cancelled():
                try:
                    done.result()
                except Exception:
                    logger.exception("SimpleX: file-transfer expiry task failed")

        task.add_done_callback(_done)

    async def _dispatch_file_fallback(self, wrapper: dict, reason: str) -> None:
        """Deliver an authorized file caption without an unavailable attachment."""
        fallback = copy.deepcopy(self._normalize_chat_item_wrapper(wrapper))
        inner = fallback.get("chatItem", {}) if fallback else {}
        if not isinstance(inner, dict):
            return
        inner.pop("file", None)
        content = inner.get("content", {}) or {}
        msg_content = content.get("msgContent", {}) if isinstance(content, dict) else {}
        text = msg_content.get("text", "") if isinstance(msg_content, dict) else ""
        if not text:
            if not isinstance(content, dict):
                content = {}
                inner["content"] = content
            content["msgContent"] = {
                "type": "text",
                "text": f"[Attachment unavailable: {reason}]",
            }
        logger.info("SimpleX: delivering file caption without attachment (%s)", reason)
        await self._handle_chat_item(fallback)

    async def _expire_file_transfer(self, file_id: int) -> None:
        await asyncio.sleep(self._file_transfer_timeout)
        wrapper = self._pending_file_transfers.pop(file_id, None)
        if not wrapper:
            return
        target = self._mark_file_terminal(file_id, "transfer timed out")
        if target:
            self._cleanup_owned_media_path(target)
        self._diagnostics["file_timeouts"] += 1
        await self._dispatch_file_fallback(wrapper, "transfer timed out")

    async def _fail_file_transfer(self, file_id: int, reason: str) -> None:
        wrapper = self._pending_file_transfers.pop(file_id, None)
        self._cancel_file_timeout(file_id)
        target = self._mark_file_terminal(file_id, reason)
        if target:
            self._cleanup_owned_media_path(target)
        self._diagnostics["file_failures"] += 1
        if wrapper:
            await self._dispatch_file_fallback(wrapper, reason)

    async def _accept_contact_request(self, request_id: str) -> None:
        resp = await self._send_command(f"/_accept {request_id}", timeout=30.0)
        error = _response_error(resp)
        if error:
            self._diagnostics["command_errors"] += 1
            logger.warning("SimpleX: contact request acceptance failed: %s", error)

    async def _receive_file(self, file_id: int, target: Optional[str]) -> None:
        command = f"/freceive {file_id} approved_relays=on"
        if target:
            command += f" {target}"
        resp = await self._send_command(command, timeout=30.0)
        error = _response_error(resp)
        if error or _response_type(resp) == "rcvFileAcceptedSndCancelled":
            self._diagnostics["command_errors"] += 1
            logger.warning(
                "SimpleX: file %s receive failed: %s",
                file_id,
                error or "sender cancelled",
            )
            await self._fail_file_transfer(
                file_id, error or "sender cancelled during acceptance"
            )

    def _resolve_file_path(self, file_path: str) -> str:
        if file_path and not os.path.isabs(file_path) and self.files_folder:
            return os.path.join(self.files_folder, file_path)
        return file_path

    @staticmethod
    def _prepare_image(file_path: str) -> tuple[str, str]:
        """Ensure *file_path* is a PNG and return ``(png_path, thumb_data_uri)``.

        SimpleX clients can't display WebP and a few other formats inline.
        This converts to PNG when needed and generates a small JPEG thumbnail
        for the ``image`` field in the ``/_send`` payload so the chat shows
        an inline preview. Uses Pillow when available, falls back to
        ImageMagick ``convert``.
        """
        import subprocess
        p = Path(file_path)
        png_path = file_path
        thumb_uri = ""

        def _temp_path(suffix: str) -> str:
            fd, path = tempfile.mkstemp(prefix="hermes-simplex-", suffix=suffix)
            os.close(fd)
            return path

        try:
            from PIL import Image

            img = Image.open(file_path)
            if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                png_path = _temp_path(".png")
                img.save(png_path, "PNG")
            thumb = img.copy()
            thumb.thumbnail((128, 128))
            import io

            buf = io.BytesIO()
            thumb.save(buf, "JPEG", quality=70)
            thumb_uri = (
                "data:image/jpg;base64,"
                + base64.b64encode(buf.getvalue()).decode()
            )
        except ImportError:
            try:
                if p.suffix.lower() not in (".png", ".jpg", ".jpeg"):
                    png_path = _temp_path(".png")
                    subprocess.run(
                        ["convert", file_path, png_path],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                    tmp_path = tmp.name
                subprocess.run(
                    [
                        "convert",
                        file_path,
                        "-resize",
                        "128x128",
                        "-quality",
                        "70",
                        tmp_path,
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                with open(tmp_path, "rb") as f:
                    thumb_uri = (
                        "data:image/jpg;base64," + base64.b64encode(f.read()).decode()
                    )
                os.remove(tmp_path)
            except (FileNotFoundError, subprocess.SubprocessError) as exc:
                logger.warning("SimpleX: image conversion unavailable: %s", exc)

        return png_path, thumb_uri

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an image. Supports ``file://`` URLs and ``http(s)://`` URLs."""
        from urllib.parse import unquote

        if image_url.startswith("file://"):
            file_path = unquote(image_url[7:])
        else:
            try:
                from gateway.platforms.base import cache_image_from_url

                file_path = await cache_image_from_url(image_url)
            except Exception as e:
                logger.warning("SimpleX: failed to download image: %s", e)
                return SendResult(success=False, error=str(e))

        if not file_path or not Path(file_path).exists():
            return SendResult(success=False, error="Image file not found")

        png_path, thumb_uri = self._prepare_image(file_path)
        owned_temp = (
            os.path.abspath(png_path) != os.path.abspath(file_path)
        )
        if owned_temp:
            self._schedule_owned_media_cleanup(png_path)

        result = await self._send_composed(
            chat_id,
            {
                "type": "image",
                "image": thumb_uri,
                "text": caption or "",
            },
            reply_to=kwargs.get("reply_to"),
            file_source=png_path,
        )
        if owned_temp:
            if result.success and result.message_id:
                self._outbound_temp_by_item[str(result.message_id)] = png_path
            elif result.error_kind != "delivery_unknown":
                self._cleanup_owned_media_path(png_path)
        return result

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file via SimpleX."""
        return await self.send_image(
            chat_id,
            f"file://{image_path}",
            caption=caption,
            reply_to=reply_to,
            **kwargs,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video file via SimpleX (as a file attachment)."""
        return await self.send_document(
            chat_id,
            video_path,
            caption=caption,
            reply_to=reply_to,
            **kwargs,
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file attachment."""
        if not Path(file_path).exists():
            return SendResult(success=False, error="File not found")

        return await self._send_composed(
            chat_id,
            {"type": "file", "text": caption or ""},
            reply_to=reply_to,
            file_source=file_path,
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        duration: int = 0,
        **kwargs,
    ) -> SendResult:
        """Send an audio file as a SimpleX voice note (plays inline).

        SimpleX distinguishes a generic file attachment (``type: "file"``)
        from an inline voice note (``type: "voice"``). ``/f`` would deliver
        a downloadable file; the structured ``/_send`` form with
        ``msgContent.type == "voice"`` produces the voice-note player.
        """
        if not Path(audio_path).exists():
            return SendResult(success=False, error="Voice file not found")

        return await self._send_composed(
            chat_id,
            {
                "type": "voice",
                "text": caption or "",
                "duration": duration,
            },
            reply_to=reply_to,
            file_source=audio_path,
        )
