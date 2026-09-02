"""
OneBot v11 Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects QQ personal accounts to Hermes
via OneBot-compatible frameworks (NapCat, Lagrange.Core, go-cqhttp) over
WebSocket, and sends messages/voice over the OneBot HTTP API.

Supports both WebSocket directions:
  - forward  (default): Hermes connects to the OneBot server (``ONEBOT_WS_URL``)
  - backward: Hermes listens for the OneBot client (``ONEBOT_WS_LISTEN``)

Configuration in config.yaml::

    gateway:
      platforms:
        onebot:
          enabled: true
          extra:
            ws_url: "ws://127.0.0.1:3001"     # or ONEBOT_WS_URL env var
            http_url: "http://127.0.0.1:3000"  # or ONEBOT_HTTP_URL env var
            ws_mode: "forward"                 # forward | backward
            ws_listen: "0.0.0.0:3001"          # backward mode listen address
            access_token: ""                   # optional OneBot auth token
            allow_from: []                     # DM whitelist (QQ user IDs)
            group_allow_from: []               # group whitelist (group IDs)
            require_at: true                   # require @mention in groups
            home_channel: ""                   # QQ user ID for cron delivery

Security model (2026-08, aligned with the QQ bridge design):
  - Empty ``allow_from`` / ``group_allow_from`` = deny all (fail closed).
    Personal-account bots must not be exposed to strangers by default.
  - DM: only whitelisted QQ user IDs can talk; no @-mention required.
  - Group: only whitelisted groups; require @mention by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]

try:
    import websockets

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None  # type: ignore[assignment]

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    _AUDIO_EXTS,
)
from gateway.config import Platform

from pathlib import Path

# 图片扩展名（base.py 里 _IMAGE_EXTS 是函数内局部变量，不可导入）
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})


# ---------------------------------------------------------------------------
# OneBot protocol helpers
# ---------------------------------------------------------------------------

def _normalize_text(msg_array) -> str:
    """Convert a OneBot array message to plain text.

    OneBot v11 messages are arrays of segments, e.g.
    ``[{"type": "text", "data": {"text": "hi"}}, {"type": "face", "data": {"id": "1"}}]``.
    """
    if isinstance(msg_array, str):
        return msg_array
    parts: List[str] = []
    for seg in msg_array:
        if not isinstance(seg, dict):
            continue
        typ = seg.get("type", "")
        data = seg.get("data", {}) or {}
        if typ == "text":
            parts.append(data.get("text", ""))
        elif typ == "at":
            qq = data.get("qq", "")
            parts.append(f"@{qq}" if qq else "@")
        elif typ == "image":
            parts.append("[图片]")
        elif typ == "record":
            parts.append("[语音]")
        elif typ == "face":
            parts.append("[表情]")
        elif typ == "file":
            parts.append(f"[文件:{data.get('name', '')}]")
        elif typ == "reply":
            parts.append(f"[回复:{data.get('id', '')}]")
        else:
            parts.append(f"[{typ}]")
    return "".join(parts)


def _parse_ws_url(url: str) -> Tuple[str, int]:
    """Extract (host, port) from a ws:// URL. Returns defaults on failure."""
    m = re.match(r"wss?://([^:/]+):?(\d+)?", url or "")
    if not m:
        return "127.0.0.1", 3001
    host = m.group(1)
    port = int(m.group(2)) if m.group(2) else 3001
    return host, port


# ---------------------------------------------------------------------------
# OneBot Adapter
# ---------------------------------------------------------------------------

