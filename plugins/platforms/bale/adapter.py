
"""
Bale (بله) platform adapter.

Uses the Telegram-compatible Bale Bot API via httpx polling.
Bale's Bot API is a fork of Telegram's Bot API, so the endpoints
and data structures are nearly identical.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://tapi.bale.ai"
POLL_TIMEOUT = 30
MAX_MESSAGE_LENGTH = 4096
RECONNECT_BACKOFF = [1, 2, 5, 10, 30]


def check_requirements() -> bool:
    if not HTTPX_AVAILABLE:
        return False
    return bool(os.getenv("BALE_BOT_TOKEN", "").strip())


def validate_config(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = extra.get("token") or os.getenv("BALE_BOT_TOKEN", "")
    return bool(token)


def is_connected(config) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = os.getenv("BALE_BOT_TOKEN") or extra.get("token", "")
    return bool(token)


class BaleAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    supports_code_blocks = True
    splits_long_messages = True
    typed_command_prefix = "/"

    def __init__(self, config: PlatformConfig):
        platform = Platform("bale")
        super().__init__(config=config, platform=platform)
        extra = config.extra or {}
        self._token: str = extra.get("token") or os.getenv("BALE_BOT_TOKEN", "")
        api_base = (
            extra.get("api_base_url")
            or os.getenv("BALE_API_BASE_URL", DEFAULT_API_BASE)
        ).rstrip("/")
        self._api_url = f"{api_base}/bot{self._token}"
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._last_offset: Optional[int] = None
        self._bot_user_id: Optional[int] = None
        self._own_message_ids: set = set()
        self._own_message_cleanup_task: Optional[asyncio.Task] = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not HTTPX_AVAILABLE:
            return False
        if not self._token:
            return False
        try:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(
                connect=15.0, read=POLL_TIMEOUT + 10, write=10.0, pool=10.0
            ))
            resp = await self._http_client.post(f"{self._api_url}/getMe", timeout=15.0)
            data = resp.json()
            if not data.get("ok"):
                self._set_fatal_error(
                    "bale_auth_failed",
                    f"Bot token rejected: {data.get('description', 'unknown')}",
                    retryable=False,
                )
                return False
            bot_info = data.get("result", {})
            self._bot_user_id = bot_info.get("id")
            self._mark_connected()
            self._poll_task = asyncio.create_task(self._poll_loop())
            self._own_message_cleanup_task = asyncio.create_task(self._cleanup_own_message_ids())
            return True
        except Exception as e:
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._poll_task:
            self._poll_task.cancel()
            try: await self._poll_task
            except asyncio.CancelledError: pass
            self._poll_task = None
        if self._own_message_cleanup_task:
            self._own_message_cleanup_task.cancel()
            try: await self._own_message_cleanup_task
            except asyncio.CancelledError: pass
            self._own_message_cleanup_task = None
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def _poll_loop(self) -> None:
        logger.warning("[Bale] Poll loop started, _running=%s", self._running)
        backoff_idx = 0
        while self._running:
            try:
                await self._poll_once()
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running: return
                logger.warning("[Bale] Poll error: %s", e)
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                await asyncio.sleep(delay)
                backoff_idx += 1
        logger.warning("[Bale] Poll loop EXITED, _running=%s", self._running)

    async def _poll_once(self) -> None:
        params = {"timeout": POLL_TIMEOUT, "allowed_updates": json.dumps(["message", "edited_message"])}
        if self._last_offset is not None:
            params["offset"] = self._last_offset
        try:
            resp = await self._http_client.post(
                f"{self._api_url}/getUpdates", json=params,
                timeout=httpx.Timeout(30.0, connect=10.0, read=POLL_TIMEOUT + 5.0),
            )
        except (httpx.TimeoutException, httpx.ReadTimeout):
            return
        data = resp.json()
        if not data.get("ok"):
            return
        for update in data.get("result", []):
            uid = update.get("update_id")
            if uid is not None:
                self._last_offset = uid + 1
            if "message" in update:
                await self._on_message(update["message"])
            elif "edited_message" in update:
                await self._on_message(update["edited_message"])

    async def _on_message(self, msg: Dict) -> None:
        mid = str(msg.get("message_id", ""))
        if not mid or mid in self._own_message_ids:
            return
        fu = msg.get("from", {})
        if fu and fu.get("id") and self._bot_user_id is not None and fu["id"] == self._bot_user_id:
            self._own_message_ids.add(mid)
            return
        chat = msg.get("chat", {})
        cid = str(chat.get("id", ""))
        ctype = chat.get("type", "private")
        cname = chat.get("title") or chat.get("first_name", "")
        uid = str(fu.get("id", "")) if fu else ""
        uname = (fu.get("username") or f"{fu.get('first_name', '')} {fu.get('last_name', '')}").strip() or uid
        text = msg.get("text", "") or msg.get("caption", "") or ""
        source = self.build_source(
            chat_id=cid, chat_name=cname or cid,
            chat_type="dm" if ctype == "private" else "group",
            user_id=uid, user_name=uname, thread_id=None,
        )
        ts = datetime.now(tz=timezone.utc)
        if msg.get("date"):
            try: ts = datetime.fromtimestamp(msg["date"], tz=timezone.utc)
            except: pass
        event = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source,
            message_id=mid, raw_message=msg, timestamp=ts,
        )
        await self.handle_message(event)

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if not self._http_client:
            return SendResult(success=False, error="no client")
        params = {"chat_id": chat_id, "text": content[:self.MAX_MESSAGE_LENGTH], "parse_mode": "Markdown"}
        if reply_to:
            params["reply_to_message_id"] = int(reply_to)
        try:
            resp = await self._http_client.post(f"{self._api_url}/sendMessage", json=params, timeout=15.0)
            data = resp.json()
            if data.get("ok"):
                m = str(data.get("result", {}).get("message_id", uuid.uuid4().hex[:12]))
                self._own_message_ids.add(m)
                return SendResult(success=True, message_id=m)
            return SendResult(success=False, error=data.get("description", str(data)))
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id, metadata=None) -> None:
        if not self._http_client: return
        try:
            await self._http_client.post(f"{self._api_url}/sendChatAction", json={"chat_id": chat_id, "action": "typing"}, timeout=5.0)
        except: pass

    async def get_chat_info(self, chat_id) -> Dict:
        return {"name": chat_id, "type": "dm"}

    async def _cleanup_own_message_ids(self):
        while self._running:
            await asyncio.sleep(600)
            if len(self._own_message_ids) > 10000:
                self._own_message_ids = set(list(self._own_message_ids)[-5000:])


def _env_enablement() -> dict | None:
    token = os.getenv("BALE_BOT_TOKEN", "").strip()
    if not token: return None
    seed = {"token": token}
    api = os.getenv("BALE_API_BASE_URL", "").strip()
    if api: seed["api_base_url"] = api
    home = os.getenv("BALE_HOME_CHANNEL", "").strip()
    if home: seed["home_channel"] = {"chat_id": home, "name": os.getenv("BALE_HOME_CHANNEL_NAME", home)}
    return seed


async def _standalone_send(pconfig, chat_id, message, **kw):
    extra = getattr(pconfig, "extra", {}) or {}
    token = extra.get("token") or os.getenv("BALE_BOT_TOKEN", "")
    if not token: return {"success": False, "error": "no token"}
    api_base = (extra.get("api_base_url") or os.getenv("BALE_API_BASE_URL", DEFAULT_API_BASE)).rstrip("/")
    payload = {"chat_id": chat_id, "text": message[:MAX_MESSAGE_LENGTH], "parse_mode": "Markdown"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{api_base}/bot{token}/sendMessage", json=payload)
            d = r.json()
            if d.get("ok"):
                return {"success": True, "message_id": str(d.get("result", {}).get("message_id", ""))}
            return {"success": False, "error": d.get("description", str(d))}
    except Exception as e:
        return {"success": False, "error": str(e)}


def register(ctx) -> None:
    ctx.register_platform(
        name="bale", label="Bale",
        adapter_factory=lambda cfg: BaleAdapter(cfg),
        check_fn=check_requirements, validate_config=validate_config,
        is_connected=is_connected, required_env=["BALE_BOT_TOKEN"],
        install_hint="pip install httpx",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="BALE_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="BALE_ALLOWED_USERS",
        allow_all_env="BALE_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH, emoji="💬",
        pii_safe=False, allow_update_command=True,
        platform_hint=("You are on Bale (بله), a Telegram-compatible platform."),
    )
