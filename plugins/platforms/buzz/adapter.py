"""Buzz platform adapter for the Hermes gateway.

Inbound messages arrive through a long-lived, NIP-42-authenticated Nostr
WebSocket subscription. Signed outbound writes use the Buzz CLI so one-shot
delivery never races a temporary WebSocket authentication handshake.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)


try:
    from .nostr_auth import build_auth_event, public_key_hex
except ImportError:
    # tests/gateway/_plugin_adapter_loader.py loads adapter.py directly under a
    # unique module name. Load the sibling without modifying sys.path.
    _auth_path = Path(__file__).with_name("nostr_auth.py")
    _auth_spec = importlib.util.spec_from_file_location(
        "plugin_adapter_buzz_nostr_auth", _auth_path
    )
    if _auth_spec is None or _auth_spec.loader is None:
        raise ImportError(f"Could not load Buzz Nostr auth module: {_auth_path}")
    _auth_module = importlib.util.module_from_spec(_auth_spec)
    sys.modules[_auth_spec.name] = _auth_module
    _auth_spec.loader.exec_module(_auth_module)
    build_auth_event = _auth_module.build_auth_event
    public_key_hex = _auth_module.public_key_hex


logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 60_000
MESSAGE_KINDS = (9, 45001, 45003)
MEMBERSHIP_ADDED_KIND = 44100
MEMBERSHIP_SUBSCRIPTION_ID = "hermes-buzz-membership"
WS_AUTH_TIMEOUT = 20.0
WS_MAX_MESSAGE_BYTES = 2_000_000
MAX_TRACKED_EVENT_IDS = 10_000
MAX_TRACKED_SENT_IDS = 2_000
_SLASH_CONFIRM_COMMANDS = frozenset({"/approve", "/always", "/cancel"})
_EXEC_APPROVAL_COMMANDS = frozenset({"/approve", "/deny"})
_EXEC_APPROVAL_RESPONSES = frozenset({
    "approve",
    "yes",
    "ok",
    "okay",
    "confirm",
    "y",
    "👍",
    "deny",
    "no",
    "reject",
    "cancel",
    "n",
    "👎",
    "always",
    "approve always",
    "always approve",
    "session",
    "approve session",
    "session approve",
})


class BuzzCliError(RuntimeError):
    """Raised when the Buzz CLI cannot complete an operation."""


def _split_csv(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = str(value or "").split(",")
    return [str(part).strip() for part in parts if str(part).strip()]


def _bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _tag_values(tags: Any, name: str) -> list[str]:
    values: list[str] = []
    if not isinstance(tags, list):
        return values
    for tag in tags:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name:
            values.append(str(tag[1]))
    return values


def _thread_id(tags: Any) -> Optional[str]:
    if not isinstance(tags, list):
        return None
    reply: Optional[str] = None
    for tag in tags:
        if not isinstance(tag, list) or len(tag) < 2 or tag[0] != "e":
            continue
        marker = str(tag[3]) if len(tag) >= 4 else ""
        if marker == "root":
            return str(tag[1])
        if marker == "reply":
            reply = str(tag[1])
    return reply


def _wake_word_pattern(wake_word: str, *, at_start: bool = False) -> re.Pattern:
    normalized = wake_word.strip().lstrip("@").strip()
    if not normalized:
        return re.compile(r"(?!x)x")
    prefix = r"^\s*" if at_start else r"(?<![\w@'-])"
    return re.compile(
        rf"{prefix}@{re.escape(normalized)}(?![\w'-])",
        flags=re.IGNORECASE,
    )


def _contains_wake_word(content: str, wake_words: list[str]) -> bool:
    return any(
        _wake_word_pattern(wake_word).search(content) is not None
        for wake_word in wake_words
    )


def _strip_leading_wake_word(content: str, wake_words: list[str]) -> str:
    for wake_word in wake_words:
        match = _wake_word_pattern(wake_word, at_start=True).match(content)
        if match is None:
            continue
        remainder = content[match.end() :]
        remainder = re.sub(r"^[ \t]*[:,][ \t]*", "", remainder, count=1)
        return remainder.lstrip()
    return content


def _event_timestamp(value: Any, *, now: Optional[int] = None) -> int:
    current = int(time.time()) if now is None else int(now)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return current
    return min(max(parsed, 0), current)


def _is_for_agent(
    event: dict[str, Any],
    *,
    agent_pubkey: str,
    wake_words: list[str],
    sent_ids: set[str],
) -> bool:
    tags = event.get("tags") or []
    if agent_pubkey and agent_pubkey.lower() in {
        value.lower() for value in _tag_values(tags, "p")
    }:
        return True
    if sent_ids.intersection(_tag_values(tags, "e")):
        return True
    content = str(event.get("content") or "")
    return _contains_wake_word(content, wake_words)


def _private_key() -> str:
    return os.getenv("BUZZ_PRIVATE_KEY", "").strip()


def _auth_tag() -> str:
    return os.getenv("BUZZ_AUTH_TAG", "").strip()


def _relay_url(config: Optional[PlatformConfig] = None) -> str:
    extra = getattr(config, "extra", {}) or {}
    return str(extra.get("relay_url") or os.getenv("BUZZ_RELAY_URL", "")).strip()


def _default_cli_paths() -> tuple[Path, ...]:
    hermes_home = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    user_home = Path.home()
    return (
        hermes_home / "bin" / "buzz",
        user_home / ".local" / "bin" / "buzz",
        user_home / ".cargo" / "bin" / "buzz",
        Path("/opt/homebrew/bin/buzz"),
        Path("/usr/local/bin/buzz"),
    )


def _resolve_cli(config: Optional[PlatformConfig] = None) -> Optional[str]:
    extra = getattr(config, "extra", {}) or {}
    configured = str(extra.get("cli") or os.getenv("BUZZ_CLI", "")).strip()
    if configured:
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return None
    resolved = shutil.which("buzz")
    if resolved:
        return resolved
    for path in _default_cli_paths():
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def _websocket_url(relay_url: str) -> str:
    parsed = urlsplit(relay_url.strip())
    scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
    if scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("Buzz relay URL must use http(s) or ws(s)")
    return urlunsplit((scheme, parsed.netloc, parsed.path or "", parsed.query, ""))


def _command_env(relay_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["BUZZ_RELAY_URL"] = relay_url
    private_key = _private_key()
    if private_key:
        env["BUZZ_PRIVATE_KEY"] = private_key
    auth_tag = _auth_tag()
    if auth_tag:
        env["BUZZ_AUTH_TAG"] = auth_tag
    return env


def _run_cli_sync(
    cli: str,
    relay_url: str,
    args: list[str],
    *,
    input_text: Optional[str] = None,
    timeout: float = 30.0,
) -> Any:
    proc = subprocess.run(
        [cli, "--format", "json", *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=_command_env(relay_url),
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "Buzz CLI failed").strip()
        try:
            parsed = json.loads(detail)
            detail = str(parsed.get("message") or detail)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise BuzzCliError(detail[:500])
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise BuzzCliError("Buzz CLI returned invalid JSON") from exc


async def _run_cli(
    cli: str,
    relay_url: str,
    args: list[str],
    *,
    input_text: Optional[str] = None,
    timeout: float = 30.0,
) -> Any:
    return await asyncio.to_thread(
        _run_cli_sync,
        cli,
        relay_url,
        args,
        input_text=input_text,
        timeout=timeout,
    )


def check_requirements() -> bool:
    """Return true only when the two required Buzz credentials are present."""
    return bool(os.getenv("BUZZ_RELAY_URL", "").strip() and _private_key())


def validate_config(config: PlatformConfig) -> bool:
    relay_url = _relay_url(config)
    private_key = _private_key()
    if not (relay_url and private_key and _resolve_cli(config)):
        return False
    try:
        _websocket_url(relay_url)
        public_key_hex(private_key)
    except ValueError:
        return False
    return True


def is_connected(config: PlatformConfig) -> bool:
    return validate_config(config)


class BuzzAdapter(BasePlatformAdapter):
    """Subscribe to Buzz channels and route addressed messages into Hermes."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("buzz"))
        extra = config.extra or {}
        self._cli = _resolve_cli(config) or "buzz"
        self._relay_url = _relay_url(config)
        try:
            self._agent_pubkey = public_key_hex(_private_key()).lower()
        except ValueError:
            self._agent_pubkey = ""

        configured_channels = _split_csv(
            extra.get("channels")
            or os.getenv("BUZZ_CHANNELS")
            or os.getenv("BUZZ_HOME_CHANNEL")
        )
        explicit_dm_channels = _split_csv(
            extra.get("dm_channels") or os.getenv("BUZZ_DM_CHANNELS")
        )
        self._channels = list(
            dict.fromkeys([*configured_channels, *explicit_dm_channels])
        )
        self._dm_channels = set(explicit_dm_channels)
        self._discover_dms = _bool(
            extra.get("discover_dms", os.getenv("BUZZ_DISCOVER_DMS")),
            default=True,
        )
        self._require_mention = _bool(
            extra.get("require_mention", os.getenv("BUZZ_REQUIRE_MENTION")),
            default=True,
        )
        self._profile_name = str(
            extra.get("profile_name") or os.getenv("BUZZ_PROFILE_NAME", "")
        ).strip()
        self._profile_about = str(
            extra.get("profile_about") or os.getenv("BUZZ_PROFILE_ABOUT", "")
        ).strip()
        configured_wake_words = _split_csv(
            extra.get("wake_words") or os.getenv("BUZZ_WAKE_WORDS", "Hermes,Maximus")
        )
        self._wake_words = list(
            dict.fromkeys([
                *configured_wake_words,
                *([self._profile_name] if self._profile_name else []),
            ])
        )

        self._ws_task: Optional[asyncio.Task] = None
        self._ws_ready = asyncio.Event()
        self._seen_ids: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._sent_ids: set[str] = set()
        self._sent_order: deque[str] = deque()
        self._since: dict[str, int] = {}
        self._membership_since = 0
        self._lock_key: Optional[str] = None

    @property
    def name(self) -> str:
        return "Buzz"

    async def _run_cli(
        self,
        args: list[str],
        *,
        input_text: Optional[str] = None,
        timeout: float = 30.0,
    ) -> Any:
        return await _run_cli(
            self._cli,
            self._relay_url,
            args,
            input_text=input_text,
            timeout=timeout,
        )

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not validate_config(self.config):
            self._set_fatal_error(
                "buzz_config_missing",
                "Buzz requires BUZZ_RELAY_URL, BUZZ_PRIVATE_KEY, and the Buzz CLI",
                retryable=False,
            )
            return False

        self._lock_key = f"{self._relay_url}:{self._agent_pubkey}"
        if not self._acquire_platform_lock(
            "buzz",
            self._lock_key,
            "Buzz identity",
        ):
            self._lock_key = None
            return False

        try:
            await self._run_cli(["channels", "list", "--member"], timeout=20.0)
            if public_key_hex(_private_key()).lower() != self._agent_pubkey:
                raise ValueError("Buzz private key changed during connection setup")
        except Exception as exc:
            logger.error("[Buzz] Relay/auth probe failed: %s", exc)
            await self._release_lock()
            self._set_fatal_error("buzz_probe_failed", str(exc), retryable=True)
            return False

        if self._discover_dms:
            await self._discover_dm_channels()

        now = int(time.time())
        if not is_reconnect:
            self._since = {channel: now for channel in self._channels}
            self._membership_since = now
        else:
            for channel in self._channels:
                self._since.setdefault(channel, now - 5)
            if not self._membership_since:
                self._membership_since = now - 5

        if not is_reconnect:
            await self._publish_profile_best_effort()
        await self._set_presence_best_effort("online")

        self._running = True
        self._ws_ready.clear()
        self._ws_task = asyncio.create_task(self._websocket_loop())
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=WS_AUTH_TIMEOUT + 5)
        except TimeoutError:
            self._running = False
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
            await self._release_lock()
            self._set_fatal_error(
                "buzz_websocket_auth_failed",
                "Buzz WebSocket did not authenticate before the timeout",
                retryable=True,
            )
            return False

        self._mark_connected()
        logger.info(
            "[Buzz] Subscribed to %d channel(s) over WebSocket",
            len(self._channels),
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        self._ws_task = None
        await self._set_presence_best_effort("offline")
        await self._release_lock()

    async def _release_lock(self) -> None:
        if not self._lock_key:
            return
        try:
            self._release_platform_lock()
        except Exception:
            logger.debug("[Buzz] Failed to release identity lock", exc_info=True)
        self._lock_key = None

    async def _discover_dm_channels(self) -> int:
        try:
            conversations = await self._run_cli(
                [
                    "channels",
                    "search",
                    "--query",
                    "DM",
                    "--include-archived",
                    "--limit",
                    "1000",
                ],
                timeout=20.0,
            )
        except Exception as exc:
            logger.warning("[Buzz] Could not discover direct messages: %s", exc)
            return 0

        added = 0
        if not isinstance(conversations, list):
            return added
        for conversation in conversations:
            if not isinstance(conversation, dict):
                continue
            if conversation.get("channel_type") != "dm":
                continue
            dm_id = str(conversation.get("channel_id") or "").strip()
            if not dm_id:
                continue
            self._dm_channels.add(dm_id)
            if dm_id not in self._channels:
                self._channels.append(dm_id)
                added += 1
        if added:
            logger.info("[Buzz] Discovered %d direct-message conversation(s)", added)
        return added

    async def _publish_profile_best_effort(self) -> None:
        args = ["users", "set-profile"]
        if self._profile_name:
            args.extend(["--name", self._profile_name])
        if self._profile_about:
            args.extend(["--about", self._profile_about])
        if len(args) == 2:
            return
        try:
            await self._run_cli(args, timeout=20.0)
        except Exception as exc:
            logger.warning("[Buzz] Could not publish identity profile: %s", exc)

    async def _set_presence_best_effort(self, status: str) -> None:
        try:
            await self._run_cli(
                ["users", "set-presence", "--status", status], timeout=10.0
            )
        except Exception as exc:
            logger.debug("[Buzz] Could not set presence to %s: %s", status, exc)

    async def _authenticate_websocket(self, websocket) -> None:
        raw = await asyncio.wait_for(websocket.recv(), timeout=WS_AUTH_TIMEOUT)
        message = json.loads(raw)
        if not isinstance(message, list) or len(message) < 2 or message[0] != "AUTH":
            raise BuzzCliError("Buzz relay did not send a NIP-42 AUTH challenge")

        event = build_auth_event(
            private_key=_private_key(),
            challenge=str(message[1]),
            relay_url=_websocket_url(self._relay_url),
            auth_tag_json=_auth_tag(),
        )
        await websocket.send(json.dumps(["AUTH", event], separators=(",", ":")))

        while True:
            raw = await asyncio.wait_for(websocket.recv(), timeout=WS_AUTH_TIMEOUT)
            response = json.loads(raw)
            if not isinstance(response, list) or not response:
                continue
            if (
                response[0] == "OK"
                and len(response) >= 4
                and response[1] == event["id"]
            ):
                if response[2] is True:
                    return
                raise BuzzCliError(f"Buzz WebSocket AUTH rejected: {response[3]}")
            if response[0] in {"NOTICE", "CLOSED"}:
                detail = response[-1] if len(response) > 1 else "authentication failed"
                raise BuzzCliError(f"Buzz WebSocket AUTH failed: {detail}")

    async def _send_channel_subscription(
        self, websocket, subscription_id: str, channel: str
    ) -> None:
        since = max(self._since.get(channel, int(time.time())) - 1, 0)
        request = [
            "REQ",
            subscription_id,
            {
                "kinds": list(MESSAGE_KINDS),
                "#h": [channel],
                "since": since,
            },
        ]
        await websocket.send(json.dumps(request, separators=(",", ":")))

    async def _subscribe_websocket(self, websocket) -> dict[str, Optional[str]]:
        subscriptions: dict[str, Optional[str]] = {}
        for index, channel in enumerate(self._channels):
            subscription_id = f"hermes-buzz-{index}"
            subscriptions[subscription_id] = channel
            await self._send_channel_subscription(websocket, subscription_id, channel)

        if self._discover_dms:
            request = [
                "REQ",
                MEMBERSHIP_SUBSCRIPTION_ID,
                {
                    "kinds": [MEMBERSHIP_ADDED_KIND],
                    "#p": [self._agent_pubkey],
                    "since": max(self._membership_since - 1, 0),
                },
            ]
            await websocket.send(json.dumps(request, separators=(",", ":")))
            subscriptions[MEMBERSHIP_SUBSCRIPTION_ID] = None
        return subscriptions

    async def _handle_dm_discovery(
        self,
        websocket,
        subscriptions: dict[str, Optional[str]],
        event: dict[str, Any],
    ) -> None:
        created_at = _event_timestamp(event.get("created_at"))
        self._membership_since = max(self._membership_since, created_at)
        dm_ids = _tag_values(event.get("tags") or [], "h")
        if not dm_ids or not dm_ids[0].strip():
            return

        subscribed_before = set(self._channels)
        await self._discover_dm_channels()
        new_dm_ids = [
            channel
            for channel in self._channels
            if channel in self._dm_channels and channel not in subscribed_before
        ]
        for new_dm_id in new_dm_ids:
            self._since[new_dm_id] = created_at
            subscription_id = f"hermes-buzz-dm-{len(subscriptions)}"
            subscriptions[subscription_id] = new_dm_id
            await self._send_channel_subscription(websocket, subscription_id, new_dm_id)
            logger.info(
                "[Buzz] Subscribed to new direct-message conversation %s",
                new_dm_id,
            )

    async def _websocket_loop(self) -> None:
        import websockets

        backoff = 1.0
        has_subscribed = False
        while self._running:
            try:
                async with websockets.connect(
                    _websocket_url(self._relay_url),
                    open_timeout=WS_AUTH_TIMEOUT,
                    close_timeout=5,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=WS_MAX_MESSAGE_BYTES,
                ) as websocket:
                    await self._authenticate_websocket(websocket)
                    subscriptions = await self._subscribe_websocket(websocket)
                    if has_subscribed:
                        logger.info(
                            "[Buzz] WebSocket reconnected; restored %d channel subscription(s)",
                            len(self._channels),
                        )
                    has_subscribed = True
                    self._ws_ready.set()
                    backoff = 1.0

                    async for raw in websocket:
                        try:
                            message = json.loads(raw)
                        except (json.JSONDecodeError, TypeError):
                            logger.warning("[Buzz] Ignoring malformed WebSocket frame")
                            continue
                        if not isinstance(message, list) or not message:
                            continue
                        if message[0] == "EVENT" and len(message) >= 3:
                            subscription_id = str(message[1])
                            event = message[2]
                            if not isinstance(event, dict):
                                continue
                            if subscription_id == MEMBERSHIP_SUBSCRIPTION_ID:
                                await self._handle_dm_discovery(
                                    websocket, subscriptions, event
                                )
                                continue
                            channel = subscriptions.get(subscription_id)
                            if channel:
                                created_at = _event_timestamp(event.get("created_at"))
                                self._since[channel] = max(
                                    self._since.get(channel, 0), created_at
                                )
                                await self._dispatch_event(channel, event)
                        elif message[0] == "CLOSED":
                            detail = (
                                message[-1]
                                if len(message) > 2
                                else "subscription closed"
                            )
                            raise BuzzCliError(str(detail))
                        elif message[0] == "NOTICE":
                            logger.warning("[Buzz] Relay notice: %s", message[-1])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    return
                logger.warning(
                    "[Buzz] WebSocket disconnected; retrying in %.1fs: %s",
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _remember_event_id(self, event_id: str) -> bool:
        if event_id in self._seen_ids:
            return False
        self._seen_ids.add(event_id)
        self._seen_order.append(event_id)
        while len(self._seen_order) > MAX_TRACKED_EVENT_IDS:
            self._seen_ids.discard(self._seen_order.popleft())
        return True

    def _remember_sent_id(self, event_id: str) -> None:
        if not event_id or event_id in self._sent_ids:
            return
        self._sent_ids.add(event_id)
        self._sent_order.append(event_id)
        while len(self._sent_order) > MAX_TRACKED_SENT_IDS:
            self._sent_ids.discard(self._sent_order.popleft())

    async def _dispatch_event(self, channel: str, event: dict[str, Any]) -> None:
        event_id = str(event.get("id") or "")
        author = str(event.get("pubkey") or "").lower()
        text = str(event.get("content") or "").strip()
        if (
            not event_id
            or not author
            or not text
            or not self._remember_event_id(event_id)
        ):
            return
        if author == self._agent_pubkey:
            return

        chat_type = "dm" if channel in self._dm_channels else "group"
        tags = event.get("tags") or []
        source = self.build_source(
            chat_id=channel,
            chat_name=channel,
            chat_type=chat_type,
            user_id=author,
            user_name=author[:12],
            thread_id=_thread_id(tags),
        )
        addressed = _is_for_agent(
            event,
            agent_pubkey=self._agent_pubkey,
            wake_words=self._wake_words,
            sent_ids=self._sent_ids,
        )
        if (
            chat_type == "group"
            and self._require_mention
            and not addressed
            and not self._is_pending_control_reply(source, text)
        ):
            return

        text = _strip_leading_wake_word(text, self._wake_words)
        if not text:
            return
        created_at = _event_timestamp(event.get("created_at"))
        message_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=event_id,
            raw_message=event,
            timestamp=datetime.fromtimestamp(created_at, tz=timezone.utc),
        )
        await self.handle_message(message_event)

    def _is_pending_control_reply(self, source, text: str) -> bool:
        from gateway.session import build_session_key

        extra = self.config.extra or {}
        session_key = build_session_key(
            source,
            group_sessions_per_user=extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
        )
        command = (text.lstrip().split(maxsplit=1) or [""])[0].lower()
        normalized_text = text.strip().lower()

        if (
            command in _EXEC_APPROVAL_COMMANDS
            or normalized_text in _EXEC_APPROVAL_RESPONSES
        ):
            try:
                from tools.approval import has_blocking_approval

                if has_blocking_approval(session_key):
                    return True
            except Exception:
                logger.debug(
                    "[Buzz] Could not inspect pending exec approval", exc_info=True
                )

        if command in _SLASH_CONFIRM_COMMANDS:
            try:
                from tools.slash_confirm import get_pending

                if get_pending(session_key) is not None:
                    return True
            except Exception:
                logger.debug(
                    "[Buzz] Could not inspect pending slash confirm", exc_info=True
                )

        if not command.startswith("/"):
            try:
                from tools.clarify_gateway import get_pending_for_session

                return (
                    get_pending_for_session(
                        session_key,
                        include_choice_prompts=True,
                    )
                    is not None
                )
            except Exception:
                logger.debug(
                    "[Buzz] Could not inspect pending clarification", exc_info=True
                )
        return False

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        args = ["messages", "send", "--channel", chat_id, "--content", "-"]
        explicit_thread = (metadata or {}).get("thread_id")
        parent = explicit_thread or (
            reply_to if chat_id not in self._dm_channels else None
        )
        if parent:
            args.extend(["--reply-to", str(parent)])
        try:
            result = await self._run_cli(args, input_text=content, timeout=60.0)
            message_id = str((result or {}).get("event_id") or "")
            self._remember_sent_id(message_id)
            accepted = bool((result or {}).get("accepted", True))
            return SendResult(
                success=accepted,
                message_id=message_id or None,
                error=None
                if accepted
                else str((result or {}).get("message") or "Relay rejected message"),
            )
        except Exception as exc:
            logger.error("[Buzz] Send failed: %s", exc)
            return SendResult(success=False, error=str(exc))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {
            "name": chat_id,
            "type": "dm" if chat_id in self._dm_channels else "group",
            "chat_id": chat_id,
        }


def _env_enablement() -> Optional[dict[str, Any]]:
    relay_url = os.getenv("BUZZ_RELAY_URL", "").strip()
    if not (relay_url and _private_key()):
        return None

    channels = _split_csv(os.getenv("BUZZ_CHANNELS") or os.getenv("BUZZ_HOME_CHANNEL"))
    seed: dict[str, Any] = {
        "relay_url": relay_url,
        "channels": channels,
        "dm_channels": _split_csv(os.getenv("BUZZ_DM_CHANNELS")),
        "discover_dms": _bool(os.getenv("BUZZ_DISCOVER_DMS"), default=True),
        "require_mention": _bool(os.getenv("BUZZ_REQUIRE_MENTION"), default=True),
        "wake_words": _split_csv(os.getenv("BUZZ_WAKE_WORDS", "Hermes,Maximus")),
    }
    home = os.getenv("BUZZ_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": "Buzz"}
    return seed


def _apply_yaml_config(
    yaml_cfg: dict[str, Any], platform_cfg: dict[str, Any]
) -> Optional[dict[str, Any]]:
    del yaml_cfg
    extra = platform_cfg.get("extra") if isinstance(platform_cfg, dict) else {}
    if not isinstance(extra, dict):
        extra = {}

    def seed(name: str, value: Any) -> None:
        if value is None or value == "" or os.getenv(name):
            return
        if name == "BUZZ_HOME_CHANNEL" and isinstance(value, dict):
            value = value.get("chat_id")
            if not value:
                return
        if isinstance(value, (list, tuple, set)):
            value = ",".join(str(item) for item in value)
        os.environ[name] = str(value)

    seed("BUZZ_RELAY_URL", extra.get("relay_url"))
    seed("BUZZ_CHANNELS", extra.get("channels"))
    seed("BUZZ_HOME_CHANNEL", extra.get("home_channel"))
    seed("BUZZ_DM_CHANNELS", extra.get("dm_channels"))
    seed("BUZZ_DISCOVER_DMS", extra.get("discover_dms"))
    seed("BUZZ_ALLOWED_USERS", extra.get("allowed_users"))
    seed("BUZZ_ALLOW_ALL_USERS", extra.get("allow_all_users"))
    seed("BUZZ_REQUIRE_MENTION", extra.get("require_mention"))
    seed("BUZZ_WAKE_WORDS", extra.get("wake_words"))
    seed("BUZZ_PROFILE_NAME", extra.get("profile_name"))
    seed("BUZZ_PROFILE_ABOUT", extra.get("profile_about"))
    seed("BUZZ_CLI", extra.get("cli"))
    return None


async def _standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list[str]] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    del force_document
    if media_files:
        return {"error": "Buzz standalone delivery does not support media files"}
    cli = _resolve_cli(pconfig)
    relay_url = _relay_url(pconfig)
    if not cli:
        return {"error": "Buzz CLI not found"}
    if not relay_url or not _private_key():
        return {"error": "BUZZ_RELAY_URL and BUZZ_PRIVATE_KEY are required"}

    args = ["messages", "send", "--channel", chat_id, "--content", "-"]
    if thread_id:
        args.extend(["--reply-to", str(thread_id)])
    try:
        result = await _run_cli(
            cli,
            relay_url,
            args,
            input_text=message,
            timeout=60.0,
        )
    except Exception as exc:
        return {"error": f"Buzz standalone send failed: {exc}"}

    accepted = bool((result or {}).get("accepted", True))
    if not accepted:
        return {"error": str((result or {}).get("message") or "Relay rejected message")}
    return {
        "success": True,
        "platform": "buzz",
        "chat_id": chat_id,
        "message_id": str((result or {}).get("event_id") or "") or None,
    }


def register(ctx) -> None:
    ctx.register_platform(
        name="buzz",
        label="Buzz",
        adapter_factory=lambda cfg: BuzzAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["BUZZ_RELAY_URL", "BUZZ_PRIVATE_KEY"],
        install_hint="Install the Buzz CLI from https://github.com/block/buzz",
        env_enablement_fn=_env_enablement,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="BUZZ_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="BUZZ_ALLOWED_USERS",
        allow_all_env="BUZZ_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🐝",
        pii_safe=True,
        allow_update_command=False,
        platform_hint=(
            "You are communicating through Buzz. Buzz supports markdown, mentions, "
            "and threaded replies. Treat the sender pubkey as the authenticated user "
            "identity. Use the normal Hermes memory, tools, approvals, and sessions."
        ),
    )
