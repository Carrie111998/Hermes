"""SimpleX Chat platform adapter (Hermes plugin).

Connects to a simplex-chat daemon running in WebSocket mode.
Inbound messages arrive via a persistent WebSocket connection.
Outbound messages use the same WebSocket with JSON commands.

This adapter ships as a Hermes platform plugin under
``plugins/platforms/simplex/``. The Hermes plugin loader scans the
directory at startup, calls ``register(ctx)``, and the platform
becomes available to ``gateway/run.py`` and ``tools/send_message_tool``
through the registry — no edits to core files are required.

SimpleX chat daemon setup:
    simplex-chat -p 5225          # start daemon on port 5225
    # or via Docker:
    # docker run -p 5225:5225 simplexchat/simplex-chat-cli -p 5225

Required environment variables:
    SIMPLEX_WS_URL             WebSocket URL of the daemon
                               (default: ws://127.0.0.1:5225)

Optional environment variables:
    SIMPLEX_ALLOWED_USERS      Comma-separated numeric contactId allowlist
                               (stable across renames; visible via
                               `/contacts` in the CLI). Display names are
                               deliberately not authorization identities.
    SIMPLEX_ALLOW_ALL_USERS    Set 'true' to allow all contacts
    SIMPLEX_AUTO_ACCEPT        Set 'false' to disable contact-request auto-accept
                               (default: 'true')
    SIMPLEX_FILES_FOLDER       Absolute path passed to simplex-chat via
                               --files-folder. Required for reliable inbound
                               attachment paths and same-filesystem XFTP moves.
    SIMPLEX_GROUP_ALLOWED      Comma-separated group IDs to monitor, or '*'
                               for any group. Omit to disable groups entirely.
    SIMPLEX_HOME_CHANNEL       Default contact/group ID for cron delivery
    SIMPLEX_HOME_CHANNEL_NAME  Human label for the home channel
    HERMES_SIMPLEX_TEXT_BATCH_DELAY
                               Quiet-period seconds (default: 0.8) used to
                               concatenate rapid-fire inbound text messages
                               into a single MessageEvent — same pattern as
                               Telegram's text batching.

Optional ``config.yaml`` settings (``platforms.simplex.extra``):
    files_folder               Absolute path of the daemon's --files-folder;
                               resolves relative received-attachment paths.

The ``websockets`` Python package is imported lazily — the plugin is
discoverable and ``hermes setup`` can describe it even when websockets is
not installed. ``check_requirements()`` returns False until the package
is present, so the gateway will not attempt to instantiate the adapter.
"""

import asyncio
import base64
import copy
import json
import logging
import os
import random
import re
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Lazy import: BasePlatformAdapter and friends live in the main repo.
# Imported at module top because they're stdlib-only inside Hermes — no
# external dependency that would block the plugin from loading.
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# The protocol's encoded JSON envelope must fit below roughly 15.6 KiB.
# This limit is measured by ``message_len_fn`` in serialized UTF-8 bytes, not
# Python code points, and leaves room for the command/chat JSON wrapper.
MAX_MESSAGE_LENGTH = 12000
WS_RETRY_DELAY_INITIAL = 2.0
WS_RETRY_DELAY_MAX = 60.0
HEALTH_CHECK_INTERVAL = 30.0
HEALTH_CHECK_STALE_THRESHOLD = 300.0

# Correlation ID prefix for requests we send so we can ignore our own echoes.
_CORR_PREFIX = "hermes-"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_comma_list(value: str) -> List[str]:
    """Split a comma-separated string into a stripped list."""
    return [v.strip() for v in value.split(",") if v.strip()]


def _redact_id(contact_id: str) -> str:
    """Redact a contact/group ID for logging."""
    if not contact_id:
        return "<none>"
    s = str(contact_id)
    if len(s) <= 4:
        return s
    return s[:2] + "**" + s[-2:]


def _guess_extension(data: bytes) -> str:
    """Guess file extension from magic bytes."""
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:2] == b"\xff\xd8":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"%PDF":
        return ".pdf"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return ".mp4"
    if data[:4] == b"OggS":
        return ".ogg"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return ".mp3"
    return ".bin"


def _is_image_ext(ext: str) -> bool:
    return ext.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _is_audio_ext(ext: str) -> bool:
    return ext.lower() in {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".opus"}


def _sanitize_filename(name: str) -> str:
    """Return a safe local path fragment for a peer-supplied file name."""
    basename = os.path.basename(str(name or "")).strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", basename)
    return cleaned[:120] or "file"