class OneBotAdapter(BasePlatformAdapter):
    """Async OneBot v11 adapter implementing the BasePlatformAdapter interface."""

    def __init__(self, config, **kwargs):
        platform = Platform("onebot")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        self.ws_url = os.getenv("ONEBOT_WS_URL") or extra.get("ws_url", "")
        self.http_url = os.getenv("ONEBOT_HTTP_URL") or extra.get("http_url", "")
        self.ws_mode = os.getenv("ONEBOT_WS_MODE") or extra.get("ws_mode", "forward")
        self.ws_listen = os.getenv("ONEBOT_WS_LISTEN") or extra.get("ws_listen", "0.0.0.0:3001")
        self.access_token = os.getenv("ONEBOT_ACCESS_TOKEN") or extra.get("access_token", "")

        # Whitelists — empty = deny all (fail closed)
        raw_allow = os.getenv("ONEBOT_ALLOW_FROM") or ""
        raw_groups = os.getenv("ONEBOT_GROUP_ALLOW_FROM") or ""
        self.allow_from: Set[str] = {
            str(x).strip() for x in (raw_allow.split(",") if raw_allow else extra.get("allow_from", []))
            if str(x).strip()
        }
        self.group_allow_from: Set[str] = {
            str(x).strip() for x in (raw_groups.split(",") if raw_groups else extra.get("group_allow_from", []))
            if str(x).strip()
        }
        require_at = os.getenv("ONEBOT_REQUIRE_AT", "")
        self.require_at = (
            require_at.lower() in {"1", "true", "yes"}
            if require_at
            else bool(extra.get("require_at", True))
        )
        self.home_channel = os.getenv("ONEBOT_HOME_CHANNEL") or extra.get("home_channel", "")
        # voice_mount: {host: ..., container: ...} — 容器部署时把宿主机音频路径
        # 映射为容器内路径（NapCat Docker 看不到宿主机路径）
        self.voice_mount = extra.get("voice_mount") or {}

        # Runtime state
        self._ws: Any = None
        self._ws_task: Optional[asyncio.Task] = None
        self._backward_server: Any = None
        self._http_session: Optional[Any] = None

    @property
    def name(self) -> str:
        return "OneBot"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to the OneBot WebSocket (forward) or start listening (backward)."""
        if not AIOHTTP_AVAILABLE or not WEBSOCKETS_AVAILABLE:
            logger.error("OneBot: aiohttp and websockets are required")
            self._set_fatal_error(
                "missing_deps",
                "aiohttp and websockets are required for OneBot adapter",
                retryable=False,
            )
            return False

        if self.ws_mode == "backward":
            return await self._start_backward()

        # forward mode: connect to the OneBot WS server
        if not self.ws_url:
            logger.error("OneBot: ws_url must be configured (forward mode)")
            self._set_fatal_error(
                "config_missing",
                "ONEBOT_WS_URL must be set for forward mode",
                retryable=False,
            )
            return False

        try:
            self._ws = await websockets.connect(self.ws_url, max_size=16 * 1024 * 1024)
        except Exception as e:
            logger.error("OneBot: WS connect failed — %s", e)
            self._set_fatal_error("connect_failed", str(e), retryable=True)
            return False

        self._ws_task = asyncio.create_task(self._receive_loop())
        self._mark_connected()
        logger.info("OneBot: connected to %s (forward mode)", self.ws_url)
        return True

    def _clear_ws_state(self) -> None:
        """Close a dead WebSocket so reconnect can start fresh.

        Does NOT cancel ``_ws_task`` — the receive loop calls this from
        inside itself, and reconnect creates a fresh task via connect().
        """
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    async def _start_backward(self) -> bool:
        """Backward mode: listen for the OneBot client to connect to us."""
        host, port = _parse_ws_url(f"ws://{self.ws_listen}" if "://" not in self.ws_listen else self.ws_listen)
        try:
            self._backward_server = await websockets.serve(self._handle_backward_client, host, port, max_size=16 * 1024 * 1024)
        except Exception as e:
            logger.error("OneBot: backward listen failed — %s", e)
            self._set_fatal_error("listen_failed", str(e), retryable=True)
            return False
        self._mark_connected()
        logger.info("OneBot: listening on %s:%s (backward mode)", host, port)
        return True

    async def _handle_backward_client(self, ws, path):
        """Handle a backward-mode client connection."""
        self._ws = ws
        try:
            async for raw in ws:
                await self._dispatch_event(raw)
        except Exception as e:
            logger.debug("OneBot: backward client disconnected — %s", e)

    async def disconnect(self) -> None:
        """Disconnect from the platform."""
        self._mark_disconnected()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except Exception:
                pass
        if self._ws and hasattr(self._ws, "close"):
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._backward_server:
            self._backward_server.close()
            try:
                await self._backward_server.wait_closed()
            except Exception:
                pass
        if self._http_session:
            await self._http_session.close()
            self._http_session = None

    # ── Receive loop ──────────────────────────────────────────────────────

    async def _receive_loop(self) -> None:
        """Forward-mode: read events from the connected WebSocket.

        On disconnect (OneBot restarted, network blip), auto-reconnect with
        backoff so the adapter recovers without a gateway restart — this is
        what makes "boot the PC, everything comes back" work.
        """
        backoff = 1.0
        while True:
            try:
                async for raw in self._ws:
                    await self._dispatch_event(raw)
                break  # ws exhausted cleanly → exit loop
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("OneBot: receive loop ended — %s; reconnecting in %.1fs", e, backoff)
            # Connection dropped: clear state and retry connect with backoff
            self._clear_ws_state()
            try:
                await asyncio.sleep(backoff)
                ok = await self.connect(is_reconnect=True)
                if ok:
                    # connect() created a fresh receive-loop task; this
                    # (dead) loop must exit so two loops don't both dispatch.
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("OneBot: reconnect failed — %s", e)
            backoff = min(backoff * 2, 60.0)

    async def _dispatch_event(self, raw: str) -> None:
        """Parse a OneBot event and dispatch message events to the handler."""
        try:
            evt = json.loads(raw)
        except Exception:
            return
        if evt.get("post_type") != "message":
            return

        msg_type = evt.get("message_type", "")
        sender = evt.get("sender", {}) or {}
        user_id = str(sender.get("user_id", ""))
        user_name = sender.get("card") or sender.get("nickname") or ""
        text = _normalize_text(evt.get("message", ""))
        message_id = str(evt.get("message_id", ""))

        if not user_id or not text.strip():
            return

        if msg_type == "private":
            await self._handle_private(user_id, user_name, text, message_id)
        elif msg_type == "group":
            group_id = str(evt.get("group_id", ""))
            await self._handle_group(group_id, user_id, user_name, text, message_id, evt)

    @staticmethod
    def _denied_by_allowlist(allowlist: Set[str], identity: str) -> bool:
        """Fail-closed allowlist check.

        Returns True when ``identity`` should be denied:
        - empty allowlist → True (deny all)
        - allowlist contains "*" or "all" → False (allow all)
        - otherwise → identity must be in the allowlist
        """
        if not identity:
            return True
        if "*" in allowlist or "all" in allowlist:
            return False
        return identity not in allowlist

    async def _handle_private(self, user_id: str, user_name: str, text: str, message_id: str) -> None:
        """DM: only whitelisted user IDs; no @-mention required.

        白名单逻辑 fail-closed（空=全拒，2026-08 grilling 决策）：
        - ``allow_from`` 为空 → 拒绝所有私聊（个人号 bot 不暴露给陌生人）
        - ``allow_from`` 含 ``*`` 或 ``all`` → 显式全开
        - 否则仅允许列出的 QQ 号
        """
        if self._denied_by_allowlist(self.allow_from, user_id):
            logger.debug("OneBot: DM from non-whitelisted user %s ignored", user_id)
            return

        await self._emit_event(
            chat_id=user_id,
            chat_name=user_name or user_id,
            chat_type="dm",
            user_id=user_id,
            user_name=user_name,
            text=text,
            message_id=message_id,
        )

    async def _handle_group(
        self,
        group_id: str,
        user_id: str,
        user_name: str,
        text: str,
        message_id: str,
        evt: Dict[str, Any],
    ) -> None:
        """Group: only whitelisted groups; require @mention by default."""
        if self._denied_by_allowlist(self.group_allow_from, group_id):
            logger.debug("OneBot: group %s not whitelisted, ignored", group_id)
            return

        # Check @mention requirement
        if self.require_at:
            message = evt.get("message", [])
            at_self = False
            self_id = str(evt.get("self_id", ""))
            if isinstance(message, list):
                for seg in message:
                    if isinstance(seg, dict) and seg.get("type") == "at":
                        at_qq = str((seg.get("data", {}) or {}).get("qq", ""))
                        if at_qq == self_id or at_qq == "all":
                            at_self = True
                            break
            if not at_self:
                logger.debug("OneBot: group message without @mention ignored")
                return

        await self._emit_event(
            chat_id=group_id,
            chat_name=f"group-{group_id}",
            chat_type="group",
            user_id=user_id,
            user_name=user_name,
            text=text,
            message_id=message_id,
        )

    async def _emit_event(
        self,
        chat_id: str,
        chat_name: str,
        chat_type: str,
        user_id: str,
        user_name: str,
        text: str,
        message_id: str,
    ) -> None:
        """Build a MessageEvent and dispatch to the gateway message handler.

        必须调用 ``self.handle_message(event)``（基类完整入口）而非直接调
        ``_message_handler``——handle_message 负责 session 构建、锁管理、
        background dispatch 与最终回复投递。直接调 _message_handler 会绕过
        回复投递链路（实测：消息处理了、回复生成了，但发不出去）。
        """
        from gateway.session import SessionSource

        source = SessionSource(
            platform=self.platform,
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            user_id=user_id,
            user_name=user_name,
            source=source,
            raw_message=None,
            message_id=message_id,
        )
        try:
            await self.handle_message(event)
        except Exception as e:
            logger.error("OneBot: handle_message error — %s", e)

    # ── Outbound ──────────────────────────────────────────────────────────

    async def _http(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST to the OneBot HTTP API."""
        if not self.http_url:
            raise RuntimeError("OneBot: http_url not configured")
        if self._http_session is None:
            self._http_session = aiohttp.ClientSession()
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        async with self._http_session.post(
            f"{self.http_url.rstrip('/')}/{action}",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
            if data.get("retcode") != 0:
                raise RuntimeError(f"OneBot API {action} failed: {data}")
            return data

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a text message to a QQ user or group."""
        logger.info("OneBot: send() called chat=%s content=%s metadata=%s",
                    chat_id, (content or "")[:40], (metadata or {}).keys())
        try:
            result = await self._http("send_private_msg", {"user_id": int(chat_id), "message": content})
            msg_id = str(result.get("data", {}).get("message_id", ""))
            logger.info("OneBot: send OK chat=%s msg_id=%s", chat_id, msg_id)
            return SendResult(success=True, message_id=msg_id)
        except Exception as e:
            logger.error("OneBot: send failed to %s — %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a voice message.

        Strategy (2026-08 decision): pass the raw audio file to OneBot and let
        the framework transcode to silk. NapCat accepts WAV/OGG and converts
        internally — manually encoding silk adds loss with no audible benefit.

        Path mapping: in container deployments (NapCat in Docker), the host
        audio path is not visible inside the container. ``voice_mount`` config
        maps a host prefix to a container prefix, e.g.:
            voice_mount: {host: C:/hermes/data, container: /app/hermes}
        then ``.../audio_cache/voice.wav`` → ``/app/hermes/audio_cache/voice.wav``.
        ``metadata.voice_path`` (if provided) overrides the mapping.
        """
        try:
            target = None
            if metadata and metadata.get("voice_path"):
                target = metadata["voice_path"]
            if target is None and audio_path:
                host_prefix = self.voice_mount.get("host", "") if self.voice_mount else ""
                cont_prefix = self.voice_mount.get("container", "") if self.voice_mount else ""
                if host_prefix and audio_path.startswith(host_prefix):
                    target = cont_prefix + audio_path[len(host_prefix):]
            if target is None:
                target = audio_path
            message = f"[CQ:record,file={target}]"
            result = await self._http("send_private_msg", {"user_id": int(chat_id), "message": message})
            msg_id = str(result.get("data", {}).get("message_id", ""))
            return SendResult(success=True, message_id=msg_id)
        except Exception as e:
            logger.error("OneBot: send_voice failed to %s — %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """No typing indicator in OneBot protocol — no-op."""
        pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic chat info."""
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    async def send_document(self, chat_id: str, path: str, caption: str = "", metadata=None) -> SendResult:
        """Send a file (uploaded as a QQ file message)."""
        try:
            message = f"[CQ:file,file={path}]"
            result = await self._http("send_private_msg", {"user_id": int(chat_id), "message": message})
            msg_id = str(result.get("data", {}).get("message_id", ""))
            return SendResult(success=True, message_id=msg_id)
        except Exception as e:
            logger.error("OneBot: send_document failed to %s — %s", chat_id, e)
            return SendResult(success=False, error=str(e))


# ---------------------------------------------------------------------------
# Plugin registration helpers
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Return True when aiohttp and websockets are available."""
    return AIOHTTP_AVAILABLE and WEBSOCKETS_AVAILABLE


def validate_config(config) -> bool:
    """Basic config sanity check."""
    extra = getattr(config, "extra", {}) or {}
    ws_url = os.getenv("ONEBOT_WS_URL") or extra.get("ws_url", "")
    http_url = os.getenv("ONEBOT_HTTP_URL") or extra.get("http_url", "")
    mode = os.getenv("ONEBOT_WS_MODE") or extra.get("ws_mode", "forward")
    if mode == "forward" and not ws_url:
        return False
    if not http_url:
        return False
    return True


def is_connected(config) -> bool:
    """Return True when the adapter is connected (best-effort from config)."""
    return True


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env vars before adapter construction.

    Lets env-only setups surface in ``hermes gateway status`` without
    instantiating the OneBot client. Returns None when OneBot isn't
    minimally configured.
    """
    ws_url = os.getenv("ONEBOT_WS_URL", "").strip()
    http_url = os.getenv("ONEBOT_HTTP_URL", "").strip()
    if not (ws_url and http_url):
        return None
    seed: dict = {
        "ws_url": ws_url,
        "http_url": http_url,
    }
    mode = os.getenv("ONEBOT_WS_MODE", "").strip()
    if mode:
        seed["ws_mode"] = mode
    listen = os.getenv("ONEBOT_WS_LISTEN", "").strip()
    if listen:
        seed["ws_listen"] = listen
    allow = os.getenv("ONEBOT_ALLOW_FROM", "").strip()
    if allow:
        seed["allow_from"] = [x.strip() for x in allow.split(",") if x.strip()]
    groups = os.getenv("ONEBOT_GROUP_ALLOW_FROM", "").strip()
    if groups:
        seed["group_allow_from"] = [x.strip() for x in groups.split(",") if x.strip()]
    req_at = os.getenv("ONEBOT_REQUIRE_AT", "").strip().lower()
    if req_at:
        seed["require_at"] = req_at in {"1", "true", "yes"}
    home = os.getenv("ONEBOT_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": home}
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[str]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Out-of-process cron delivery: send via OneBot HTTP without a live adapter.

    Supports media_files: each path is sent as a CQ record/file message
    (voice files get the voice_mount host→container path mapping so NapCat
    inside Docker can read them).
    """
    extra = getattr(pconfig, "extra", {}) or {}
    http_url = os.getenv("ONEBOT_HTTP_URL") or extra.get("http_url", "")
    token = os.getenv("ONEBOT_ACCESS_TOKEN") or extra.get("access_token", "")
    voice_mount = extra.get("voice_mount") or {}
    # 兜底：pconfig.extra 可能缺 voice_mount（旧缓存），从 config.yaml 补读
    if not voice_mount:
        try:
            from gateway.config import load_gateway_config, Platform
            _cfg = load_gateway_config()
            _pc = _cfg.platforms.get(Platform("onebot"))
            if _pc is not None:
                voice_mount = (getattr(_pc, "extra", {}) or {}).get("voice_mount") or {}
            else:
                logger.warning("OneBot: voice_mount fallback — onebot not in config")
        except Exception as e:
            logger.warning("OneBot: voice_mount fallback failed — %s", e)
    if not http_url:
        return {"error": "OneBot standalone send: ONEBOT_HTTP_URL not configured"}

    try:
        import aiohttp

        async def _post(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{http_url.rstrip('/')}/{action}",
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    if data.get("retcode") != 0:
                        raise RuntimeError(f"OneBot API {action} failed: {data}")
                    return data

        # 1. Text message
        if message and message.strip():
            await _post("send_private_msg", {"user_id": int(chat_id), "message": message})

        # 2. Media files (voice / image / document)
        # media_files 的元素是 (path, is_voice) 元组（Hermes 约定），必须解包
        sent_any_media = False
        for mpath in media_files or []:
            if isinstance(mpath, (tuple, list)):
                p = str(mpath[0]) if len(mpath) > 0 else ""
                is_voice = bool(mpath[1]) if len(mpath) > 1 else False
            else:
                p = str(mpath)
                is_voice = False
            if not p:
                continue
            ext = Path(p).suffix.lower() if p else ""
            # Map host audio dir to container path for NapCat visibility
            # 路径规范化（Windows 反斜杠 vs 配置正斜杠）：统一转正斜杠比较
            target = p
            host_prefix = voice_mount.get("host", "") if voice_mount else ""
            cont_prefix = voice_mount.get("container", "") if voice_mount else ""
            norm_p = p.replace("\\", "/")
            norm_host = host_prefix.replace("\\", "/") if host_prefix else ""
            if norm_host and norm_p.startswith(norm_host):
                target = cont_prefix + norm_p[len(norm_host):]
            if is_voice or ext in _AUDIO_EXTS:
                cq = f"[CQ:record,file={target}]"
            elif ext in _IMAGE_EXTS:
                cq = f"[CQ:image,file={target}]"
            else:
                cq = f"[CQ:file,file={target}]"
            await _post("send_private_msg", {"user_id": int(chat_id), "message": cq})
            sent_any_media = True

        return {"success": True, "message_id": str(int(time.time() * 1000))}
    except Exception as e:
        return {"error": f"OneBot standalone send failed: {e}"}


def register(ctx):
    """Register the OneBot platform adapter with the gateway plugin system."""
    ctx.register_platform(
        name="onebot",
        label="OneBot 11 (QQ)",
        adapter_factory=lambda config: OneBotAdapter(config),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["ONEBOT_WS_URL", "ONEBOT_HTTP_URL"],
        install_hint="pip install aiohttp websockets",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="ONEBOT_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="ONEBOT_ALLOW_FROM",
        allow_all_env="",
        emoji="🐧",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via QQ through the OneBot protocol. "
            "Keep responses concise. Voice messages are sent as audio "
            "files (WAV/OGG) that the OneBot framework transcodes to silk."
        ),
    )
