"""Native Fluxer messaging platform adapter for Hermes Agent.

Fluxer exposes a Discord-shaped JSON API, but it is a distinct service with
its own REST origin and Gateway.  This adapter talks to those interfaces
directly through aiohttp; it does not patch or impersonate discord.py.

Environment variables:
    FLUXER_BOT_TOKEN                Bot token from Fluxer Developer Settings
    FLUXER_API_URL                  REST base (default https://api.fluxer.app/v1)
    FLUXER_GATEWAY_URL              Optional Gateway URL override
    FLUXER_ALLOWED_USERS            Comma-separated Fluxer user IDs
    FLUXER_ALLOW_ALL_USERS          Allow any user (development only)
    FLUXER_ALLOWED_CHANNELS         Optional guild-channel allowlist
    FLUXER_FREE_RESPONSE_CHANNELS   Guild channels that do not require mention
    FLUXER_REQUIRE_MENTION          Require bot mention in guild channels (true)
    FLUXER_HOME_CHANNEL             Default cron/notification channel
    FLUXER_PROXY                    HTTP/SOCKS proxy override
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import random
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    _ssrf_redirect_guard,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
    proxy_kwargs_for_aiohttp,
    resolve_channel_prompt,
    resolve_proxy_url,
)
from gateway.platforms.helpers import MessageDeduplicator

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.fluxer.app/v1"
MAX_MESSAGE_LENGTH = 4000
MAX_ATTACHMENTS = 10
_DEFAULT_DOWNLOAD_LIMIT = 25 * 1024 * 1024
_DEFAULT_UPLOAD_LIMIT = 25 * 1024 * 1024
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 60.0
_RECONNECT_JITTER = 0.2
_SAFE_ALLOWED_MENTIONS = {"parse": [], "replied_user": False}
_TEXT_MESSAGE_TYPES = {0, 19}  # default and reply
_CHANNEL_TYPE_MAP = {
    0: "channel",  # guild text
    1: "dm",
    2: "channel",  # guild voice text chat
    3: "group",
    999: "dm",  # personal notes
}


class _ReconnectRequested(RuntimeError):
    """Internal signal used to restart the Gateway socket."""

    def __init__(self, message: str, retry_delay: Optional[float] = None):
        super().__init__(message)
        self.retry_delay = retry_delay


class _PermanentGatewayError(RuntimeError):
    """Gateway failure that reconnecting cannot repair."""


class _UploadTooLarge(ValueError):
    """Outbound file exceeded the configured bounded-read limit."""


def _read_file_bounded(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes without unbounded buffering."""
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _UploadTooLarge(f"File exceeds Fluxer upload limit of {limit} bytes")
    return data


def _csv_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return {part.strip() for part in str(value).split(",") if part.strip()}


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalise_api_url(value: str) -> str:
    raw = (value or DEFAULT_API_URL).strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError(
            "Fluxer API URL must include a valid hostname and port"
        ) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Fluxer API URL must be an absolute HTTP(S) URL")
    _validate_endpoint_host(parsed, "Fluxer API")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Fluxer API URL must not include userinfo, query, or fragment")
    if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
        raise ValueError("Fluxer API URL must use HTTPS (HTTP is loopback-only)")
    return raw


def _is_loopback_host(hostname: Optional[str]) -> bool:
    if not hostname:
        return False
    host = hostname.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_endpoint_host(parsed: Any, label: str) -> str:
    """Reject malformed token-bearing endpoint authorities before any I/O."""
    try:
        hostname = parsed.hostname
        # Accessing ``port`` forces urllib to reject non-numeric/out-of-range ports.
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} URL must include a valid hostname and port") from exc
    if not hostname:
        raise ValueError(f"{label} URL must include a valid hostname and port")

    host = hostname.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return hostname
    except ValueError:
        pass

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"{label} URL must include a valid hostname and port") from exc
    labels = ascii_host.split(".")
    if (
        not ascii_host
        or len(ascii_host) > 253
        or any(
            not part
            or len(part) > 63
            or part.startswith("-")
            or part.endswith("-")
            or re.fullmatch(r"[A-Za-z0-9-]+", part) is None
            for part in labels
        )
    ):
        raise ValueError(f"{label} URL must include a valid hostname and port")
    return hostname


def _normalise_gateway_url(value: str) -> str:
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError(
            "Fluxer Gateway URL must include a valid hostname and port"
        ) from exc
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("Fluxer Gateway URL must be an absolute WS(S) URL")
    _validate_endpoint_host(parsed, "Fluxer Gateway")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Fluxer Gateway URL must not include userinfo or fragment")
    if parsed.scheme == "ws" and not _is_loopback_host(parsed.hostname):
        raise ValueError("Fluxer Gateway URL must use WSS (WS is loopback-only)")
    return raw


def _valid_http_base(value: str) -> bool:
    try:
        _normalise_api_url(value)
    except ValueError:
        return False
    return True


def check_fluxer_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401

        return True
    except ImportError:
        logger.warning("Fluxer: aiohttp is not installed")
        return False