def _simplex_payload_len(text: str) -> int:
    """Measure a string as it appears inside the ensure_ascii=False JSON body."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _delivered_source_prefix(content: str, chunks: List[str]) -> str:
    """Map formatted overflow chunks back to a monotonic source prefix."""
    cursor = 0
    for index, formatted in enumerate(chunks):
        visible = re.sub(r" \(\d+/\d+\)$", "", formatted)
        variants = {visible}
        if visible.endswith("\n```"):
            variants.add(visible[:-4])
        if visible.startswith("```") and "\n" in visible:
            without_open = visible.split("\n", 1)[1]
            variants.add(without_open)
            if without_open.endswith("\n```"):
                variants.add(without_open[:-4])

        if index:
            while cursor < len(content) and content[cursor].isspace():
                cursor += 1
        remaining = content[cursor:]
        matches = [candidate for candidate in variants if remaining.startswith(candidate)]
        if not matches:
            return ""
        cursor += len(max(matches, key=len))
    return content[:cursor]


def _response_type(resp: Optional[dict]) -> str:
    return str((resp or {}).get("type") or "") if isinstance(resp, dict) else ""


def _response_error(resp: Optional[dict]) -> Optional[str]:
    """Return a bounded diagnostic string for a daemon error response."""
    if not isinstance(resp, dict):
        return "SimpleX daemon did not answer"
    resp_type = _response_type(resp)
    if resp_type in {"localCommandOutcomeUnknown", "localCommandNotSubmitted"}:
        return str(resp.get("error") or "SimpleX command outcome is unknown")[:1000]
    if resp_type not in {"chatCmdError", "chatError", "chatErrors"}:
        return None
    detail = resp.get("chatError") or resp.get("chatErrors") or resp
    try:
        rendered = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(detail)
    return f"{resp_type}: {rendered[:1000]}"


def _response_item_ids(resp: Optional[dict]) -> List[str]:
    """Extract stable daemon chat-item IDs from a command response."""
    if not isinstance(resp, dict):
        return []
    wrappers: List[dict] = []
    if isinstance(resp.get("chatItems"), list):
        wrappers.extend(item for item in resp["chatItems"] if isinstance(item, dict))
    if isinstance(resp.get("chatItem"), dict):
        wrappers.append(resp["chatItem"])
    ids: List[str] = []
    for wrapper in wrappers:
        inner = wrapper.get("chatItem", {}) if isinstance(wrapper, dict) else {}
        meta = inner.get("meta", {}) if isinstance(inner, dict) else {}
        item_id = meta.get("itemId") if isinstance(meta, dict) else None
        if item_id is not None:
            ids.append(str(item_id))
    return ids


# ---------------------------------------------------------------------------
# SimpleX Adapter
# ---------------------------------------------------------------------------

class SimplexAdapter(BasePlatformAdapter):
    """SimpleX Chat adapter using the simplex-chat daemon WebSocket API.

    Instantiated by the ``adapter_factory`` passed to
    ``ctx.register_platform()`` in :func:`register`.
    """

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    splits_long_messages = True
    REQUIRES_EDIT_FINALIZE = True

    _EA_HEADER = "⚠️ Dangerous command requires approval\n"
    _EA_CMD_BUDGET = 2000

    @property
    def message_len_fn(self):
        """SimpleX constrains encoded command bytes, not Unicode code points."""
        return _simplex_payload_len

    def __init__(self, config: PlatformConfig, **kwargs):
        platform = Platform("simplex")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}
        self.ws_url = extra.get("ws_url", "ws://127.0.0.1:5225").rstrip("/")

        # Contact-request auto-accept (on by default — matches the way most
        # bot deployments expect to behave). Read from env first, then fall
        # back to the value seeded by ``_env_enablement``.
        env_auto = os.getenv("SIMPLEX_AUTO_ACCEPT")
        if env_auto is not None:
            self.auto_accept = env_auto.strip().lower() not in {"0", "false", "no", ""}
        else:
            self.auto_accept = bool(extra.get("auto_accept", True))

        # The daemon reports received paths relative to --files-folder and
        # exposes only a setter, not a query API. Mirror the non-secret path in
        # config so downstream media consumers always receive openable paths.
        files_folder = os.getenv("SIMPLEX_FILES_FOLDER", "").strip() or str(
            extra.get("files_folder", "") or ""
        ).strip()
        self.files_folder = (
            os.path.abspath(os.path.expanduser(files_folder))
            if files_folder
            else ""
        )
        self._file_transfer_timeout = max(
            1.0, float(extra.get("file_transfer_timeout", 300.0))
        )
        self.retain_received_files = bool(extra.get("retain_received_files", False))
        self._media_cleanup_timeout = max(
            60.0, float(extra.get("media_cleanup_timeout", 3600.0))
        )

        allow_entries = _parse_comma_list(os.getenv("SIMPLEX_ALLOWED_USERS", ""))
        ignored_allow_entries = [
            entry for entry in allow_entries if entry != "*" and not entry.isdigit()
        ]
        if ignored_allow_entries:
            logger.warning(
                "SimpleX: %d non-numeric SIMPLEX_ALLOWED_USERS entries do not "
                "authorize DMs; display names are ignored. Keep an entry only "
                "if it is an exact group memberId, otherwise migrate to a "
                "stable contactId from /contacts",
                len(ignored_allow_entries),
            )

        # Group allowlist. Without ``SIMPLEX_GROUP_ALLOWED``, group messages
        # are ignored entirely (safer default — a bot in a group otherwise
        # processes every member's traffic). Use ``*`` to accept any group.
        group_allowed_str = os.getenv("SIMPLEX_GROUP_ALLOWED", "") or extra.get(
            "group_allowed", ""
        )
        self.group_allow_from = set(_parse_comma_list(group_allowed_str))

        # Running state
        self._ws = None  # websockets connection
        self._ws_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_ws_activity = 0.0
        self._ws_ready = asyncio.Event()
        self._connect_timeout = float(extra.get("connect_timeout", 10.0))

        # Track sent correlation IDs to filter echoes
        self._pending_corr_ids: set = set()
        self._max_pending_corr = 200

        # File transfers awaiting rcvFileComplete (keyed by fileId). Populated
        # when a newChatItems event carries an unfinished rcvFileTransfer,
        # consumed when the file finishes downloading.
        self._pending_file_transfers: Dict[int, dict] = {}
        self._file_transfer_tasks: Dict[int, asyncio.Task] = {}
        self._file_receive_targets: Dict[int, str] = {}
        self._terminal_file_transfers: Dict[int, dict] = {}
        self._owned_media_cleanup_tasks: Dict[str, asyncio.Task] = {}
        self._outbound_temp_by_item: Dict[str, str] = {}

        # Correlation tracking for ``_send_command``. Separate from
        # ``_pending_corr_ids`` (which is the upstream cosmetic echo filter)
        # because we actually await responses to commands we send.
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._corr_counter = 0
        self._command_tasks: set[asyncio.Task] = set()
        self._dispatch_tasks: set[asyncio.Task] = set()

        # Bounded, non-secret runtime diagnostics. The details are also logged;
        # these counters make health checks useful without scraping prose.
        self._diagnostics: Dict[str, int] = {
            "command_errors": 0,
            "async_errors": 0,
            "reconnects": 0,
            "send_failures": 0,
            "file_rejections": 0,
            "file_failures": 0,
            "file_timeouts": 0,
            "late_file_completions": 0,
        }

        # Direct-message reaction approvals. State is intentionally ephemeral:
        # after restart old reactions are ignored and the typed /approve lane
        # remains authoritative.
        self._approval_prompts_by_item: Dict[str, dict] = {}
        self._approval_prompt_by_session: Dict[str, str] = {}
        self._approval_typed_only_until: Dict[str, float] = {}

        # Text message batching — concatenate rapid-fire messages into one
        # event before dispatching, mirroring Telegram's batching.
        self._text_batch_delay = float(
            os.getenv("HERMES_SIMPLEX_TEXT_BATCH_DELAY", "0.8")
        )
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}

        logger.info(
            "SimpleX adapter initialized: url=%s auto_accept=%s groups=%s",
            self.ws_url,
            self.auto_accept,
            "enabled" if self.group_allow_from else "disabled",
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Start the listener and return only after its real socket is ready."""
        try:
            import websockets  # noqa: F401
        except ImportError:
            logger.error(
                "SimpleX: 'websockets' package not installed. "
                "Run: pip install websockets"
            )
            return False

        if not self.ws_url:
            logger.error("SimpleX: SIMPLEX_WS_URL is required")
            return False

        if self._running and self._ws is not None and self._ws_ready.is_set():
            return True

        self._running = True
        self._ws_ready.clear()
        self._ws_task = asyncio.create_task(self._ws_listener())

        try:
            await asyncio.wait_for(
                self._ws_ready.wait(), timeout=max(self._connect_timeout, 0.1)
            )
        except asyncio.TimeoutError:
            logger.error("SimpleX: listener did not become ready before timeout")
            await self.disconnect()
            return False

        if self._ws is None:
            await self.disconnect()
            return False

        self._health_task = asyncio.create_task(self._health_monitor())
        if hasattr(self, "_mark_connected"):
            self._mark_connected()
        logger.info("SimpleX: connected to %s", self.ws_url)
        # Plugin-registered native handlers (ctx.register_platform_handler).
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        """Stop WebSocket listener and clean up."""
        self._running = False

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Cancel pending text-batch flush timers
        for task in list(self._pending_text_batch_tasks.values()):
            if not task.done():
                task.cancel()
        self._pending_text_batch_tasks.clear()
        self._pending_text_batches.clear()

        # Cancel pending command futures
        for fut in self._pending_responses.values():
            if not fut.done():
                fut.set_exception(ConnectionError("SimpleX adapter disconnected"))
        self._pending_responses.clear()

        for task in list(self._command_tasks):
            if not task.done():
                task.cancel()
        if self._command_tasks:
            await asyncio.gather(*self._command_tasks, return_exceptions=True)
        self._command_tasks.clear()

        for task in list(self._dispatch_tasks):
            if not task.done():
                task.cancel()
        if self._dispatch_tasks:
            await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
        self._dispatch_tasks.clear()

        for task in list(self._file_transfer_tasks.values()):
            if not task.done():
                task.cancel()
        if self._file_transfer_tasks:
            await asyncio.gather(
                *self._file_transfer_tasks.values(), return_exceptions=True
            )
        self._file_transfer_tasks.clear()
        self._pending_file_transfers.clear()
        self._file_receive_targets.clear()
        self._terminal_file_transfers.clear()
        for path in list(self._owned_media_cleanup_tasks):
            self._cleanup_owned_media_path(path)
        for task in list(self._owned_media_cleanup_tasks.values()):
            if not task.done():
                task.cancel()
        self._owned_media_cleanup_tasks.clear()
        self._outbound_temp_by_item.clear()
        self._ws_ready.clear()
        self._approval_prompts_by_item.clear()
        self._approval_prompt_by_session.clear()
        self._approval_typed_only_until.clear()

        if hasattr(self, "_mark_disconnected"):
            self._mark_disconnected()
        logger.info("SimpleX: disconnected")

    # ------------------------------------------------------------------
    # WebSocket listener
    # ------------------------------------------------------------------

    async def _ws_listener(self) -> None:
        """Maintain a persistent WebSocket connection to the daemon."""
        import websockets as _wsclient
        from websockets.exceptions import ConnectionClosed

        backoff = WS_RETRY_DELAY_INITIAL

        while self._running:
            try:
                logger.debug("SimpleX WS: connecting to %s", self.ws_url)
                async with _wsclient.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                ) as ws:
                    self._ws = ws
                    self._ws_ready.set()
                    backoff = WS_RETRY_DELAY_INITIAL
                    self._last_ws_activity = time.time()
                    if self._diagnostics["reconnects"]:
                        logger.info("SimpleX WS: reconnected")
                    else:
                        logger.info("SimpleX WS: connected")
                    if hasattr(self, "_mark_connected"):
                        self._mark_connected()

                    async for raw in ws:
                        if not self._running:
                            break
                        self._last_ws_activity = time.time()
                        try:
                            msg = json.loads(raw)
                            await self._handle_event(msg)
                        except json.JSONDecodeError:
                            logger.debug("SimpleX WS: invalid JSON: %.100s", raw)
                        except Exception:
                            logger.exception("SimpleX WS: error handling event")

            except asyncio.CancelledError:
                break
            except ConnectionClosed as e:
                if self._running:
                    logger.warning(
                        "SimpleX WS: connection closed: %s (reconnecting in %.0fs)",
                        e, backoff,
                    )
            except Exception as e:
                if self._running:
                    logger.warning(
                        "SimpleX WS: unexpected error: %s (reconnecting in %.0fs)",
                        e, backoff,
                    )
            finally:
                if self._ws is not None:
                    self._ws = None
                self._ws_ready.clear()
                for corr_id, fut in list(self._pending_responses.items()):
                    if not fut.done():
                        fut.set_exception(
                            ConnectionError("SimpleX WebSocket disconnected")
                        )
                    self._pending_responses.pop(corr_id, None)
                    self._pending_corr_ids.discard(corr_id)
                # Do not call ``_mark_disconnected`` here: the base helper
                # sets ``_running = False`` and would terminate this listener's
                # own reconnect loop after the first socket drop. Keep the
                # adapter alive while publishing transient disconnected state;
                # explicit ``disconnect()`` owns the terminal state change.
                if self._running and hasattr(self, "_write_runtime_status_safe"):
                    self._write_runtime_status_safe(
                        "disconnected",
                        platform_state="disconnected",
                        error_code=None,
                        error_message=None,
                    )

            if self._running:
                self._diagnostics["reconnects"] += 1
                jitter = backoff * 0.2 * random.random()
                await asyncio.sleep(backoff + jitter)
                backoff = min(backoff * 2, WS_RETRY_DELAY_MAX)

    # ------------------------------------------------------------------
    # Health monitor
    # ------------------------------------------------------------------

    async def _health_monitor(self) -> None:
        """Observe WebSocket idleness without reconnecting healthy quiet links.

        simplex-chat can legitimately stay application-silent for long periods
        when no messages arrive. The websockets client already sends protocol
        pings (see _ws_listener ping_interval/ping_timeout), so treating lack of
        chat events as a stale connection causes needless reconnect churn.
        """
        while self._running:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if not self._running:
                break
            elapsed = time.time() - self._last_ws_activity
            if elapsed > HEALTH_CHECK_STALE_THRESHOLD:
                logger.debug("SimpleX: WS application-idle for %.0fs", elapsed)

    # ------------------------------------------------------------------
    # Inbound event handling
    # ------------------------------------------------------------------

    async def _handle_event(self, event: dict) -> None:
        """Dispatch a daemon event to the appropriate handler."""
        # simplex-chat WebSocket messages are usually shaped as:
        #   {"corrId": "...", "resp": {"type": "newChatItems", ...}}
        # Older/examples may put the response fields at top-level. Normalize
        # both forms before dispatching, otherwise inbound chatItems are lost.
        resp = event.get("resp") if isinstance(event.get("resp"), dict) else event
        corr_id = event.get("corrId")

        # Handle correlated responses (replies to our own commands)
        if corr_id and corr_id in self._pending_responses:
            fut = self._pending_responses[corr_id]
            self._pending_corr_ids.discard(corr_id)
            if not fut.done():
                fut.set_result(resp)
            return

        # Cosmetic echo filter: prefixed corrIds are ours but didn't make it
        # into _pending_responses (e.g. fire-and-forget).
        if corr_id and isinstance(corr_id, str) and corr_id.startswith(_CORR_PREFIX):
            self._pending_corr_ids.discard(corr_id)
            error = _response_error(resp)
            if error:
                self._diagnostics["command_errors"] += 1
                logger.warning("SimpleX: unawaited command failed: %s", error)
            return

        resp_type = resp.get("type") or event.get("type", "")

        # Auto-accept contact requests
        if resp_type == "receivedContactRequest" and self.auto_accept:
            contact_req = resp.get("contactRequest", {}) or {}
            contact_req_id = contact_req.get("contactRequestId")
            if contact_req_id is not None:
                logger.info(
                    "SimpleX: auto-accepting contact request %s",
                    _redact_id(str(contact_req_id)),
                )
                self._spawn_command_task(
                    self._accept_contact_request(str(contact_req_id))
                )
            return

        # Early file-descriptor ready: simplex fires this before newChatItems
        # for some file types (especially large files and voice messages
        # transferred via XFTP). Send /freceive immediately so the download
        # starts; the chat item arrives in a subsequent newChatItems event.
        if resp_type == "rcvFileDescrReady":
            rcv_file = resp.get("rcvFileTransfer", {}) or {}
            file_id = rcv_file.get("fileId") if isinstance(rcv_file, dict) else None
            if file_id is not None:
                file_id = int(file_id)
                if file_id in self._terminal_file_transfers:
                    logger.debug(
                        "SimpleX: ignoring descriptor for terminal fileId=%s",
                        file_id,
                    )
                    return
                wrapper = self._normalize_chat_item_wrapper(resp.get("chatItem", {}))
                if not wrapper:
                    wrapper = self._pending_file_transfers.get(file_id, {})
                if not wrapper or not self._file_sender_is_authorized(wrapper):
                    self._diagnostics["file_rejections"] += 1
                    logger.warning(
                        "SimpleX: refusing file %s before sender authorization",
                        file_id,
                    )
                    return
                self._track_pending_file(file_id, wrapper)
                inner = wrapper.get("chatItem", {}) if wrapper else {}
                file_info = inner.get("file", {}) if isinstance(inner, dict) else {}
                file_name = (
                    file_info.get("fileName", "")
                    if isinstance(file_info, dict)
                    else ""
                ) or rcv_file.get("fileName", "")
                target: Optional[str] = None
                if self.files_folder:
                    target = os.path.join(
                        self.files_folder,
                        f"simplex-rcv-{uuid.uuid4().hex}-{_sanitize_filename(file_name)}",
                    )
                while target and os.path.exists(target):
                    target = os.path.join(
                        self.files_folder,
                        f"simplex-rcv-{uuid.uuid4().hex}-{_sanitize_filename(file_name)}",
                    )
                if target:
                    self._file_receive_targets[file_id] = target
                    if not self.retain_received_files:
                        self._schedule_owned_media_cleanup(target)
                else:
                    logger.warning(
                        "SimpleX: SIMPLEX_FILES_FOLDER is not configured; "
                        "receiving file %s to the daemon-managed folder without "
                        "claiming cleanup ownership",
                        file_id,
                    )
                logger.debug(
                    "SimpleX: rcvFileDescrReady for fileId=%s — accepting transfer",
                    file_id,
                )
                self._spawn_command_task(self._receive_file(int(file_id), target))
            return

        # New messages — simplex-chat sends "newChatItems" with an array
        if resp_type == "newChatItems":
            chat_items = resp.get("chatItems", []) or []
            if not isinstance(chat_items, list):
                chat_items = [chat_items]
            for item in chat_items:
                try:
                    await self._handle_chat_item(self._normalize_chat_item_wrapper(item))
                except Exception:
                    logger.exception("SimpleX: error processing chat item")
            return

        # Singular variant — some daemon versions emit this. The AChatItem is
        # usually nested one level down ({"type": "newChatItem", "chatItem":
        # {"chatInfo": ..., "chatItem": ...}}); _handle_chat_item reads
        # chatInfo/chatItem at the top level, so unwrap before dispatching or
        # the message is silently dropped.
        if resp_type == "newChatItem":
            try:
                await self._handle_chat_item(self._normalize_chat_item_wrapper(resp))
            except Exception:
                logger.exception("SimpleX: error processing chat item")
            return

        if resp_type == "chatItemUpdated":
            try:
                await self._handle_chat_item(resp.get("chatItem", {}), is_edit=True)
            except Exception:
                logger.exception("SimpleX: error processing chat item update")
            return

        if resp_type == "chatItemReaction":
            self._spawn_dispatch_task(self._handle_reaction_event(resp))
            return

        if resp_type in {
            "sndFileComplete",
            "sndFileCompleteXFTP",
            "sndFileError",
            "sndFileWarning",
        }:
            wrapper = self._normalize_chat_item_wrapper(
                resp.get("chatItem") or resp.get("chatItem_") or {}
            )
            item_id = self._item_id_from_wrapper(wrapper)
            if item_id is not None:
                temp_path = self._outbound_temp_by_item.pop(str(item_id), None)
                if temp_path:
                    self._cleanup_owned_media_path(temp_path)
            return

        if resp_type in {"rcvFileSndCancelled", "rcvFileError"}:
            wrapper = self._normalize_chat_item_wrapper(
                resp.get("chatItem") or resp.get("chatItem_") or {}
            )
            rcv_file = resp.get("rcvFileTransfer", {}) or {}
            raw_file_id = (
                rcv_file.get("fileId") if isinstance(rcv_file, dict) else None
            )
            file_id = self._file_id_from_wrapper(wrapper)
            if file_id is None and raw_file_id is not None:
                try:
                    file_id = int(raw_file_id)
                except (TypeError, ValueError):
                    file_id = None
            if file_id is not None:
                if wrapper and file_id not in self._pending_file_transfers:
                    self._pending_file_transfers[file_id] = wrapper
                await self._fail_file_transfer(file_id, resp_type)
            return

        # File transfer completion — deliver any deferred chat item
        if resp_type == "rcvFileComplete":
            chat_item = self._normalize_chat_item_wrapper(
                resp.get("chatItem", {}) or {}
            )
            chat_item_data = chat_item.get("chatItem", {}) or {}
            file_info = chat_item_data.get("file", {}) or {}
            file_id = self._file_id_from_wrapper(chat_item)
            if file_id is not None:
                if file_id in self._terminal_file_transfers:
                    self._diagnostics["late_file_completions"] += 1
                    late_source = file_info.get("fileSource", {}) or {}
                    late_path = (
                        late_source.get("filePath")
                        if isinstance(late_source, dict)
                        else None
                    )
                    target = self._terminal_file_target(file_id)
                    terminal_reason = self._terminal_file_transfers[file_id].get(
                        "reason"
                    )
                    if late_path and target and terminal_reason != "completed":
                        resolved = self._resolve_file_path(late_path)
                        if os.path.abspath(resolved) == os.path.abspath(target):
                            self._cleanup_owned_media_path(target)
                    logger.info(
                        "SimpleX: ignored late/duplicate completion for fileId=%s",
                        file_id,
                    )
                    return
                pending = self._pending_file_transfers.get(file_id) or chat_item
                if not self._file_sender_is_authorized(pending):
                    self._diagnostics["file_rejections"] += 1
                    return
                file_source = file_info.get("fileSource", {}) or {}
                file_path = (
                    file_source.get("filePath")
                    if isinstance(file_source, dict)
                    else None
                )
                if file_path:
                    self._pending_file_transfers.pop(file_id, None)
                    self._cancel_file_timeout(file_id)
                    file_path = self._resolve_file_path(file_path)
                    if not os.path.isabs(file_path):
                        self._pending_file_transfers[file_id] = pending
                        await self._fail_file_transfer(
                            file_id,
                            "SIMPLEX_FILES_FOLDER is required to resolve the "
                            "daemon's relative attachment path",
                        )
                        return
                    pending_item_data = pending.get("chatItem", {}) or {}
                    pending_file = pending_item_data.setdefault("file", {})
                    pending_file["fileSource"] = {"filePath": file_path}
                    pending_file["fileStatus"] = {"type": "rcvComplete"}
                    pending["chatItem"] = pending_item_data
                    try:
                        await self._handle_chat_item(pending)
                    except Exception:
                        logger.exception(
                            "SimpleX: error processing deferred file message"
                        )
                elif pending:
                    # Some daemon orderings announce completion before the
                    # AChatItem acquires fileSource.filePath.  Keep the
                    # authorized wrapper pending until the file-bearing
                    # newChatItems event arrives (or the bounded timeout emits
                    # the caption fallback); never terminalize pathless data.
                    self._track_pending_file(file_id, pending)
            return

        if resp_type in {"chatError", "chatErrors", "messageError"}:
            self._diagnostics["async_errors"] += 1
            logger.warning("SimpleX: asynchronous daemon error: %s", _response_error(resp) or resp_type)
            return

        if resp_type:
            logger.debug("SimpleX: unhandled event type: %s", resp_type)

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
        normalized = SimplexAdapter._normalize_chat_item_wrapper(wrapper)
        inner = normalized.get("chatItem", {}) if normalized else {}
        file_info = inner.get("file", {}) if isinstance(inner, dict) else {}
        raw_id = file_info.get("fileId") if isinstance(file_info, dict) else None
        try:
            return int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _item_id_from_wrapper(wrapper: dict) -> Optional[str]:
        normalized = SimplexAdapter._normalize_chat_item_wrapper(wrapper)
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
        except OSError:
            logger.debug(
                "SimpleX: failed to remove owned temporary media %s",
                os.path.basename(normalized),
                exc_info=True,
            )

    async def _expire_owned_media_path(self, path: str) -> None:
        try:
            await asyncio.sleep(self._media_cleanup_timeout)
            self._cleanup_owned_media_path(path)
        except asyncio.CancelledError:
            return

    def _schedule_owned_media_cleanup(self, path: str) -> None:
        """Install a TTL backstop for an adapter-created media path."""
        normalized = os.path.abspath(path)
        existing = self._owned_media_cleanup_tasks.get(normalized)
        if existing and not existing.done():
            return
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

    async def _handle_chat_item(self, chat_item: dict, is_edit: bool = False) -> None:
        """Process one received item or correlated item update."""
        # Normalizing here as well keeps every caller (singular event, batch
        # array, deferred rcvFileComplete replay) safe — the helper is
        # idempotent on already-normalized wrappers.
        chat_item = self._normalize_chat_item_wrapper(chat_item)
        chat_info = chat_item.get("chatInfo", {}) or {}
        chat_item_data = chat_item.get("chatItem", {}) or {}

        chat_type = chat_info.get("type", "")

        meta = chat_item_data.get("meta", {}) or {}
        content = chat_item_data.get("content", {}) or {}
        msg_content = content.get("msgContent", {}) or {}

        # Filter out our own messages
        item_direction = chat_item_data.get("chatDir", {}) or {}
        direction_type = (
            item_direction.get("type", "") if isinstance(item_direction, dict) else ""
        )
        if direction_type in ("directSnd", "groupSnd"):
            return

        # Only process received messages
        content_type = content.get("type", "") if isinstance(content, dict) else ""
        if content_type != "rcvMsgContent":
            return

        # Text content
        text = ""
        msg_type_str = (
            msg_content.get("type", "") if isinstance(msg_content, dict) else ""
        )
        if msg_type_str in ("text", "file", "image", "voice", "link", "video"):
            text = msg_content.get("text", "")

        if not text and msg_type_str not in ("image", "file", "voice"):
            return

        # Sender + chat IDs
        sender_id = ""
        sender_name = ""
        chat_id = ""
        is_group = False

        if chat_type == "direct":
            contact = chat_info.get("contact", {}) or {}
            sender_id = str(contact.get("contactId", ""))
            sender_name = contact.get("localDisplayName", "") or contact.get(
                "profile", {}
            ).get("displayName", "")
            chat_id = sender_id
        elif chat_type == "group":
            group_info = chat_info.get("groupInfo", {}) or {}
            group_id = str(group_info.get("groupId", ""))
            chat_id = f"group:{group_id}"
            is_group = True

            member = item_direction.get("groupMember", {}) or {}
            sender_identity = member.get("memberContactId")
            if sender_identity is None:
                sender_identity = member.get("memberId", "")
            sender_id = str(sender_identity)
            sender_name = member.get("localDisplayName", "") or member.get(
                "memberProfile", {}
            ).get("displayName", "")

            # Group allowlist
            if self.group_allow_from:
                if (
                    "*" not in self.group_allow_from
                    and group_id not in self.group_allow_from
                ):
                    logger.debug(
                        "SimpleX: group %s not in allowlist",
                        _redact_id(group_id),
                    )
                    return
            else:
                logger.debug(
                    "SimpleX: ignoring group message (no SIMPLEX_GROUP_ALLOWED)"
                )
                return
        else:
            logger.debug("SimpleX: unhandled chat type: %s", chat_type)
            return

        if not sender_id:
            logger.debug("SimpleX: ignoring message with no sender")
            return

        # File / image / voice attachment handling. File info is at
        # chatItem.chatItem.file (sibling of meta, content, chatDir).
        media_urls: List[str] = []
        media_types: List[str] = []
        file_info = chat_item_data.get("file")
        owned_media_path: Optional[str] = None
        completed_file_id: Optional[int] = None

        if file_info and isinstance(file_info, dict):
            raw_file_id = file_info.get("fileId")
            try:
                guarded_file_id = (
                    int(raw_file_id) if raw_file_id is not None else None
                )
            except (TypeError, ValueError):
                guarded_file_id = None
            terminal = (
                self._terminal_file_transfers.get(guarded_file_id)
                if guarded_file_id is not None
                else None
            )
            if terminal:
                if is_edit and terminal.get("reason") == "completed":
                    # A caption edit retains the chat-item correlation but is
                    # text-only: the attachment was already consumed and its
                    # cleanup ownership must not be armed a second time.
                    file_info = None
                else:
                    self._diagnostics["late_file_completions"] += 1
                    logger.info(
                        "SimpleX: ignored duplicate completed chat item for fileId=%s",
                        guarded_file_id,
                    )
                    return

        if file_info and isinstance(file_info, dict):
            file_source = file_info.get("fileSource", {}) or {}
            file_path = (
                file_source.get("filePath")
                if isinstance(file_source, dict)
                else None
            )
            file_name = file_info.get("fileName", "")
            file_id = file_info.get("fileId")
            file_status = file_info.get("fileStatus", {}) or {}
            file_status_type = (
                file_status.get("type") if isinstance(file_status, dict) else None
            )
            try:
                normalized_file_id = int(file_id) if file_id is not None else None
            except (TypeError, ValueError):
                normalized_file_id = None
            if file_path:
                file_path = self._resolve_file_path(file_path)

            # XFTP-backed files can arrive before the download completes.
            # Accept exactly once from rcvFileDescrReady; accepting here can
            # race the descriptor and leave the transfer parked indefinitely.
            if file_id is not None and (
                not file_path
                or file_status_type not in (None, "rcvComplete")
            ):
                try:
                    normalized_file_id = int(file_id)
                except (TypeError, ValueError):
                    normalized_file_id = None
                if (
                    normalized_file_id is None
                    or not self._file_sender_is_authorized(chat_item)
                ):
                    self._diagnostics["file_rejections"] += 1
                    logger.warning(
                        "SimpleX: refusing pending file before sender authorization"
                    )
                    if not text:
                        return
                    file_info = None
                else:
                    logger.info(
                        "SimpleX: file %d pending descriptor/completion",
                        normalized_file_id,
                    )
                    self._track_pending_file(normalized_file_id, chat_item)
                    return

            if file_info and file_path:
                if normalized_file_id is not None:
                    self._pending_file_transfers.pop(normalized_file_id, None)
                    self._cancel_file_timeout(normalized_file_id)
                completed_file_id = normalized_file_id
                receive_target = (
                    self._terminal_file_target(normalized_file_id)
                    if normalized_file_id is not None
                    else None
                )
                if (
                    receive_target
                    and os.path.abspath(file_path) == os.path.abspath(receive_target)
                ):
                    owned_media_path = receive_target
                ext = Path(file_name).suffix.lower() or Path(file_path).suffix.lower()
                if not _is_image_ext(ext) and not _is_audio_ext(ext):
                    try:
                        with open(file_path, "rb") as media_file:
                            sniffed = _guess_extension(media_file.read(16))
                        if sniffed != ".bin":
                            ext = sniffed
                    except OSError:
                        logger.warning(
                            "SimpleX: received file path is not readable: %s",
                            os.path.basename(file_path),
                        )
                if _is_image_ext(ext):
                    media_urls.append(file_path)
                    media_types.append(f"image/{ext.lstrip('.')}")
                elif _is_audio_ext(ext):
                    media_urls.append(file_path)
                    media_types.append(f"audio/{ext.lstrip('.')}")
                else:
                    media_urls.append(file_path)
                    media_types.append("application/octet-stream")

        # Source
        chat_name = sender_name
        if is_group:
            group_info = chat_info.get("groupInfo", {}) or {}
            chat_name = group_info.get("localDisplayName", "") or group_info.get(
                "groupProfile", {}
            ).get("displayName", chat_id)

        item_id = meta.get("itemId")
        message_id = str(item_id) if item_id is not None else None

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type="group" if is_group else "dm",
            user_id=sender_id,
            user_name=sender_name or sender_id,
            message_id=message_id,
        )

        # Message type
        msg_type = MessageType.TEXT
        if media_types:
            if any(mt.startswith("audio/") for mt in media_types):
                msg_type = MessageType.VOICE
            elif any(mt.startswith("image/") for mt in media_types):
                msg_type = MessageType.PHOTO
            else:
                # Catch-all: non-image/non-audio files (tagged
                # application/octet-stream above) are documents so run.py's
                # document-context injection surfaces the file to the agent.
                msg_type = MessageType.DOCUMENT

        # Timestamp
        ts_str = meta.get("itemTs") or meta.get("createdAt", "")
        try:
            if ts_str:
                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            else:
                timestamp = datetime.now(tz=timezone.utc)
        except (ValueError, AttributeError):
            timestamp = datetime.now(tz=timezone.utc)

        quoted = chat_item_data.get("quotedItem", {}) or {}
        quoted_content = quoted.get("content", {}) if isinstance(quoted, dict) else {}
        quoted_text = (
            quoted_content.get("text", "")
            if isinstance(quoted_content, dict)
            else ""
        )
        quoted_dir = quoted.get("chatDir", {}) if isinstance(quoted, dict) else {}
        quoted_dir_type = (
            quoted_dir.get("type", "") if isinstance(quoted_dir, dict) else ""
        )

        msg_event = MessageEvent(
            source=source,
            text=text or "",
            message_type=msg_type,
            media_urls=media_urls,
            media_types=media_types,
            timestamp=timestamp,
            raw_message=chat_item,
            message_id=message_id,
            reply_to_message_id=(
                str(quoted.get("itemId"))
                if isinstance(quoted, dict) and quoted.get("itemId") is not None
                else None
            ),
            reply_to_text=quoted_text or None,
            reply_to_is_own_message=quoted_dir_type in {"directSnd", "groupSnd"},
            metadata={"is_edit": True} if is_edit else {},
        )
        if owned_media_path and not self.retain_received_files:
            self._schedule_owned_media_cleanup(owned_media_path)
            setattr(
                msg_event,
                "_post_turn_cleanup_callbacks",
                [
                    lambda path=owned_media_path: self._cleanup_owned_media_path(
                        path
                    )
                ],
            )

        logger.debug(
            "SimpleX: message from %s in %s: %s",
            _redact_id(sender_id),
            chat_id[:20],
            (text or "")[:50],
        )

        # Mark only after constructing and dispatching the first completed
        # item.  Subsequent rcvFileComplete/newChatItems duplicates with the
        # same fileId are then ignored without deleting a file the active turn
        # may still be consuming.
        if completed_file_id is not None:
            self._mark_file_terminal(completed_file_id, "completed")

        # Batch consecutive text messages so the agent sees one combined
        # message instead of dropping earlier ones when the user pastes
        # several lines in quick succession.
        if is_edit and self._replace_pending_batch_edit(msg_event):
            return
        if is_edit:
            self._spawn_dispatch_task(self.handle_message(msg_event))
        elif msg_type == MessageType.TEXT and text:
            self._enqueue_text_event(msg_event)
        else:
            self._spawn_dispatch_task(self.handle_message(msg_event))

    # ------------------------------------------------------------------
    # Text message batching
    # ------------------------------------------------------------------

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-and-sender scoped key for text batching.

        Group members share a chat id, so omitting the sender would merge one
        member's words into another member's authorized event.
        """
        return (
            f"{event.source.platform.value}:{event.source.chat_id}:"
            f"{event.source.user_id or ''}"
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer."""
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        event.metadata = dict(event.metadata or {})
        event.metadata.setdefault(
            "simplex_batch_items",
            [{"message_id": event.message_id, "text": event.text or ""}],
        )
        if existing is None:
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = (
                    f"{existing.text}\n{event.text}" if existing.text else event.text
                )
            existing.metadata = dict(existing.metadata or {})
            existing.metadata.setdefault("simplex_batch_items", []).extend(
                event.metadata["simplex_batch_items"]
            )
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )

    def _replace_pending_batch_edit(self, event: MessageEvent) -> bool:
        """Supersede a SimpleX text item still inside the quiet-period batch."""
        key = self._text_batch_key(event)
        pending = self._pending_text_batches.get(key)
        if pending is None or not event.message_id:
            return False
        items = (pending.metadata or {}).get("simplex_batch_items", [])
        if not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("message_id")) != str(event.message_id):
                continue
            item["text"] = event.text or ""
            pending.text = "\n".join(
                str(component.get("text", ""))
                for component in items
                if isinstance(component, dict)
            )
            logger.info(
                "SimpleX: superseded batched message item_id=%s",
                event.message_id,
            )
            return True
        return False

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text."""
        current_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._text_batch_delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[SimpleX] Flushing text batch %s (%d chars)",
                key,
                len(event.text or ""),
            )
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    # ------------------------------------------------------------------
    # Command interface
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Outbound — text
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Channel directory enumeration
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Outbound — media
    # ------------------------------------------------------------------

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

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        """Update a streaming preview and finalize it with complete text."""
        chunks: List[str] = []
        for logical_chunk in self.truncate_message(
            content, MAX_MESSAGE_LENGTH, len_fn=self.message_len_fn
        ):
            chunks.extend(self._split_utf8_payload(logical_chunk))
        first_chunk = chunks[0] if chunks else ""
        updated = {
            "msgContent": {"type": "text", "text": first_chunk},
            "mentions": {},
        }
        live_flag = "" if finalize else " live=on"
        command = (
            f"/_update item {self._chat_ref(chat_id)} {message_id}{live_flag} json "
            f"{json.dumps(updated, ensure_ascii=False)}"
        )
        resp = await self._send_command(command)
        result = self._send_result_from_response(
            resp, expected={"chatItemUpdated", "chatItemNotChanged"}
        )
        if not result.success:
            return result
        result.message_id = str(message_id)

        # Mid-stream edits remain one stable preview bubble. Only the final
        # update emits overflow continuations, preventing each growing token
        # update from duplicating the response.
        if not finalize or len(chunks) <= 1:
            return result

        continuation_ids: List[str] = []
        for chunk in chunks[1:]:
            continuation = await self._send_composed(
                chat_id, {"type": "text", "text": chunk}
            )
            if not continuation.success:
                delivered_chunks = chunks[: 1 + len(continuation_ids)]
                delivered_prefix = _delivered_source_prefix(
                    content, delivered_chunks
                )
                continuation.message_id = (
                    continuation_ids[-1] if continuation_ids else str(message_id)
                )
                continuation.continuation_message_ids = tuple(continuation_ids)
                continuation.error_kind = "partial_delivery"
                continuation.retryable = False
                continuation.raw_response = {
                    "partial_overflow": True,
                    "delivered_chunks": 1 + len(continuation_ids),
                    "total_chunks": len(chunks),
                    "last_message_id": continuation.message_id,
                    "delivered_prefix": delivered_prefix,
                    "continuation_message_ids": tuple(continuation_ids),
                    "daemon_response": continuation.raw_response,
                }
                return continuation
            if continuation.message_id:
                continuation_ids.append(continuation.message_id)

        return SendResult(
            success=True,
            message_id=continuation_ids[-1] if continuation_ids else str(message_id),
            continuation_message_ids=tuple(continuation_ids),
        )

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        resp = await self._send_command(
            f"/_delete item {self._chat_ref(chat_id)} {message_id} broadcast"
        )
        return self._send_result_from_response(
            resp, expected={"chatItemsDeleted"}
        ).success

    async def _set_reaction(
        self,
        chat_id: str,
        message_id: str,
        emoji: str,
        *,
        added: bool,
    ) -> SendResult:
        reaction = json.dumps(
            {"type": "emoji", "emoji": emoji}, ensure_ascii=False
        )
        toggle = "on" if added else "off"
        resp = await self._send_command(
            f"/_reaction {self._chat_ref(chat_id)} {message_id} {toggle} {reaction}",
            timeout=10.0,
        )
        return self._send_result_from_response(
            resp, expected={"chatItemReaction"}
        )

    @staticmethod
    def _approval_timeout() -> float:
        try:
            from tools.approval import _get_approval_timeout

            return max(1.0, float(_get_approval_timeout()))
        except Exception:
            return 300.0

    def _approval_text(
        self,
        command: str,
        description: str,
        *,
        allow_session: bool,
        allow_permanent: bool,
        smart_denied: bool,
        reactions: bool,
    ) -> str:
        prefix = self.typed_command_prefix
        lines = [self._format_exec_approval(command, description, smart_denied)]
        choices = [f"Reply `{prefix}approve` to run once"]
        if not smart_denied and allow_session:
            choices.append(f"`{prefix}approve session` for this session")
        if not smart_denied and allow_permanent:
            choices.append(f"`{prefix}approve always` to persist the pattern")
        choices.append(f"`{prefix}deny` to cancel")
        lines.append(", or ".join(choices) + ".")
        if reactions:
            taps = ["✅ = run once"]
            if not smart_denied and allow_session:
                taps.append("🚀 = allow for this session")
            taps.append("👎 = deny")
            lines.append("Tap a reaction: " + "; ".join(taps) + ".")
        return "\n\n".join(lines)

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[dict] = None,
        allow_permanent: bool = True,
        allow_session: bool = True,
        smart_denied: bool = False,
    ) -> SendResult:
        """Offer unambiguous direct-message reaction approvals with typed fallback."""
        now = time.monotonic()
        self._sweep_approval_prompts(now)
        is_dm = not str(chat_id).startswith("group:")
        existing_id = self._approval_prompt_by_session.get(session_key)
        superseded_prior = bool(existing_id)
        typed_only = self._approval_typed_only_until.get(session_key, 0.0) > now

        if existing_id:
            self._retire_approval_prompt(existing_id)
            self._approval_typed_only_until[session_key] = now + self._approval_timeout()
            typed_only = True

        reaction_lane = is_dm and not typed_only
        text = self._approval_text(
            command,
            description,
            allow_session=allow_session,
            allow_permanent=allow_permanent,
            smart_denied=smart_denied,
            reactions=reaction_lane,
        )
        if superseded_prior:
            text = (
                "The earlier approval prompt was superseded; its reactions no "
                "longer apply. Use the typed choices below.\n\n" + text
            )
        result = await self.send(chat_id, text, metadata=metadata)
        if not result.success or not result.message_id or not reaction_lane:
            if not result.success or not result.message_id:
                self._approval_typed_only_until[session_key] = (
                    now + self._approval_timeout()
                )
            return result

        choices = {"✅": "once", "👎": "deny"}
        seeds = ["👎", "✅"]
        if not smart_denied and allow_session:
            choices["🚀"] = "session"
            seeds.insert(1, "🚀")
        prompt = {
            "session_key": session_key,
            "chat_id": str(chat_id),
            "item_id": str(result.message_id),
            "choices": choices,
            "seeded": [],
            "expires_at": now + self._approval_timeout(),
        }
        self._approval_prompts_by_item[prompt["item_id"]] = prompt
        self._approval_prompt_by_session[session_key] = prompt["item_id"]
        self._spawn_command_task(self._seed_approval_reactions(prompt, seeds))
        self._spawn_command_task(self._expire_approval_prompt(prompt["item_id"]))
        return result

    async def _seed_approval_reactions(self, prompt: dict, seeds: List[str]) -> None:
        for emoji in seeds[:3]:
            if self._approval_prompts_by_item.get(prompt["item_id"]) is not prompt:
                return
            result = await self._set_reaction(
                prompt["chat_id"], prompt["item_id"], emoji, added=True
            )
            if not result.success:
                logger.info(
                    "SimpleX: reaction seed unavailable; typed approval remains active"
                )
                return
            prompt["seeded"].append(emoji)

    async def _expire_approval_prompt(self, item_id: str) -> None:
        prompt = self._approval_prompts_by_item.get(item_id)
        if not prompt:
            return
        await asyncio.sleep(max(0.0, prompt["expires_at"] - time.monotonic()))
        if self._approval_prompts_by_item.get(item_id) is prompt:
            self._retire_approval_prompt(item_id)

    def _sweep_approval_prompts(self, now: Optional[float] = None) -> None:
        current = time.monotonic() if now is None else now
        for item_id, prompt in list(self._approval_prompts_by_item.items()):
            if prompt["expires_at"] <= current:
                self._retire_approval_prompt(item_id)
        for session_key, expiry in list(self._approval_typed_only_until.items()):
            if expiry <= current:
                self._approval_typed_only_until.pop(session_key, None)

    def _retire_approval_prompt(self, item_id: str) -> None:
        prompt = self._approval_prompts_by_item.pop(str(item_id), None)
        if not prompt:
            return
        if self._approval_prompt_by_session.get(prompt["session_key"]) == str(item_id):
            self._approval_prompt_by_session.pop(prompt["session_key"], None)
        if prompt["seeded"]:
            self._spawn_command_task(self._clear_approval_reactions(prompt))

    async def _clear_approval_reactions(self, prompt: dict) -> None:
        for emoji in list(prompt["seeded"]):
            await self._set_reaction(
                prompt["chat_id"], prompt["item_id"], emoji, added=False
            )

    @staticmethod
    def _reaction_context(resp: dict) -> Optional[dict]:
        wrapper = resp.get("reaction", {}) or {}
        if not isinstance(wrapper, dict):
            return None
        chat_info = wrapper.get("chatInfo", {}) or {}
        reaction = wrapper.get("chatReaction", {}) or {}
        if not isinstance(chat_info, dict) or not isinstance(reaction, dict):
            return None
        chat_item = reaction.get("chatItem", {}) or {}
        meta = chat_item.get("meta", {}) if isinstance(chat_item, dict) else {}
        item_id = meta.get("itemId") if isinstance(meta, dict) else None
        msg_reaction = reaction.get("reaction", {}) or {}
        emoji = (
            msg_reaction.get("emoji", "")
            if isinstance(msg_reaction, dict)
            else ""
        ).replace("\ufe0f", "")
        chat_type = chat_info.get("type", "")
        chat_dir = reaction.get("chatDir", {}) or {}
        if chat_type == "direct":
            if not isinstance(chat_dir, dict) or chat_dir.get("type") != "directRcv":
                return None
            contact = chat_info.get("contact", {}) or {}
            chat_id = str(contact.get("contactId", ""))
            user_id = chat_id
            user_name = contact.get("localDisplayName", "")
        elif chat_type == "group":
            if not isinstance(chat_dir, dict) or chat_dir.get("type") != "groupRcv":
                return None
            group = chat_info.get("groupInfo", {}) or {}
            chat_id = f"group:{group.get('groupId', '')}"
            member = chat_dir.get("groupMember", {}) if isinstance(chat_dir, dict) else {}
            member_identity = member.get("memberContactId")
            if member_identity is None:
                member_identity = member.get("memberId", "")
            user_id = str(member_identity)
            user_name = member.get("localDisplayName", "")
        else:
            return None
        return {
            "item_id": str(item_id) if item_id is not None else "",
            "chat_id": chat_id,
            "user_id": user_id,
            "user_name": user_name,
            "emoji": emoji,
            "added": bool(resp.get("added", False)),
            "raw": resp,
        }

    async def _handle_reaction_event(self, resp: dict) -> None:
        ctx = self._reaction_context(resp)
        if not ctx:
            return
        hook = getattr(self, "_reaction_handler", None)
        if hook is not None:
            await hook(
                {
                    "event_name": (
                        "reaction:added" if ctx["added"] else "reaction:removed"
                    ),
                    "platform": "simplex",
                    **ctx,
                }
            )

        if not ctx["added"]:
            return
        prompt = self._approval_prompts_by_item.get(ctx["item_id"])
        if not prompt or prompt["chat_id"] != ctx["chat_id"]:
            return
        if not prompt["chat_id"].startswith("group:") and ctx["user_id"] != prompt["chat_id"]:
            logger.warning("SimpleX: ignored approval reaction from another contact")
            return
        if time.monotonic() >= prompt["expires_at"]:
            self._retire_approval_prompt(prompt["item_id"])
            return
        choice = prompt["choices"].get(ctx["emoji"])
        if not choice:
            return

        try:
            from tools.approval import resolve_gateway_approval

            count = int(resolve_gateway_approval(prompt["session_key"], choice) or 0)
        except Exception:
            logger.exception("SimpleX: failed to resolve reaction approval")
            return
        self._retire_approval_prompt(prompt["item_id"])
        acknowledgement = {
            "once": "Approved — running this once.",
            "session": "Approved for this session.",
            "deny": "Denied — the command will not run.",
        }[choice]
        if count <= 0:
            acknowledgement = "That approval is no longer pending. Nothing ran."
        self._spawn_command_task(self.send(prompt["chat_id"], acknowledgement))

    def get_runtime_diagnostics(self) -> Dict[str, Any]:
        """Return bounded transport state for status/incident tooling."""
        return {
            **self._diagnostics,
            "ready": self._ws is not None and self._ws_ready.is_set(),
            "pending_commands": len(self._pending_responses),
            "pending_files": len(self._pending_file_transfers),
            "pending_approval_prompts": len(self._approval_prompts_by_item),
            "last_activity_age_seconds": (
                max(0.0, time.time() - self._last_ws_activity)
                if self._last_ws_activity
                else None
            ),
        }

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """SimpleX has no typing-indicator API — no-op."""

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return basic chat info."""
        if chat_id.startswith("group:"):
            return {"chat_id": chat_id, "type": "group", "name": chat_id[6:]}
        return {"chat_id": chat_id, "type": "dm", "name": chat_id}


