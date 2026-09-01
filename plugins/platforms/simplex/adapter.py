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
from plugins.platforms.simplex.approvals import SimplexApprovalMixin
from plugins.platforms.simplex.batching import prepend_cancelled_batch
from plugins.platforms.simplex.config import (
    profile_scoped as _profile_scoped,
    profile_simplex_extra as _profile_simplex_extra,
    scoped_platform_setting as _scoped_platform_setting,
)
from plugins.platforms.simplex.media import SimplexMediaMixin
from plugins.platforms.simplex.messaging import SimplexMessagingMixin
from plugins.platforms.simplex.protocol import (
    response_error as _response_error,
    response_item_ids as _response_item_ids,
    response_type as _response_type,
    simplex_payload_len as _simplex_payload_len,
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


# ---------------------------------------------------------------------------
# SimpleX Adapter
# ---------------------------------------------------------------------------

class SimplexAdapter(
    SimplexMessagingMixin,
    SimplexMediaMixin,
    SimplexApprovalMixin,
    BasePlatformAdapter,
):
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
        env_auto = _scoped_platform_setting(
            "SIMPLEX_AUTO_ACCEPT", extra, "auto_accept"
        )
        auto_value = extra.get("auto_accept", True) if env_auto is None else env_auto
        self.auto_accept = str(auto_value).strip().lower() not in {
            "0",
            "false",
            "no",
            "",
        }

        # The daemon reports received paths relative to --files-folder and
        # exposes only a setter, not a query API. Mirror the non-secret path in
        # config so downstream media consumers always receive openable paths.
        env_files_folder = _scoped_platform_setting(
            "SIMPLEX_FILES_FOLDER", extra, "files_folder"
        )
        files_folder = str(
            env_files_folder or extra.get("files_folder", "")
        ).strip()
        expanded_files_folder = os.path.expanduser(files_folder)
        if expanded_files_folder and not os.path.isabs(expanded_files_folder):
            logger.warning(
                "SimpleX: ignoring relative SIMPLEX_FILES_FOLDER; configure the "
                "exact absolute path passed to simplex-chat --files-folder"
            )
            self.files_folder = ""
        else:
            self.files_folder = expanded_files_folder
        self._file_transfer_timeout = max(
            1.0, float(extra.get("file_transfer_timeout", 300.0))
        )
        self.retain_received_files = bool(extra.get("retain_received_files", False))
        self._media_cleanup_timeout = max(
            60.0, float(extra.get("media_cleanup_timeout", 3600.0))
        )

        env_allowed_users = _scoped_platform_setting(
            "SIMPLEX_ALLOWED_USERS", extra, "allowed_users"
        )
        allow_entries = _parse_comma_list(str(env_allowed_users or ""))
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
        env_group_allowed = _scoped_platform_setting(
            "SIMPLEX_GROUP_ALLOWED", extra, "group_allowed"
        )
        group_allowed_str = env_group_allowed or extra.get("group_allowed", "")
        self.group_allow_from = set(_parse_comma_list(group_allowed_str))

        # Running state
        self._ws = None  # websockets connection
        self._ws_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_ws_activity = 0.0
        self._ws_ready = asyncio.Event()
        self._connect_timeout = float(extra.get("connect_timeout", 10.0))
        self._listener_lock_acquired = False

        # Track sent correlation IDs to filter echoes
        self._pending_corr_ids: set = set()
        self._max_pending_corr = 200

        # File transfers awaiting rcvFileComplete (keyed by fileId). Populated
        # when a newChatItems event carries an unfinished rcvFileTransfer,
        # consumed when the file finishes downloading.
        self._pending_file_transfers: Dict[int, dict] = {}
        self._file_transfer_tasks: Dict[int, asyncio.Task] = {}
        self._file_receive_started: set[int] = set()
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
            "media_cleanup_failures": 0,
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

        try:
            if not self._acquire_platform_lock(
                "simplex-ws", self.ws_url, "SimpleX daemon URL"
            ):
                return False
        except Exception as exc:
            logger.error("SimpleX: could not acquire daemon URL lock: %s", exc)
            return False
        self._listener_lock_acquired = True

        try:
            self._running = True
            self._ws_ready.clear()
            self._ws_task = asyncio.create_task(self._ws_listener())
            await asyncio.wait_for(
                self._ws_ready.wait(), timeout=max(self._connect_timeout, 0.1)
            )
        except asyncio.TimeoutError:
            logger.error("SimpleX: listener did not become ready before timeout")
            await self.disconnect()
            return False
        except asyncio.CancelledError:
            await self.disconnect()
            raise
        except Exception:
            logger.exception("SimpleX: listener startup failed")
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

        # Cancel and drain pending text-batch flush timers before clearing the
        # buffers. A flush cancelled after popping its event re-buffers it;
        # draining first prevents that recovery path from creating ghost state
        # after disconnect has already cleared the dictionaries.
        text_batch_tasks = list(self._pending_text_batch_tasks.values())
        for task in text_batch_tasks:
            if not task.done():
                task.cancel()
        if text_batch_tasks:
            await asyncio.gather(*text_batch_tasks, return_exceptions=True)
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
        self._file_receive_started.clear()
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

        if self._listener_lock_acquired:
            self._release_platform_lock()
            self._listener_lock_acquired = False

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
                if file_id in self._file_receive_started:
                    logger.debug(
                        "SimpleX: ignoring duplicate descriptor for fileId=%s",
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
                self._file_receive_started.add(file_id)
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

            # A daemon-relative path is only meaningful relative to the exact
            # --files-folder configured on simplex-chat. Never pass a path
            # relative to Hermes' unrelated working directory to media tools.
            if file_path and not os.path.isabs(file_path):
                reason = (
                    "SIMPLEX_FILES_FOLDER is required to resolve the daemon's "
                    "relative attachment path"
                )
                if normalized_file_id is not None:
                    self._pending_file_transfers.pop(normalized_file_id, None)
                    self._cancel_file_timeout(normalized_file_id)
                    self._mark_file_terminal(normalized_file_id, reason)
                self._diagnostics["file_failures"] += 1
                logger.warning("SimpleX: %s", reason)
                file_info = None
                file_path = None
                if not text:
                    text = f"[Attachment unavailable: {reason}]"

            # XFTP-backed files can arrive before the download completes.
            # Accept exactly once from rcvFileDescrReady; accepting here can
            # race the descriptor and leave the transfer parked indefinitely.
            if file_info and file_id is not None and (
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
            # Reaching this point for a group proves it passed the explicit
            # SIMPLEX_GROUP_ALLOWED intake gate. Carry that decision across
            # the generic gateway boundary; DM senders remain subject to the
            # separate SIMPLEX_ALLOWED_USERS/pairing policy.
            role_authorized=is_group,
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
            # Descriptor receipt arms a backstop while the transfer is in
            # flight. Re-arm it at completion so a long-running transfer does
            # not shorten the consuming turn's cleanup window.
            self._schedule_owned_media_cleanup(owned_media_path, reset=True)
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
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._restart_text_batch_flush(key, prior_task)
        )

    async def _restart_text_batch_flush(
        self, key: str, prior_task: Optional[asyncio.Task]
    ) -> None:
        """Cancel and drain the prior timer before starting its replacement.

        Draining serializes the old flush's cancellation recovery with the new
        flush, so the replacement cannot pop the newer event before the old
        in-flight event has been restored ahead of it.
        """
        if prior_task is not None:
            if not prior_task.done():
                prior_task.cancel()
            await asyncio.gather(prior_task, return_exceptions=True)
        await self._flush_text_batch(key)

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
            try:
                await self.handle_message(event)
            except asyncio.CancelledError:
                newer = self._pending_text_batches.get(key)
                self._pending_text_batches[key] = prepend_cancelled_batch(
                    event, newer
                )
                raise
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    # ------------------------------------------------------------------
    # Command interface
    # ------------------------------------------------------------------












    # ------------------------------------------------------------------
    # Outbound — text
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Channel directory enumeration
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Outbound — media
    # ------------------------------------------------------------------







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
    if _profile_scoped():
        if not str(_profile_simplex_extra().get("ws_url", "")).strip():
            return False
    elif not os.getenv("SIMPLEX_WS_URL"):
        return False
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    env_ws_url = _scoped_platform_setting("SIMPLEX_WS_URL", extra, "ws_url")
    ws_url = env_ws_url or extra.get("ws_url", "")
    return bool(ws_url)


def is_connected(config) -> bool:
    """Check whether SimpleX is configured (env or config.yaml)."""
    extra = getattr(config, "extra", {}) or {}
    env_ws_url = _scoped_platform_setting("SIMPLEX_WS_URL", extra, "ws_url")
    ws_url = env_ws_url or extra.get("ws_url", "")
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
    if _profile_scoped():
        return None
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
    env_ws_url = _scoped_platform_setting("SIMPLEX_WS_URL", extra, "ws_url")
    ws_url = env_ws_url or extra.get("ws_url", "ws://127.0.0.1:5225")
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
            except OSError as exc:
                logger.debug(
                    "SimpleX: failed to remove standalone image conversion %s (%s)",
                    os.path.basename(temp_path),
                    type(exc).__name__,
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