def validate_fluxer_config(config: PlatformConfig) -> bool:
    extra = getattr(config, "extra", {}) or {}
    token = (
        getattr(config, "token", None)
        or extra.get("token")
        or os.getenv("FLUXER_BOT_TOKEN", "")
    )
    gateway_url = str(
        extra.get("gateway_url") or os.getenv("FLUXER_GATEWAY_URL", "")
    ).strip()
    try:
        api_url = _normalise_api_url(
            extra.get("api_url") or os.getenv("FLUXER_API_URL", DEFAULT_API_URL)
        )
        if gateway_url:
            _normalise_gateway_url(gateway_url)
    except ValueError:
        return False
    return bool(str(token).strip()) and _valid_http_base(api_url)


class FluxerAdapter(BasePlatformAdapter):
    """Hermes gateway adapter for Fluxer's REST API and real-time Gateway."""

    splits_long_messages = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("fluxer"))
        extra = config.extra or {}
        self._token = str(
            config.token or extra.get("token") or os.getenv("FLUXER_BOT_TOKEN", "")
        ).strip()
        self._api_url = _normalise_api_url(
            str(extra.get("api_url") or os.getenv("FLUXER_API_URL", DEFAULT_API_URL))
        )
        gateway_url = str(
            extra.get("gateway_url") or os.getenv("FLUXER_GATEWAY_URL", "")
        ).strip()
        self._gateway_url = _normalise_gateway_url(gateway_url) if gateway_url else ""

        self._session: Any = None
        self._ws: Any = None
        self._gateway_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_acknowledged = True
        self._gateway_was_ready = False
        self._ready_event = asyncio.Event()
        self._closing = False

        self._bot_user_id = ""
        self._bot_username = ""
        self._session_id = ""
        self._resume_gateway_url = ""
        self._sequence = 0
        self._channel_cache: Dict[str, Dict[str, Any]] = {}
        self._dedup = MessageDeduplicator()

        self._last_http_status: Optional[int] = None
        self._last_http_error = ""
        self._last_retry_after: Optional[float] = None
        try:
            self._max_upload_bytes = int(
                extra.get("max_upload_bytes")
                or os.getenv("FLUXER_MAX_UPLOAD_BYTES", _DEFAULT_UPLOAD_LIMIT)
            )
        except (TypeError, ValueError):
            self._max_upload_bytes = _DEFAULT_UPLOAD_LIMIT
        if self._max_upload_bytes <= 0:
            self._max_upload_bytes = _DEFAULT_UPLOAD_LIMIT

    # ------------------------------------------------------------------
    # REST helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-Agent/Fluxer",
        }

    def _reset_http_result(self) -> None:
        self._last_http_status = None
        self._last_http_error = ""
        self._last_retry_after = None

    async def _api(
        self,
        method: str,
        path: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Call a Fluxer REST endpoint and return decoded JSON on success."""
        import aiohttp

        self._reset_http_result()
        if self._session is None:
            self._last_http_error = "Fluxer client is not connected"
            return None
        if ".." in path:
            self._last_http_status = 400
            self._last_http_error = "Unsafe Fluxer API path"
            return None

        url = f"{self._api_url}/{path.lstrip('/')}"
        try:
            async with self._session.request(
                method.upper(),
                url,
                headers=self._headers(),
                json=json,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
                **(getattr(self, "_request_proxy_kwargs", {}) or {}),
            ) as response:
                self._last_http_status = response.status
                if response.status == 204:
                    return {}
                if response.status >= 400:
                    body = await response.text()
                    self._last_http_error = body[:1000]
                    if response.status == 429:
                        try:
                            data = json_module_loads(body)
                        except (TypeError, ValueError):
                            data = {}
                        retry = (
                            data.get("retry_after") if isinstance(data, dict) else None
                        )
                        if retry is None:
                            retry = response.headers.get("Retry-After")
                        try:
                            self._last_retry_after = (
                                float(retry) if retry is not None else None
                            )
                        except (TypeError, ValueError):
                            self._last_retry_after = None
                    logger.warning(
                        "Fluxer REST %s %s returned HTTP %s: %s",
                        method.upper(),
                        path,
                        response.status,
                        body[:300],
                    )
                    return None
                if response.content_length == 0:
                    return {}
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._last_http_error = str(exc)
            logger.warning("Fluxer REST %s %s failed: %s", method.upper(), path, exc)
            return None

    async def _api_multipart(
        self,
        method: str,
        path: str,
        *,
        payload: Dict[str, Any],
        files: Sequence[Tuple[str, bytes, str]],
    ) -> Optional[Dict[str, Any]]:
        """Send Fluxer's Discord-compatible multipart message shape."""
        import aiohttp

        self._reset_http_result()
        if self._session is None:
            self._last_http_error = "Fluxer client is not connected"
            return None
        if ".." in path:
            self._last_http_status = 400
            self._last_http_error = "Unsafe Fluxer API path"
            return None

        form = aiohttp.FormData()
        form.add_field(
            "payload_json", json.dumps(payload), content_type="application/json"
        )
        for index, (filename, data, content_type) in enumerate(files[:MAX_ATTACHMENTS]):
            form.add_field(
                f"files[{index}]",
                data,
                filename=filename,
                content_type=content_type or "application/octet-stream",
            )

        headers = {
            "Authorization": f"Bot {self._token}",
            "User-Agent": "Hermes-Agent/Fluxer",
        }
        url = f"{self._api_url}/{path.lstrip('/')}"
        try:
            async with self._session.request(
                method.upper(),
                url,
                headers=headers,
                data=form,
                timeout=aiohttp.ClientTimeout(total=90),
                **(getattr(self, "_request_proxy_kwargs", {}) or {}),
            ) as response:
                self._last_http_status = response.status
                if response.status >= 400:
                    body = await response.text()
                    self._last_http_error = body[:1000]
                    if response.status == 429:
                        retry: Any = None
                        try:
                            body_data = json_module_loads(body)
                            if isinstance(body_data, dict):
                                retry = body_data.get("retry_after")
                        except (TypeError, ValueError):
                            pass
                        if retry is None:
                            retry = response.headers.get("Retry-After")
                        try:
                            self._last_retry_after = (
                                float(retry) if retry is not None else None
                            )
                        except (TypeError, ValueError):
                            self._last_retry_after = None
                    return None
                return await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            self._last_http_error = str(exc)
            return None

    def _send_failure(self, action: str) -> SendResult:
        status = self._last_http_status
        retryable = False
        retry_after = None
        if status == 429:
            kind = "rate_limited"
            retryable = True
            retry_after = self._last_retry_after
        elif status in {401, 403}:
            kind = "forbidden"
        elif status == 404:
            kind = "not_found"
        elif status is not None and status >= 500:
            kind = "transient"
            retryable = True
        elif status is None:
            kind = "transient"
            retryable = True
        else:
            kind = "unknown"
        detail = self._last_http_error or (
            f"HTTP {status}" if status is not None else "network error"
        )
        return SendResult(
            success=False,
            error=f"Fluxer {action} failed: {detail}",
            error_kind=kind,
            retryable=retryable,
            retry_after=retry_after,
        )

    @staticmethod
    def _message_payload(
        content: str,
        chat_id: str,
        reply_to: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "content": content,
            "nonce": uuid.uuid4().hex,
            "allowed_mentions": dict(_SAFE_ALLOWED_MENTIONS),
        }
        if reply_to:
            payload["message_reference"] = {
                "message_id": str(reply_to),
                "channel_id": str(chat_id),
                "type": 0,
            }
        return payload

    # ------------------------------------------------------------------
    # Base adapter lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        import aiohttp

        if not self._token or not _valid_http_base(self._api_url):
            self._set_fatal_error(
                "fluxer_config",
                "FLUXER_BOT_TOKEN and a valid FLUXER_API_URL are required",
                retryable=False,
            )
            return False

        lock_identity = hashlib.sha256(self._token.encode()).hexdigest()[:24]
        if not self._acquire_platform_lock("fluxer", lock_identity, "Fluxer bot token"):
            return False

        try:
            proxy = resolve_proxy_url(
                platform_env_var="FLUXER_PROXY",
                target_hosts=["api.fluxer.app", "gateway.fluxer.app"],
            )
            session_kwargs, request_kwargs = proxy_kwargs_for_aiohttp(proxy)
            # aiohttp's ws_connect does not inherit per-request proxy kwargs, so
            # retain them and pass them explicitly for both REST and Gateway.
            self._request_proxy_kwargs = request_kwargs
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30), **session_kwargs
            )
            self._closing = False
            self._ready_event.clear()

            me = await self._api("GET", "users/@me")
            if not isinstance(me, dict) or not me.get("id"):
                self._set_fatal_error(
                    "fluxer_auth",
                    "Fluxer authentication failed; check FLUXER_BOT_TOKEN",
                    retryable=False,
                )
                await self.disconnect()
                return False
            self._bot_user_id = str(me["id"])
            self._bot_username = str(me.get("username") or me.get("global_name") or "")

            if not self._gateway_url:
                gateway = await self._api("GET", "gateway/bot")
                if not isinstance(gateway, dict) or not gateway.get("url"):
                    self._set_fatal_error(
                        "fluxer_gateway_discovery",
                        "Fluxer did not return a Gateway URL",
                        retryable=True,
                    )
                    await self.disconnect()
                    return False
                try:
                    self._gateway_url = _normalise_gateway_url(str(gateway["url"]))
                except ValueError as exc:
                    self._set_fatal_error(
                        "fluxer_gateway_url_invalid", str(exc), retryable=False
                    )
                    await self.disconnect()
                    return False

            self._gateway_task = asyncio.create_task(
                self._gateway_loop(), name="fluxer-gateway"
            )
            try:
                await asyncio.wait_for(self._ready_event.wait(), timeout=20.0)
            except asyncio.TimeoutError:
                self._set_fatal_error(
                    "fluxer_gateway_timeout",
                    "Timed out waiting for Fluxer Gateway READY",
                    retryable=True,
                )
                await self.disconnect()
                return False

            if self._fatal_error_code and not self._fatal_error_retryable:
                await self.disconnect()
                return False

            self._mark_connected()
            logger.info(
                "Fluxer: connected as %s (%s)",
                self._bot_username or "bot",
                self._bot_user_id,
            )
            return True
        except asyncio.CancelledError:
            # Cancellation is not an Exception on modern Python. Clean up the
            # HTTP session, Gateway task, and machine-local token lock before
            # preserving cancellation semantics for the gateway runner.
            await asyncio.shield(self.disconnect())
            raise
        except Exception as exc:
            self._set_fatal_error(
                "fluxer_connect_error", f"Fluxer startup failed: {exc}", retryable=True
            )
            logger.error("Fluxer startup failed: %s", exc, exc_info=True)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        self._closing = True
        current = asyncio.current_task()
        for task in (self._heartbeat_task, self._gateway_task):
            if task and task is not current and not task.done():
                task.cancel()
        pending = [
            task
            for task in (self._heartbeat_task, self._gateway_task)
            if task and task is not current
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._heartbeat_task = None
        self._gateway_task = None

        if self._ws is not None:
            try:
                await self._ws.close(code=1000, message=b"Hermes shutdown")
            except Exception:
                pass
            self._ws = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

        try:
            self._release_platform_lock()
        except Exception:
            logger.warning("Fluxer: failed to release token lock", exc_info=True)
        self._mark_disconnected()

    # ------------------------------------------------------------------
    # Outbound messages
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not content:
            return SendResult(success=True)
        chunks = self.truncate_message(self.format_message(content), MAX_MESSAGE_LENGTH)
        message_ids: List[str] = []
        for chunk in chunks:
            data = await self._api(
                "POST",
                f"channels/{chat_id}/messages",
                json=self._message_payload(chunk, chat_id, reply_to),
            )
            if not isinstance(data, dict) or not data.get("id"):
                return self._send_failure("message send")
            message_ids.append(str(data["id"]))
        return SendResult(
            success=True,
            message_id=message_ids[-1],
            continuation_message_ids=tuple(message_ids[:-1]),
        )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        payload = {
            "content": self.format_message(content)[:MAX_MESSAGE_LENGTH],
            "allowed_mentions": dict(_SAFE_ALLOWED_MENTIONS),
        }
        data = await self._api(
            "PATCH", f"channels/{chat_id}/messages/{message_id}", json=payload
        )
        if not isinstance(data, dict) or not data.get("id"):
            return self._send_failure("message edit")
        return SendResult(success=True, message_id=str(data["id"]))

    async def send_typing(
        self, chat_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        await self._api("POST", f"channels/{chat_id}/typing", json={})

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        channel = await self._get_channel(chat_id)
        channel_type = int(channel.get("type", 0)) if channel else 0
        return {
            "name": channel.get("name") or channel.get("display_name") or chat_id,
            "type": _CHANNEL_TYPE_MAP.get(channel_type, "channel"),
        }

    async def _send_files(
        self,
        chat_id: str,
        files: Sequence[Tuple[str, bytes, str]],
        caption: Optional[str],
        reply_to: Optional[str],
    ) -> SendResult:
        attachments = [
            {"id": index, "filename": filename, "content_type": content_type}
            for index, (filename, _data, content_type) in enumerate(
                files[:MAX_ATTACHMENTS]
            )
        ]
        payload = self._message_payload(
            (caption or "")[:MAX_MESSAGE_LENGTH], chat_id, reply_to
        )
        payload["attachments"] = attachments
        data = await self._api_multipart(
            "POST",
            f"channels/{chat_id}/messages",
            payload=payload,
            files=files[:MAX_ATTACHMENTS],
        )
        if not isinstance(data, dict) or not data.get("id"):
            return self._send_failure("file send")
        return SendResult(success=True, message_id=str(data["id"]))

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        path = Path(file_path)
        filename = file_name or path.name
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            data = await asyncio.to_thread(
                _read_file_bounded, path, self._max_upload_bytes
            )
        except (FileNotFoundError, IsADirectoryError):
            return SendResult(success=False, error=f"File not found: {file_path}")
        except OSError as exc:
            return SendResult(success=False, error=f"Could not read file: {exc}")
        except _UploadTooLarge as exc:
            return SendResult(
                success=False,
                error=str(exc),
                error_kind="file_too_large",
                retryable=False,
            )
        return await self._send_files(
            chat_id, [(filename, data, content_type)], caption, reply_to
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self.send_document(
            chat_id, image_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self.send_document(
            chat_id, audio_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self.send_document(
            chat_id, video_path, caption=caption, reply_to=reply_to, metadata=metadata
        )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        cached = await self._download_attachment({
            "url": image_url,
            "filename": "image",
            "content_type": "image/png",
        })
        if not cached:
            return await self.send(
                chat_id, f"{caption or ''}\n{image_url}".strip(), reply_to, metadata
            )
        local_path, _mime = cached
        return await self.send_document(
            chat_id,
            local_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Gateway protocol
    # ------------------------------------------------------------------

    def _gateway_connect_url(self) -> str:
        gateway_url = (
            self._resume_gateway_url
            if self._session_id and self._resume_gateway_url
            else self._gateway_url
        )
        parsed = urlsplit(_normalise_gateway_url(gateway_url))
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update({"v": "1", "encoding": "json", "compress": "none"})
        return urlunsplit((
            parsed.scheme,
            parsed.netloc,
            parsed.path or "/",
            urlencode(query),
            parsed.fragment,
        ))

    async def _gateway_loop(self) -> None:
        delay = _RECONNECT_BASE_DELAY
        while not self._closing:
            self._gateway_was_ready = False
            sleep_delay = delay
            apply_backoff = True
            try:
                await self._gateway_once()
                self._mark_disconnected()
                delay = _RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                return
            except _PermanentGatewayError as exc:
                was_ready = self._ready_event.is_set()
                self._mark_disconnected()
                self._set_fatal_error("fluxer_gateway_auth", str(exc), retryable=False)
                # Wake ``connect()`` immediately so a permanent close is not
                # misreported 20 seconds later as a retryable READY timeout.
                self._ready_event.set()
                if was_ready:
                    await self._notify_fatal_error()
                logger.error("Fluxer Gateway permanent failure: %s", exc)
                return
            except _ReconnectRequested as exc:
                if self._closing:
                    return
                self._mark_disconnected()
                if self._gateway_was_ready:
                    delay = _RECONNECT_BASE_DELAY
                if exc.retry_delay is not None:
                    sleep_delay = exc.retry_delay
                    apply_backoff = False
                else:
                    sleep_delay = delay
                logger.warning(
                    "Fluxer Gateway disconnected: %s; reconnecting in %.1fs",
                    exc,
                    sleep_delay,
                )
            except Exception as exc:
                if self._closing:
                    return
                self._mark_disconnected()
                if self._gateway_was_ready:
                    delay = _RECONNECT_BASE_DELAY
                logger.warning(
                    "Fluxer Gateway disconnected: %s; reconnecting in %.1fs", exc, delay
                )
            if self._closing:
                return
            jitter = (
                random.random() * sleep_delay * _RECONNECT_JITTER
                if apply_backoff
                else 0.0
            )
            await asyncio.sleep(sleep_delay + jitter)
            if apply_backoff:
                delay = min(delay * 2, _RECONNECT_MAX_DELAY)

    async def _gateway_once(self) -> None:
        import aiohttp

        url = self._gateway_connect_url()
        kwargs = dict(getattr(self, "_request_proxy_kwargs", {}) or {})
        ws_timeout_factory = getattr(aiohttp, "ClientWSTimeout")
        ws = await self._session.ws_connect(
            url,
            headers={"User-Agent": "Hermes-Agent/Fluxer"},
            heartbeat=30.0,
            timeout=ws_timeout_factory(ws_close=10.0),
            **kwargs,
        )
        self._ws = ws
        try:
            async for message in ws:
                if self._closing:
                    return
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except (TypeError, ValueError):
                        continue
                    await self._handle_gateway_payload(payload, ws)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    try:
                        payload = json.loads(message.data.decode("utf-8"))
                    except (AttributeError, UnicodeDecodeError, ValueError):
                        continue
                    await self._handle_gateway_payload(payload, ws)
                elif message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
            close_code = ws.close_code
            if close_code in {4004, 4010, 4011, 4012}:
                raise _PermanentGatewayError(
                    f"Fluxer Gateway rejected the connection (close {close_code})"
                )
            if close_code in {4007, 4009}:
                self._session_id = ""
                self._resume_gateway_url = ""
                self._sequence = 0
            raise _ReconnectRequested(f"Gateway closed ({close_code})")
        finally:
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                await asyncio.gather(self._heartbeat_task, return_exceptions=True)
            self._heartbeat_task = None
            if not ws.closed:
                try:
                    await ws.close(code=1000, message=b"Hermes reconnect")
                except Exception:
                    logger.debug("Fluxer Gateway socket close failed", exc_info=True)
            if self._ws is ws:
                self._ws = None

    def _start_heartbeat(self, ws: Any, interval_ms: Any) -> None:
        try:
            interval = max(1.0, float(interval_ms) / 1000.0)
        except (TypeError, ValueError):
            interval = 45.0
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_acknowledged = True
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(ws, interval), name="fluxer-heartbeat"
        )

    async def _heartbeat_loop(self, ws: Any, interval: float) -> None:
        try:
            while not self._closing and not ws.closed:
                await asyncio.sleep(max(1.0, interval * 0.8))
                if not self._heartbeat_acknowledged:
                    logger.warning("Fluxer Gateway missed heartbeat ACK; reconnecting")
                    await ws.close(code=4000, message=b"Heartbeat ACK timeout")
                    return
                self._heartbeat_acknowledged = False
                try:
                    await ws.send_json({"op": 1, "d": self._sequence})
                except Exception:
                    if not ws.closed:
                        await ws.close(code=4000, message=b"Heartbeat send failed")
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Fluxer heartbeat stopped: %s", exc)

    async def _handle_gateway_payload(self, payload: Dict[str, Any], ws: Any) -> None:
        opcode = payload.get("op")
        if opcode == 0:
            sequence = payload.get("s")
            if isinstance(sequence, int):
                self._sequence = sequence
            event = payload.get("t")
            data = payload.get("d") or {}
            if event == "READY":
                self._session_id = str(data.get("session_id") or "")
                self._resume_gateway_url = ""
                resume_url = str(data.get("resume_gateway_url") or "").strip()
                if resume_url:
                    try:
                        self._resume_gateway_url = _normalise_gateway_url(resume_url)
                    except ValueError:
                        self._resume_gateway_url = ""
                        logger.warning(
                            "Fluxer Gateway supplied an invalid resume URL; ignoring it"
                        )
                ready_user = data.get("user") or {}
                if ready_user.get("id"):
                    self._bot_user_id = str(ready_user["id"])
                self._ready_event.set()
                self._gateway_was_ready = True
                self._mark_connected()
            elif event == "RESUMED":
                self._ready_event.set()
                self._gateway_was_ready = True
                self._mark_connected()
            elif event == "MESSAGE_CREATE" and isinstance(data, dict):
                await self._handle_message_create(data)
            return

        if opcode == 10:
            hello = payload.get("d") or {}
            self._start_heartbeat(ws, hello.get("heartbeat_interval", 45000))
            if self._session_id:
                await ws.send_json({
                    "op": 6,
                    "d": {
                        "token": self._token,
                        "session_id": self._session_id,
                        "seq": self._sequence,
                    },
                })
            else:
                await ws.send_json({
                    "op": 2,
                    "d": {
                        "token": self._token,
                        "properties": {
                            "os": os.name,
                            "browser": "hermes-agent",
                            "device": "hermes-agent",
                        },
                        "presence": None,
                        # Hermes only consumes new user messages. Asking the
                        # Gateway not to emit noisy high-volume events keeps
                        # the bot's connection lightweight.
                        "ignored_events": [
                            "MESSAGE_UPDATE",
                            "MESSAGE_DELETE",
                            "TYPING_START",
                            "PRESENCE_UPDATE",
                        ],
                        "flags": 0,
                    },
                })
            return

        if opcode == 1:
            self._heartbeat_acknowledged = False
            await ws.send_json({"op": 1, "d": self._sequence})
        elif opcode == 11:
            self._heartbeat_acknowledged = True
        elif opcode == 7:
            raise _ReconnectRequested("Gateway requested reconnect", retry_delay=0.0)
        elif opcode == 9:
            if payload.get("d") is not True:
                self._session_id = ""
                self._resume_gateway_url = ""
                self._sequence = 0
            raise _ReconnectRequested(
                "Gateway invalidated session", retry_delay=random.uniform(2.5, 3.5)
            )

    # ------------------------------------------------------------------
    # Inbound messages
    # ------------------------------------------------------------------

    async def _get_channel(self, channel_id: str) -> Dict[str, Any]:
        cached = self._channel_cache.get(str(channel_id))
        if cached is not None:
            return cached
        channel = await self._api("GET", f"channels/{channel_id}")
        if not isinstance(channel, dict):
            # Do not cache a synthetic type: a transient lookup failure must not
            # permanently turn a DM into a mention-gated guild channel.
            return {"id": str(channel_id), "_lookup_failed": True}
        self._channel_cache[str(channel_id)] = channel
        return channel

    def _extra_or_env_set(self, extra_key: str, env_key: str) -> set[str]:
        extra = self.config.extra or {}
        value = extra.get(extra_key)
        if value is None:
            value = os.getenv(env_key, "")
        return _csv_set(value)

    def _requires_mention(self) -> bool:
        extra = self.config.extra or {}
        value = extra.get("require_mention")
        if value is None:
            value = os.getenv("FLUXER_REQUIRE_MENTION", "true")
        return _truthy(value, default=True)

    async def _handle_message_create(self, message: Dict[str, Any]) -> None:
        message_id = str(message.get("id") or "")
        channel_id = str(message.get("channel_id") or "")
        author = message.get("author") or {}
        author_id = str(author.get("id") or "")
        if not message_id or not channel_id or not author_id:
            return
        if author_id == self._bot_user_id or bool(author.get("bot")):
            return
        if message.get("webhook_id"):
            return
        try:
            message_type_value = int(message.get("type", 0))
        except (TypeError, ValueError):
            return
        if message_type_value not in _TEXT_MESSAGE_TYPES:
            return
        if self._dedup.is_duplicate(message_id):
            return

        channel = await self._get_channel(channel_id)
        try:
            channel_type_value = int(channel.get("type", 0))
        except (TypeError, ValueError):
            channel_type_value = 0
        guild_id = str(message.get("guild_id") or channel.get("guild_id") or "") or None
        if channel.get("_lookup_failed"):
            chat_type = "channel" if guild_id else "dm"
        else:
            chat_type = _CHANNEL_TYPE_MAP.get(channel_type_value, "channel")
        text = str(message.get("content") or "")

        if chat_type == "channel":
            allowed = self._extra_or_env_set(
                "allowed_channels", "FLUXER_ALLOWED_CHANNELS"
            )
            if allowed and channel_id not in allowed:
                return
            free = self._extra_or_env_set(
                "free_response_channels", "FLUXER_FREE_RESPONSE_CHANNELS"
            )
            mentions = message.get("mentions") or []
            mentioned_ids = {
                str(item.get("id"))
                for item in mentions
                if isinstance(item, dict) and item.get("id")
            }
            mention_pattern = re.compile(rf"<@!?{re.escape(self._bot_user_id)}>")
            has_mention = self._bot_user_id in mentioned_ids or bool(
                self._bot_user_id and mention_pattern.search(text)
            )
            if self._requires_mention() and channel_id not in free and not has_mention:
                return
            if has_mention:
                text = mention_pattern.sub("", text).strip()

        # GatewayRunner registers the same platform-bound authorization check
        # used by central ingress. Apply it before downloading attacker-controlled
        # attachments so denied users cannot consume bandwidth or cache space.
        sender_authorized = self._is_sender_authorized(author_id, chat_type, channel_id)
        # Unknown DM senders must still reach central ingress so it can issue a
        # pairing code. Keep their attachments out of the download path until
        # that authorization succeeds.
        attachments = (
            (message.get("attachments") or []) if sender_authorized is True else []
        )
        media_urls: List[str] = []
        media_types: List[str] = []
        for attachment in attachments[:MAX_ATTACHMENTS]:
            if not isinstance(attachment, dict) or not attachment.get("url"):
                continue
            cached = await self._download_attachment(attachment)
            if cached:
                local_path, mime = cached
                media_urls.append(local_path)
                media_types.append(mime)

        if not text and not media_urls and sender_authorized is True:
            return
        if text[:1].isspace() and text.lstrip().startswith("/"):
            text = text.lstrip()
        normalized_type = (
            MessageType.COMMAND if text.startswith("/") else MessageType.TEXT
        )
        if normalized_type == MessageType.TEXT and media_types:
            if any(mime.startswith("image/") for mime in media_types):
                normalized_type = MessageType.PHOTO
            elif any(mime.startswith("video/") for mime in media_types):
                normalized_type = MessageType.VIDEO
            elif any(mime.startswith("audio/") for mime in media_types):
                normalized_type = MessageType.VOICE
            else:
                normalized_type = MessageType.DOCUMENT

        reference = message.get("message_reference") or {}
        referenced = message.get("referenced_message") or {}
        referenced_author = referenced.get("author") or {}
        reply_id = str(reference.get("message_id") or "") or None

        source = self.build_source(
            chat_id=channel_id,
            chat_name=channel.get("name") or channel.get("display_name"),
            chat_type=chat_type,
            user_id=author_id,
            user_name=author.get("global_name") or author.get("username") or author_id,
            scope_id=guild_id,
            guild_id=guild_id,
            message_id=message_id,
        )
        channel_prompt = resolve_channel_prompt(
            self.config.extra or {}, channel_id, None
        )
        event = MessageEvent(
            text=text,
            message_type=normalized_type,
            source=source,
            raw_message=message,
            message_id=message_id,
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=reply_id,
            reply_to_text=(
                str(referenced.get("content"))
                if referenced.get("content") is not None
                else None
            ),
            reply_to_author_id=(
                str(referenced_author.get("id"))
                if referenced_author.get("id")
                else None
            ),
            reply_to_author_name=(
                referenced_author.get("global_name")
                or referenced_author.get("username")
                or None
            ),
            reply_to_is_own_message=bool(
                referenced_author.get("id")
                and str(referenced_author.get("id")) == self._bot_user_id
            ),
            channel_prompt=channel_prompt,
        )
        await self.handle_message(event)

    async def _download_attachment(
        self, attachment: Dict[str, Any]
    ) -> Optional[Tuple[str, str]]:
        import httpx
        from tools.url_safety import create_ssrf_safe_async_client, is_safe_url

        url = str(attachment.get("url") or "")
        if not url or not is_safe_url(url):
            logger.warning("Fluxer: blocked unsafe attachment URL")
            return None
        filename = Path(str(attachment.get("filename") or "attachment")).name
        declared_mime = str(
            attachment.get("content_type")
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        try:
            limit = int(os.getenv("FLUXER_MAX_DOWNLOAD_BYTES", _DEFAULT_DOWNLOAD_LIMIT))
        except (TypeError, ValueError):
            limit = _DEFAULT_DOWNLOAD_LIMIT
        try:
            async with create_ssrf_safe_async_client(
                timeout=60.0,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
            ) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if (
                        content_length
                        and content_length.isdigit()
                        and int(content_length) > limit
                    ):
                        logger.warning(
                            "Fluxer: attachment exceeds download limit: %s", filename
                        )
                        return None
                    chunks: List[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes(64 * 1024):
                        size += len(chunk)
                        if size > limit:
                            logger.warning(
                                "Fluxer: attachment exceeded download limit: %s",
                                filename,
                            )
                            return None
                        chunks.append(chunk)
                    data = b"".join(chunks)
                    mime = (
                        response.headers.get("Content-Type", "").split(";", 1)[0]
                        or declared_mime
                    )
        except (httpx.HTTPError, ValueError, asyncio.TimeoutError) as exc:
            logger.warning(
                "Fluxer: failed to download attachment %s: %s", filename, exc
            )
            return None

        suffix = Path(filename).suffix
        if mime.startswith("image/"):
            return cache_image_from_bytes(data, suffix or ".png"), mime
        if mime.startswith("audio/"):
            return cache_audio_from_bytes(data, suffix or ".ogg"), mime
        return cache_document_from_bytes(data, filename), mime


# Keep json parsing mockable without shadowing the ``json=`` keyword in _api.
def json_module_loads(value: str) -> Any:
    return json.loads(value)


# ---------------------------------------------------------------------------
# Plugin configuration, standalone delivery, and setup
# ---------------------------------------------------------------------------


def _env_enablement() -> Optional[Dict[str, Any]]:
    token = os.getenv("FLUXER_BOT_TOKEN", "").strip()
    if not token:
        return None
    seed: Dict[str, Any] = {"token": token}
    api_url = os.getenv("FLUXER_API_URL", "").strip()
    if api_url:
        seed["api_url"] = _normalise_api_url(api_url)
    gateway_url = os.getenv("FLUXER_GATEWAY_URL", "").strip()
    if gateway_url:
        seed["gateway_url"] = gateway_url
    home = os.getenv("FLUXER_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("FLUXER_HOME_CHANNEL_NAME", "Home").strip() or "Home",
        }
    return seed


def _is_connected(config: PlatformConfig) -> bool:
    return bool(getattr(config, "enabled", False)) and validate_fluxer_config(config)


def _apply_yaml_config(_yaml_cfg: dict, fluxer_cfg: dict) -> Optional[dict]:
    extras: Dict[str, Any] = {}
    for key in (
        "api_url",
        "gateway_url",
        "allowed_channels",
        "free_response_channels",
        "require_mention",
        "max_upload_bytes",
    ):
        if key in fluxer_cfg:
            extras[key] = fluxer_cfg[key]
    return extras or None


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    import aiohttp

    adapter = FluxerAdapter(pconfig)
    proxy = resolve_proxy_url(
        platform_env_var="FLUXER_PROXY",
        target_hosts=["api.fluxer.app", "gateway.fluxer.app"],
    )
    session_kwargs, request_kwargs = proxy_kwargs_for_aiohttp(proxy)
    adapter._request_proxy_kwargs = request_kwargs
    adapter._session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=90), **session_kwargs
    )
    try:
        files: List[Tuple[str, bytes, str]] = []
        for media in (media_files or [])[:MAX_ATTACHMENTS]:
            path_value = media.get("path") if isinstance(media, dict) else media
            if not path_value:
                continue
            path = Path(str(path_value))
            if not path.is_file():
                continue
            if path.stat().st_size > adapter._max_upload_bytes:
                return {
                    "error": (
                        "File exceeds Fluxer upload limit of "
                        f"{adapter._max_upload_bytes} bytes: {path.name}"
                    )
                }
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            try:
                data = await asyncio.to_thread(
                    _read_file_bounded, path, adapter._max_upload_bytes
                )
            except _UploadTooLarge as exc:
                return {"error": f"{exc}: {path.name}"}
            files.append((path.name, data, mime))
        if files:
            result = await adapter._send_files(chat_id, files, message, thread_id)
        else:
            result = await adapter.send(chat_id, message, reply_to=thread_id)
        if not result.success:
            return {"error": result.error or "Fluxer send failed"}
        return {
            "success": True,
            "platform": "fluxer",
            "chat_id": chat_id,
            "message_id": result.message_id,
        }
    finally:
        await adapter._session.close()
        adapter._session = None


def interactive_setup() -> None:
    from hermes_cli.cli_output import (
        print_header,
        print_info,
        print_success,
        prompt,
        prompt_yes_no,
    )
    from hermes_cli.config import get_env_value, remove_env_value, save_env_value

    print_header("Fluxer")
    if get_env_value("FLUXER_BOT_TOKEN"):
        print_info("Fluxer is already configured")
        if not prompt_yes_no("Reconfigure Fluxer?", False):
            return

    print_info("Create a bot in Fluxer Developer Settings and copy its bot token.")
    print_info("Do not paste the token into a chat message.")
    token = prompt("Fluxer bot token", password=True)
    if not token:
        return
    save_env_value("FLUXER_BOT_TOKEN", token.strip())
    print_success("Fluxer bot token saved")

    # Guided setup is fail-closed. Clear any stale broad-access override before
    # applying the selected allowlist/default pairing policy.
    remove_env_value("FLUXER_ALLOW_ALL_USERS")
    allowed = prompt(
        "Allowed Fluxer user IDs (comma-separated, empty for default pairing policy)"
    )
    if allowed.strip():
        save_env_value("FLUXER_ALLOWED_USERS", allowed.replace(" ", ""))
        print_success("Fluxer user allowlist configured")
    else:
        remove_env_value("FLUXER_ALLOWED_USERS")
        print_info("No Fluxer user allowlist configured")

    home = prompt("Home channel ID (empty to set later with /set-home)").strip()
    if home:
        save_env_value("FLUXER_HOME_CHANNEL", home)
    else:
        remove_env_value("FLUXER_HOME_CHANNEL")


def register(ctx) -> None:
    ctx.register_platform(
        name="fluxer",
        label="Fluxer",
        adapter_factory=lambda config: FluxerAdapter(config),
        check_fn=check_fluxer_requirements,
        validate_config=validate_fluxer_config,
        is_connected=_is_connected,
        required_env=["FLUXER_BOT_TOKEN"],
        install_hint="pip install aiohttp",
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="FLUXER_ALLOWED_USERS",
        allow_all_env="FLUXER_ALLOW_ALL_USERS",
        cron_deliver_env_var="FLUXER_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🟣",
        platform_hint=(
            "You are chatting via Fluxer. Markdown, replies, and file attachments "
            "are supported. Do not emit mass mentions unless the user explicitly asks."
        ),
        allow_update_command=True,
    )
