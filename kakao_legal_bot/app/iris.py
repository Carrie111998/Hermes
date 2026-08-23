"""Iris bridge: parse inbound webhooks, push replies back into the room.

Iris (the KakaoTalk control layer running on the rooted emulator) has gone
through several payload shapes across releases, so the parser is written to
accept any of them rather than one exact schema — a missing field is a
degraded event, never a 500 on the webhook.

Two delivery modes, because the emulator usually sits behind a home NAT:

* ``direct`` — this server POSTs to Iris. Requires Iris to be reachable
  (Cloudflare Tunnel / Tailscale / ngrok). Lowest latency.
* ``poll``   — replies land in the ``outbox`` table and the relay script
  running next to the emulator pulls them. No inbound port needed.
* ``hybrid`` — try direct, fall back to the outbox when Iris is unreachable.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger(__name__)

# KakaoTalk chat log types. 1 = plain text; everything else is media,
# emoticons, invitations, feeds… which we acknowledge but do not answer.
TEXT_MESSAGE_TYPES = {"1", "26", ""}


def _first_str(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return ""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


@dataclass(frozen=True)
class IrisEvent:
    """A normalised inbound KakaoTalk message."""

    room_id: str
    room_name: str
    sender_name: str
    sender_id: str
    text: str
    msg_type: str
    log_id: str
    created_at: float
    is_direct_chat: bool | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_text(self) -> bool:
        return self.msg_type in TEXT_MESSAGE_TYPES

    @property
    def event_key(self) -> str:
        """Dedupe key — Iris retries, and duplicated answers look terrible."""
        if self.log_id:
            return f"log:{self.log_id}"
        return f"fallback:{self.room_id}:{self.sender_id}:{hash(self.text) & 0xFFFFFFFF}"

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> IrisEvent:
        inner = _as_dict(payload.get("json")) or _as_dict(payload.get("raw"))
        # Some builds nest the chatlog one level deeper again.
        if not inner and isinstance(payload.get("data"), (dict, str)):
            inner = _as_dict(payload.get("data"))

        room_id = _first_str(payload, "room_id", "chat_id", "chatId") or _first_str(
            inner, "chat_id", "chatId", "room_id"
        )
        room_name = _first_str(payload, "room", "room_name", "roomName") or _first_str(
            inner, "room", "room_name"
        )
        text = _first_str(payload, "msg", "message", "text") or _first_str(
            inner, "message", "msg", "text"
        )
        sender_name = _first_str(payload, "sender", "sender_name", "nickname") or _first_str(
            inner, "sender", "nickname", "name"
        )
        sender_id = _first_str(payload, "user_id", "sender_id") or _first_str(
            inner, "user_id", "userId", "sender_id"
        )
        msg_type = _first_str(payload, "type", "msg_type") or _first_str(inner, "type", "msg_type")
        log_id = _first_str(payload, "log_id", "id", "_id") or _first_str(inner, "_id", "id", "log_id")

        created_raw = _first_str(payload, "created_at", "timestamp") or _first_str(
            inner, "created_at", "timestamp"
        )
        try:
            created_at = float(created_raw)
            # Kakao stores seconds; some fields are milliseconds.
            if created_at > 1e12:
                created_at /= 1000.0
        except ValueError:
            created_at = 0.0

        # `v` carries chat metadata as an embedded JSON string. When it
        # names the chat type we can tell a 1:1 room from a group room
        # without asking the user to configure anything.
        is_direct: bool | None = None
        meta = _as_dict(inner.get("v")) or _as_dict(payload.get("v"))
        for key in ("isSingleChat", "is_single_chat", "isDirectChat"):
            if key in meta:
                is_direct = bool(meta[key])
                break
        chat_type = _first_str(meta, "chatType", "chat_type") or _first_str(
            payload, "chat_type", "room_type"
        )
        if is_direct is None and chat_type:
            is_direct = chat_type.lower() in {"directchat", "direct", "single", "memochat"}

        # A room without an id is unroutable; fall back to the name so at
        # least single-room setups keep working.
        if not room_id:
            room_id = room_name

        return cls(
            room_id=room_id,
            room_name=room_name,
            sender_name=sender_name,
            sender_id=sender_id,
            text=text,
            msg_type=msg_type,
            log_id=log_id,
            created_at=created_at,
            is_direct_chat=is_direct,
            raw=payload,
        )


def split_for_kakao(text: str, limit: int) -> list[str]:
    """Split a long answer on paragraph/sentence boundaries.

    KakaoTalk will accept a very long message but it renders as a
    "긴 메시지" attachment the reader has to tap through, which is a bad
    consultation experience. Chunking on blank lines keeps each bubble
    readable.
    """
    text = (text or "").strip()
    if not text:
        return []
    if limit <= 0 or len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if len(para) > limit:
            flush()
            # Sentence-level split for a single oversized paragraph.
            piece = ""
            for sentence in re.split(r"(?<=[.!?。다요])\s+", para):
                if len(piece) + len(sentence) + 1 > limit and piece:
                    chunks.append(piece.strip())
                    piece = ""
                piece = f"{piece} {sentence}".strip() if piece else sentence
                while len(piece) > limit:
                    chunks.append(piece[:limit])
                    piece = piece[limit:]
            if piece.strip():
                chunks.append(piece.strip())
            continue
        if len(current) + len(para) + 2 > limit:
            flush()
        current = f"{current}\n\n{para}" if current else para
    flush()
    return chunks


class IrisSendError(RuntimeError):
    pass


class IrisClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._settings.iris_timeout_s)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send_text(self, room_id: str, text: str) -> None:
        """POST one message to Iris. Raises IrisSendError on any failure."""
        base = self._settings.iris_base_url
        if not base:
            raise IrisSendError("IRIS_BASE_URL is not configured")
        client = await self._http()
        payload = {"type": "text", "room": room_id, "data": text}
        try:
            response = await client.post(f"{base}/reply", json=payload)
        except httpx.HTTPError as exc:  # network, DNS, timeout
            raise IrisSendError(f"iris unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise IrisSendError(f"iris returned {response.status_code}: {response.text[:200]}")

    async def query(self, sql: str, bind: list[Any] | None = None) -> list[dict[str, Any]]:
        """Run a read query against the KakaoTalk database through Iris.

        Used for backfilling a room's history when the bot joins a room
        mid-conversation. Iris exposes this as POST /query.
        """
        base = self._settings.iris_base_url
        if not base:
            raise IrisSendError("IRIS_BASE_URL is not configured")
        client = await self._http()
        try:
            response = await client.post(f"{base}/query", json={"query": sql, "bind": bind or []})
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise IrisSendError(f"iris query failed: {exc}") from exc
        if isinstance(body, dict):
            rows = body.get("data") or body.get("rows") or []
        else:
            rows = body
        return [row for row in rows if isinstance(row, dict)]
