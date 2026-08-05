"""
Telegram platform adapter.

Uses python-telegram-bot library for:
- Receiving messages from users/groups
- Sending responses back
- Handling media and commands
"""

import asyncio
import faulthandler
import logging
import os
import html as _html
import re
import threading
import time
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Any

logger = logging.getLogger(__name__)


def _scoped_gate_env(name: str, default: str = "") -> str:
    """Read a TELEGRAM_*/GATEWAY_* authorization gate env var per-profile.

    Under gateway.multiplex_profiles the process env is first-writer-wins
    (the YAML→env bridge in ``_apply_yaml_config``), so a raw ``os.getenv``
    can return ANOTHER profile's allowlist (issue #72348, Telegram mirror).
    Reads the active profile's secret scope when installed; falls back to
    ``os.getenv`` outside multiplex — identical single-profile behavior.
    """
    try:
        from gateway.authz_mixin import _platform_gate_env

        return _platform_gate_env(name, default)
    except Exception:
        return (os.getenv(name) or default).strip()


def _consume_abandoned_task(task: asyncio.Task) -> None:
    """Observe a detached task's terminal exception to avoid noisy loop logs."""
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.debug("Abandoned Telegram init task failed after timeout", exc_info=True)


# Grace period after the wall-clock deadline fires: if the event loop still
# hasn't processed the expiry callback by then, the loop thread itself is
# blocked in a synchronous call — the exact state in which every asyncio-based
# timeout (including this helper's own expiry hand-off) goes silent, so the
# gateway hangs at "attempt 1/8" with no further output (#63309).
_LOOP_BLOCKED_DUMP_GRACE = 5.0


def _dump_loop_blocked_diagnostics(timeout: float, grace: float) -> None:
    """Emit diagnostics from the deadline timer thread when the loop is stuck.

    Runs OFF the event loop, so it works precisely when the loop cannot. The
    faulthandler dump names the frame the loop thread is blocked in — the one
    piece of information #63309-class hangs otherwise never surface.
    """
    logger.warning(
        "[Telegram] init deadline (%.0fs) expired but the event loop has not "
        "processed the expiry after a further %.0fs — the loop thread appears "
        "BLOCKED in a synchronous call, which is why no timeout fires (#63309). "
        "Dumping all thread stacks to stderr to identify the blocking frame.",
        timeout,
        grace,
    )
    try:
        faulthandler.dump_traceback(all_threads=True)
    except Exception:
        logger.debug("faulthandler traceback dump failed", exc_info=True)


async def _await_with_thread_deadline(awaitable, timeout: float, *, on_abandon=None):
    """Await with a wall-clock deadline that does not depend on loop timers.

    ``asyncio.wait_for`` schedules its timeout on the event loop and then waits
    for cancellation to propagate.  PTB/httpcore initialization can sit inside
    cancellation-shielded anyio scopes, so a timed-out initialize() may never
    hand control back to the retry ladder under some supervisors.  This helper
    lets a daemon ``threading.Timer`` wake the loop and, on timeout, abandons
    the shielded task instead of awaiting cancellation completion.

    ``on_abandon`` (optional) is a zero-arg callable returning an awaitable that
    is scheduled as a detached best-effort cleanup when the task is abandoned on
    timeout.  The abandoned initialize() may leave a half-built httpx client /
    connection pool open (it never completed and we do not await its
    cancellation), so the caller uses this to shut that state down and avoid
    leaking a pool per retry attempt.  Cleanup runs detached and its own errors
    are swallowed, so it can never re-block the retry ladder.
    """
    task = asyncio.ensure_future(awaitable)
    loop = asyncio.get_running_loop()
    deadline = loop.create_future()
    # Set the moment the loop actually runs the expiry callback (or the helper
    # exits normally). threading.Event so the watchdog thread can read it
    # without touching asyncio state from off-loop.
    loop_processed_expiry = threading.Event()

    def _mark_expired() -> None:
        loop_processed_expiry.set()
        if not deadline.done():
            deadline.set_result(None)

    def _expire_from_thread() -> None:
        loop.call_soon_threadsafe(_mark_expired)

    def _watchdog_check() -> None:
        # The deadline fired _LOOP_BLOCKED_DUMP_GRACE ago but the loop never
        # ran _mark_expired: the loop thread is stuck in a synchronous call.
        # Diagnose from this thread — the loop can't.
        if not loop_processed_expiry.is_set():
            _dump_loop_blocked_diagnostics(timeout, _LOOP_BLOCKED_DUMP_GRACE)

    timer = threading.Timer(max(timeout, 0.0), _expire_from_thread)
    timer.daemon = True
    timer.start()
    watchdog = threading.Timer(
        max(timeout, 0.0) + _LOOP_BLOCKED_DUMP_GRACE, _watchdog_check
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        done, _ = await asyncio.wait(
            {task, deadline},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            if not deadline.done():
                deadline.cancel()
            return await task

        task.cancel()
        task.add_done_callback(_consume_abandoned_task)
        if on_abandon is not None:
            # Detached best-effort cleanup: close the half-built app's httpx
            # client/pool so an abandoned attempt can't leak sockets across the
            # retry ladder. Detached + exception-observed so it never re-blocks
            # or re-hangs the ladder we are trying to advance.
            cleanup = asyncio.ensure_future(_run_abandon_cleanup(on_abandon))
            cleanup.add_done_callback(_consume_abandoned_task)
        raise asyncio.TimeoutError()
    finally:
        timer.cancel()
        watchdog.cancel()
        # cancel() cannot stop a Timer whose callback is already running;
        # setting the event closes that race so a completed await can never
        # be misreported as a blocked loop.
        loop_processed_expiry.set()


async def _first_completed(*futures: "asyncio.Future") -> None:
    """Return when the first of ``futures`` completes.

    Used by the strict cold-start readiness gate to wait on "progress OR
    polling error", whichever fires first (#67498). Does not cancel the
    losers — the caller owns their lifecycle.
    """
    await asyncio.wait(set(futures), return_when=asyncio.FIRST_COMPLETED)


async def _run_abandon_cleanup(on_abandon) -> None:
    """Run the abandonment cleanup coroutine, swallowing any failure.

    Wrapped so a cleanup that itself hangs or raises cannot surface as an
    unhandled task error or block anything — it is fully fire-and-forget.
    """
    try:
        result = on_abandon()
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            await result
    except Exception:
        logger.debug("Abandoned Telegram init cleanup failed", exc_info=True)


async def _shutdown_abandoned_app(app) -> None:
    """Release a half-built PTB app's httpx transports after init was abandoned.

    ``Application.shutdown()`` / ``Bot.shutdown()`` are gated on the app's
    ``_initialized`` / ``_requests_initialized`` flags, which a wedged
    ``initialize()`` (the case this whole path exists for) may never have set —
    so calling only ``app.shutdown()`` no-ops and leaks the connection pool it
    was meant to close.  ``HTTPXRequest`` builds its ``httpx.AsyncClient``
    eagerly in its constructor and its ``shutdown()`` gates only on
    ``client.is_closed``, so closing the request transports directly releases
    the pool regardless of PTB init state.  We try the clean path first, then
    fall back to the transports.  All best-effort and swallowed.
    """
    if app is None:
        return
    try:
        await app.shutdown()
    except Exception:
        logger.debug("Abandoned Telegram app.shutdown() failed", exc_info=True)
    # Directly close the underlying request transports (bypasses PTB's
    # init-gated shutdown so the eagerly-built httpx pool is released even when
    # the abandoned initialize() never flipped _initialized).
    bot = getattr(app, "bot", None)
    requests = getattr(bot, "_request", None) if bot is not None else None
    if not requests:
        return
    for request in requests:
        shutdown = getattr(request, "shutdown", None)
        if shutdown is None:
            continue
        try:
            result = shutdown()
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                await result
        except Exception:
            logger.debug("Abandoned Telegram request shutdown failed", exc_info=True)

try:
    from telegram import Update, Bot, Message, InlineKeyboardButton, InlineKeyboardMarkup
    try:
        from telegram import LinkPreviewOptions
    except ImportError:
        LinkPreviewOptions = None
    from telegram.ext import (
        Application,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler as TelegramMessageHandler,
        ContextTypes,
        filters,
    )
    from telegram.constants import ParseMode, ChatType
    from telegram.request import HTTPXRequest
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    Update = Any
    Bot = Any
    Message = Any
    InlineKeyboardButton = Any
    InlineKeyboardMarkup = Any
    LinkPreviewOptions = None
    Application = Any
    CommandHandler = Any
    CallbackQueryHandler = Any
    TelegramMessageHandler = Any
    HTTPXRequest = Any
    filters = None
    ParseMode = None
    ChatType = None

    # Mock ContextTypes so type annotations using ContextTypes.DEFAULT_TYPE
    # don't crash during class definition when the library isn't installed.
    class _MockContextTypes:
        DEFAULT_TYPE = Any
    ContextTypes = _MockContextTypes

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[3]))

from gateway.authz_mixin import _coerce_allow_set
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
    classify_send_error,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_video_from_bytes,
    cache_document_from_bytes,
    resolve_proxy_url,
    SUPPORTED_VIDEO_TYPES,
    SUPPORTED_DOCUMENT_TYPES,
    SUPPORTED_IMAGE_DOCUMENT_TYPES,
    _TEXT_INJECT_EXTENSIONS,
    utf16_len,
)
from plugins.platforms.telegram.telegram_ids import (
    normalize_telegram_chat_id,
)
from plugins.platforms.telegram.telegram_network import (
    TelegramFallbackTransport,
    discover_fallback_ips,
    parse_fallback_ip_env,
)
from plugins.platforms.telegram.telegram_messaging import (
    TelegramTextDeliveryMixin,
)
from plugins.platforms.telegram.telegram_rich import TelegramRichMixin
from plugins.platforms.telegram.telegram_polling import TelegramPollingMixin
from plugins.platforms.telegram.telegram_dm_topics import TelegramDmTopicMixin
from plugins.platforms.telegram.telegram_lifecycle import TelegramLifecycleMixin
from plugins.platforms.telegram.telegram_reactions import TelegramReactionsMixin
from plugins.platforms.telegram.telegram_media import TelegramMediaMixin
from plugins.platforms.telegram.telegram_interactive import TelegramInteractiveMixin
from plugins.platforms.telegram.telegram_config_mention import TelegramConfigMentionMixin
from utils import atomic_replace, env_float, env_int

from plugins.platforms.telegram.telegram_inbound import (
    TelegramIngestMixin,
    _TELEGRAM_IMAGE_EXTENSIONS,
    _TELEGRAM_IMAGE_EXT_TO_MIME,
    _TELEGRAM_IMAGE_MIME_TO_EXT,
    _redact_telegram_error_text,
)

def _coerce_duration_seconds(value: Any) -> Optional[int]:
    """Round a raw length to whole positive seconds, or None if unusable."""
    try:
        secs = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def _probe_voice_duration_seconds(path: str) -> Optional[int]:
    """Best-effort audio length in whole seconds for outgoing voice/audio.

    Telegram only auto-derives a clip's duration from container metadata for
    short recordings; longer ones (roughly 5 min+) are sent with duration 0
    and render as ``0:00`` in the player. We read the length locally and pass
    it explicitly so the bubble shows the real time.

    Mirrors ``gateway.run._probe_audio_duration``: stdlib ``wave`` for WAV,
    then mutagen for OGG/Opus/MP3/M4A metadata, then an ``ffprobe`` fallback.
    All three are optional — when none can read the file we return ``None``
    and the caller omits ``duration``, falling back to Telegram's own
    (possibly absent) metadata, i.e. the prior behavior. Blocking (mutagen
    read + ffprobe subprocess), so call it via ``asyncio.to_thread``.
    """
    ext = os.path.splitext(path)[1].lower()

    if ext == ".wav":
        try:
            import wave

            with wave.open(path, "rb") as wf:
                rate = wf.getframerate() or 0
                if rate:
                    secs = _coerce_duration_seconds(wf.getnframes() / float(rate))
                    if secs is not None:
                        return secs
        except Exception:
            pass

    try:
        import mutagen

        audio = mutagen.File(path)
        secs = _coerce_duration_seconds(
            getattr(getattr(audio, "info", None), "length", None)
        )
        if secs is not None:
            return secs
    except Exception:
        pass

    try:
        import shutil
        import subprocess

        if shutil.which("ffprobe"):
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            if proc.returncode == 0:
                return _coerce_duration_seconds(proc.stdout.strip())
    except Exception:
        pass

    return None


def check_telegram_requirements() -> bool:
    """Check if Telegram dependencies are available.

    If python-telegram-bot is missing, attempts to lazy-install it via
    ``tools.lazy_deps.ensure("platform.telegram")``. After a successful
    install, re-imports the SDK and flips ``TELEGRAM_AVAILABLE`` to True
    so the adapter's class-level type aliases get rebound.
    """
    global TELEGRAM_AVAILABLE, Update, Bot, Message, InlineKeyboardButton
    global InlineKeyboardMarkup, LinkPreviewOptions, Application
    global CommandHandler, CallbackQueryHandler, TelegramMessageHandler
    global ContextTypes, filters, ParseMode, ChatType, HTTPXRequest
    if TELEGRAM_AVAILABLE:
        return True
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("platform.telegram", prompt=False)
    except Exception:
        return False
    try:
        from telegram import Update as _Update, Bot as _Bot, Message as _Message
        from telegram import InlineKeyboardButton as _IKB, InlineKeyboardMarkup as _IKM
        try:
            from telegram import LinkPreviewOptions as _LPO
        except ImportError:
            _LPO = None
        from telegram.ext import (
            Application as _App, CommandHandler as _CH,
            CallbackQueryHandler as _CQH,
            MessageHandler as _MH,
            ContextTypes as _CT, filters as _filters,
        )
        from telegram.constants import ParseMode as _PM, ChatType as _CtT
        from telegram.request import HTTPXRequest as _HR
    except ImportError:
        return False
    Update = _Update
    Bot = _Bot
    Message = _Message
    InlineKeyboardButton = _IKB
    InlineKeyboardMarkup = _IKM
    LinkPreviewOptions = _LPO
    Application = _App
    CommandHandler = _CH
    CallbackQueryHandler = _CQH
    TelegramMessageHandler = _MH
    ContextTypes = _CT
    filters = _filters
    ParseMode = _PM
    ChatType = _CtT
    HTTPXRequest = _HR
    TELEGRAM_AVAILABLE = True
    return True


# Matches every character that MarkdownV2 requires to be backslash-escaped
# when it appears outside a code span or fenced code block.
_MDV2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#\+\-=|{}.!\\])')


def _escape_mdv2(text: str) -> str:
    """Escape Telegram MarkdownV2 special characters with a preceding backslash."""
    return _MDV2_ESCAPE_RE.sub(r'\\\1', text)