# ---------------------------------------------------------------------------
# Plugin entry-point hooks
# ---------------------------------------------------------------------------

def check_requirements() -> bool:
    """Plugin gate: require SIMPLEX_WS_URL AND the websockets package.

    Returning False keeps the platform out of ``get_connected_platforms()``
    so the gateway never instantiates the adapter when the dependency is
    missing or no daemon URL is configured.
    """
    if not os.getenv("SIMPLEX_WS_URL"):
        return False
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    ws_url = os.getenv("SIMPLEX_WS_URL") or extra.get("ws_url", "")
    return bool(ws_url)


def is_connected(config) -> bool:
    """Check whether SimpleX is configured (env or config.yaml)."""
    extra = getattr(config, "extra", {}) or {}
    ws_url = os.getenv("SIMPLEX_WS_URL") or extra.get("ws_url", "")
    return bool(ws_url)


def _env_enablement() -> Optional[dict]:
    """Seed ``PlatformConfig.extra`` from env vars during gateway config load.

    Called by the platform registry's env-enablement hook BEFORE adapter
    construction, so ``gateway status`` and ``get_connected_platforms()``
    reflect env-only configuration without instantiating the WebSocket
    client. Returns ``None`` when SimpleX isn't minimally configured.

    The special ``home_channel`` key is handled by the core hook — it
    becomes a proper ``HomeChannel`` dataclass on the ``PlatformConfig``
    rather than being merged into ``extra``.
    """
    ws_url = os.getenv("SIMPLEX_WS_URL", "").strip()
    if not ws_url:
        return None
    seed: dict = {"ws_url": ws_url}

    auto_accept = os.getenv("SIMPLEX_AUTO_ACCEPT", "").strip().lower()
    if auto_accept:
        seed["auto_accept"] = auto_accept not in {"0", "false", "no"}

    group_allowed = os.getenv("SIMPLEX_GROUP_ALLOWED", "").strip()
    if group_allowed:
        seed["group_allowed"] = group_allowed

    files_folder = os.getenv("SIMPLEX_FILES_FOLDER", "").strip()
    if files_folder:
        seed["files_folder"] = files_folder

    home = os.getenv("SIMPLEX_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("SIMPLEX_HOME_CHANNEL_NAME", "").strip() or home,
        }
    return seed


