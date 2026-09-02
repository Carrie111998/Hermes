"""Correlated command and text egress lifecycle for SimpleX."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from gateway.platforms.base import SendResult
from plugins.platforms.simplex.protocol import (
    response_error as _response_error,
    response_item_ids as _response_item_ids,
    response_type as _response_type,
    simplex_payload_len as _simplex_payload_len,
)

logger = logging.getLogger(__name__)
MAX_MESSAGE_LENGTH = 12000
_CORR_PREFIX = "hermes-"


class SimplexMessagingMixin:
    def _make_corr_id(self) -> str:
        """Mint a new correlation ID and remember it for echo-filtering.

        We add every minted id to ``_pending_corr_ids`` so the inbound
        event loop can drop the daemon's echo of our own commands without
        ever invoking ``_handle_chat_item``. The set is bounded — when
        it grows past ``_max_pending_corr``, the oldest entries are
        evicted in a single sweep.
        """
        self._corr_counter += 1
        corr_id = f"{_CORR_PREFIX}{self._corr_counter}-{int(time.time() * 1000)}"
        self._pending_corr_ids.add(corr_id)
        if len(self._pending_corr_ids) > self._max_pending_corr:
            overflow = len(self._pending_corr_ids) - self._max_pending_corr
            for _ in range(overflow):
                try:
                    self._pending_corr_ids.pop()
                except KeyError:
                    break
        return corr_id

    async def _send_ws(self, payload: dict) -> bool:
        """Send one JSON payload over the active WebSocket.

        Returns True on success, False if the socket is unavailable or an
        error occurs so callers can surface failures instead of silently
        reporting success.
        """
        ws = self._ws
        if not ws:
            logger.debug("SimpleX: WS send rejected (not connected)")
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except Exception as e:
            logger.warning("SimpleX: WS send error: %s", e)
            return False

    async def _send_command(
        self,
        command: str,
        timeout: float = 30.0,
    ) -> Optional[dict]:
        """Send a command and await the correlated response."""
        ws = self._ws
        if not ws or not self._ws_ready.is_set():
            logger.warning("SimpleX: command rejected while WebSocket is not ready")
            return None

        corr_id = self._make_corr_id()
        payload = json.dumps({"corrId": corr_id, "cmd": command})

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_responses[corr_id] = fut

        try:
            await ws.send(payload)
        except Exception as e:
            logger.warning(
                "SimpleX: command was not submitted: %s — %s",
                command.split(" ", 1)[0],
                e,
            )
            self._pending_responses.pop(corr_id, None)
            self._pending_corr_ids.discard(corr_id)
            if not fut.done():
                fut.cancel()
            return {
                "type": "localCommandNotSubmitted",
                "error": "SimpleX command was not submitted to the daemon",
            }

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logger.warning("SimpleX: command timed out: %s", command.split(" ", 1)[0])
            return {
                "type": "localCommandOutcomeUnknown",
                "error": "SimpleX daemon confirmation timed out; delivery may have occurred",
            }
        except Exception as e:
            logger.warning(
                "SimpleX: command failed: %s — %s",
                command.split(" ", 1)[0],
                e,
            )
            return {
                "type": "localCommandOutcomeUnknown",
                "error": "SimpleX connection failed after command submission; delivery outcome is unknown",
            }
        finally:
            self._pending_responses.pop(corr_id, None)
            self._pending_corr_ids.discard(corr_id)

    def _spawn_command_task(self, coroutine) -> asyncio.Task:
        """Run a correlated command outside the WebSocket reader task."""
        task = asyncio.create_task(coroutine)
        self._command_tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            self._command_tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:
                logger.exception("SimpleX: background command task failed")

        task.add_done_callback(_done)
        return task

    def _spawn_dispatch_task(self, coroutine) -> asyncio.Task:
        """Dispatch inbound work without ever blocking the WebSocket reader."""
        task = asyncio.create_task(coroutine)
        self._dispatch_tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            self._dispatch_tasks.discard(done)
            if done.cancelled():
                return
            try:
                done.result()
            except Exception:
                logger.exception("SimpleX: background message dispatch failed")

        task.add_done_callback(_done)
        return task

    async def _send_fire_and_forget(self, command: str) -> None:
        """Send without blocking the reader; explicit errors remain logged."""
        corr_id = self._make_corr_id()
        ok = await self._send_ws({"corrId": corr_id, "cmd": command})
        if not ok:
            self._pending_corr_ids.discard(corr_id)
            self._diagnostics["send_failures"] += 1

    @staticmethod
    def _chat_ref(chat_id: str) -> str:
        """Return the structured daemon ChatRef for a stable Hermes chat id."""
        raw = str(chat_id or "")
        if raw.startswith("group:"):
            return f"#{raw[6:].split('|', 1)[0]}"
        return f"@{raw.split('|', 1)[0]}"

    @staticmethod
    def _error_kind(error: str) -> tuple[str, bool]:
        lowered = (error or "").lower()
        if "largemsg" in lowered or "large compressed message" in lowered:
            return "too_long", False
        if "notfound" in lowered or "not found" in lowered:
            return "not_found", False
        if "forbidden" in lowered or "permission" in lowered:
            return "forbidden", False
        if any(token in lowered for token in ("timeout", "closed", "network", "broker")):
            return "transient", True
        return "unknown", False

    def _send_result_from_response(
        self,
        resp: Optional[dict],
        *,
        expected: set[str],
    ) -> SendResult:
        error = _response_error(resp)
        if error:
            self._diagnostics["command_errors"] += 1
            if _response_type(resp) == "localCommandOutcomeUnknown":
                return SendResult(
                    success=False,
                    error=error,
                    error_kind="delivery_unknown",
                    retryable=False,
                    raw_response=resp,
                )
            if _response_type(resp) == "localCommandNotSubmitted":
                return SendResult(
                    success=False,
                    error=error,
                    error_kind="transient",
                    retryable=True,
                    raw_response=resp,
                )
            kind, retryable = self._error_kind(error)
            return SendResult(
                success=False,
                error=error,
                error_kind=kind,
                retryable=retryable,
                raw_response=resp,
            )
        if resp is None:
            self._diagnostics["send_failures"] += 1
            return SendResult(
                success=False,
                error="SimpleX daemon did not confirm the command",
                error_kind="transient",
                retryable=True,
            )
        resp_type = _response_type(resp)
        if resp_type not in expected:
            self._diagnostics["send_failures"] += 1
            return SendResult(
                success=False,
                error=f"Unexpected SimpleX response: {resp_type or '<missing>'}",
                error_kind="unknown",
                raw_response=resp,
            )
        item_ids = _response_item_ids(resp)
        return SendResult(
            success=True,
            message_id=item_ids[-1] if item_ids else None,
            continuation_message_ids=tuple(item_ids[:-1]),
            raw_response=resp,
        )

    async def _send_composed(
        self,
        chat_id: str,
        msg_content: dict,
        *,
        reply_to: Optional[str] = None,
        file_source: Optional[str] = None,
        live: bool = False,
        timeout: float = 30.0,
    ) -> SendResult:
        composed: Dict[str, Any] = {
            "msgContent": msg_content,
            "mentions": {},
        }
        if reply_to is not None:
            try:
                composed["quotedItemId"] = int(reply_to)
            except (TypeError, ValueError):
                logger.debug("SimpleX: ignoring non-numeric reply item id")
        if file_source:
            composed["fileSource"] = {"filePath": file_source}

        live_flag = " live=on" if live else ""
        command = (
            f"/_send {self._chat_ref(chat_id)}{live_flag} json "
            f"{json.dumps([composed], ensure_ascii=False)}"
        )
        resp = await self._send_command(command, timeout=timeout)
        return self._send_result_from_response(resp, expected={"newChatItems"})

    @staticmethod
    def _split_utf8_payload(
        text: str, byte_budget: int = MAX_MESSAGE_LENGTH
    ) -> List[str]:
        """Split by serialized UTF-8 bytes, never through a code point.

        ``simplex-chat`` limits the encoded command envelope rather than
        Python character count.  A margin below the protocol ceiling leaves
        room for the JSON wrapper, chat reference, and quoting expansion.
        """
        if not text:
            return [""]
        chunks: List[str] = []
        start = 0
        while start < len(text):
            used = 0
            end = start
            last_break: Optional[int] = None
            while end < len(text):
                char_cost = _simplex_payload_len(text[end])
                if used + char_cost > byte_budget and end > start:
                    break
                used += char_cost
                end += 1
                if text[end - 1].isspace():
                    last_break = end
            if end < len(text) and last_break and last_break > start:
                end = last_break
            if end == start:
                end += 1
            chunks.append(text[start:end])
            start = end
        return chunks

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver text and embedded media with daemon-confirmed results."""
        _voice_exts = {".ogg", ".mp3", ".wav", ".m4a", ".opus"}
        media_paths = re.findall(r"MEDIA:(\S+)", content)
        if media_paths:
            content = re.sub(r"MEDIA:\S+", "", content).strip()

        delivered_ids: List[str] = []
        if content:
            initial_chunks: List[str] = []
            for logical_chunk in self.truncate_message(
                content, MAX_MESSAGE_LENGTH, len_fn=self.message_len_fn
            ):
                initial_chunks.extend(self._split_utf8_payload(logical_chunk))
            queue = [
                (chunk, index == 0)
                for index, chunk in enumerate(initial_chunks)
            ]
            while queue:
                chunk, carries_reply = queue.pop(0)
                result = await self._send_composed(
                    chat_id,
                    {"type": "text", "text": chunk},
                    reply_to=reply_to if carries_reply else None,
                    live=bool((metadata or {}).get("expect_edits")),
                )
                if (
                    not result.success
                    and result.error_kind == "too_long"
                    and len(chunk) > 512
                ):
                    retry_chunks = self.truncate_message(
                        chunk, max(256, len(chunk) // 2)
                    )
                    queue = [
                        (retry_chunk, carries_reply and index == 0)
                        for index, retry_chunk in enumerate(retry_chunks)
                    ] + queue
                    continue
                if not result.success:
                    result.message_id = delivered_ids[-1] if delivered_ids else None
                    if delivered_ids:
                        result.error_kind = "partial_delivery"
                        result.retryable = False
                        result.raw_response = {
                            "partial_delivery": True,
                            "delivered_message_ids": tuple(delivered_ids),
                            "daemon_response": result.raw_response,
                        }
                    return result
                if result.message_id:
                    delivered_ids.append(result.message_id)

        for path in media_paths:
            is_voice = os.path.splitext(path)[1].lower() in _voice_exts
            if is_voice:
                media_result = await self.send_voice(chat_id, path)
            else:
                media_result = await self.send_document(chat_id, path)
            if not media_result.success:
                if delivered_ids:
                    media_result.message_id = delivered_ids[-1]
                    media_result.error_kind = "partial_delivery"
                    media_result.retryable = False
                    media_result.raw_response = {
                        "partial_delivery": True,
                        "delivered_message_ids": tuple(delivered_ids),
                        "daemon_response": media_result.raw_response,
                    }
                return media_result
            if media_result.message_id:
                delivered_ids.append(media_result.message_id)
        return SendResult(
            success=True,
            message_id=delivered_ids[-1] if delivered_ids else None,
            continuation_message_ids=tuple(delivered_ids[:-1]),
        )

    async def list_channels(self) -> Optional[List[Dict[str, Any]]]:
        """Enumerate contacts and allowed groups for the channel directory.

        Called by ``gateway.channel_directory.build_channel_directory()``
        every refresh cycle. Uses the daemon's ``/contacts`` and ``/groups``
        commands over the live WebSocket. Returns ``None`` (not ``[]``) when
        the WebSocket is down so the directory falls back to session-history
        discovery instead of wiping previously known targets.

        Entry ``id`` values use immutable numeric contact/group IDs. Display
        names remain labels only and never become authorization or routing
        identities.
        """
        if not self._ws:
            return None

        channels: List[Dict[str, Any]] = []

        resp = await self._send_command("/contacts", timeout=10.0)
        if resp is None or _response_error(resp):
            # Daemon unresponsive — keep whatever the directory already has.
            return None
        for contact in resp.get("contacts") or []:
            if not isinstance(contact, dict):
                continue
            contact_id = contact.get("contactId")
            name = (
                contact.get("localDisplayName", "")
                or (contact.get("profile", {}) or {}).get("displayName", "")
            )
            if contact_id is None:
                continue
            channels.append({
                "id": str(contact_id),
                "name": str(name or contact_id),
                "type": "dm",
            })

        resp = await self._send_command("/groups", timeout=10.0)
        if resp is not None and not _response_error(resp):
            for group in resp.get("groups") or []:
                # The daemon returns each group as either a groupInfo dict
                # or a [groupInfo, groupSummary] pair depending on version.
                if isinstance(group, list) and group:
                    group = group[0]
                if not isinstance(group, dict):
                    continue
                group_id = group.get("groupId")
                if group_id is None:
                    continue
                name = (
                    group.get("localDisplayName", "")
                    or (group.get("groupProfile", {}) or {}).get("displayName", "")
                    or str(group_id)
                )
                channels.append({
                    "id": f"group:{group_id}",
                    "name": str(name),
                    "type": "group",
                })

        return channels