def _strip_mdv2(text: str) -> str:
    """Strip MarkdownV2 escape backslashes to produce clean plain text.

    Also removes MarkdownV2 formatting markers so the fallback
    doesn't show stray syntax characters from format_message conversion.
    """
    # Remove escape backslashes before special characters
    cleaned = re.sub(r'\\([_*\[\]()~`>#\+\-=|{}.!\\])', r'\1', text)
    # Remove standard markdown bold (**text** → text) BEFORE MarkdownV2 bold
    cleaned = re.sub(r'\*\*([^*]+)\*\*', r'\1', cleaned)
    # Remove MarkdownV2 bold markers that format_message converted from **bold**
    cleaned = re.sub(r'\*([^*]+)\*', r'\1', cleaned)
    # Remove MarkdownV2 italic markers that format_message converted from *italic*
    # Use word boundary (\b) to avoid breaking snake_case like my_variable_name
    cleaned = re.sub(r'(?<!\w)_([^_]+)_(?!\w)', r'\1', cleaned)
    # Remove MarkdownV2 strikethrough markers (~text~ → text)
    cleaned = re.sub(r'~([^~]+)~', r'\1', cleaned)
    # Remove MarkdownV2 spoiler markers (||text|| → text)
    cleaned = re.sub(r'\|\|([^|]+)\|\|', r'\1', cleaned)
    return cleaned


_CHUNK_INDICATOR_ON_FENCE_RE = re.compile(
    r'(?m)^``` (?P<indicator>(?:\\)?\(\d+/\d+(?:\\)?\))$'
)


def _separate_chunk_indicator_from_fence(text: str) -> str:
    """Move ``(N/M)`` chunk markers off Telegram code-fence lines.

    ``truncate_message()`` appends chunk indicators to the end of a chunk. When
    the chunk had to close an in-progress fenced code block, that creates a
    line like ````` \\(1/2\\)`` after MarkdownV2 escaping. Telegram does not
    treat that as a clean closing fence, so it can reject MarkdownV2 and fall
    back to plain text. Put the indicator on its own line immediately after the
    closing fence.
    """
    return _CHUNK_INDICATOR_ON_FENCE_RE.sub(r'```\n\g<indicator>', text)


# ---------------------------------------------------------------------------
# Markdown table → Telegram-friendly row groups
# ---------------------------------------------------------------------------
# Telegram's MarkdownV2 has no table syntax — '|' is just an escaped literal,
# so pipe tables render as noisy backslash-pipe text with no alignment.
# The shared convert_table_to_bullets() in gateway.platforms.helpers handles
# the full conversion (detection + rendering); Telegram just calls it.
# `_wrap_markdown_tables` stays re-exported here: tests import it from the
# adapter module (test_telegram_format.py), and `format_message` (now in
# TelegramConfigMentionMixin) resolves it through this namespace at call time.

from gateway.platforms.helpers import (
    convert_table_to_bullets as _wrap_markdown_tables,
)


# Watchdog bound for `await updater.stop()`. When the underlying TCP socket is
# in CLOSE-WAIT the PTB polling task is blocked on epoll on the dead socket and
# never wakes, so an unguarded stop() hangs indefinitely and wedges the whole
# reconnect/teardown ladder. This is an internal safety bound (not a user knob),
# applied identically at every stop() site so no path can hang on a dead socket.
_UPDATER_STOP_TIMEOUT = 15.0
# start_polling() can also hang when the connection pool is in a degraded state
# after _drain_polling_connections(), particularly when both primary and fallback
# Telegram endpoints are unreachable. Bounding start_polling() prevents the
# reconnect ladder from stalling indefinitely and allows the heartbeat loop to
# trigger its own recovery path. Refs: NousResearch/hermes-agent#59614
_UPDATER_START_TIMEOUT = 30.0
# Initial connect is not healthy until the dedicated getUpdates request completes
# one successful round trip. Unlike reconnect, initial bootstrap must fail closed
# so GatewayRunner disposes the partial adapter and retries with a fresh PTB app.
_INITIAL_POLLING_PROGRESS_TIMEOUT = 60.0
# shutdown()/initialize() on the getUpdates httpx request close and rebuild the
# connection pool. When a connection is wedged on a stale CLOSE-WAIT socket that
# close can block forever, hanging _drain_polling_connections() and freezing the
# whole reconnect ladder (the tracked _polling_error_task never completes, so
# every escalation path stays gated behind its in-flight guard). Bound the drain
# so the ladder always advances toward the fatal-restart escalation. Matches
# _UPDATER_STOP_TIMEOUT. Refs: NousResearch/hermes-agent#66377
_DRAIN_TIMEOUT = 15.0
# Cause-agnostic wedged-recovery watchdog (#66377). Every recovery path (the
# reconnect ladder's re-entry, the pending-update probe, PTB's error callback)
# gates new recovery on ``_polling_error_task.done()``; if that task ever wedges
# on a hung await that no local bound covers, the whole gateway goes silently
# deaf with nothing retrying. The heartbeat loop force-escalates a recovery task
# that stays in-flight far longer than any healthy ladder attempt could take —
# stop (_UPDATER_STOP_TIMEOUT) + drain (2x_DRAIN_TIMEOUT) + start
# (_UPDATER_START_TIMEOUT) + max backoff (60s) is ~135s, so 300s is
# unambiguously stuck.
_POLLING_ERROR_TASK_STUCK_TIMEOUT = 300.0
# A generation is not healthy until the dedicated getUpdates request returns
# successfully. This exceeds a normal long-poll cycle for healthy idle bots.
_POLLING_PROGRESS_TIMEOUT = 60.0
# Telegram transcodes an uploaded video before it answers sendVideo, so the
# wait for the response is unrelated to how fast the bytes went out and can
# outlast the 20s read timeout the rest of the Bot API is tuned for. Only
# media sends take this longer budget; ordinary calls keep the short one so a
# dead request is still noticed quickly. Kept modest deliberately — this is
# also how long a user waits to be told the attachment failed.
_MEDIA_SEND_READ_TIMEOUT = 60.0
_POLLING_GENERATION_CONTEXT: ContextVar[Optional[int]] = ContextVar(
    "telegram_polling_generation", default=None
)


class _PollingLifecycleAbort(RuntimeError):
    """Internal control flow for polling startup fenced by teardown."""