async def _standalone_send(
    pconfig,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[List[Any]] = None,
    force_document: bool = False,
) -> Dict[str, Any]:
    """Open an ephemeral WebSocket to the daemon, send, and close.

    Used by ``tools/send_message_tool._send_via_adapter`` when the gateway
    runner is not in this process (e.g. ``hermes cron`` running as a
    separate process from ``hermes gateway``). Without this hook,
    ``deliver=simplex`` cron jobs fail with "No live adapter for platform".

    ``thread_id`` is accepted for signature parity. Text and media are
    reported successful only after a correlated daemon acknowledgement.
    """
    try:
        import websockets as _wsclient
    except ImportError:
        return {"error": "websockets not installed. Run: pip install websockets"}

    extra = getattr(pconfig, "extra", {}) or {}
    ws_url = os.getenv("SIMPLEX_WS_URL") or extra.get(
        "ws_url", "ws://127.0.0.1:5225"
    )
    if not ws_url:
        return {"error": "SimpleX standalone send: SIMPLEX_WS_URL is required"}

    async def _send_confirmed(
        ws,
        command: str,
        corr_id: str,
        *,
        await_file_complete: bool = False,
    ) -> dict:
        await ws.send(json.dumps({"corrId": corr_id, "cmd": command}))
        deferred: List[dict] = []
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
            event = json.loads(raw)
            if event.get("corrId") != corr_id:
                resp = (
                    event.get("resp")
                    if isinstance(event.get("resp"), dict)
                    else event
                )
                if isinstance(resp, dict):
                    deferred.append(resp)
                continue
            resp = event.get("resp") if isinstance(event.get("resp"), dict) else event
            error = _response_error(resp)
            if error:
                raise RuntimeError(error)
            if _response_type(resp) != "newChatItems":
                raise RuntimeError(
                    f"unexpected SimpleX response: {_response_type(resp) or '<missing>'}"
                )
            break

        if not await_file_complete:
            return resp

        ack_ids = set(_response_item_ids(resp))

        def _matches_file_terminal(candidate: dict) -> Optional[bool]:
            candidate_type = _response_type(candidate)
            if candidate_type not in {
                "sndFileComplete",
                "sndFileCompleteXFTP",
                "sndFileError",
                "sndFileWarning",
            }:
                return None
            wrapper = SimplexAdapter._normalize_chat_item_wrapper(
                candidate.get("chatItem") or candidate.get("chatItem_") or {}
            )
            item_id = SimplexAdapter._item_id_from_wrapper(wrapper)
            if ack_ids:
                if item_id is None or item_id not in ack_ids:
                    return None
            return candidate_type in {"sndFileComplete", "sndFileCompleteXFTP"}

        for candidate in deferred:
            terminal = _matches_file_terminal(candidate)
            if terminal is True:
                return resp
            if terminal is False:
                raise RuntimeError("SimpleX standalone file transfer failed")

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=300.0)
            event = json.loads(raw)
            candidate = (
                event.get("resp")
                if isinstance(event.get("resp"), dict)
                else event
            )
            if not isinstance(candidate, dict):
                continue
            terminal = _matches_file_terminal(candidate)
            if terminal is True:
                return resp
            if terminal is False:
                raise RuntimeError("SimpleX standalone file transfer failed")

    cleanup_temp_paths: set[str] = set()
    try:
        chat_ref = SimplexAdapter._chat_ref(chat_id)
        commands: List[tuple[str, Optional[str]]] = []
        if message:
            for chunk in BasePlatformAdapter.truncate_message(
                message, MAX_MESSAGE_LENGTH, len_fn=_simplex_payload_len
            ):
                composed = [{
                    "msgContent": {"type": "text", "text": chunk},
                    "mentions": {},
                }]
                commands.append(
                    (
                        f"/_send {chat_ref} json "
                        f"{json.dumps(composed, ensure_ascii=False)}",
                        None,
                    )
                )

        for media in media_files or []:
            if isinstance(media, (tuple, list)):
                path = str(media[0])
                is_voice = bool(media[1]) if len(media) > 1 else False
            else:
                path = str(media)
                is_voice = False
            if not Path(path).is_file():
                return {"error": f"SimpleX media file not found: {os.path.basename(path)}"}
            ext = Path(path).suffix.lower()
            if is_voice:
                msg_content = {"type": "voice", "text": "", "duration": 0}
                file_path = path
            elif not force_document and _is_image_ext(ext):
                file_path, thumb = SimplexAdapter._prepare_image(path)
                msg_content = {"type": "image", "image": thumb, "text": ""}
            else:
                msg_content = {"type": "file", "text": ""}
                file_path = path
            composed = [{
                "fileSource": {"filePath": file_path},
                "msgContent": msg_content,
                "mentions": {},
            }]
            owned_conversion = (
                file_path
                if os.path.abspath(file_path) != os.path.abspath(path)
                else None
            )
            commands.append(
                (
                    f"/_send {chat_ref} json "
                    f"{json.dumps(composed, ensure_ascii=False)}",
                    owned_conversion,
                )
            )

        item_ids: List[str] = []
        async with _wsclient.connect(ws_url, open_timeout=10, close_timeout=5) as ws:
            for index, (command, owned_conversion) in enumerate(commands):
                corr_id = f"{_CORR_PREFIX}snd-{uuid.uuid4().hex}-{index}"
                try:
                    resp = await _send_confirmed(
                        ws,
                        command,
                        corr_id,
                        await_file_complete=owned_conversion is not None,
                    )
                except asyncio.TimeoutError:
                    # The daemon may still be reading the file after its chat
                    # acknowledgement. Preserve the only conversion on an
                    # ambiguous timeout rather than corrupting delivery.
                    raise
                except RuntimeError:
                    if owned_conversion:
                        cleanup_temp_paths.add(owned_conversion)
                    raise
                if owned_conversion:
                    cleanup_temp_paths.add(owned_conversion)
                item_ids.extend(_response_item_ids(resp))

        return {
            "success": True,
            "platform": "simplex",
            "chat_id": chat_id,
            "message_id": item_ids[-1] if item_ids else None,
        }
    except Exception as e:
        return {"error": f"SimpleX send failed: {e}"}
    finally:
        for temp_path in cleanup_temp_paths:
            try:
                if os.path.isfile(temp_path) or os.path.islink(temp_path):
                    os.remove(temp_path)
            except OSError:
                logger.debug(
                    "SimpleX: failed to remove standalone image conversion %s",
                    os.path.basename(temp_path),
                    exc_info=True,
                )