class TelegramAdapter(TelegramConfigMentionMixin, TelegramInteractiveMixin, TelegramMediaMixin, TelegramLifecycleMixin, TelegramReactionsMixin, TelegramPollingMixin, TelegramIngestMixin, TelegramTextDeliveryMixin, TelegramRichMixin, TelegramDmTopicMixin, BasePlatformAdapter):
    """
    Telegram bot adapter.

    Handles:
    - Receiving messages from users and groups
    - Sending responses with Telegram markdown
    - Forum topics (thread_id support)
    - Media messages
    """

    # Telegram message limits
    MAX_MESSAGE_LENGTH = 4096
    supports_code_blocks = True  # Telegram MarkdownV2 renders fenced code blocks
    splits_long_messages = True  # send() chunks via truncate_message(MAX_MESSAGE_LENGTH)
    # Threshold for detecting Telegram client-side message splits.
    # When a chunk is near this limit, a continuation is almost certain.
    _SPLIT_THRESHOLD = 4000
    MEDIA_GROUP_WAIT_SECONDS = 0.8
    _GENERAL_TOPIC_THREAD_ID = "1"

    # Telegram's edit_message applies MarkdownV2 formatting only on the
    # finalize=True path.  Without this flag, stream_consumer._send_or_edit
    # short-circuits when the raw text is unchanged between the last streamed
    # edit and the final edit, skipping the plain-text → MarkdownV2 conversion.
    # Fixes #25710.
    REQUIRES_EDIT_FINALIZE: bool = True
    # Retrying a turn-final edit consumes more of the same Telegram flood
    # budget while the completed answer remains undelivered. Move directly to
    # the final fallback path instead.
    FALLBACK_ON_FINAL_EDIT_FLOOD: bool = True
    # A failed final edit can leave Telegram clients with only a partial or
    # non-durable preview. Commit empty-tail fallbacks as a fresh final message
    # instead of trusting the preview as completed delivery.
    RESEND_FINAL_ON_EMPTY_STREAM_FALLBACK: bool = True

    # Adaptive text-batch ingress: short messages need a tighter delay so the
    # first token reaches the agent fast.  Numbers tuned for "feels instant":
    # ≤320 codepoints (one short paragraph) settles in ~180ms; ≤1024
    # (a normal paragraph) in ~240ms; longer waits the configured cap.
    # Always clamped to ``_text_batch_delay_seconds`` so an operator can lower
    # the cap further via env var.
    _TEXT_BATCH_FAST_LEN = 320
    _TEXT_BATCH_FAST_DELAY_S = 0.18
    _TEXT_BATCH_SHORT_LEN = 1024
    _TEXT_BATCH_SHORT_DELAY_S = 0.24

    @staticmethod
    def _env_float_clamped(
        name: str,
        default: float,
        *,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
    ) -> float:
        """Read a float env var, reject non-finite values, and clamp to bounds.

        Guarantees the returned value is a finite number usable directly in
        ``asyncio.sleep()`` and similar APIs that reject NaN / Inf.
        """
        import math

        raw = os.getenv(name)
        try:
            value = float(raw) if raw is not None else float(default)
        except (TypeError, ValueError):
            value = float(default)
        if not math.isfinite(value):
            value = float(default)
        if min_value is not None:
            value = max(value, min_value)
        if max_value is not None:
            value = min(value, max_value)
        return value

    @property
    def message_len_fn(self):
        """Telegram measures message length in UTF-16 code units."""
        return utf16_len

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.TELEGRAM)
        self._app: Optional[Application] = None
        self._bot: Optional[Bot] = None
        self._init_polling_state()
        self._mention_patterns = self._compile_mention_patterns()
        self._reply_to_mode: str = getattr(config, 'reply_to_mode', 'first') or 'first'
        self._disable_link_previews: bool = self._coerce_bool_extra("disable_link_previews", False)
        # Bot API 10.1 Rich Messages: render constructs the legacy MarkdownV2
        # path degrades (tables → bullet lists, task lists, <details>, block
        # math) via sendRichMessage / editMessageText's rich_message param using
        # the raw agent markdown. Disabled by default so Telegram messages stay
        # easy to copy as plain text; users can opt in for richer rendering on
        # clients that accept but render rich messages poorly via
        # platforms.telegram.extra.rich_messages: true.  Keep this opt-in:
        # current Telegram clients can make rich messages difficult to copy
        # as plain text, which is worse than degraded table/task-list rendering
        # for command snippets and mobile handoffs.
        self._rich_messages_enabled: bool = self._coerce_bool_extra("rich_messages", False)
        # Rich draft previews use a separate opt-in. Telegram macOS / Desktop
        # can leave Bot API 10.1 rich draft frames visually overlaid until the
        # chat is redrawn, while final rich messages remain useful.
        self._rich_drafts_enabled: bool = self._coerce_bool_extra("rich_drafts", False)
        # Latched off after a capability failure on sendRichMessage /
        # sendRichMessageDraft (e.g. older python-telegram-bot without the
        # endpoint) so later sends skip the doomed rich attempt entirely.
        self._rich_send_disabled: bool = False
        self._rich_draft_disabled: bool = False
        # Transient Telegram sendChatAction failures (network blips, 429/5xx)
        # can happen on every keep-typing tick while the agent is waiting on a
        # long model call. Back off per chat so a short Telegram-side outage
        # does not spam the API/logs or burn the keep-typing budget.
        self._telegram_typing_cooldown_until: Dict[str, float] = {}
        self._telegram_typing_cooldown_seconds: float = self._coerce_float_extra(
            "typing_cooldown_seconds",
            30.0,
            min_value=1.0,
            max_value=300.0,
        )
        # Inbound ingest batching/grouping state lives on TelegramIngestMixin.
        self._init_ingest_state()
        self._drop_delayed_deliveries = False
        self._polling_conflict_count: int = 0
        self._polling_conflict_recovery_generation: Optional[int] = None
        self._polling_network_error_count: int = 0
        self._polling_progress_verifier_task: Optional[asyncio.Task] = None
        self._polling_heartbeat_task: Optional[asyncio.Task] = None
        # Live @username, refreshed whenever Telegram tells us what it is.
        # PTB caches getMe() in Bot._bot_user at initialize() and only rewrites
        # it inside get_me(), so a BotFather rename leaves self._bot.username
        # pointing at the old handle until something calls getMe again. Every
        # mention/routing comparison reads _current_bot_username() instead.
        self._bot_username_observed: Optional[str] = None
        # None = never checked. Must NOT be 0.0: these are compared against
        # time.monotonic(), whose epoch is arbitrary and on a freshly-booted
        # host starts near zero — so a 0.0 sentinel reads as "checked just
        # now" and suppresses the first refresh for the first TTL seconds of
        # uptime.
        self._bot_identity_checked_at: Optional[float] = None
        self._bot_identity_refresh_task: Optional[asyncio.Task] = None
        # Consecutive heartbeat probes that saw queued updates the running
        # poller is not consuming. get_me() can't see this — the send path is
        # healthy while the getUpdates consumer is wedged — so the heartbeat
        # also probes get_webhook_info().pending_update_count and escalates to
        # recovery after two consecutive stuck probes (#42909).
        self._polling_pending_stuck_count: int = 0
        # Consecutive heartbeat probes that found the updater stopped entirely
        # (running=False) while we are in polling mode with no reconnect in
        # flight. Distinct from the wedged-but-running case above: the long-poll
        # task is simply gone, so neither the connectivity probe nor PTB's
        # error_callback ever fires and the gateway silently stops receiving
        # messages with the process still alive (#55769).
        self._polling_not_running_count: int = 0
        # A polling generation stays degraded until the dedicated getUpdates
        # request makes successful progress. start_polling() return and getMe()
        # success on the general request path are not polling-health signals.
        # While True, send() short-circuits to a failure so callers
        # (cron live-adapter branch) fall through to standalone delivery.
        self._send_path_degraded: bool = False
        self._general_request_drain_lock = asyncio.Lock()
        # DM Topics: map of topic_name -> message_thread_id (populated at startup)
        self._dm_topics: Dict[str, int] = {}
        # Track forum chats where we've already registered bot commands
        self._forum_command_registered: set[int] = set()
        # Lock per la registrazione sicura dei comandi nei forum supergroup
        self._forum_lock = asyncio.Lock()
        # Status indicator: when enabled, the bot's short description (the line
        # shown under its name in the profile) is set to "Online" on connect and
        # "Offline" on clean disconnect, so users can tell whether the gateway is
        # up. Telegram bots have no real presence/online dot (that's a user-account
        # feature), so the short description is the closest available surface.
        # Off by default — this mutates the bot's GLOBAL profile, visible to all
        # users. Opt in via gateway config: extra.status_indicator: true, or set
        # custom strings via extra.status_online / extra.status_offline.
        self._status_indicator_enabled: bool = bool(
            self.config.extra.get("status_indicator", False)
        )
        self._status_online_text: str = str(
            self.config.extra.get("status_online", "Online")
        )
        self._status_offline_text: str = str(
            self.config.extra.get("status_offline", "Offline")
        )
        # DM Topics config from extra.dm_topics
        self._dm_topics_config: List[Dict[str, Any]] = self.config.extra.get("dm_topics", [])
        # Precomputed chat_ids that have DM topics configured (for O(1) root-DM ignore check)
        self._dm_topic_chat_ids: Set[str] = {
            str(e["chat_id"]) for e in self._dm_topics_config if "chat_id" in e
        }
        # Document size cap. Telegram's public Bot API caps getFile at 20MB; a
        # locally-hosted telegram-bot-api server (configured via extra.base_url)
        # raises that to 2GB, so the presence of base_url is the opt-in.
        self._max_doc_bytes: int = (
            2 * 1024 * 1024 * 1024
            if self.config.extra.get("base_url")
            else 20 * 1024 * 1024
        )
        # Interactive model picker state per chat
        self._model_picker_state: Dict[str, dict] = {}
        self._choice_picker_state: Dict[str, dict] = {}
        # Approval button state: message_id → session_key
        self._approval_state: Dict[int, str] = {}
        # Slash-confirm button state: confirm_id → session_key (for /reload-mcp
        # and any other slash-confirm prompts; see GatewayRunner._request_slash_confirm).
        self._slash_confirm_state: Dict[str, str] = {}
        # Clarify button state: clarify_id → session_key (for the clarify tool's
        # multiple-choice prompts; see GatewayRunner clarify_callback wiring).
        self._clarify_state: Dict[str, str] = {}
        # Notification mode for message sends.
        # "important" — only final responses, approvals, and slash confirmations
        #               trigger notifications; tool progress, streaming, status
        #               messages are delivered silently via disable_notification.
        #               This is the default — Telegram users found per-tool-call
        #               push notifications too noisy.
        # "all"       — every message triggers a push notification (legacy
        #               behavior; opt-in via display.platforms.telegram.notifications).
        self._notifications_mode: str = "important"
        # send_or_update_status() bookkeeping: {(chat_id, status_key) -> bot message_id}
        # Tracks status bubbles owned by this adapter so subsequent calls with the
        # same key edit the same message instead of appending new ones (#30045).
        self._status_message_ids: Dict[tuple, str] = {}
        # Last truncated mid-stream preview delivered per (chat_id, message_id).
        # Once an oversized streaming edit saturates at the 4096 preview cap,
        # every subsequent progressive edit truncates to the SAME text; sending
        # it again is a no-op that still burns Telegram's flood budget (~1
        # edit/0.8s × the rest of the stream ⇒ flood control with 200s+
        # penalties, hanging final delivery). Dedup here so a saturated preview
        # goes quiet until finalize. Bounded: entries are dropped on finalize.
        self._last_overflow_preview: Dict[tuple, str] = {}
        # Background task that runs post-connect housekeeping (command-menu
        # registration + DM-topic setup) off the connect path so a slow Bot
        # API call (e.g. a set_my_commands stall for certain tokens) cannot
        # blow the gateway's connect timeout (#46298).
        self._post_connect_task: Optional[asyncio.Task] = None

    def _mark_connected(self) -> None:
        self._drop_delayed_deliveries = False
        super()._mark_connected()

    def _mark_disconnected(self) -> None:
        self._drop_delayed_deliveries = True
        super()._mark_disconnected()

    def _set_fatal_error(self, code: str, message: str, *, retryable: bool) -> None:
        self._drop_delayed_deliveries = True
        super()._set_fatal_error(code, message, retryable=retryable)

    def _should_drop_delayed_delivery(self) -> bool:
        """True once teardown/fatal-error started — delayed flushes must drop.

        Buffered text/photo/media-group flushes sit behind an asyncio.sleep().
        If disconnect wins the race, dispatching them spawns an agent on a
        torn-down session, producing stale/duplicate deliveries.
        """
        return bool(getattr(self, "_drop_delayed_deliveries", False))

    def _is_callback_user_authorized(
        self,
        user_id: str,
        *,
        chat_id: Optional[str] = None,
        chat_type: Optional[str] = None,
        thread_id: Optional[str] = None,
        user_name: Optional[str] = None,
    ) -> bool:
        """Return whether a Telegram inline-button caller may perform gated actions."""
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return False

        runner = getattr(getattr(self, "_message_handler", None), "__self__", None)
        auth_fn = getattr(runner, "_is_user_authorized", None)
        if callable(auth_fn):
            try:
                from gateway.session import SessionSource

                normalized_chat_type = str(chat_type or "dm").strip().lower() or "dm"
                if normalized_chat_type == "private":
                    normalized_chat_type = "dm"
                elif normalized_chat_type == "supergroup":
                    normalized_chat_type = "forum" if thread_id is not None else "group"

                source = SessionSource(
                    platform=Platform.TELEGRAM,
                    chat_id=str(chat_id or normalized_user_id),
                    chat_type=normalized_chat_type,
                    user_id=normalized_user_id,
                    user_name=str(user_name).strip() if user_name else None,
                    thread_id=str(thread_id) if thread_id is not None else None,
                )
                return bool(auth_fn(source))
            except Exception:
                logger.debug(
                    "[Telegram] Falling back to env-only callback auth for user %s",
                    normalized_user_id,
                    exc_info=True,
                )

        allowed_csv = _scoped_gate_env("TELEGRAM_ALLOWED_USERS").strip()
        if not allowed_csv:
            # Fail-closed: no allowlist means deny by default.
            # The runner auth path in _is_user_authorized() handles
            # GATEWAY_ALLOW_ALL_USERS; this fallback must not silently
            # allow everyone (fixes #24457).
            return _scoped_gate_env("GATEWAY_ALLOW_ALL_USERS").lower() in {"true", "1", "yes"}
        allowed_ids = {uid.strip() for uid in allowed_csv.split(",") if uid.strip()}
        return "*" in allowed_ids or normalized_user_id in allowed_ids

    def _source_from_message_for_auth(self, message: Message):
        """Build the same Telegram source shape the gateway auth path expects.

        Resolves the identity to authorize from ``from_user`` for normal
        messages, falling back to ``sender_chat`` for channel posts (which
        carry no ``from_user``) so a removed/unauthorized channel cannot
        inject content via the broadcast path either.
        """
        from gateway.session import SessionSource

        user = getattr(message, "from_user", None)
        chat = getattr(message, "chat", None)
        user_id = str(getattr(user, "id", "")).strip() or None
        user_name = (
            str(getattr(user, "username", "") or getattr(user, "full_name", "") or "").strip()
            or None
        )
        # Channel posts have no from_user — authorize the sender chat instead.
        if not user_id:
            sender_chat = getattr(message, "sender_chat", None)
            if sender_chat is not None:
                user_id = str(getattr(sender_chat, "id", "")).strip() or None
                if not user_name:
                    user_name = (
                        str(getattr(sender_chat, "title", "") or "").strip() or None
                    )

        chat_id = str(getattr(chat, "id", "")).strip() or user_id
        chat_type = str(getattr(chat, "type", "dm")).strip().lower() or "dm"
        if chat_type == "private":
            chat_type = "dm"
        elif chat_type == "supergroup":
            thread_id_raw = getattr(message, "message_thread_id", None)
            is_topic_message = bool(getattr(message, "is_topic_message", False))
            is_forum_group = getattr(chat, "is_forum", False) is True
            chat_type = (
                "forum"
                if thread_id_raw is not None and (is_topic_message or is_forum_group)
                else "group"
            )

        thread_id = None
        thread_id_raw = getattr(message, "message_thread_id", None)
        if thread_id_raw is not None:
            is_topic_message = bool(getattr(message, "is_topic_message", False))
            is_forum_group = getattr(chat, "is_forum", False) is True
            if chat_type == "forum" and (is_topic_message or is_forum_group):
                thread_id = str(thread_id_raw)
            elif chat_type == "dm" and is_topic_message:
                thread_id = str(thread_id_raw)

        return SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id or "",
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            thread_id=thread_id,
        )

    def _telegram_auth_env_configured(self) -> bool:
        """Return True when Telegram auth env vars make an early decision safe."""
        keys = (
            "TELEGRAM_ALLOWED_USERS",
            "TELEGRAM_GROUP_ALLOWED_USERS",
            "TELEGRAM_GROUP_ALLOWED_CHATS",
            "TELEGRAM_ALLOW_ALL_USERS",
            "GATEWAY_ALLOWED_USERS",
            "GATEWAY_ALLOW_ALL_USERS",
        )
        return any(_scoped_gate_env(key).strip() for key in keys)

    def _should_pass_unauthorized_dm_for_pairing(self, source) -> bool:
        """Return True when an unauthorized DM must still reach gateway pairing.

        Early auth (#40863) rejects before event construction. That is correct
        when unauthorized DMs are ignored, but it must not short-circuit the
        gateway pairing handshake when ``unauthorized_dm_behavior`` resolves
        to ``pair`` — including the case where an allowlist is set and the
        operator explicitly opted back into pairing via a platform override
        (resolution rule 1 in ``_get_unauthorized_dm_behavior``).
        """
        if source.chat_type != "dm":
            return False

        runner = getattr(getattr(self, "_message_handler", None), "__self__", None)
        behavior_fn = getattr(runner, "_get_unauthorized_dm_behavior", None)
        if callable(behavior_fn):
            try:
                return (
                    behavior_fn(
                        Platform.TELEGRAM,
                        profile=getattr(source, "profile", None),
                    )
                    == "pair"
                )
            except Exception:
                logger.debug(
                    "[Telegram] Failed to resolve unauthorized DM behavior; "
                    "falling back to adapter-local override",
                    exc_info=True,
                )

        extra = getattr(getattr(self, "config", None), "extra", None) or {}
        return str(extra.get("unauthorized_dm_behavior", "")).strip().lower() == "pair"

    def _is_user_authorized_from_message(self, message: Message) -> bool:
        """Check if the sender of a Telegram message is authorized.

        Intake prefilter that runs BEFORE text batching, event construction,
        and unmentioned-group observation, so a removed/unauthorized user
        cannot inject prompt content into the agent path or the observed
        transcript (fixes #40863). It only rejects when it can make the same
        context-aware decision the runner would make. Unknown DMs with no
        allowlist still pass through so the normal pairing flow can run.
        Unknown DMs with an allowlist still pass through when pairing is the
        effective unauthorized-DM behavior (explicit platform override).
        """
        source = self._source_from_message_for_auth(message)
        user_id = source.user_id
        # No identity at all → genuine group service message (pin, delete,
        # new_chat_members, etc.). Defer to the cold path. Channel posts
        # without sender_chat already resolved to None above and fall here;
        # they carry no authorizable identity, so let the normal
        # _should_process_message gating handle them.
        if not user_id:
            return True

        authorized: Optional[bool] = None

        # Adapter-level allow_from / group_allow_from: when set, they are the
        # sole authority.  Group chats use group_allow_from; DMs use allow_from.
        chat_type = source.chat_type or ""
        if chat_type in ("group", "forum", "channel"):
            adapter_allow_from = self.config.extra.get("group_allow_from")
        else:
            adapter_allow_from = self.config.extra.get("allow_from")
        if adapter_allow_from is not None:
            allowed = _coerce_allow_set(adapter_allow_from)
            authorized = user_id in allowed or "*" in allowed

        # Test/custom injection only. The class method named
        # _is_callback_user_authorized is for inline button callbacks and must
        # not be treated as a user-id-only shortcut for real messages — only
        # honor an instance-level override (set in tests).
        if authorized is None:
            callback_auth = self.__dict__.get("_is_callback_user_authorized")
            if callable(callback_auth):
                try:
                    authorized = bool(
                        callback_auth(
                            user_id,
                            chat_id=source.chat_id,
                            chat_type=source.chat_type,
                            thread_id=source.thread_id,
                            user_name=source.user_name,
                        )
                    )
                except Exception:
                    pass

        if authorized is None:
            runner = getattr(getattr(self, "_message_handler", None), "__self__", None)
            auth_fn = getattr(runner, "_is_user_authorized", None)
            if callable(auth_fn):
                # Only make an early decision via the runner when an allowlist
                # actually exists; otherwise unknown DMs must reach the pairing
                # flow rather than being default-denied here.
                if not self._telegram_auth_env_configured():
                    return True
                try:
                    authorized = bool(auth_fn(source))
                except Exception:
                    logger.debug(
                        "[Telegram] Falling back to env-only auth for user %s",
                        user_id,
                        exc_info=True,
                    )

        if authorized is None:
            allowed_csv = _scoped_gate_env("TELEGRAM_ALLOWED_USERS").strip()
            if not allowed_csv:
                return True
            allowed_ids = {uid.strip() for uid in allowed_csv.split(",") if uid.strip()}
            authorized = "*" in allowed_ids or user_id in allowed_ids

        if authorized:
            return True
        # Unauthorized DM that the gateway would pair: forward so pairing can run.
        return self._should_pass_unauthorized_dm_for_pairing(source)

    def _fallback_ips(self) -> list[str]:
        """Return validated fallback IPs from config (populated by _apply_env_overrides)."""
        configured = self.config.extra.get("fallback_ips", []) if getattr(self.config, "extra", None) else []
        if isinstance(configured, str):
            configured = configured.split(",")
        return parse_fallback_ip_env(",".join(str(v) for v in configured) if configured else None)

    @staticmethod
    def _looks_like_polling_conflict(error: Exception) -> bool:
        text = str(error).lower()
        return (
            error.__class__.__name__.lower() == "conflict"
            or "terminated by other getupdates request" in text
            or "another bot instance is running" in text
        )

    @staticmethod
    def _looks_like_network_error(error: Exception) -> bool:
        """Return True for transient transport failures that warrant reconnect."""
        name = error.__class__.__name__.lower()
        if name in {"badrequest", "invalidtoken", "forbidden", "retryafter"}:
            return False
        if name in {"networkerror", "timedout", "connectionerror"}:
            return True
        try:
            from telegram.error import (
                BadRequest,
                Forbidden,
                InvalidToken,
                NetworkError,
                RetryAfter,
                TimedOut,
            )
            if isinstance(error, (BadRequest, InvalidToken, Forbidden, RetryAfter)):
                return False
            if isinstance(error, (NetworkError, TimedOut)):
                return True
        except ImportError:
            pass
        return isinstance(error, OSError)

    @staticmethod
    def _looks_like_connect_timeout(error: Exception) -> bool:
        """Return True when a Telegram TimedOut wraps a connect-timeout.

        A plain Telegram TimedOut may mean the request reached Telegram and
        should not be re-sent. A ConnectTimeout means the TCP connection was
        never established, so retrying is safe and prevents silent drops.
        """
        seen: set[int] = set()
        stack: list[BaseException] = [error]
        while stack:
            cur = stack.pop()
            ident = id(cur)
            if ident in seen:
                continue
            seen.add(ident)
            name = cur.__class__.__name__.lower()
            text = str(cur).lower()
            if "connecttimeout" in name or "connect timeout" in text or "connect timed out" in text:
                return True
            cause = getattr(cur, "__cause__", None)
            context = getattr(cur, "__context__", None)
            if cause is not None:
                stack.append(cause)
            if context is not None:
                stack.append(context)
        return False

    @staticmethod
    def _looks_like_pool_timeout(error: Exception) -> bool:
        """Return True when a Telegram TimedOut wraps an httpx pool timeout.

        PTB converts ``httpx.PoolTimeout`` into ``telegram.error.TimedOut`` with
        a message that explicitly states the request was *not* sent
        (``"Pool timeout: All connections in the connection pool are occupied.
        Request was *not* sent to Telegram."``). Because the request never left
        the process, re-sending is safe and cannot duplicate -- the opposite of
        a generic TimedOut, which may have reached Telegram. We match the
        wrapped ``httpx.PoolTimeout`` class as well as the message string so the
        check survives PTB message-wording changes.
        """
        seen: set[int] = set()
        stack: list[BaseException] = [error]
        while stack:
            cur = stack.pop()
            ident = id(cur)
            if ident in seen:
                continue
            seen.add(ident)
            name = cur.__class__.__name__.lower()
            text = str(cur).lower()
            if "pooltimeout" in name or "pool timeout" in text or (
                "connection pool" in text and "occupied" in text
            ):
                return True
            cause = getattr(cur, "__cause__", None)
            context = getattr(cur, "__context__", None)
            if cause is not None:
                stack.append(cause)
            if context is not None:
                stack.append(context)
        return False

    def _coerce_bool_extra(self, key: str, default: bool = False) -> bool:
        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        if value is None:
            return default
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"true", "1", "yes", "on"}:
                return True
            if lowered in {"false", "0", "no", "off"}:
                return False
            return default
        return bool(value)

    def _coerce_float_extra(
        self,
        key: str,
        default: float,
        *,
        min_value: Optional[float] = None,
        max_value: Optional[float] = None,
    ) -> float:
        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        if value is None:
            return default
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return default
        if min_value is not None:
            parsed = max(parsed, min_value)
        if max_value is not None:
            parsed = min(parsed, max_value)
        return parsed



    async def _bot_identity_refresh_loop(self) -> None:
        """Keep the cached @username fresh when no heartbeat is running.

        Polling mode re-reads identity via the heartbeat's ``get_me()`` probe.
        Webhook mode has no such probe — nothing calls ``get_me()`` again after
        ``initialize()`` — so without this loop a BotFather rename breaks
        mention routing until the gateway restarts.
        """
        while True:
            try:
                await asyncio.sleep(self._BOT_IDENTITY_TTL_SECONDS)
                if getattr(self, "_polling_teardown_started", False):
                    return
                if self.has_fatal_error:
                    return
                await self._refresh_bot_identity(force=True)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.debug(
                    "[%s] Telegram identity refresh loop iteration failed",
                    self.name, exc_info=True,
                )

    def _start_post_connect_housekeeping(self) -> None:
        """Kick off deferred post-connect housekeeping in the background.

        Idempotent: if a previous housekeeping task is still running (e.g. a
        rapid reconnect), it is left in place rather than double-scheduled.
        """
        task = self._post_connect_task
        if task and not task.done():
            return
        self._post_connect_task = asyncio.ensure_future(
            self._run_post_connect_housekeeping()
        )

    async def _run_post_connect_housekeeping(self) -> None:
        """Register the command menu, surface the status indicator, and set up
        DM topics — all off the connect path so a slow Bot API call cannot blow
        the gateway connect timeout (#46298). Every step is non-fatal."""
        try:
            # Register bot commands so Telegram shows a hint menu when users type /
            # List is derived from the central COMMAND_REGISTRY — adding a new
            # gateway command there automatically adds it to the Telegram menu.
            try:
                from telegram import (
                    BotCommand,
                    BotCommandScopeAllPrivateChats,
                    BotCommandScopeAllGroupChats,
                    BotCommandScopeDefault,
                )
                from hermes_cli.commands import telegram_menu_commands, telegram_menu_max_commands
                if not self._bot:
                    return
                # Telegram allows up to 100 commands but has an undocumented
                # payload size limit (~4KB total).  Hermes defaults to 60 to
                # keep built-ins plus common skill commands visible while
                # staying under the threshold; users can tune the cap via
                # platforms.telegram.extra.command_menu.
                max_commands = telegram_menu_max_commands()
                menu_commands, hidden_count = telegram_menu_commands(max_commands=max_commands)
                bot_commands = [BotCommand(name, desc) for name, desc in menu_commands]
                # Register for all scopes independently — Telegram picks the
                # narrowest matching scope per chat type (forum topics fall
                # through to AllGroupChats or Default).
                for scope_cls in (BotCommandScopeDefault, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats):
                    scope_name = getattr(scope_cls, "__name__", str(scope_cls))
                    try:
                        await self._bot.set_my_commands(bot_commands, scope=scope_cls())
                        logger.info("[%s] set_my_commands OK for scope %s (%d cmds)", self.name, scope_name, len(bot_commands))
                    except Exception as scope_err:
                        logger.warning("[%s] set_my_commands FAILED for scope %s: %s", self.name, scope_name, scope_err)
                # Forum topics don't inherit AllGroupChats — Telegram resolves
                # commands via BotCommandScopeChat(chat_id) for forum groups.
                # Lazy registration happens in _ensure_forum_commands on first
                # message from a forum topic (see _handle_text_message).
                if hidden_count:
                    logger.info(
                        "[%s] Telegram menu: %d commands registered, %d hidden (over %d limit). Use /commands for full list.",
                        self.name, len(menu_commands), hidden_count, max_commands,
                    )
            except Exception as e:
                logger.warning(
                    "[%s] Could not register Telegram command menu: %s",
                    self.name,
                    _redact_telegram_error_text(e),
                    exc_info=True,
                )

            # Surface the gateway as "Online" in the bot's short description
            # (opt-in via extra.status_indicator). Non-fatal.
            try:
                await self._set_status_indicator(online=True)
            except Exception:
                pass

            # Set up DM topics (Bot API 9.4 — Private Chat Topics)
            # Runs after connection is established so the bot can call createForumTopic.
            # Failures here are non-fatal — the bot works fine without topics.
            try:
                await self._setup_dm_topics()
            except Exception as topics_err:
                logger.warning(
                    "[%s] DM topics setup failed (non-fatal): %s",
                    self.name, topics_err, exc_info=True,
                )
        except asyncio.CancelledError:
            raise
        finally:
            if self._post_connect_task is asyncio.current_task():
                self._post_connect_task = None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to Telegram via polling or webhook.

        By default, uses long polling (outbound connection to Telegram).
        If ``TELEGRAM_WEBHOOK_URL`` is set, starts an HTTP webhook server
        instead.  Webhook mode is useful for cloud deployments (Fly.io,
        Railway) where inbound HTTP can wake a suspended machine.

        ``is_reconnect`` distinguishes a cold first boot (False — drop any
        stale Bot API queue) from a watcher reconnect after a prolonged
        outage (True — preserve the updates Telegram queued while the bot
        was offline, otherwise every message sent during the outage is
        silently lost). The in-process network-error ladder and the
        409-conflict handler already pass ``drop_pending_updates=False``
        for the same reason; bootstrap follows suit on the reconnect path.

        Env vars for webhook mode::

            TELEGRAM_WEBHOOK_URL    Public HTTPS URL (e.g. https://app.fly.dev/telegram)
            TELEGRAM_WEBHOOK_PORT   Local listen port (default 8443)
            TELEGRAM_WEBHOOK_HOST   Bind host (default: unset → dual-stack,
                                    all interfaces IPv4+IPv6)
            TELEGRAM_WEBHOOK_SECRET Secret token for update verification
        """
        # Explicit connect() is the only operation allowed to reopen polling
        # after a completed, serialized teardown. Background recovery never
        # clears this fence.
        self._polling_teardown_started = False
        # Mode selection is re-evaluated on every explicit connection. Keep
        # webhook state false unless this connection starts its webhook.
        self._webhook_mode = False

        if not TELEGRAM_AVAILABLE:
            logger.error(
                "[%s] python-telegram-bot not installed. Run: pip install python-telegram-bot",
                self.name,
            )
            self._set_fatal_error("missing_dependency", "python-telegram-bot not installed", retryable=False)
            return False
        
        if not self.config.token:
            logger.error("[%s] No bot token configured", self.name)
            self._set_fatal_error("missing_credentials", "No bot token configured", retryable=False)
            return False
        
        try:
            if not self._acquire_platform_lock('telegram-bot-token', self.config.token, 'Telegram bot token'):
                return False

            # Build the application
            builder = Application.builder().token(self.config.token)
            custom_base_url = self.config.extra.get("base_url")
            if custom_base_url:
                builder = builder.base_url(custom_base_url)
                builder = builder.base_file_url(
                    self.config.extra.get("base_file_url", custom_base_url)
                )
                logger.info(
                    "[%s] Using custom Telegram base_url: %s",
                    self.name, custom_base_url,
                )
            # In local-mode telegram-bot-api, file_path is an absolute path on the
            # server's filesystem rather than a relative HTTP path. PTB needs
            # local_mode=True so download_*() reads from disk instead of issuing
            # an HTTP GET that would 404. Requires that the same path is
            # readable by the Hermes process (shared mount, same machine, etc.).
            if self.config.extra.get("local_mode"):
                builder = builder.local_mode(True)
                logger.info("[%s] Using Telegram local_mode (read files from disk)", self.name)

            # PTB defaults (pool_timeout=1s) are too aggressive on flaky networks and
            # can trigger "Pool timeout: All connections in the connection pool are occupied"
            # during reconnect/bootstrap. Use safer defaults and allow env overrides.
            def _env_int(name: str, default: int) -> int:
                try:
                    return int(os.getenv(name, str(default)))
                except (TypeError, ValueError):
                    return default

            def _env_float(name: str, default: float) -> float:
                try:
                    return float(os.getenv(name, str(default)))
                except (TypeError, ValueError):
                    return default

            request_kwargs = {
                "connection_pool_size": _env_int("HERMES_TELEGRAM_HTTP_POOL_SIZE", 512),
                "pool_timeout": _env_float("HERMES_TELEGRAM_HTTP_POOL_TIMEOUT", 8.0),
                "connect_timeout": _env_float("HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT", 10.0),
                "read_timeout": _env_float("HERMES_TELEGRAM_HTTP_READ_TIMEOUT", 20.0),
                "write_timeout": _env_float("HERMES_TELEGRAM_HTTP_WRITE_TIMEOUT", 20.0),
                # Not a duplicate of write_timeout: PTB routes any request
                # carrying files to media_write_timeout instead, so the line
                # above never applied to an upload and every upload was pinned
                # to PTB's own 20s default. httpx budgets this per socket
                # write rather than across the upload, so it is stall
                # tolerance, not a size or bandwidth allowance — a slow but
                # steady uplink never accumulates against it. 60s rides out
                # the buffer stalls a congested link produces; going higher
                # only lengthens how long a dead socket takes to report
                # itself.
                "media_write_timeout": 60.0,
            }

            # CLOSE_WAIT fd leak (#31599, same class as #18451): PTB's
            # HTTPXRequest builds the underlying httpx.AsyncClient with
            # `limits = httpx.Limits(max_connections=connection_pool_size)`
            # and *no* keepalive tuning, so httpx's default
            # keepalive_expiry=5.0 applies. Behind an HTTP proxy (Cloudflare
            # Warp etc.) a peer-initiated FIN can sit in CLOSE_WAIT longer
            # than that, leaking fds in the general request pool (_request[1])
            # which _drain_polling_connections never resets. Wire the shared
            # platform_httpx_limits() helper into the httpx client so idle
            # keepalive sockets drain aggressively, while preserving PTB's
            # max_connections (= connection_pool_size). httpx_kwargs is spread
            # last into PTB's client kwargs, so `limits` here wins.
            from gateway.platforms._http_client_limits import platform_httpx_limits

            _base_limits = platform_httpx_limits()
            if _base_limits is not None:
                import httpx as _httpx

                _pool_limits = _httpx.Limits(
                    max_connections=request_kwargs["connection_pool_size"],
                    max_keepalive_connections=_base_limits.max_keepalive_connections,
                    keepalive_expiry=_base_limits.keepalive_expiry,
                )
            else:  # pragma: no cover — httpx always present alongside PTB
                _pool_limits = None

            def _with_limits(httpx_kwargs: Optional[dict] = None) -> dict:
                """Merge tuned keepalive limits into httpx client kwargs.

                Used by the proxy and direct-DNS branches, where httpx honours
                the client-level ``limits`` kwarg. A caller-supplied ``limits``
                is left untouched; otherwise the CLOSE_WAIT-safe limits are
                injected. The fallback-IP branch does NOT use this helper — see
                the ``_transport_kwargs`` note below for why.
                """
                kwargs = dict(httpx_kwargs or {})
                if _pool_limits is not None and "limits" not in kwargs:
                    kwargs["limits"] = _pool_limits
                return kwargs

            disable_fallback = (os.getenv("HERMES_TELEGRAM_DISABLE_FALLBACK_IPS", "").strip().lower() in {"1", "true", "yes", "on"})
            fallback_ips = self._fallback_ips()
            if not fallback_ips:
                logger.warning("[%s] Discovering Telegram API fallback IPs via DNS-over-HTTPS…", self.name)
                fallback_ips = await discover_fallback_ips()
                logger.info(
                    "[%s] Auto-discovered Telegram fallback IPs: %s",
                    self.name,
                    ", ".join(fallback_ips),
                )

            proxy_targets = ["api.telegram.org", *fallback_ips]
            proxy_url = resolve_proxy_url("TELEGRAM_PROXY", target_hosts=proxy_targets)
            if fallback_ips and not proxy_url and not disable_fallback:
                logger.info(
                    "[%s] Telegram fallback IPs active: %s",
                    self.name,
                    ", ".join(fallback_ips),
                )
                # Keep request/update pools separate to reduce contention during
                # polling reconnect + bot API bootstrap/delete_webhook calls.
                # httpx ignores the client-level `limits` kwarg when a custom
                # `transport` is supplied (#58790).  Unlike the proxy/direct
                # branches (which inject limits at the client level via
                # `_with_limits`), this branch MUST pass the tuned limits
                # directly into TelegramFallbackTransport so its inner
                # AsyncHTTPTransport instances honour keepalive_expiry — do not
                # route this through `_with_limits`, httpx would discard it.
                _transport_kwargs: dict = {}
                if _pool_limits is not None:
                    _transport_kwargs["limits"] = _pool_limits
                request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={
                        "transport": TelegramFallbackTransport(
                            fallback_ips, **_transport_kwargs
                        )
                    },
                )
                get_updates_request = HTTPXRequest(
                    **request_kwargs,
                    httpx_kwargs={
                        "transport": TelegramFallbackTransport(
                            fallback_ips, **_transport_kwargs
                        )
                    },
                )
            elif proxy_url:
                logger.info("[%s] Proxy detected; passing explicitly to HTTPXRequest: %s", self.name, proxy_url)
                request = HTTPXRequest(
                    **request_kwargs, proxy=proxy_url, httpx_kwargs=_with_limits()
                )
                get_updates_request = HTTPXRequest(
                    **request_kwargs, proxy=proxy_url, httpx_kwargs=_with_limits()
                )
            else:
                if disable_fallback:
                    logger.info("[%s] Telegram fallback-IP transport disabled via env", self.name)
                request = HTTPXRequest(**request_kwargs, httpx_kwargs=_with_limits())
                get_updates_request = HTTPXRequest(
                    **request_kwargs, httpx_kwargs=_with_limits()
                )

            get_updates_request = self._instrument_polling_request(get_updates_request)
            builder = builder.request(request).get_updates_request(get_updates_request)
            self._app = builder.build()
            self._bot = self._app.bot
            
            # Register handlers
            self._app.add_handler(TelegramMessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_text_message
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.COMMAND,
                self._handle_command
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.LOCATION | getattr(filters, "VENUE", filters.LOCATION),
                self._handle_location_message
            ))
            self._app.add_handler(TelegramMessageHandler(
                filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL | filters.Sticker.ALL,
                self._handle_media_message
            ))
            # Handle inline keyboard button callbacks (update prompts)
            self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))
            
            # Start polling — retry initialize() for transient TLS resets.
            # Each attempt is capped by _init_timeout so a single unreachable
            # fallback-IP chain can't block startup indefinitely.
            _max_connect = 8
            _init_timeout = _env_float("HERMES_TELEGRAM_INIT_TIMEOUT", 30.0)
            # Total watchdog: ensure the entire connect loop has an upper bound
            # even if the retry loop itself silently stalls (#67498). This is
            # the per-attempt timeout PLUS generous margins between attempts so
            # we never hang past the sum even when all attempts are exhausted.
            _total_deadline = (
                asyncio.get_running_loop().time()
                + _init_timeout * _max_connect
                + 120.0  # extra margin for between-attempt sleeps + overhead
            )
            for _attempt in range(_max_connect):
                rebuild_app = False
                try:
                    # Check total watchdog deadline — if we blew past it the
                    # retry ladder must yield even if no individual attempt
                    # has raised.
                    if asyncio.get_running_loop().time() >= _total_deadline:
                        raise OSError(
                            f"Telegram initialization timed out after {_max_connect} attempts "
                            f"({_init_timeout:.0f}s each) — total connect watchdog "
                            f"deadline ({_init_timeout * _max_connect + 120.0:.0f}s) exceeded. "
                            f"Check network connectivity to api.telegram.org "
                            f"or set HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT / "
                            f"HERMES_TELEGRAM_INIT_TIMEOUT to a lower value."
                        )
                    logger.warning(
                        "[%s] Connecting to Telegram (attempt %d/%d)…",
                        self.name, _attempt + 1, _max_connect,
                    )
                    await _await_with_thread_deadline(
                        self._app.initialize(),
                        timeout=_init_timeout,
                        # On timeout the initialize() task is abandoned without
                        # awaiting its cancellation (it may be wedged in a
                        # shielded scope). Best-effort release the half-built
                        # app's httpx client/connection pool so it isn't leaked
                        # across the retry ladder (mirrors the client-close-on-
                        # timeout pattern in agent/auxiliary_client.py).
                        on_abandon=lambda app=self._app: _shutdown_abandoned_app(app),
                    )
                    break
                except asyncio.TimeoutError:
                    rebuild_app = True
                    if _attempt < _max_connect - 1:
                        wait = min(2 ** _attempt, 15)
                        logger.warning(
                            "[%s] Connect attempt %d/%d timed out after %.0fs — retrying in %ds",
                            self.name, _attempt + 1, _max_connect, _init_timeout, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise OSError(
                            f"Telegram initialization timed out after {_max_connect} attempts "
                            f"({_init_timeout:.0f}s each). Check network connectivity to api.telegram.org "
                            f"or set HERMES_TELEGRAM_HTTP_CONNECT_TIMEOUT to a lower value."
                        )
                except OSError as init_err:
                    rebuild_app = True
                    if _attempt < _max_connect - 1:
                        wait = min(2 ** _attempt, 15)
                        logger.warning(
                            "[%s] Connect attempt %d/%d failed: %s — retrying in %ds",
                            self.name, _attempt + 1, _max_connect, init_err, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
                except Exception as init_err:
                    rebuild_app = True
                    if not self._looks_like_network_error(init_err):
                        raise
                    if _attempt < _max_connect - 1:
                        wait = min(2 ** _attempt, 15)
                        logger.warning(
                            "[%s] Connect attempt %d/%d failed: %s — retrying in %ds",
                            self.name, _attempt + 1, _max_connect, init_err, wait,
                        )
                        await asyncio.sleep(wait)
                    else:
                        raise
                except BaseException:
                    # Catch CancelledError and other BaseException subclasses
                    # that the existing except handlers miss. Log the event so
                    # the operator can diagnose, then reraise so cancellation
                    # semantics are preserved (#67498).
                    # NOTE: placed LAST so Exception handlers above have
                    # priority — BaseException catches everything including
                    # Exception.
                    logger.warning(
                        "[%s] Connect attempt %d/%d interrupted by %s — propagating",
                        self.name,
                        _attempt + 1,
                        _max_connect,
                        "CancelledError"
                        if isinstance(sys.exc_info()[1], asyncio.CancelledError)
                        else type(sys.exc_info()[1]).__name__,
                    )
                    raise
                finally:
                    # After a failed attempt the app may be in a partially-
                    # initialized state (closed transports, half-built handlers).
                    # Rebuild from the same token/config so the next attempt
                    # starts with a fresh Application — the old one is discarded
                    # and will be GC'd (#67498).
                    if rebuild_app and _attempt < _max_connect - 1:
                        old_app = self._app
                        self._app = builder.build()
                        self._bot = self._app.bot
                        # Re-register handlers on the new app
                        self._app.add_handler(TelegramMessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            self._handle_text_message
                        ))
                        self._app.add_handler(TelegramMessageHandler(
                            filters.COMMAND,
                            self._handle_command
                        ))
                        self._app.add_handler(TelegramMessageHandler(
                            filters.LOCATION | getattr(filters, "VENUE", filters.LOCATION),
                            self._handle_location_message
                        ))
                        self._app.add_handler(TelegramMessageHandler(
                            filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Document.ALL | filters.Sticker.ALL,
                            self._handle_media_message
                        ))
                        self._app.add_handler(CallbackQueryHandler(self._handle_callback_query))
                        # Best-effort discard the old app's resources
                        try:
                            await _shutdown_abandoned_app(old_app)
                        except Exception:
                            pass
            await self._app.start()

            # Decide between webhook and polling mode
            webhook_started = await self._start_webhook(is_reconnect=is_reconnect)
            if not webhook_started:
                # ── Polling mode (default) ───────────────────────────
                # Clear any stale webhook first so polling doesn't inherit a
                # previous webhook registration and silently stop receiving
                # updates. Best-effort: a transient Bot API network error here
                # must not fail gateway startup — degrade to background polling
                # recovery instead.
                await self._delete_webhook_best_effort(
                    require_success=not is_reconnect
                )

                loop = asyncio.get_running_loop()

                def _polling_error_callback(error: Exception) -> None:
                    if getattr(self, "_polling_teardown_started", False):
                        return
                    if self._polling_error_task and not self._polling_error_task.done():
                        return
                    if self._looks_like_polling_conflict(error):
                        # Synchronously stop PTB's internal network_retry_loop
                        # BEFORE scheduling our async recovery task.  PTB calls
                        # this callback synchronously inside its loop and then
                        # keeps polling on its own; if we only schedule a task
                        # here, PTB's retry and our stop->restart overlap and
                        # produce a fresh 409.  Disarming the loop now makes it
                        # exit on its next tick so recovery owns polling alone.
                        self._disarm_ptb_retry_loop()
                        self._polling_error_task = loop.create_task(self._handle_polling_conflict(error))
                        self._background_tasks.add(self._polling_error_task)
                        self._polling_error_task.add_done_callback(self._background_tasks.discard)
                    elif self._looks_like_network_error(error):
                        logger.warning("[%s] Telegram network _redact_telegram_error_text(error), scheduling reconnect: %s", self.name, error)
                        self._polling_error_task = loop.create_task(self._handle_polling_network_error(error))
                        self._background_tasks.add(self._polling_error_task)
                        self._polling_error_task.add_done_callback(self._background_tasks.discard)
                    else:
                        logger.error("[%s] Telegram polling _redact_telegram_error_text(error): %s", self.name, error, exc_info=True)

                # Store reference for retry use in _handle_polling_conflict
                self._polling_error_callback_ref = _polling_error_callback

                polling_started = await self._start_polling_resilient(
                    # On a cold first boot drop the stale Bot API queue; on a
                    # watcher reconnect after an outage preserve it so messages
                    # sent while the bot was offline are delivered (#46621).
                    drop_pending_updates=not is_reconnect,
                    error_callback=_polling_error_callback,
                    require_progress=not is_reconnect,
                )
                if not polling_started:
                    logger.warning(
                        "[%s] Connected in degraded Telegram mode: gateway is alive, "
                        "polling will be retried in the background",
                        self.name,
                    )
            
            self._mark_connected()
            mode = "webhook" if self._webhook_mode else "polling"
            logger.info("[%s] Connected to Telegram (%s mode)", self.name, mode)

            # Start the persistent heartbeat loop in polling mode. Webhook mode
            # receives updates via incoming pushes — there is no long-poll
            # socket to wedge in CLOSE-WAIT, so the loop is not needed there.
            if not self._webhook_mode:
                if self._polling_heartbeat_task and not self._polling_heartbeat_task.done():
                    self._polling_heartbeat_task.cancel()
                self._polling_heartbeat_task = asyncio.ensure_future(
                    self._polling_heartbeat_loop()
                )

            # Seed the live identity from whatever PTB cached during
            # initialize(), then keep it fresh. Polling mode rides the
            # heartbeat's get_me() probe; webhook mode has no probe at all, so
            # it gets a dedicated low-frequency refresh loop — otherwise a
            # BotFather rename breaks mention routing until restart.
            self._note_bot_username(getattr(self._bot, "username", None))
            self._bot_identity_checked_at = time.monotonic()
            if self._webhook_mode:
                identity_task = getattr(self, "_bot_identity_refresh_task", None)
                if identity_task and not identity_task.done():
                    identity_task.cancel()
                self._bot_identity_refresh_task = asyncio.ensure_future(
                    self._bot_identity_refresh_loop()
                )

            # Command-menu registration, DM-topic setup, and the status
            # indicator each make Bot API calls that can stall for certain
            # tokens. Running them here — inside the connect() coroutine that
            # the gateway wraps in a connect timeout — means one slow call
            # blows the whole connect and the adapter never comes up, even
            # though polling/webhook is already live (#46298). Defer them to a
            # cancellable background task so connect() returns as soon as the
            # transport is up.
            self._start_post_connect_housekeeping()

            return True
            
        except Exception as e:
            self._release_platform_lock()
            safe_error = _redact_telegram_error_text(e)
            message = f"Telegram startup failed: {safe_error}"
            self._set_fatal_error("telegram_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect to Telegram: %s", self.name, safe_error)
            return False

    async def _set_status_indicator(self, online: bool) -> None:
        """Set the bot's short description to the online/offline status text.

        The short description is the line shown under the bot's name in its
        profile. It is the closest Bot API surface to a presence indicator —
        bots have no real online/offline dot (that's a user-account feature).

        No-op unless ``extra.status_indicator`` is enabled. Best-effort: any
        failure is logged at debug and swallowed so it never blocks connect or
        disconnect. The default (no language_code) description applies to every
        user who doesn't have a language-specific one set.
        """
        if not getattr(self, "_status_indicator_enabled", False):
            return
        bot = self._bot
        if bot is None:
            return
        text = self._status_online_text if online else self._status_offline_text
        # Telegram caps short_description at 120 chars.
        text = text[:120]
        try:
            await bot.set_my_short_description(short_description=text)
            logger.info("[%s] Set bot status indicator to %r", self.name, text)
        except Exception as e:
            logger.debug(
                "[%s] Failed to set bot status indicator to %r: %s",
                self.name, text, _redact_telegram_error_text(e),
            )

    async def _cancel_pending_delivery_tasks(self) -> None:
        """Cancel every delayed-delivery task family before disconnect completes.

        Covers media-group, photo-batch and text-batch flush tasks plus the
        polling-error recovery task. Each sits behind an ``asyncio.sleep()``;
        if teardown leaves them running they dispatch ``handle_message`` into a
        torn-down session. Skips the current task so the coroutine driving
        teardown does not cancel itself.
        """
        current_task = asyncio.current_task()
        pending_tasks: list[asyncio.Task] = []
        awaitable_tasks: list[asyncio.Task] = []
        seen: set[int] = set()

        def collect(task: Optional[asyncio.Task]) -> None:
            if not task or task.done() or task is current_task:
                return
            marker = id(task)
            if marker in seen:
                return
            seen.add(marker)
            pending_tasks.append(task)
            if asyncio.isfuture(task) or asyncio.iscoroutine(task):
                awaitable_tasks.append(task)

        for task in list(self._media_group_tasks.values()):
            collect(task)
        for task in list(self._pending_photo_batch_tasks.values()):
            collect(task)
        for task in list(self._pending_text_batch_tasks.values()):
            collect(task)
        collect(getattr(self, "_polling_error_task", None))
        collect(getattr(self, "_polling_progress_verifier_task", None))

        for task in pending_tasks:
            task.cancel()
        if awaitable_tasks:
            await asyncio.gather(*awaitable_tasks, return_exceptions=True)

        self._media_group_tasks.clear()
        self._media_group_events.clear()
        self._pending_photo_batch_tasks.clear()
        self._pending_photo_batches.clear()
        self._pending_text_batch_tasks.clear()
        self._pending_text_batches.clear()
        if getattr(self, "_polling_error_task", None) is not current_task:
            self._polling_error_task = None
        if getattr(self, "_polling_progress_verifier_task", None) is not current_task:
            self._polling_progress_verifier_task = None

    async def disconnect(self) -> None:
        """Stop polling/webhook, cancel pending delayed deliveries, and disconnect."""
        # Mark disconnected first so the drop guard short-circuits any flush
        # that wins the race against teardown and prevents new delayed tasks
        # from being scheduled by late update handlers.
        self._mark_disconnected()
        self._polling_teardown_started = True
        self._polling_progress_accepting = False
        self._polling_generation = getattr(self, "_polling_generation", 0) + 1
        self._send_path_degraded = True

        # Recovery can be suspended in stop/drain/start while disconnect begins.
        # Cancel and await both polling lifecycle owners immediately after the
        # fence, before any other teardown await lets them start a new generation.
        current_task = asyncio.current_task()
        lifecycle_tasks: list[asyncio.Task] = []
        lifecycle_seen: set[int] = set()
        for task in (
            getattr(self, "_polling_error_task", None),
            getattr(self, "_polling_progress_verifier_task", None),
        ):
            if not task or task.done() or task is current_task:
                continue
            marker = id(task)
            if marker in lifecycle_seen:
                continue
            lifecycle_seen.add(marker)
            task.cancel()
            if asyncio.isfuture(task) or asyncio.iscoroutine(task):
                lifecycle_tasks.append(task)
        if lifecycle_tasks:
            await asyncio.gather(*lifecycle_tasks, return_exceptions=True)
        if getattr(self, "_polling_error_task", None) is not current_task:
            self._polling_error_task = None
        if getattr(self, "_polling_progress_verifier_task", None) is not current_task:
            self._polling_progress_verifier_task = None

        # Cancellation callbacks may have run while awaited; the teardown fence
        # remains authoritative regardless of their finalizers.
        self._polling_progress_accepting = False
        self._send_path_degraded = True

        # Cancel deferred post-connect housekeeping (command-menu / DM-topic /
        # status-indicator Bot API calls) so it cannot fire into a half-torn-down
        # bot client (#46298). getattr guards the object.__new__ test pattern
        # where __init__ (which sets this attr) is never called.
        post_connect_task = getattr(self, "_post_connect_task", None)
        if post_connect_task and not post_connect_task.done():
            post_connect_task.cancel()
            await asyncio.gather(post_connect_task, return_exceptions=True)
        self._post_connect_task = None

        # Cancel the heartbeat before tearing down the app so the probe task
        # cannot fire get_me() into a half-shutdown bot client.
        polling_heartbeat_task = getattr(self, "_polling_heartbeat_task", None)
        if polling_heartbeat_task and not polling_heartbeat_task.done():
            polling_heartbeat_task.cancel()
            try:
                await polling_heartbeat_task
            except asyncio.CancelledError:
                pass
        self._polling_heartbeat_task = None

        # Cancel the webhook-mode identity refresh loop on the same fence as
        # the heartbeat so it cannot fire get_me() into a torn-down client.
        identity_task = getattr(self, "_bot_identity_refresh_task", None)
        if identity_task and not identity_task.done():
            identity_task.cancel()
            try:
                await identity_task
            except asyncio.CancelledError:
                pass
        self._bot_identity_refresh_task = None

        # Mark the bot "Offline" in its short description while the bot's HTTP
        # client is still alive (before app shutdown closes it). Opt-in via
        # extra.status_indicator. Non-fatal. This is the clean-shutdown path;
        # a hard crash leaves the last-known status, which is the expected
        # limitation of a profile-text indicator.
        try:
            await self._set_status_indicator(online=False)
        except Exception:
            pass

        await self._cancel_pending_delivery_tasks()

        if self._app:
            try:
                # Only stop the updater if it's running.  Bounded with a
                # timeout: a CLOSE-WAIT socket can wedge stop() on epoll
                # indefinitely, which would hang disconnect() (and any
                # gateway shutdown/restart waiting on it) forever.  On timeout
                # we fall through to app.stop()/shutdown() to force teardown.
                if self._app.updater and self._app.updater.running:
                    try:
                        await asyncio.wait_for(self._app.updater.stop(), timeout=_UPDATER_STOP_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "[%s] updater.stop() timed out during disconnect "
                            "(likely CLOSE-WAIT socket); forcing app shutdown",
                            self.name,
                        )
                if self._app.running:
                    await self._app.stop()
                await self._app.shutdown()
            except Exception as e:
                logger.warning(
                    "[%s] Error during Telegram disconnect: %s",
                    self.name, _redact_telegram_error_text(e),
                )
        self._release_platform_lock()

        self._app = None
        self._bot = None
        logger.info("[%s] Disconnected from Telegram", self.name)

    def _missing_media_path_error(self, label: str, path: str) -> str:
        """Build an actionable file-not-found error for gateway MEDIA delivery.

        Paths like /workspace/... or /output/... often only exist inside the
        Docker sandbox, while the gateway process runs on the host.
        """
        error = f"{label} file not found: {path}"
        if path.startswith(("/workspace/", "/output/", "/outputs/")):
            error += (
                " (path may only exist inside the Docker sandbox. "
                "Bind-mount a host directory and emit the host-visible "
                "path in MEDIA: for gateway file delivery.)"
            )
        return error

    def _telegram_media_too_large_note(self, label: str, file_size: Any, max_bytes: int) -> str:
        limit_mb = max(1, max_bytes // (1024 * 1024))
        try:
            size_mb = int(file_size or 0) / (1024 * 1024)
            size_text = f"{size_mb:.1f} MB"
        except (TypeError, ValueError):
            size_text = "unknown size"
        return (
            f"[Telegram {label} skipped: file size {size_text} exceeds the "
            f"{limit_mb} MB limit. Ask the user to send a smaller file.]"
        )

    def _telegram_media_size_allowed(self, source: Any, label: str) -> tuple[bool, Optional[str]]:
        """Validate Telegram media size before downloading into memory."""
        max_bytes = int(getattr(self, "_max_doc_bytes", 20 * 1024 * 1024) or 20 * 1024 * 1024)
        file_size = getattr(source, "file_size", None)
        try:
            size = int(file_size or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            return True, None
        if size <= max_bytes:
            return True, None
        return False, self._telegram_media_too_large_note(label, size, max_bytes)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send audio as a native Telegram voice message or audio file."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        
        try:
            if not os.path.exists(audio_path):
                return SendResult(success=False, error=self._missing_media_path_error("Audio", audio_path))
            
            # Compute duration locally — Telegram drops it for long clips
            # (~5 min+), which then show 0:00 in the player.
            _duration_secs = await asyncio.to_thread(
                _probe_voice_duration_seconds, audio_path
            )

            # Render caption markdown (#32029): auto-TTS captions carry the
            # agent's markdown reply, which showed literal *asterisks* and
            # [links](...) without a parse_mode. Format to MarkdownV2 when it
            # fits the 1024-char caption cap; fall back to the raw text
            # (previous behaviour) when formatting would overflow or the
            # Bot API rejects the entities.
            _caption_variants: List[tuple] = []
            if caption:
                try:
                    _formatted_caption = self.format_message(caption)
                    if utf16_len(_formatted_caption) <= 1024:
                        _caption_variants.append(
                            (_formatted_caption, ParseMode.MARKDOWN_V2)
                        )
                except Exception:
                    logger.debug(
                        "[%s] voice caption MarkdownV2 formatting failed; "
                        "sending plain caption", self.name, exc_info=True,
                    )
                _caption_variants.append((caption[:1024], None))
            else:
                _caption_variants.append((None, None))

            with open(audio_path, "rb") as audio_file:
                ext = os.path.splitext(audio_path)[1].lower()
                # .ogg / .opus files -> send as voice (round playable bubble)
                if ext in {".ogg", ".opus"}:
                    _voice_thread = self._metadata_thread_id(metadata)
                    reply_to_id = self._reply_to_message_id_for_send(reply_to, metadata, reply_to_mode=self._reply_to_mode)
                    voice_thread_kwargs = self._thread_kwargs_for_send(
                        chat_id,
                        _voice_thread,
                        metadata,
                        reply_to_message_id=reply_to_id,
                        reply_to_mode=self._reply_to_mode
                    )
                    msg = None
                    _last_parse_error: Optional[Exception] = None
                    for _cap_text, _cap_parse_mode in _caption_variants:
                        try:
                            msg = await self._send_with_dm_topic_reply_anchor_retry(
                                self._bot.send_voice,
                                {
                                    "chat_id": normalize_telegram_chat_id(chat_id),
                                    "voice": audio_file,
                                    "caption": _cap_text,
                                    "parse_mode": _cap_parse_mode,
                                    "reply_to_message_id": reply_to_id,
                                    "duration": _duration_secs,
                                    "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                                    **voice_thread_kwargs,
                                    **self._notification_kwargs(metadata),
                                },
                                metadata,
                                reply_to_id,
                                "voice",
                                reset_media=lambda: audio_file.seek(0),
                            )
                            break
                        except Exception as _cap_error:
                            # Only retry the next (plain) variant on entity
                            # parse failures; anything else is a real send
                            # error for the outer handler.
                            if (_cap_parse_mode is not None
                                    and ("parse" in str(_cap_error).lower()
                                         or "entit" in str(_cap_error).lower())):
                                logger.warning(
                                    "[%s] voice caption MarkdownV2 rejected, "
                                    "retrying plain: %s",
                                    self.name,
                                    _redact_telegram_error_text(_cap_error),
                                )
                                _last_parse_error = _cap_error
                                audio_file.seek(0)
                                continue
                            raise
                    if msg is None:
                        raise _last_parse_error or RuntimeError(
                            "Telegram send_voice failed for all caption variants"
                        )
                elif ext in {".mp3", ".m4a"}:
                    # Telegram's Bot API sendAudio only accepts MP3 / M4A.
                    _audio_thread = self._metadata_thread_id(metadata)
                    reply_to_id = self._reply_to_message_id_for_send(reply_to, metadata, reply_to_mode=self._reply_to_mode)
                    audio_thread_kwargs = self._thread_kwargs_for_send(
                        chat_id,
                        _audio_thread,
                        metadata,
                        reply_to_message_id=reply_to_id,
                        reply_to_mode=self._reply_to_mode
                    )
                    msg = await self._send_with_dm_topic_reply_anchor_retry(
                        self._bot.send_audio,
                        {
                            "chat_id": normalize_telegram_chat_id(chat_id),
                            "audio": audio_file,
                            "caption": caption[:1024] if caption else None,
                            "reply_to_message_id": reply_to_id,
                            "duration": _duration_secs,
                            "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                            **audio_thread_kwargs,
                            **self._notification_kwargs(metadata),
                        },
                        metadata,
                        reply_to_id,
                        "audio",
                        reset_media=lambda: audio_file.seek(0),
                    )
                else:
                    # Formats Telegram can't play natively (.wav, .flac, ...)
                    # — fall back to document delivery instead of raising.
                    return await self.send_document(
                        chat_id=chat_id,
                        file_path=audio_path,
                        caption=caption,
                        reply_to=reply_to,
                        metadata=metadata,
                    )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[%s] Failed to send Telegram voice/audio, falling back to base adapter: %s",
                self.name,
                _redact_telegram_error_text(e),
                exc_info=True,
            )
            return await super().send_voice(chat_id, audio_path, caption, reply_to, metadata=metadata)

    async def send_multiple_images(
        self,
        chat_id: str,
        images: List[tuple],
        metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Send a batch of images natively via Telegram's media group API.

        Telegram's ``send_media_group`` bundles up to 10 photos/videos into
        a single album. Larger batches are chunked. Animated GIFs cannot
        go into a media group (they require ``send_animation``), so they
        are peeled off and sent individually via the base default path.

        URL-based photos go into the group directly; local files are
        opened as byte streams. On failure the whole batch falls back to
        the base adapter's per-image loop.
        """
        if not self._bot:
            return
        if not images:
            return

        try:
            from telegram import InputMediaPhoto
        except Exception as exc:  # pragma: no cover - missing SDK
            logger.warning(
                "[%s] InputMediaPhoto unavailable, falling back to per-image send: %s",
                self.name, exc,
            )
            await super().send_multiple_images(chat_id, images, metadata, human_delay)
            return

        # Peel off animations — they need send_animation, not send_media_group
        animations: List[tuple] = []
        photos: List[tuple] = []
        for image_url, alt_text in images:
            if not image_url.startswith("file://") and self._is_animation_url(image_url):
                animations.append((image_url, alt_text))
            else:
                photos.append((image_url, alt_text))

        # Animations: route through the base default (per-image send_animation)
        if animations:
            await super().send_multiple_images(
                chat_id, animations, metadata, human_delay=human_delay,
            )

        if not photos:
            return

        from urllib.parse import unquote as _unquote
        _thread = self._metadata_thread_id(metadata)

        # Chunk into groups of 10 (Telegram's album limit)
        CHUNK = 10
        chunks = [photos[i:i + CHUNK] for i in range(0, len(photos), CHUNK)]

        for chunk_idx, chunk in enumerate(chunks):
            if human_delay > 0 and chunk_idx > 0:
                await asyncio.sleep(human_delay)

            media: List[Any] = []
            opened_files: List[Any] = []
            try:
                for image_url, alt_text in chunk:
                    caption = alt_text[:1024] if alt_text else None
                    if image_url.startswith("file://"):
                        local_path = _unquote(image_url[7:])
                        if not os.path.exists(local_path):
                            logger.warning(
                                "[%s] Skipping missing image in media group: %s",
                                self.name, local_path,
                            )
                            continue
                        fh = open(local_path, "rb")
                        opened_files.append(fh)
                        media.append(InputMediaPhoto(media=fh, caption=caption))
                    else:
                        media.append(InputMediaPhoto(media=image_url, caption=caption))

                if not media:
                    continue

                logger.info(
                    "[%s] Sending media group of %d photo(s) (chunk %d/%d)",
                    self.name, len(media), chunk_idx + 1, len(chunks),
                )
                reply_to_id = self._reply_to_message_id_for_send(None, metadata, reply_to_mode=self._reply_to_mode)
                thread_kwargs = self._thread_kwargs_for_send(
                    chat_id,
                    _thread,
                    metadata,
                    reply_to_message_id=reply_to_id,
                    reply_to_mode=self._reply_to_mode
                )

                def _reset_opened_files() -> None:
                    for fh in opened_files:
                        try:
                            fh.seek(0)
                        except Exception:
                            pass

                await self._send_with_dm_topic_reply_anchor_retry(
                    self._bot.send_media_group,
                    {
                        "chat_id": normalize_telegram_chat_id(chat_id),
                        "media": media,
                        "reply_to_message_id": reply_to_id,
                        "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                        **thread_kwargs,
                        **self._notification_kwargs(metadata),
                    },
                    metadata,
                    reply_to_id,
                    "media group",
                    reset_media=_reset_opened_files,
                )
            except Exception as e:
                logger.warning(
                    "[%s] send_media_group failed (chunk %d/%d), falling back to per-image: %s",
                    self.name, chunk_idx + 1, len(chunks), _redact_telegram_error_text(e),
                    exc_info=True,
                )
                # Fallback: send each photo in this chunk individually
                await super().send_multiple_images(
                    chat_id, chunk, metadata, human_delay=human_delay,
                )
            finally:
                for fh in opened_files:
                    try:
                        fh.close()
                    except Exception:
                        pass

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file natively as a Telegram photo."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(image_path):
                return SendResult(success=False, error=self._missing_media_path_error("Image", image_path))

            _thread = self._metadata_thread_id(metadata)
            reply_to_id = self._reply_to_message_id_for_send(reply_to, metadata, reply_to_mode=self._reply_to_mode)
            thread_kwargs = self._thread_kwargs_for_send(
                chat_id,
                _thread,
                metadata,
                reply_to_message_id=reply_to_id,
                reply_to_mode=self._reply_to_mode
            )
            with open(image_path, "rb") as image_file:
                msg = await self._send_with_dm_topic_reply_anchor_retry(
                    self._bot.send_photo,
                    {
                        "chat_id": normalize_telegram_chat_id(chat_id),
                        "photo": image_file,
                        "caption": caption[:1024] if caption else None,
                        "reply_to_message_id": reply_to_id,
                        "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                        **thread_kwargs,
                        **self._notification_kwargs(metadata),
                    },
                    metadata,
                    reply_to_id,
                    "photo",
                    reset_media=lambda: image_file.seek(0),
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            error_str = str(e)
            # Dimension-related errors are the expected case for valid image
            # files that Telegram just refuses as photos (screenshots, extreme
            # aspect ratios). Log at INFO because the document fallback is
            # the correct path. Any other send_photo failure also falls back
            # to document (rate limits, corrupt file markers, format edge
            # cases), but at WARNING because it's unexpected and worth
            # surfacing in logs.
            is_dim_error = (
                "Photo_invalid_dimensions" in error_str
                or "PHOTO_INVALID_DIMENSIONS" in error_str
            )
            if is_dim_error:
                logger.info(
                    "[%s] Image dimensions exceed Telegram photo limits, "
                    "sending as document: %s",
                    self.name,
                    image_path,
                )
            else:
                logger.warning(
                    "[%s] Failed to send Telegram local image as photo, "
                    "trying document fallback: %s",
                    self.name,
                    _redact_telegram_error_text(e),
                    exc_info=True,
                )
            # Fallback to sending as document (file) — no dimension limit,
            # only 50MB size limit. If even that fails, fall back to the
            # base adapter's text-only "Image: /path" rendering.
            try:
                return await self.send_document(
                    chat_id=chat_id,
                    file_path=image_path,
                    caption=caption,
                    file_name=os.path.basename(image_path),
                    reply_to=reply_to,
                    metadata=metadata,
                )
            except Exception as doc_err:
                logger.error(
                    "[%s] Failed to send Telegram local image as document, "
                    "falling back to base adapter: %s",
                    self.name,
                    doc_err,
                    exc_info=True,
                )
                return await super().send_image_file(chat_id, image_path, caption, reply_to, metadata=metadata)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a document/file natively as a Telegram file attachment."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(file_path):
                return SendResult(success=False, error=self._missing_media_path_error("File", file_path))

            display_name = file_name or os.path.basename(file_path)
            _thread = self._metadata_thread_id(metadata)
            reply_to_id = self._reply_to_message_id_for_send(reply_to, metadata, reply_to_mode=self._reply_to_mode)
            thread_kwargs = self._thread_kwargs_for_send(
                chat_id,
                _thread,
                metadata,
                reply_to_message_id=reply_to_id,
                reply_to_mode=self._reply_to_mode
            )

            with open(file_path, "rb") as f:
                msg = await self._send_with_dm_topic_reply_anchor_retry(
                    self._bot.send_document,
                    {
                        "chat_id": normalize_telegram_chat_id(chat_id),
                        "document": f,
                        "filename": display_name,
                        "caption": caption[:1024] if caption else None,
                        "reply_to_message_id": reply_to_id,
                        "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                        **thread_kwargs,
                        **self._notification_kwargs(metadata),
                    },
                    metadata,
                    reply_to_id,
                    "document",
                    reset_media=lambda: f.seek(0),
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning(
                "[%s] Failed to send document: %s",
                self.name, _redact_telegram_error_text(e),
            )
            return await super().send_document(chat_id, file_path, caption, file_name, reply_to, metadata=metadata)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Send a video natively as a Telegram video message."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        try:
            if not os.path.exists(video_path):
                return SendResult(success=False, error=self._missing_media_path_error("Video", video_path))

            _thread = self._metadata_thread_id(metadata)
            reply_to_id = self._reply_to_message_id_for_send(reply_to, metadata, reply_to_mode=self._reply_to_mode)
            thread_kwargs = self._thread_kwargs_for_send(
                chat_id,
                _thread,
                metadata,
                reply_to_message_id=reply_to_id,
                reply_to_mode=self._reply_to_mode
            )
            with open(video_path, "rb") as f:
                msg = await self._send_with_dm_topic_reply_anchor_retry(
                    self._bot.send_video,
                    {
                        "chat_id": normalize_telegram_chat_id(chat_id),
                        "video": f,
                        "caption": caption[:1024] if caption else None,
                        "reply_to_message_id": reply_to_id,
                        "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                        **thread_kwargs,
                        **self._notification_kwargs(metadata),
                    },
                    metadata,
                    reply_to_id,
                    "video",
                    reset_media=lambda: f.seek(0),
                )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning(
                "[%s] Failed to send video: %s",
                self.name, _redact_telegram_error_text(e),
            )
            return await super().send_video(chat_id, video_path, caption, reply_to, metadata=metadata)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively as a Telegram photo.
        
        Tries URL-based send first (fast, works for <5MB images).
        Falls back to downloading and uploading as file (supports up to 10MB).
        """
        if not self._bot:
            return SendResult(success=False, error="Not connected")

        from tools.url_safety import is_safe_url
        if not is_safe_url(image_url):
            logger.warning("[%s] Blocked unsafe image URL (SSRF protection)", self.name)
            return await super().send_image(chat_id, image_url, caption, reply_to, metadata=metadata)

        try:
            # Telegram can send photos directly from URLs (up to ~5MB)
            _photo_thread = self._metadata_thread_id(metadata)
            reply_to_id = self._reply_to_message_id_for_send(reply_to, metadata, reply_to_mode=self._reply_to_mode)
            photo_thread_kwargs = self._thread_kwargs_for_send(
                chat_id,
                _photo_thread,
                metadata,
                reply_to_message_id=reply_to_id,
                reply_to_mode=self._reply_to_mode
            )
            msg = await self._send_with_dm_topic_reply_anchor_retry(
                self._bot.send_photo,
                {
                    "chat_id": normalize_telegram_chat_id(chat_id),
                    "photo": image_url,
                    "caption": caption[:1024] if caption else None,
                    "reply_to_message_id": reply_to_id,
                    "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                    **photo_thread_kwargs,
                    **self._notification_kwargs(metadata),
                },
                metadata,
                reply_to_id,
                "URL photo",
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.warning(
                "[%s] URL-based send_photo failed, trying file upload: %s",
                self.name,
                _redact_telegram_error_text(e),
                exc_info=True,
            )
            # Fallback: download and upload as file (supports up to 10MB)
            try:
                from gateway.platforms.base import _ssrf_redirect_guard
                from tools.url_safety import create_ssrf_safe_async_client

                async with create_ssrf_safe_async_client(
                    timeout=30.0,
                    event_hooks={"response": [_ssrf_redirect_guard]},
                ) as client:
                    resp = await client.get(image_url)
                    resp.raise_for_status()
                    image_data = resp.content

                upload_thread_kwargs = self._thread_kwargs_for_send(
                    chat_id,
                    _photo_thread,
                    metadata,
                    reply_to_message_id=reply_to_id,
                    reply_to_mode=self._reply_to_mode
                )
                msg = await self._send_with_dm_topic_reply_anchor_retry(
                    self._bot.send_photo,
                    {
                        "chat_id": normalize_telegram_chat_id(chat_id),
                        "photo": image_data,
                        "caption": caption[:1024] if caption else None,
                        "reply_to_message_id": reply_to_id,
                        "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                        **upload_thread_kwargs,
                        **self._notification_kwargs(metadata),
                    },
                    metadata,
                    reply_to_id,
                    "uploaded photo",
                )
                return SendResult(success=True, message_id=str(msg.message_id))
            except Exception as e2:
                logger.error(
                    "[%s] File upload send_photo also failed: %s",
                    self.name,
                    e2,
                    exc_info=True,
                )
                # Final fallback: send URL as text
                return await super().send_image(chat_id, image_url, caption, reply_to, metadata=metadata)

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an animated GIF natively as a Telegram animation (auto-plays inline)."""
        if not self._bot:
            return SendResult(success=False, error="Not connected")
        
        try:
            _anim_thread = self._metadata_thread_id(metadata)
            reply_to_id = self._reply_to_message_id_for_send(reply_to, metadata, reply_to_mode=self._reply_to_mode)
            animation_thread_kwargs = self._thread_kwargs_for_send(
                chat_id,
                _anim_thread,
                metadata,
                reply_to_message_id=reply_to_id,
                reply_to_mode=self._reply_to_mode
            )
            msg = await self._send_with_dm_topic_reply_anchor_retry(
                self._bot.send_animation,
                {
                    "chat_id": normalize_telegram_chat_id(chat_id),
                    "animation": animation_url,
                    "caption": caption[:1024] if caption else None,
                    "reply_to_message_id": reply_to_id,
                    "read_timeout": _MEDIA_SEND_READ_TIMEOUT,
                    **animation_thread_kwargs,
                    **self._notification_kwargs(metadata),
                },
                metadata,
                reply_to_id,
                "animation",
            )
            return SendResult(success=True, message_id=str(msg.message_id))
        except Exception as e:
            logger.error(
                "[%s] Failed to send Telegram animation, falling back to photo: %s",
                self.name,
                _redact_telegram_error_text(e),
                exc_info=True,
            )
            # Fallback: try as a regular photo
            return await self.send_image(chat_id, animation_url, caption, reply_to, metadata=metadata)

    @staticmethod
    def _is_transient_typing_error(exc: Exception) -> bool:
        """Return True for Telegram typing errors worth cooling down."""
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            return True

        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if isinstance(status_code, int) and (status_code == 429 or status_code >= 500):
            return True

        text = str(exc).lower()
        if any(marker in text for marker in ("too many requests", "rate limit", "timed out", "timeout", "temporar")):
            return True
        if isinstance(exc, (OSError, TimeoutError, ConnectionError, asyncio.TimeoutError)):
            return True
        return False

    def _record_typing_cooldown(self, chat_id: str, exc: Exception) -> None:
        """Suppress Telegram typing refreshes for this chat after transient failures."""
        if not hasattr(self, "_telegram_typing_cooldown_until"):
            self._telegram_typing_cooldown_until = {}
        loop = asyncio.get_running_loop()
        retry_after = getattr(exc, "retry_after", None)
        try:
            delay = float(retry_after) if retry_after is not None else self._telegram_typing_cooldown_seconds
        except (TypeError, ValueError):
            delay = self._telegram_typing_cooldown_seconds
        delay = max(1.0, min(delay, 300.0))
        self._telegram_typing_cooldown_until[str(chat_id)] = loop.time() + delay

    def _typing_in_cooldown(self, chat_id: str) -> bool:
        if not hasattr(self, "_telegram_typing_cooldown_until"):
            self._telegram_typing_cooldown_until = {}
            self._telegram_typing_cooldown_seconds = 30.0
        until = self._telegram_typing_cooldown_until.get(str(chat_id))
        if until is None:
            return False
        if asyncio.get_running_loop().time() < until:
            return True
        self._telegram_typing_cooldown_until.pop(str(chat_id), None)
        return False

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Send typing indicator."""
        if not self._bot or self._typing_in_cooldown(chat_id):
            return

        _is_dm_topic: bool = False
        message_thread_id: Optional[int] = None
        try:
            _typing_thread = self._metadata_thread_id(metadata)
            _is_dm_topic = bool(metadata and metadata.get("telegram_dm_topic_reply_fallback"))
            message_thread_id = self._message_thread_id_for_typing(_typing_thread)
            await self._bot.send_chat_action(
                chat_id=normalize_telegram_chat_id(chat_id),
                action="typing",
                message_thread_id=message_thread_id,
            )
            self._telegram_typing_cooldown_until.pop(str(chat_id), None)
        except Exception as e:
            # For DM topic lanes, Telegram may reject message_thread_id.
            # Fall back to sending typing without thread_id so the typing
            # indicator at least appears in the main DM view.
            if _is_dm_topic and message_thread_id is not None:
                try:
                    await self._bot.send_chat_action(
                        chat_id=normalize_telegram_chat_id(chat_id),
                        action="typing",
                    )
                    self._telegram_typing_cooldown_until.pop(str(chat_id), None)
                    return
                except Exception as fallback_exc:
                    if self._is_transient_typing_error(fallback_exc):
                        self._record_typing_cooldown(chat_id, fallback_exc)
            elif self._is_transient_typing_error(e):
                self._record_typing_cooldown(chat_id, e)
            # Typing failures are non-fatal; log at debug level only.
            logger.debug(
                "[%s] Failed to send Telegram typing indicator: %s",
                self.name,
                _redact_telegram_error_text(e),
                exc_info=True,
            )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a Telegram chat."""
        if not self._bot:
            return {"name": "Unknown", "type": "dm"}
        
        try:
            chat = await self._bot.get_chat(normalize_telegram_chat_id(chat_id))
            
            chat_type = "dm"
            if chat.type == ChatType.GROUP:
                chat_type = "group"
            elif chat.type == ChatType.SUPERGROUP:
                chat_type = "group"
                if chat.is_forum:
                    chat_type = "forum"
            elif chat.type == ChatType.CHANNEL:
                chat_type = "channel"
            
            return {
                "name": chat.title or chat.full_name or str(chat_id),
                "type": chat_type,
                "username": chat.username,
                "is_forum": getattr(chat, "is_forum", False),
            }
        except Exception as e:
            logger.error(
                "[%s] Failed to get Telegram chat info for %s: %s",
                self.name,
                chat_id,
                _redact_telegram_error_text(e),
                exc_info=True,
            )
            return {"name": str(chat_id), "type": "dm", "error": str(e)}

    def _observe_unmentioned_group_message(
        self,
        message: Message,
        msg_type: MessageType,
        update_id: Optional[int] = None,
        event: Optional[MessageEvent] = None,
    ) -> None:
        """Append skipped group chatter to the target session without dispatching."""
        store = getattr(self, "_session_store", None)
        if not store:
            return
        try:
            event = event or self._build_message_event(message, msg_type, update_id=update_id)
            shared_source = self._telegram_group_observe_shared_source(event.source)
            session_entry = store.get_or_create_session(shared_source)
            entry = {
                "role": "user",
                "content": self._telegram_group_observe_attributed_text(event),
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "observed": True,
            }
            if event.message_id:
                entry["message_id"] = str(event.message_id)
            store.append_to_transcript(session_entry.session_id, entry)
            adapter_name = getattr(self, "name", "telegram")
            logger.info(
                "[%s] Telegram group message observed (no bot trigger): chat=%s from=%s",
                adapter_name,
                getattr(getattr(message, "chat", None), "id", "unknown"),
                event.source.user_id or "unknown",
            )
        except Exception as exc:
            adapter_name = getattr(self, "name", "telegram")
            logger.warning("[%s] Failed to observe Telegram group message: %s", adapter_name, exc)

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

    def _reactions_enabled(self) -> bool:
        """Check if message reactions are enabled via config/env."""
        return os.getenv("TELEGRAM_REACTIONS", "false").lower() not in {"false", "0", "no"}

    async def _set_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Set a single emoji reaction on a Telegram message."""
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=normalize_telegram_chat_id(chat_id),
                message_id=int(message_id),
                reaction=emoji,
            )
            return True
        except Exception as e:
            logger.debug("[%s] set_message_reaction failed (%s): %s", self.name, emoji, _redact_telegram_error_text(e))
            return False

    async def _clear_reactions(self, chat_id: str, message_id: str) -> bool:
        """Clear all reactions from a Telegram message.

        Calling ``set_message_reaction`` with ``reaction=None`` (or an empty
        sequence) is the documented Bot API way to remove all bot-set
        reactions on a message — equivalent to Bot API 10.0's
        ``deleteMessageReaction`` but supported in PTB 22.6 already.
        """
        if not self._bot:
            return False
        try:
            await self._bot.set_message_reaction(
                chat_id=normalize_telegram_chat_id(chat_id),
                message_id=int(message_id),
                reaction=None,
            )
            return True
        except Exception as e:
            logger.debug("[%s] clear reactions failed: %s", self.name, _redact_telegram_error_text(e))
            return False

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an in-progress reaction when message processing begins."""
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if chat_id and message_id:
            await self._set_reaction(chat_id, message_id, "\U0001f440")

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        """Swap the in-progress reaction for a final success/failure reaction.

        Unlike Discord (additive reactions), Telegram's set_message_reaction
        replaces all existing reactions in one call — no remove step needed.

        On CANCELLED outcomes (e.g. the user runs ``/stop``, or a session is
        interrupted mid-flight), we explicitly clear the 👀 in-progress
        reaction so it doesn't linger on the user's message indefinitely.
        Without this clear, the only way to remove the 👀 was to wait for
        another agent run to swap it to 👍/👎 — which never happens if the
        cancellation was the last activity in the chat.
        """
        if not self._reactions_enabled():
            return
        chat_id = getattr(event.source, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if not (chat_id and message_id):
            return
        if outcome == ProcessingOutcome.CANCELLED:
            await self._clear_reactions(chat_id, message_id)
        else:
            await self._set_reaction(
                chat_id,
                message_id,
                "\U0001f44d" if outcome == ProcessingOutcome.SUCCESS else "\U0001f44e",
            )


# ──────────────────────────────────────────────────────────────────────────
# Plugin migration glue (#41112 / #3823)
#
# Added when the Telegram adapter (+ its telegram_network satellite) moved from
# gateway/platforms/ into this bundled plugin. Mirrors the Discord (#24356) /
# Slack migrations: a register(ctx) entry point plus hook implementations that
# replace the per-platform core touchpoints (the Platform.TELEGRAM branch in
# gateway/run.py, the telegram_cfg YAML→env/extra block in gateway/config.py,
# the _setup_telegram wizard + _PLATFORMS["telegram"] static dict in
# hermes_cli/{setup,gateway}.py, and the _send_telegram dispatch in
# tools/send_message_tool.py).  Telegram uses the generic token connected
# check, so no is_connected override is needed.
# ──────────────────────────────────────────────────────────────────────────


def _resolve_notifications_mode() -> str:
    """Resolve the Telegram notification mode (all/important) from env or
    config.yaml display.platforms.telegram.notifications, defaulting to
    'important'.  Mirrors the post-construction logic that used to live in
    gateway/run.py::_create_adapter()."""
    mode = os.getenv("HERMES_TELEGRAM_NOTIFICATIONS", "")
    if not mode:
        try:
            from gateway.config import load_gateway_config
            from gateway.run import cfg_get
            _gw_cfg = load_gateway_config()
            _raw = cfg_get(_gw_cfg, "display", "platforms", "telegram", "notifications")
            if _raw not in {None, ""}:
                mode = str(_raw).strip().lower()
        except Exception:
            pass
    mode = mode or "important"
    if mode not in {"all", "important"}:
        logger.warning(
            "Unknown telegram notifications mode '%s', defaulting to 'important' "
            "(valid: all, important)", mode,
        )
        mode = "important"
    return mode


def _build_adapter(config):
    """Factory wrapper that constructs TelegramAdapter and applies the
    notification mode (preserving the gateway/run.py post-construction step)."""
    adapter = TelegramAdapter(config)
    try:
        adapter._notifications_mode = _resolve_notifications_mode()
    except Exception:
        adapter._notifications_mode = "important"
    return adapter


def _is_connected(config) -> bool:
    """Telegram is connected when a bot token is configured.

    check_telegram_requirements() only verifies the python-telegram-bot SDK is
    importable, NOT that a token is set — so without this is_connected the
    registry-driven plugin-enable pass in gateway/config.py would enable
    Telegram on any machine that merely has the SDK installed. Gate on the
    token (env or PlatformConfig.token), matching the generic token check
    Telegram had as a built-in.
    """
    token = getattr(config, "token", None)
    if not token:
        import hermes_cli.gateway as gateway_mod
        token = gateway_mod.get_env_value("TELEGRAM_BOT_TOKEN") or ""
    return bool(str(token).strip())


async def _standalone_send(
    pconfig,
    chat_id,
    message,
    *,
    thread_id=None,
    media_files=None,
    force_document=False,
):
    """Out-of-process Telegram delivery. Delegates to the standalone
    ``_send_telegram`` REST sender in tools/send_message_tool.py (which already
    handles chunking-agnostic single sends, threads, media, retries, and
    parse-mode fallback). Implements the standalone_sender_fn contract so
    deliver=telegram cron jobs succeed when cron runs separately from the
    gateway."""
    token = getattr(pconfig, "token", None)
    if not token:
        # Profile-scoped read: honor the secret scope's verdict rather than
        # borrowing another profile's env-bridged token under multiplex.
        from agent.secret_scope import get_secret

        token = get_secret("TELEGRAM_BOT_TOKEN", "") or ""
    disable_link_previews = bool(
        getattr(pconfig, "extra", {}) and pconfig.extra.get("disable_link_previews")
    )
    from tools.send_message_tool import _send_telegram
    return await _send_telegram(
        token,
        chat_id,
        message,
        media_files=media_files,
        thread_id=thread_id,
        disable_link_previews=disable_link_previews,
        force_document=force_document,
    )


def interactive_setup() -> None:
    """Configure Telegram bot credentials and allowlist.

    Delegates to the existing CLI setup helpers (managed-bot QR onboarding,
    token validation, allowlist capture) via lazy import so the full wizard
    behavior is preserved without duplicating ~150 lines. Replaces the
    _PLATFORMS["telegram"] static dict dispatch in hermes_cli/gateway.py.
    """
    from hermes_cli import setup as _setup_mod
    _setup_mod._setup_telegram()


def _apply_yaml_config(yaml_cfg: dict, telegram_cfg: dict) -> dict | None:
    """Translate config.yaml telegram: keys into TELEGRAM_* env vars and
    PlatformConfig.extra entries.

    Implements the apply_yaml_config_fn contract (#24849). Mirrors the legacy
    telegram_cfg block from gateway/config.py::load_gateway_config(). Env vars
    take precedence over YAML. Returns a dict of extras to merge into
    PlatformConfig.extra (disable_topic_auto_rename + runtime flags), or None.
    """
    import json as _json
    extras: dict = {}

    # Under multiplex, a secondary profile's config loads inside its runtime
    # scope; its authorization gate values must NOT be written to the
    # process-global env, where first-writer-wins would pin them for every
    # other profile (issue #72348 Telegram mirror). They are seeded into
    # PlatformConfig.extra / read via the profile secret scope instead.
    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active

        _skip_env_bridge = bool(is_multiplex_active() and current_secret_scope() is not None)
    except Exception:
        _skip_env_bridge = False

    if "disable_topic_auto_rename" in telegram_cfg:
        extras.setdefault("disable_topic_auto_rename", telegram_cfg["disable_topic_auto_rename"])

    _effective_rm = telegram_cfg.get("require_mention", yaml_cfg.get("require_mention"))
    if _effective_rm is not None and not os.getenv("TELEGRAM_REQUIRE_MENTION"):
        os.environ["TELEGRAM_REQUIRE_MENTION"] = str(_effective_rm).lower()
    if "mention_patterns" in telegram_cfg and not os.getenv("TELEGRAM_MENTION_PATTERNS"):
        os.environ["TELEGRAM_MENTION_PATTERNS"] = _json.dumps(telegram_cfg["mention_patterns"])
    if "exclusive_bot_mentions" in telegram_cfg and not os.getenv("TELEGRAM_EXCLUSIVE_BOT_MENTIONS"):
        os.environ["TELEGRAM_EXCLUSIVE_BOT_MENTIONS"] = str(telegram_cfg["exclusive_bot_mentions"]).lower()
    if "allow_bots" in telegram_cfg and not os.getenv("TELEGRAM_ALLOW_BOTS"):
        os.environ["TELEGRAM_ALLOW_BOTS"] = str(telegram_cfg["allow_bots"]).lower()
    if "guest_mode" in telegram_cfg and not os.getenv("TELEGRAM_GUEST_MODE"):
        os.environ["TELEGRAM_GUEST_MODE"] = str(telegram_cfg["guest_mode"]).lower()
    if "observe_unmentioned_group_messages" in telegram_cfg and not os.getenv("TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES"):
        os.environ["TELEGRAM_OBSERVE_UNMENTIONED_GROUP_MESSAGES"] = str(telegram_cfg["observe_unmentioned_group_messages"]).lower()
    frc = telegram_cfg.get("free_response_chats")
    if frc is not None:
        extras.setdefault("free_response_chats", frc)
        if isinstance(frc, list):
            frc = ",".join(str(v) for v in frc)
        if not _skip_env_bridge and not os.getenv("TELEGRAM_FREE_RESPONSE_CHATS"):
            os.environ["TELEGRAM_FREE_RESPONSE_CHATS"] = str(frc)
    frt = telegram_cfg.get("free_response_topics")
    if frt is not None:
        if isinstance(frt, list):
            frt = ",".join(str(v) for v in frt)
        if not _skip_env_bridge and not os.getenv("TELEGRAM_FREE_RESPONSE_TOPICS"):
            os.environ["TELEGRAM_FREE_RESPONSE_TOPICS"] = str(frt)
    ac = telegram_cfg.get("allowed_chats")
    if ac is not None:
        if isinstance(ac, list):
            ac = ",".join(str(v) for v in ac)
        # NOTE: no extras seed here — gateway/config.py's shared-key loop
        # already bridges ``allowed_chats`` into PlatformConfig.extra with its
        # original type, and the apply_yaml_config merge would clobber it.
        if not _skip_env_bridge and not os.getenv("TELEGRAM_ALLOWED_CHATS"):
            os.environ["TELEGRAM_ALLOWED_CHATS"] = str(ac)
    allowed_topics = telegram_cfg.get("allowed_topics")
    if allowed_topics is not None:
        if isinstance(allowed_topics, list):
            allowed_topics = ",".join(str(v) for v in allowed_topics)
        # extras seed intentionally omitted (shared-key loop bridges allowed_topics).
        if not _skip_env_bridge and not os.getenv("TELEGRAM_ALLOWED_TOPICS"):
            os.environ["TELEGRAM_ALLOWED_TOPICS"] = str(allowed_topics)
    ignored_threads = telegram_cfg.get("ignored_threads")
    if ignored_threads is not None:
        extras.setdefault("ignored_threads", ignored_threads)
        if isinstance(ignored_threads, list):
            ignored_threads = ",".join(str(v) for v in ignored_threads)
        if not _skip_env_bridge and not os.getenv("TELEGRAM_IGNORED_THREADS"):
            os.environ["TELEGRAM_IGNORED_THREADS"] = str(ignored_threads)
    if "reactions" in telegram_cfg and not os.getenv("TELEGRAM_REACTIONS"):
        os.environ["TELEGRAM_REACTIONS"] = str(telegram_cfg["reactions"]).lower()
    if "proxy_url" in telegram_cfg and not os.getenv("TELEGRAM_PROXY"):
        os.environ["TELEGRAM_PROXY"] = str(telegram_cfg["proxy_url"]).strip()
    _telegram_extra = telegram_cfg.get("extra") if isinstance(telegram_cfg.get("extra"), dict) else {}
    _telegram_rtm = (
        telegram_cfg["reply_to_mode"] if "reply_to_mode" in telegram_cfg
        else _telegram_extra.get("reply_to_mode")
    )
    if _telegram_rtm is not None and not os.getenv("TELEGRAM_REPLY_TO_MODE"):
        _rtm_str = "off" if _telegram_rtm is False else str(_telegram_rtm).lower()
        os.environ["TELEGRAM_REPLY_TO_MODE"] = _rtm_str
    allowed_users = telegram_cfg.get("allow_from")
    if allowed_users is not None:
        if isinstance(allowed_users, list):
            allowed_users = ",".join(str(v) for v in allowed_users)
        if not _skip_env_bridge and not os.getenv("TELEGRAM_ALLOWED_USERS"):
            os.environ["TELEGRAM_ALLOWED_USERS"] = str(allowed_users)
    group_allowed_users = telegram_cfg.get("group_allow_from") or _telegram_extra.get("group_allow_from")
    if group_allowed_users is not None:
        if isinstance(group_allowed_users, list):
            group_allowed_users = ",".join(str(v) for v in group_allowed_users)
        if not _skip_env_bridge and not os.getenv("TELEGRAM_GROUP_ALLOWED_USERS"):
            os.environ["TELEGRAM_GROUP_ALLOWED_USERS"] = str(group_allowed_users)
    group_allowed_chats = telegram_cfg.get("group_allowed_chats") or _telegram_extra.get("group_allowed_chats")
    if group_allowed_chats is not None:
        if isinstance(group_allowed_chats, list):
            group_allowed_chats = ",".join(str(v) for v in group_allowed_chats)
        # extras seed intentionally omitted (shared-key loop bridges group_allowed_chats).
        if not _skip_env_bridge and not os.getenv("TELEGRAM_GROUP_ALLOWED_CHATS"):
            os.environ["TELEGRAM_GROUP_ALLOWED_CHATS"] = str(group_allowed_chats)
    for _key in ("guest_mode", "disable_link_previews", "observe_unmentioned_group_messages", "free_response_topics"):
        if _key in telegram_cfg:
            extras.setdefault(_key, telegram_cfg[_key])
    # Pass through telegram-specific extra keys (e.g. base_url proxy override),
    # but EXCLUDE the generic shared-config keys that _merge_platform_map in
    # gateway/config.py already merges with correct top-level-over-nested
    # precedence. The apply_yaml_config_fn dispatch merges our return via
    # dict.update() (clobber), so re-emitting those generic keys here would
    # undo that precedence (top-level losing to a nested-fallback block).
    _GENERIC_MERGE_KEYS = {
        "reply_prefix", "reply_in_thread", "reply_to_mode",
        "unauthorized_dm_behavior", "notice_delivery", "require_mention",
        "channel_skill_bindings", "channel_prompts", "gateway_restart_notification",
        "allow_from", "allow_admin_from", "dm_policy", "group_policy",
    }
    for _k, _v in _telegram_extra.items():
        if _k not in _GENERIC_MERGE_KEYS:
            extras.setdefault(_k, _v)

    return extras or None


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="telegram",
        label="Telegram",
        adapter_factory=_build_adapter,
        check_fn=check_telegram_requirements,
        is_connected=_is_connected,
        required_env=["TELEGRAM_BOT_TOKEN"],
        install_hint="Run `hermes setup` to install Telegram support.",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="TELEGRAM_ALLOWED_USERS",
        allow_all_env="TELEGRAM_ALLOW_ALL_USERS",
        cron_deliver_env_var="TELEGRAM_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4096,
        emoji="✈️",
        allow_update_command=True,
    )