def interactive_setup() -> None:
    """Minimal stdin wizard for ``hermes setup gateway`` → SimpleX.

    Prompts for the WebSocket URL and the optional allowlist / groups /
    auto-accept / home channel. Writes to ``~/.hermes/.env`` via
    ``hermes_cli.config``.
    """
    print()
    print("SimpleX Chat setup")
    print("------------------")
    print("Requirements:")
    print("  1. simplex-chat daemon running (e.g. `simplex-chat -p 5225`).")
    print("  2. Python package `websockets` installed (`pip install websockets`).")
    print()

    try:
        from hermes_cli.config import get_env_value, save_env_value
    except ImportError:
        print(
            "hermes_cli.config not available; set SIMPLEX_* vars manually in "
            "~/.hermes/.env"
        )
        return

    def _prompt(var: str, prompt: str, *, secret: bool = False) -> None:
        existing = get_env_value(var) if callable(get_env_value) else None
        suffix = " [keep current]" if existing else ""
        try:
            if secret:
                from hermes_cli.secret_prompt import masked_secret_prompt
                value = masked_secret_prompt(f"{prompt}{suffix}: ")
            else:
                value = input(f"{prompt}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if value:
            save_env_value(var, value)

    _prompt("SIMPLEX_WS_URL", "Daemon WebSocket URL (default ws://127.0.0.1:5225)")
    _prompt(
        "SIMPLEX_ALLOWED_USERS",
        "Allowed numeric contactIds (comma-separated; blank=skip)",
    )
    _prompt(
        "SIMPLEX_GROUP_ALLOWED",
        "Allowed group IDs (comma-separated, or '*' for any; blank=disable groups)",
    )
    _prompt(
        "SIMPLEX_AUTO_ACCEPT",
        "Auto-accept incoming contact requests? (true/false, default true)",
    )
    _prompt("SIMPLEX_HOME_CHANNEL", "Home channel contact/group ID (or empty)")
    print(
        "Done. Make sure the simplex-chat daemon is running before starting "
        "the gateway."
    )


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    ctx.register_platform(
        name="simplex",
        label="SimpleX Chat",
        adapter_factory=lambda cfg: SimplexAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["SIMPLEX_WS_URL"],
        install_hint=(
            "pip install websockets   # SimpleX adapter requires the "
            "websockets package"
        ),
        setup_fn=interactive_setup,
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="SIMPLEX_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="SIMPLEX_ALLOWED_USERS",
        allow_all_env="SIMPLEX_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="🔒",
        # SimpleX uses opaque contact IDs only — no phone numbers or email
        # addresses to redact.
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via SimpleX Chat, a private decentralised "
            "messenger. Contacts are identified by opaque internal IDs, "
            "not phone numbers or usernames. SimpleX supports standard "
            "markdown formatting. There is no typing indicator; long "
            "messages are split safely by the adapter. "
            "You can attach native images, voice notes, and arbitrary "
            "files; the adapter handles MEDIA:<path> tags by sending them "
            "as inline voice notes (audio extensions) or documents."
        ),
    )
