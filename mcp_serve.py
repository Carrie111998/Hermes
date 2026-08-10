"""
Hermes MCP Server â€” expose messaging conversations as MCP tools.

Starts a stdio MCP server that lets any MCP client (Claude Code, Cursor, Codex,
etc.) list conversations, read message history, send messages, poll for live
events, and manage approval requests across all connected platforms.

Matches OpenClaw's 9-tool MCP channel bridge surface:
  conversations_list, conversation_get, messages_read, attachments_fetch,
  events_poll, events_wait, messages_send, permissions_list_open,
  permissions_respond

Plus: channels_list (Hermes-specific extra)

Usage:
    hermes mcp serve
    hermes mcp serve --verbose

MCP client config (e.g. claude_desktop_config.json):
    {
        "mcpServers": {
            "hermes": {
                "command": "hermes",
                "args": ["mcp", "serve"]
            }
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("hermes.mcp_serve")

# ---------------------------------------------------------------------------
# Lazy MCP SDK import
# ---------------------------------------------------------------------------

_MCP_SERVER_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP

    _MCP_SERVER_AVAILABLE = True
except ImportError:
    FastMCP = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_sessions_dir() -> Path:
    """Return the sessions directory using HERMES_HOME."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "sessions"
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "sessions"


def _get_session_db():
    """Get a SessionDB instance for reading message transcripts."""
    try:
        from hermes_state import SessionDB
        return SessionDB()
    except Exception as e:
        logger.debug("SessionDB unavailable: %s", e)
        return None


def _load_session_messages(session_id: str):
    """Read one session and close the temporary database handle."""
    db = _get_session_db()
    if db is None:
        return None, "Session database unavailable"
    try:
        return db.get_messages(session_id), None
    except Exception as e:
        return None, f"Failed to read messages: {e}"
    finally:
        try:
            db.close()
        except Exception:
            logger.debug("Failed to close MCP SessionDB", exc_info=True)


def _load_sessions_index() -> dict:
    """Load the gateway session routing index.

    Returns a dict of session_key -> entry_dict with platform routing info.

    state.db is the primary source (#9006): gateway sessions persist their
    routing metadata (session_key, chat/thread ids, display_name, origin) on
    the durable session row, so a single database read replaces the old
    dual-file sessions.json dependency.  Falls back to sessions.json for
    pre-migration databases where no gateway rows carry a session_key yet.
    """
    entries = _load_sessions_index_from_db()
    if entries:
        return entries
    return _load_sessions_index_from_json()


def _row_to_index_entry(row: dict) -> dict:
    """Convert a state.db gateway session row to the sessions.json entry shape."""
    origin = {}
    origin_json = row.get("origin_json")
    if origin_json:
        try:
            parsed = json.loads(origin_json)
            if isinstance(parsed, dict):
                origin = parsed
        except (TypeError, ValueError):
            pass
    if not origin:
        # Pre-origin_json rows: synthesize the minimal origin from columns.
        origin = {
            "platform": row.get("source", ""),
            "chat_id": row.get("chat_id"),
            "chat_type": row.get("chat_type"),
            "thread_id": row.get("thread_id"),
            "user_id": row.get("user_id"),
        }

    def _iso(ts) -> str:
        try:
            return datetime.fromtimestamp(float(ts)).isoformat() if ts else ""
        except (TypeError, ValueError, OSError):
            return ""

    input_tokens = int(row.get("input_tokens") or 0)
    output_tokens = int(row.get("output_tokens") or 0)
    return {
        "session_id": str(row.get("id", "")),
        "session_key": row.get("session_key", ""),
        "platform": row.get("source", ""),
        "chat_type": row.get("chat_type") or origin.get("chat_type", ""),
        "display_name": row.get("display_name") or origin.get("chat_name") or "",
        "origin": origin,
        "created_at": _iso(row.get("started_at")),
        "updated_at": _iso(row.get("last_active") or row.get("started_at")),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _load_sessions_index_from_db() -> dict:
    """Build the routing index from state.db gateway session rows."""
    db = _get_session_db()
    if db is None:
        return {}
    try:
        lister = getattr(db, "list_gateway_sessions", None)
        if not callable(lister):
            return {}
        rows = lister(active_only=True)
        entries = {}
        for row in rows:
            key = row.get("session_key")
            if not key:
                continue
            entries[key] = _row_to_index_entry(row)
        return entries
    except Exception as e:
        logger.debug("Failed to load gateway sessions from state.db: %s", e)
        return {}
    finally:
        try:
            db.close()
        except Exception:
            pass


def _load_sessions_index_from_json() -> dict:
    """Legacy fallback: load the gateway sessions.json index directly.

    Used only for pre-migration databases whose gateway rows don't carry a
    session_key yet.  This avoids importing the full SessionStore which
    needs GatewayConfig.
    """
    sessions_file = _get_sessions_dir() / "sessions.json"
    if not sessions_file.exists():
        return {}
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Drop documentation/metadata sentinels (keys starting with "_", e.g.
        # the "_README" note the gateway writes into the index). They are not
        # session entries and would break consumers that treat every value as
        # an entry dict.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if not str(k).startswith("_")}
        return {}
    except Exception as e:
        logger.debug("Failed to load sessions.json: %s", e)
        return {}


def _load_channel_directory() -> dict:
    """Load the cached channel directory for available targets."""
    try:
        from hermes_constants import get_hermes_home
        directory_file = get_hermes_home() / "channel_directory.json"
    except ImportError:
        directory_file = Path(
            os.environ.get("HERMES_HOME", Path.home() / ".hermes")
        ) / "channel_directory.json"

    if not directory_file.exists():
        return {}
    try:
        with open(directory_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug("Failed to load channel_directory.json: %s", e)
        return {}


def _coerce_int(
    value,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Coerce value to int with fallback and clamping.

    Used at MCP tool boundaries to handle invalid types from external clients.
    Returns default if value cannot be converted to int.
    """
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = default
    return max(minimum, min(coerced, maximum))


def _extract_message_content(msg: dict) -> str:
    """Extract text content from a message, handling multi-part content."""
    content = msg.get("content", "")
    if isinstance(content, list):
        text_parts = [
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(text_parts)
    return str(content) if content else ""


def _extract_attachments(msg: dict) -> List[dict]:
    """Extract non-text attachments from a message.

    Finds: multi-part image/file content blocks, MEDIA: tags in text,
    image URLs, and file references.
    """
    attachments = []
    content = msg.get("content", "")

    # Multi-part content blocks (image_url, file, etc.)
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype == "image_url":
                url = part.get("image_url", {}).get("url", "") if isinstance(part.get("image_url"), dict) else ""
                if url:
                    attachments.append({"type": "image", "url": url})
            elif ptype == "image":
                url = part.get("url", part.get("source", {}).get("url", ""))
                if url:
                    attachments.append({"type": "image", "url": url})
            elif ptype not in {"text",}:
                # Unknown non-text content type
                attachments.append({"type": ptype, "data": part})

    # MEDIA: tags in text content
    text = _extract_message_content(msg)
    if text:
        media_pattern = re.compile(r'MEDIA:\s*(\S+)')
        for match in media_pattern.finditer(text):
            path = match.group(1)
            attachments.append({"type": "media", "path": path})

    return attachments


# ---------------------------------------------------------------------------
# Event Bridge â€” polls SessionDB for new messages, maintains event queue
# ---------------------------------------------------------------------------

QUEUE_LIMIT = 1000
POLL_INTERVAL = 0.2  # seconds between DB polls (200ms)


@dataclass
class QueueEvent:
    """An event in the bridge's in-memory queue."""
    cursor: int
    type: str  # "message", "approval_requested", "approval_resolved"
    session_key: str = ""
    data: dict = field(default_factory=dict)


def _ts_float(ts) -> float:
    """Normalize a message timestamp (epoch int/float or ISO string) to float."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts:
        try:
            return float(ts)
        except ValueError:
            try:
                return datetime.fromisoformat(ts).timestamp()
            except Exception:
                return 0.0
    return 0.0


class EventBridge:
    """Background poller that watches SessionDB for new messages and
    maintains an in-memory event queue with waiter support.

    This is the Hermes equivalent of OpenClaw's WebSocket gateway bridge.
    Instead of WebSocket events, we poll the SQLite database for changes.
    """

    def __init__(self):
        self._queue: List[QueueEvent] = []
        self._cursor = 0
        self._lock = threading.Lock()
        self._new_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_poll_timestamps: Dict[str, float] = {}  # session_key -> unix timestamp
        # In-memory approval tracking (populated from events)
        self._pending_approvals: Dict[str, dict] = {}
        # mtime cache â€” skip expensive work when state.db hasn't changed
        self._state_db_mtime: float = 0.0
        self._cached_sessions_index: dict = {}

    def start(self):
        """Start the background polling thread."""
        if self._running:
            return
        # Snapshot existing history BEFORE the poll loop starts so pre-existing
        # messages are not replayed as new events on startup (#13414). Sessions
        # that first appear afterwards are absent from the baseline and default
        # to last_seen=0.0 in _poll_once, so new-conversation delivery is
        # preserved. Unit tests that drive _poll_once directly bypass start()
        # and still observe first-poll delivery.
        self._establish_baseline()
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.debug("EventBridge started")

    def stop(self):
        """Stop the background polling thread."""
        self._running = False
        self._new_event.set()  # Wake any waiters
        if self._thread:
            self._thread.join(timeout=5)
        logger.debug("EventBridge stopped")

    def poll_events(
        self,
        after_cursor: int = 0,
        session_key: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Return events since after_cursor, optionally filtered by session_key."""
        with self._lock:
            events = [
                e for e in self._queue
                if e.cursor > after_cursor
                and (not session_key or e.session_key == session_key)
            ][:limit]

        next_cursor = events[-1].cursor if events else after_cursor
        return {
            "events": [
                {"cursor": e.cursor, "type": e.type,
                 "session_key": e.session_key, **e.data}
                for e in events
            ],
            "next_cursor": next_cursor,
        }

    def wait_for_event(
        self,
        after_cursor: int = 0,
        session_key: Optional[str] = None,
        timeout_ms: int = 30000,
    ) -> Optional[dict]:
        """Block until a matching event arrives or timeout expires."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)

        while time.monotonic() < deadline:
            with self._lock:
                for e in self._queue:
                    if e.cursor > after_cursor and (
                        not session_key or e.session_key == session_key
                    ):
                        return {
                            "cursor": e.cursor, "type": e.type,
                            "session_key": e.session_key, **e.data,
                        }

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._new_event.clear()
            self._new_event.wait(timeout=min(remaining, POLL_INTERVAL))

        return None

    def list_pending_approvals(self) -> List[dict]Ûßw¶‰žËkºwµçqÑ•È½¹Ù•ÉÍ…Ñ¥½¹Ì‰ä¹…µ”4(€€€€€€€€ˆˆˆ4(€€€€€€€±¥µ¥Ð€ô}½•É•}¥¹Ð¡±¥µ¥Ð°‘•™…Õ±ÐôÔÀ°µ¥¹¥µÕ´ôÄ°µ…á¥µÕ´ôÈÀÀ¤4(€€€€€€€•¹ÑÉ¥•Ì€ô}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•à ¤4(€€€€€€€½¹Ù•ÉÍ…Ñ¥½¹Ì€ômt4(4(€€€€€€€™½È­•ä°•¹ÑÉä¥¸•¹ÑÉ¥•Ì¹¥Ñ•µÌ ¤è4(€€€€€€€€€€€½É¥¥¸€ô•¹ÑÉä¹•Ð ‰½É¥¥¸ˆ°íô¤4(€€€€€€€€€€€•¹ÑÉå}Á±…Ñ™½É´€ô•¹ÑÉä¹•Ð ‰Á±…Ñ™½É´ˆ¤½È½É¥¥¸¹•Ð ‰Á±…Ñ™½É´ˆ°€ˆˆ¤4(4(€€€€€€€€€€€¥˜Á±…Ñ™½É´…¹•¹ÑÉå}Á±…Ñ™½É´¹±½Ý•È ¤€„ôÁ±…Ñ™½É´¹±½Ý•È ¤è4(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(4(€€€€€€€€€€€‘¥ÍÁ±…å}¹…µ”€ô•¹ÑÉä¹•Ð ‰‘¥ÍÁ±…å}¹…µ”ˆ°€ˆˆ¤4(€€€€€€€€€€€¡…Ñ}¹…µ”€ô½É¥¥¸¹•Ð ‰¡…Ñ}¹…µ”ˆ°€ˆˆ¤4(€€€€€€€€€€€¥˜Í•…É è4(€€€€€€€€€€€€€€€Í•…É¡}±½Ý•È€ôÍ•…É ¹±½Ý•È ¤4(€€€€€€€€€€€€€€€¥˜€¡Í•…É¡}±½Ý•È¹½Ð¥¸‘¥ÍÁ±…å}¹…µ”¹±½Ý•È ¤4(€€€€€€€€€€€€€€€€€€€€€€€…¹Í•…É¡}±½Ý•È¹½Ð¥¸¡…Ñ}¹…µ”¹±½Ý•È ¤4(€€€€€€€€€€€€€€€€€€€€€€€…¹Í•…É¡}±½Ý•È¹½Ð¥¸­•ä¹±½Ý•È ¤¤è4(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(4(€€€€€€€€€€€½¹Ù•ÉÍ…Ñ¥½¹Ì¹…ÁÁ•¹¡ì4(€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}­•äˆè­•ä°4(€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆè•¹ÑÉä¹•Ð ‰Í•ÍÍ¥½¹}¥ˆ°€ˆˆ¤°4(€€€€€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè•¹ÑÉå}Á±…Ñ™½É´°4(€€€€€€€€€€€€€€€€‰¡…Ñ}ÑåÁ”ˆè•¹ÑÉä¹•Ð ‰¡…Ñ}ÑåÁ”ˆ°½É¥¥¸¹•Ð ‰¡…Ñ}ÑåÁ”ˆ°€ˆˆ¤¤°4(€€€€€€€€€€€€€€€€‰‘¥ÍÁ±…å}¹…µ”ˆè‘¥ÍÁ±…å}¹…µ”°4(€€€€€€€€€€€€€€€€‰¡…Ñ}¹…µ”ˆè¡…Ñ}¹…µ”°4(€€€€€€€€€€€€€€€€‰ÕÍ•É}¹…µ”ˆè½É¥¥¸¹•Ð ‰ÕÍ•É}¹…µ”ˆ°€ˆˆ¤°4(€€€€€€€€€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ðˆè•¹ÑÉä¹•Ð ‰ÕÁ‘…Ñ•‘}…Ðˆ°€ˆˆ¤°4(€€€€€€€€€€€ô¤4(4(€€€€€€€½¹Ù•ÉÍ…Ñ¥½¹Ì¹Í½ÉÐ¡­•äõ±…µ‰‘„ŒèŒ¹•Ð ‰ÕÁ‘…Ñ•‘}…Ðˆ°€ˆˆ¤°É•Ù•ÉÍ”õQÉÕ”¤4(€€€€€€€½¹Ù•ÉÍ…Ñ¥½¹Ì€ô½¹Ù•ÉÍ…Ñ¥½¹Ílé±¥µ¥Ñt4(4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì4(€€€€€€€€€€€€‰½Õ¹Ðˆè±•¸¡½¹Ù•ÉÍ…Ñ¥½¹Ì¤°4(€€€€€€€€€€€€‰½¹Ù•ÉÍ…Ñ¥½¹Ìˆè½¹Ù•ÉÍ…Ñ¥½¹Ì°4(€€€€€€€ô°¥¹‘•¹ÐôÈ¤4(4(€€€€Œ€´´½¹Ù•ÉÍ…Ñ¥½¹}•Ð€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜½¹Ù•ÉÍ…Ñ¥½¹}•Ð¡Í•ÍÍ¥½¹}­•äèÍÑÈ¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰•Ð‘•Ñ…¥±•¥¹™¼…‰½ÕÐ½¹”½¹Ù•ÉÍ…Ñ¥½¸‰ä¥ÑÌÍ•ÍÍ¥½¸­•ä¸4(4(€€€€€€€ÉÌè4(€€€€€€€€€€€Í•ÍÍ¥½¹}­•äèQ¡”Í•ÍÍ¥½¸­•ä™É½´½¹Ù•ÉÍ…Ñ¥½¹Í}±¥ÍÐ4(€€€€€€€€ˆˆˆ4(€€€€€€€•¹ÑÉ¥•Ì€ô}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•à ¤4(€€€€€€€•¹ÑÉä€ô•¹ÑÉ¥•Ì¹•Ð¡Í•ÍÍ¥½¹}­•ä¤4(4(€€€€€€€¥˜¹½Ð•¹ÑÉäè4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè˜‰½¹Ù•ÉÍ…Ñ¥½¸¹½Ð™½Õ¹èíÍ•ÍÍ¥½¹}­•åô‰ô¤4(4(€€€€€€€½É¥¥¸€ô•¹ÑÉä¹•Ð ‰½É¥¥¸ˆ°íô¤4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì4(€€€€€€€€€€€€‰Í•ÍÍ¥½¹}­•äˆèÍ•ÍÍ¥½¹}­•ä°4(€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆè•¹ÑÉä¹•Ð ‰Í•ÍÍ¥½¹}¥ˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰Á±…Ñ™½É´ˆè•¹ÑÉä¹•Ð ‰Á±…Ñ™½É´ˆ¤½È½É¥¥¸¹•Ð ‰Á±…Ñ™½É´ˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰¡…Ñ}ÑåÁ”ˆè•¹ÑÉä¹•Ð ‰¡…Ñ}ÑåÁ”ˆ°½É¥¥¸¹•Ð ‰¡…Ñ}ÑåÁ”ˆ°€ˆˆ¤¤°4(€€€€€€€€€€€€‰‘¥ÍÁ±…å}¹…µ”ˆè•¹ÑÉä¹•Ð ‰‘¥ÍÁ±…å}¹…µ”ˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰ÕÍ•É}¹…µ”ˆè½É¥¥¸¹•Ð ‰ÕÍ•É}¹…µ”ˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰¡…Ñ}¹…µ”ˆè½É¥¥¸¹•Ð ‰¡…Ñ}¹…µ”ˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰¡…Ñ}¥ˆè½É¥¥¸¹•Ð ‰¡…Ñ}¥ˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰Ñ¡É•…‘}¥ˆè½É¥¥¸¹•Ð ‰Ñ¡É•…‘}¥ˆ¤°4(€€€€€€€€€€€€‰ÕÁ‘…Ñ•‘}…Ðˆè•¹ÑÉä¹•Ð ‰ÕÁ‘…Ñ•‘}…Ðˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰É•…Ñ•‘}…Ðˆè•¹ÑÉä¹•Ð ‰É•…Ñ•‘}…Ðˆ°€ˆˆ¤°4(€€€€€€€€€€€€‰¥¹ÁÕÑ}Ñ½­•¹Ìˆè•¹ÑÉä¹•Ð ‰¥¹ÁÕÑ}Ñ½­•¹Ìˆ°€À¤°4(€€€€€€€€€€€€‰½ÕÑÁÕÑ}Ñ½­•¹Ìˆè•¹ÑÉä¹•Ð ‰½ÕÑÁÕÑ}Ñ½­•¹Ìˆ°€À¤°4(€€€€€€€€€€€€‰Ñ½Ñ…±}Ñ½­•¹Ìˆè•¹ÑÉä¹•Ð ‰Ñ½Ñ…±}Ñ½­•¹Ìˆ°€À¤°4(€€€€€€€ô°¥¹‘•¹ÐôÈ¤4(4(€€€€Œ€´´µ•ÍÍ…•Í}É•…€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜µ•ÍÍ…•Í}É•… 4(€€€€€€€Í•ÍÍ¥½¹}­•äèÍÑÈ°4(€€€€€€€±¥µ¥Ðè¥¹Ð€ô€ÔÀ°4(€€€€¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰I•…É••¹Ðµ•ÍÍ…•Ì™É½´„½¹Ù•ÉÍ…Ñ¥½¸¸4(4(€€€€€€€I•ÑÕÉ¹ÌÑ¡”µ•ÍÍ…”¡¥ÍÑ½Éä¥¸¡É½¹½±½¥…°½É‘•ÈÝ¥Ñ É½±”°½¹Ñ•¹Ð°4(€€€€€€€…¹Ñ¥µ•ÍÑ…µÀ™½È•… µ•ÍÍ…”¸4(4(€€€€€€€ÉÌè4(€€€€€€€€€€€Í•ÍÍ¥½¹}­•äèQ¡”Í•ÍÍ¥½¸­•ä™É½´½¹Ù•ÉÍ…Ñ¥½¹Í}±¥ÍÐ4(€€€€€€€€€€€±¥µ¥Ðè5…á¥µÕ´¹Õµ‰•È½˜µ•ÍÍ…•ÌÑ¼É•ÑÕÉ¸€¡‘•™…Õ±Ð€ÔÀ°µ½ÍÐÉ••¹Ð¤4(€€€€€€€€ˆˆˆ4(€€€€€€€±¥µ¥Ð€ô}½•É•}¥¹Ð¡±¥µ¥Ð°‘•™…Õ±ÐôÔÀ°µ¥¹¥µÕ´ôÄ°µ…á¥µÕ´ôÈÀÀ¤4(€€€€€€€•¹ÑÉ¥•Ì€ô}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•à ¤4(€€€€€€€•¹ÑÉä€ô•¹ÑÉ¥•Ì¹•Ð¡Í•ÍÍ¥½¹}­•ä¤4(€€€€€€€¥˜¹½Ð•¹ÑÉäè4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè˜‰½¹Ù•ÉÍ…Ñ¥½¸¹½Ð™½Õ¹èíÍ•ÍÍ¥½¹}­•åô‰ô¤4(4(€€€€€€€Í•ÍÍ¥½¹}¥€ô•¹ÑÉä¹•Ð ‰Í•ÍÍ¥½¹}¥ˆ°€ˆˆ¤4(€€€€€€€¥˜¹½ÐÍ•ÍÍ¥½¹}¥è4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè€‰9¼Í•ÍÍ¥½¸%™½ÈÑ¡¥Ì½¹Ù•ÉÍ…Ñ¥½¸‰ô¤4(4(€€€€€€€…±±}µ•ÍÍ…•Ì°•ÉÉ½È€ô}±½…‘}Í•ÍÍ¥½¹}µ•ÍÍ…•Ì¡Í•ÍÍ¥½¹}¥¤(€€€€€€€¥˜•ÉÉ½Èè(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè•ÉÉ½Éô¤(4(€€€€€€€™¥±Ñ•É•€ômt4(€€€€€€€™½ÈµÍœ¥¸…±±}µ•ÍÍ…•Ìè4(€€€€€€€€€€€É½±”€ôµÍœ¹•Ð ‰É½±”ˆ°€ˆˆ¤4(€€€€€€€€€€€¥˜É½±”¥¸ì‰ÕÍ•Èˆ°€‰…ÍÍ¥ÍÑ…¹Ð‰ôè4(€€€€€€€€€€€€€€€½¹Ñ•¹Ð€ô}•áÑÉ…Ñ}µ•ÍÍ…•}½¹Ñ•¹Ð¡µÍœ¤4(€€€€€€€€€€€€€€€¥˜½¹Ñ•¹Ðè4(€€€€€€€€€€€€€€€€€€€™¥±Ñ•É•¹…ÁÁ•¹¡ì4(€€€€€€€€€€€€€€€€€€€€€€€€‰¥ˆèÍÑÈ¡µÍœ¹•Ð ‰¥ˆ°€ˆˆ¤¤°4(€€€€€€€€€€€€€€€€€€€€€€€€‰É½±”ˆèÉ½±”°4(€€€€€€€€€€€€€€€€€€€€€€€€‰½¹Ñ•¹Ðˆè½¹Ñ•¹ÑlèÈÀÀÁt°4(€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ¥µ•ÍÑ…µÀˆèµÍœ¹•Ð ‰Ñ¥µ•ÍÑ…µÀˆ°€ˆˆ¤°4(€€€€€€€€€€€€€€€€€€€ô¤4(4(€€€€€€€µ•ÍÍ…•Ì€ô™¥±Ñ•É•‘lµ±¥µ¥Ðét4(4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì4(€€€€€€€€€€€€‰Í•ÍÍ¥½¹}­•äˆèÍ•ÍÍ¥½¹}­•ä°4(€€€€€€€€€€€€‰½Õ¹Ðˆè±•¸¡µ•ÍÍ…•Ì¤°4(€€€€€€€€€€€€‰Ñ½Ñ…±}¥¹}Í•ÍÍ¥½¸ˆè±•¸¡™¥±Ñ•É•¤°4(€€€€€€€€€€€€‰µ•ÍÍ…•Ìˆèµ•ÍÍ…•Ì°4(€€€€€€€ô°¥¹‘•¹ÐôÈ¤4(4(€€€€Œ€´´…ÑÑ…¡µ•¹ÑÍ}™•Ñ €´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜…ÑÑ…¡µ•¹ÑÍ}™•Ñ  4(€€€€€€€Í•ÍÍ¥½¹}­•äèÍÑÈ°4(€€€€€€€µ•ÍÍ…•}¥èÍÑÈ°4(€€€€¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰1¥ÍÐ¹½¸µÑ•áÐ…ÑÑ…¡µ•¹ÑÌ™½È„µ•ÍÍ…”¥¸„½¹Ù•ÉÍ…Ñ¥½¸¸4(4(€€€€€€€áÑÉ…ÑÌ¥µ…•Ì°µ•‘¥„™¥±•Ì°…¹½Ñ¡•È¹½¸µÑ•áÐ½¹Ñ•¹Ð‰±½­Ì4(€€€€€€€™É½´Ñ¡”ÍÁ•¥™¥•µ•ÍÍ…”¸4(4(€€€€€€€ÉÌè4(€€€€€€€€€€€Í•ÍÍ¥½¹}­•äèQ¡”Í•ÍÍ¥½¸­•ä™É½´½¹Ù•ÉÍ…Ñ¥½¹Í}±¥ÍÐ4(€€€€€€€€€€€µ•ÍÍ…•}¥èQ¡”µ•ÍÍ…”%™É½´µ•ÍÍ…•Í}É•…4(€€€€€€€€ˆˆˆ4(€€€€€€€•¹ÑÉ¥•Ì€ô}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•à ¤4(€€€€€€€•¹ÑÉä€ô•¹ÑÉ¥•Ì¹•Ð¡Í•ÍÍ¥½¹}­•ä¤4(€€€€€€€¥˜¹½Ð•¹ÑÉäè4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè˜‰½¹Ù•ÉÍ…Ñ¥½¸¹½Ð™½Õ¹èíÍ•ÍÍ¥½¹}­•åô‰ô¤4(4(€€€€€€€Í•ÍÍ¥½¹}¥€ô•¹ÑÉä¹•Ð ‰Í•ÍÍ¥½¹}¥ˆ°€ˆˆ¤4(€€€€€€€¥˜¹½ÐÍ•ÍÍ¥½¹}¥è4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè€‰9¼Í•ÍÍ¥½¸%™½ÈÑ¡¥Ì½¹Ù•ÉÍ…Ñ¥½¸‰ô¤4(4(€€€€€€€…±±}µ•ÍÍ…•Ì°•ÉÉ½È€ô}±½…‘}Í•ÍÍ¥½¹}µ•ÍÍ…•Ì¡Í•ÍÍ¥½¹}¥¤(€€€€€€€¥˜•ÉÉ½Èè(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè•ÉÉ½Éô¤(4(€€€€€€€€Œ¥¹Ñ¡”Ñ…É•Ðµ•ÍÍ…”4(€€€€€€€Ñ…É•Ñ}µÍœ€ô9½¹”4(€€€€€€€™½ÈµÍœ¥¸…±±}µ•ÍÍ…•Ìè4(€€€€€€€€€€€¥˜ÍÑÈ¡µÍœ¹•Ð ‰¥ˆ°€ˆˆ¤¤€ôôµ•ÍÍ…•}¥è4(€€€€€€€€€€€€€€€Ñ…É•Ñ}µÍœ€ôµÍœ4(€€€€€€€€€€€€€€€‰É•…¬4(4(€€€€€€€¥˜¹½ÐÑ…É•Ñ}µÍœè4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè˜‰5•ÍÍ…”¹½Ð™½Õ¹èíµ•ÍÍ…•}¥‘ô‰ô¤4(4(€€€€€€€…ÑÑ…¡µ•¹ÑÌ€ô}•áÑÉ…Ñ}…ÑÑ…¡µ•¹ÑÌ¡Ñ…É•Ñ}µÍœ¤4(4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì4(€€€€€€€€€€€€‰µ•ÍÍ…•}¥ˆèµ•ÍÍ…•}¥°4(€€€€€€€€€€€€‰½Õ¹Ðˆè±•¸¡…ÑÑ…¡µ•¹ÑÌ¤°4(€€€€€€€€€€€€‰…ÑÑ…¡µ•¹ÑÌˆè…ÑÑ…¡µ•¹ÑÌ°4(€€€€€€€ô°¥¹‘•¹ÐôÈ¤4(4(€€€€Œ€´´•Ù•¹ÑÍ}Á½±°€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜•Ù•¹ÑÍ}Á½±° 4(€€€€€€€…™Ñ•É}ÕÉÍ½Èè¥¹Ð€ô€À°4(€€€€€€€Í•ÍÍ¥½¹}­•äè=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”°4(€€€€€€€±¥µ¥Ðè¥¹Ð€ô€ÈÀ°4(€€€€¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰A½±°™½È¹•Ü½¹Ù•ÉÍ…Ñ¥½¸•Ù•¹ÑÌÍ¥¹”„ÕÉÍ½ÈÁ½Í¥Ñ¥½¸¸4(4(€€€€€€€I•ÑÕÉ¹Ì•Ù•¹ÑÌÑ¡…Ð¡…Ù”½ÕÉÉ•Í¥¹”Ñ¡”¥Ù•¸ÕÉÍ½È¸UÍ”Ñ¡”4(€€€€€€€É•ÑÕÉ¹•¹•áÑ}ÕÉÍ½ÈÙ…±Õ”™½ÈÍÕ‰Í•ÅÕ•¹ÐÁ½±±Ì¸4(4(€€€€€€€Ù•¹ÐÑåÁ•Ìèµ•ÍÍ…”°…ÁÁÉ½Ù…±}É•ÅÕ•ÍÑ•°…ÁÁÉ½Ù…±}É•Í½±Ù•4(4(€€€€€€€ÉÌè4(€€€€€€€€€€€…™Ñ•É}ÕÉÍ½ÈèI•ÑÕÉ¸•Ù•¹ÑÌ…™Ñ•ÈÑ¡¥ÌÕÉÍ½È€ À™½È…±°¤4(€€€€€€€€€€€Í•ÍÍ¥½¹}­•äè=ÁÑ¥½¹…°™¥±Ñ•ÈÑ¼½¹”½¹Ù•ÉÍ…Ñ¥½¸4(€€€€€€€€€€€±¥µ¥Ðè5…á¥µÕ´•Ù•¹ÑÌÑ¼É•ÑÕÉ¸€¡‘•™…Õ±Ð€ÈÀ¤4(€€€€€€€€ˆˆˆ4(€€€€€€€…™Ñ•É}ÕÉÍ½È€ô}½•É•}¥¹Ð¡…™Ñ•É}ÕÉÍ½È°‘•™…Õ±ÐôÀ°µ¥¹¥µÕ´ôÀ°µ…á¥µÕ´ôÄÀ¨¨Äà¤4(€€€€€€€±¥µ¥Ð€ô}½•É•}¥¹Ð¡±¥µ¥Ð°‘•™…Õ±ÐôÈÀ°µ¥¹¥µÕ´ôÄ°µ…á¥µÕ´ôÈÀÀ¤4(€€€€€€€É•ÍÕ±Ð€ô‰É¥‘”¹Á½±±}•Ù•¹ÑÌ 4(€€€€€€€€€€€…™Ñ•É}ÕÉÍ½Èõ…™Ñ•É}ÕÉÍ½È°4(€€€€€€€€€€€Í•ÍÍ¥½¹}­•äõÍ•ÍÍ¥½¹}­•ä°4(€€€€€€€€€€€±¥µ¥Ðõ±¥µ¥Ð°4(€€€€€€€€¤4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡É•ÍÕ±Ð°¥¹‘•¹ÐôÈ¤4(4(€€€€Œ€´´•Ù•¹ÑÍ}Ý…¥Ð€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜•Ù•¹ÑÍ}Ý…¥Ð 4(€€€€€€€…™Ñ•É}ÕÉÍ½Èè¥¹Ð€ô€À°4(€€€€€€€Í•ÍÍ¥½¹}­•äè=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”°4(€€€€€€€Ñ¥µ•½ÕÑ}µÌè¥¹Ð€ô€ÌÀÀÀÀ°4(€€€€¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰]…¥Ð™½ÈÑ¡”¹•áÐ½¹Ù•ÉÍ…Ñ¥½¸•Ù•¹Ð€¡±½¹œµÁ½±°¤¸4(4(€€€€€€€	±½­ÌÕ¹Ñ¥°„µ…Ñ¡¥¹œ•Ù•¹Ð…ÉÉ¥Ù•Ì½ÈÑ¡”Ñ¥µ•½ÕÐ•áÁ¥É•Ì¸4(€€€€€€€UÍ”Ñ¡¥Ì™½È¹•…ÈµÉ•…°µÑ¥µ”•Ù•¹Ð‘•±¥Ù•ÉäÝ¥Ñ¡½ÕÐÁ½±±¥¹œ¸4(4(€€€€€€€ÉÌè4(€€€€€€€€€€€…™Ñ•É}ÕÉÍ½Èè]…¥Ð™½È•Ù•¹ÑÌ…™Ñ•ÈÑ¡¥ÌÕÉÍ½È4(€€€€€€€€€€€Í•ÍÍ¥½¹}­•äè=ÁÑ¥½¹…°™¥±Ñ•ÈÑ¼½¹”½¹Ù•ÉÍ…Ñ¥½¸4(€€€€€€€€€€€Ñ¥µ•½ÕÑ}µÌè5…á¥µÕ´Ý…¥ÐÑ¥µ”¥¸µ¥±±¥Í•½¹‘Ì€¡‘•™…Õ±Ð€ÌÀÀÀÀ¤4(€€€€€€€€ˆˆˆ4(€€€€€€€…™Ñ•É}ÕÉÍ½È€ô}½•É•}¥¹Ð¡…™Ñ•É}ÕÉÍ½È°‘•™…Õ±ÐôÀ°µ¥¹¥µÕ´ôÀ°µ…á¥µÕ´ôÄÀ¨¨Äà¤4(€€€€€€€Ñ¥µ•½ÕÑ}µÌ€ô}½•É•}¥¹Ð 4(€€€€€€€€€€€Ñ¥µ•½ÕÑ}µÌ°4(€€€€€€€€€€€‘•™…Õ±ÐôÌÀÀÀÀ°4(€€€€€€€€€€€µ¥¹¥µÕ´ôÀ°4(€€€€€€€€€€€µ…á¥µÕ´ôÌÀÀÀÀÀ°4(€€€€€€€€¤€€Œ…À…Ð€Ôµ¥¹ÕÑ•Ì4(€€€€€€€•Ù•¹Ð€ô‰É¥‘”¹Ý…¥Ñ}™½É}•Ù•¹Ð 4(€€€€€€€€€€€…™Ñ•É}ÕÉÍ½Èõ…™Ñ•É}ÕÉÍ½È°4(€€€€€€€€€€€Í•ÍÍ¥½¹}­•äõÍ•ÍÍ¥½¹}­•ä°4(€€€€€€€€€€€Ñ¥µ•½ÕÑ}µÌõÑ¥µ•½ÕÑ}µÌ°4(€€€€€€€€¤4(€€€€€€€¥˜•Ù•¹Ðè4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•Ù•¹Ðˆè•Ù•¹Ñô°¥¹‘•¹ÐôÈ¤4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•Ù•¹Ðˆè9½¹”°€‰É•…Í½¸ˆè€‰Ñ¥µ•½ÕÐ‰ô°¥¹‘•¹ÐôÈ¤4(4(€€€€Œ€´´µ•ÍÍ…•Í}Í•¹€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜µ•ÍÍ…•Í}Í•¹ 4(€€€€€€€Ñ…É•ÐèÍÑÈ°4(€€€€€€€µ•ÍÍ…”èÍÑÈ°4(€€€€¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰M•¹„µ•ÍÍ…”Ñ¼„Á±…Ñ™½É´½¹Ù•ÉÍ…Ñ¥½¸¸4(4(€€€€€€€Q¡”Ñ…É•Ð™½Éµ…Ð¥Ì€‰Á±…Ñ™½É´é¡…Ñ}¥ˆƒŠPÍ…µ”™½Éµ…ÐÕÍ•‰äÑ¡”4(€€€€€€€¡…¹¹•±Í}±¥ÍÐÑ½½°¸e½Ô…¸…±Í¼ÕÍ”¡Õµ…¸µ™É¥•¹‘±ä¡…¹¹•°¹…µ•Ì4(€€€€€€€Ñ¡…ÐÝ¥±°‰”É•Í½±Ù•…ÕÑ½µ…Ñ¥…±±ä¸4(4(€€€€€€€á…µÁ±•Ìè4(€€€€€€€€€€€Ñ…É•Ðô‰Ñ•±•É…´èØÌÀàäàÄàØÔˆ4(€€€€€€€€€€€Ñ…É•Ðô‰‘¥Í½Éè•¹•É…°ˆ4(€€€€€€€€€€€Ñ…É•Ðô‰Í±…¬è•¹¥¹••É¥¹œˆ4(4(€€€€€€€ÉÌè4(€€€€€€€€€€€Ñ…É•ÐèA±…Ñ™½É´Ñ…É•Ð¥¸€‰Á±…Ñ™½É´é¥‘•¹Ñ¥™¥•Èˆ™½Éµ…Ð4(€€€€€€€€€€€µ•ÍÍ…”èQ¡”µ•ÍÍ…”Ñ•áÐÑ¼Í•¹4(€€€€€€€€ˆˆˆ4(€€€€€€€¥˜¹½ÐÑ…É•Ð½È¹½Ðµ•ÍÍ…”è4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè€‰	½Ñ Ñ…É•Ð…¹µ•ÍÍ…”…É”É•ÅÕ¥É•‰ô¤4(4(€€€€€€€ÑÉäè4(€€€€€€€€€€€™É½´Ñ½½±Ì¹Í•¹‘}µ•ÍÍ…•}Ñ½½°¥µÁ½ÉÐÍ•¹‘}µ•ÍÍ…•}Ñ½½°4(€€€€€€€€€€€É•ÍÕ±Ñ}ÍÑÈ€ôÍ•¹‘}µ•ÍÍ…•}Ñ½½° 4(€€€€€€€€€€€€€€€ì‰…Ñ¥½¸ˆè€‰Í•¹ˆ°€‰Ñ…É•ÐˆèÑ…É•Ð°€‰µ•ÍÍ…”ˆèµ•ÍÍ…•ô4(€€€€€€€€€€€€¤4(€€€€€€€€€€€É•ÑÕÉ¸É•ÍÕ±Ñ}ÍÑÈ4(€€€€€€€•á•ÁÐ%µÁ½ÉÑÉÉ½Èè4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè€‰M•¹µ•ÍÍ…”Ñ½½°¹½Ð…Ù…¥±…‰±”‰ô¤4(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰•ÉÉ½Èˆè˜‰M•¹™…¥±•èí•ô‰ô¤4(4(€€€€Œ€´´¡…¹¹•±Í}±¥ÍÐ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜¡…¹¹•±Í}±¥ÍÐ¡Á±…Ñ™½É´è=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰1¥ÍÐ…Ù…¥±…‰±”µ•ÍÍ…¥¹œ¡…¹¹•±Ì…¹Ñ…É•ÑÌ…É½ÍÌÁ±…Ñ™½ÉµÌ¸4(4(€€€€€€€I•ÑÕÉ¹Ì¡…¹¹•±ÌÑ¡…Ðå½Ô…¸Í•¹µ•ÍÍ…•ÌÑ¼¸Q¡”Ñ…É•ÐÍÑÉ¥¹Ì4(€€€€€€€É•ÑÕÉ¹•¡•É”…¸‰”ÕÍ•‘¥É•Ñ±äÝ¥Ñ Ñ¡”µ•ÍÍ…•Í}Í•¹Ñ½½°¸4(4(€€€€€€€ÉÌè4(€€€€€€€€€€€Á±…Ñ™½É´è¥±Ñ•È‰äÁ±…Ñ™½É´¹…µ”€¡Ñ•±•É…´°‘¥Í½É°Í±…¬°•ÑŒ¸¤4(€€€€€€€€ˆˆˆ4(€€€€€€€‘¥É•Ñ½Éä€ô}±½…‘}¡…¹¹•±}‘¥É•Ñ½Éä ¤4(€€€€€€€¥˜¹½Ð‘¥É•Ñ½Éäè4(€€€€€€€€€€€•¹ÑÉ¥•Ì€ô}±½…‘}Í•ÍÍ¥½¹Í}¥¹‘•à ¤4(€€€€€€€€€€€Ñ…É•ÑÌ€ômt4(€€€€€€€€€€€Í••¸€ôÍ•Ð ¤4(€€€€€€€€€€€™½È­•ä°•¹ÑÉä¥¸•¹ÑÉ¥•Ì¹¥Ñ•µÌ ¤è4(€€€€€€€€€€€€€€€½É¥¥¸€ô•¹ÑÉä¹•Ð ‰½É¥¥¸ˆ°íô¤4(€€€€€€€€€€€€€€€À€ô•¹ÑÉä¹•Ð ‰Á±…Ñ™½É´ˆ¤½È½É¥¥¸¹•Ð ‰Á±…Ñ™½É´ˆ°€ˆˆ¤4(€€€€€€€€€€€€€€€¡…Ñ}¥€ô½É¥¥¸¹•Ð ‰¡…Ñ}¥ˆ°€ˆˆ¤4(€€€€€€€€€€€€€€€¥˜¹½ÐÀ½È¹½Ð¡…Ñ}¥è4(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€€€€€€€€€¥˜Á±…Ñ™½É´…¹À¹±½Ý•È ¤€„ôÁ±…Ñ™½É´¹±½Ý•È ¤è4(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€€€€€€€€€Ñ…É•Ñ}ÍÑÈ€ô˜‰íÁôéí¡…Ñ}¥‘ôˆ4(€€€€€€€€€€€€€€€¥˜Ñ…É•Ñ}ÍÑÈ¥¸Í••¸è4(€€€€€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€€€€€€€€€Í••¸¹…‘¡Ñ…É•Ñ}ÍÑÈ¤4(€€€€€€€€€€€€€€€Ñ…É•ÑÌ¹…ÁÁ•¹¡ì4(€€€€€€€€€€€€€€€€€€€€‰Ñ…É•ÐˆèÑ…É•Ñ}ÍÑÈ°4(€€€€€€€€€€€€€€€€€€€€‰Á±…Ñ™½É´ˆèÀ°4(€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆè•¹ÑÉä¹•Ð ‰‘¥ÍÁ±…å}¹…µ”ˆ¤½È½É¥¥¸¹•Ð ‰¡…Ñ}¹…µ”ˆ°€ˆˆ¤°4(€€€€€€€€€€€€€€€€€€€€‰¡…Ñ}ÑåÁ”ˆè•¹ÑÉä¹•Ð ‰¡…Ñ}ÑåÁ”ˆ°½É¥¥¸¹•Ð ‰¡…Ñ}ÑåÁ”ˆ°€ˆˆ¤¤°4(€€€€€€€€€€€€€€€ô¤4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰½Õ¹Ðˆè±•¸¡Ñ…É•ÑÌ¤°€‰¡…¹¹•±ÌˆèÑ…É•ÑÍô°¥¹‘•¹ÐôÈ¤4(4(€€€€€€€¡…¹¹•±Ì€ômt4(€€€€€€€™½ÈÁ±…Ð°•¹ÑÉ¥•Í}±¥ÍÐ¥¸‘¥É•Ñ½Éä¹•Ð ‰Á±…Ñ™½ÉµÌˆ°íô¤¹¥Ñ•µÌ ¤è4(€€€€€€€€€€€¥˜Á±…Ñ™½É´…¹Á±…Ð¹±½Ý•È ¤€„ôÁ±…Ñ™½É´¹±½Ý•È ¤è4(€€€€€€€€€€€€€€€½¹Ñ¥¹Õ”4(€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡•¹ÑÉ¥•Í}±¥ÍÐ°±¥ÍÐ¤è4(€€€€€€€€€€€€€€€™½È ¥¸•¹ÑÉ¥•Í}±¥ÍÐè4(€€€€€€€€€€€€€€€€€€€¥˜¥Í¥¹ÍÑ…¹”¡ °‘¥Ð¤è4(€€€€€€€€€€€€€€€€€€€€€€€¡…Ñ}¥€ô ¹•Ð ‰¥ˆ° ¹•Ð ‰¡…Ñ}¥ˆ°€ˆˆ¤¤4(€€€€€€€€€€€€€€€€€€€€€€€¡…¹¹•±Ì¹…ÁÁ•¹¡ì4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Ñ…É•Ðˆè˜‰íÁ±…Ñôéí¡…Ñ}¥‘ôˆ¥˜¡…Ñ}¥•±Í”Á±…Ð°4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰Á±…Ñ™½É´ˆèÁ±…Ð°4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¹…µ”ˆè ¹•Ð ‰¹…µ”ˆ° ¹•Ð ‰‘¥ÍÁ±…å}¹…µ”ˆ°€ˆˆ¤¤°4(€€€€€€€€€€€€€€€€€€€€€€€€€€€€‰¡…Ñ}ÑåÁ”ˆè ¹•Ð ‰ÑåÁ”ˆ°€ˆˆ¤°4(€€€€€€€€€€€€€€€€€€€€€€€ô¤4(4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì‰½Õ¹Ðˆè±•¸¡¡…¹¹•±Ì¤°€‰¡…¹¹•±Ìˆè¡…¹¹•±Íô°¥¹‘•¹ÐôÈ¤4(4(€€€€Œ€´´Á•Éµ¥ÍÍ¥½¹Í}±¥ÍÑ}½Á•¸€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜Á•Éµ¥ÍÍ¥½¹Í}±¥ÍÑ}½Á•¸ ¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰1¥ÍÐÁ•¹‘¥¹œ…ÁÁÉ½Ù…°É•ÅÕ•ÍÑÌ½‰Í•ÉÙ•‘ÕÉ¥¹œÑ¡¥Ì‰É¥‘”Í•ÍÍ¥½¸¸4(4(€€€€€€€I•ÑÕÉ¹Ì•á•Œ…¹Á±Õ¥¸…ÁÁÉ½Ù…°É•ÅÕ•ÍÑÌÑ¡…ÐÑ¡”‰É¥‘”¡…ÌÍ••¸4(€€€€€€€Í¥¹”¥ÐÍÑ…ÉÑ•¸ÁÁÉ½Ù…±Ì…É”±¥Ù”µÍ•ÍÍ¥½¸½¹±äƒŠP½±‘•È…ÁÁÉ½Ù…±Ì4(€€€€€€€™É½´‰•™½É”Ñ¡”‰É¥‘”½¹¹•Ñ•…É”¹½Ð¥¹±Õ‘•¸4(€€€€€€€€ˆˆˆ4(€€€€€€€…ÁÁÉ½Ù…±Ì€ô‰É¥‘”¹±¥ÍÑ}Á•¹‘¥¹}…ÁÁÉ½Ù…±Ì ¤4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì4(€€€€€€€€€€€€‰½Õ¹Ðˆè±•¸¡…ÁÁÉ½Ù…±Ì¤°4(€€€€€€€€€€€€‰…ÁÁÉ½Ù…±Ìˆè…ÁÁÉ½Ù…±Ì°4(€€€€€€€ô°¥¹‘•¹ÐôÈ¤4(4(€€€€Œ€´´Á•Éµ¥ÍÍ¥½¹Í}É•ÍÁ½¹€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4(€€€µÀ¹Ñ½½° ¤4(€€€‘•˜Á•Éµ¥ÍÍ¥½¹Í}É•ÍÁ½¹ 4(€€€€€€€¥èÍÑÈ°4(€€€€€€€‘•¥Í¥½¸èÍÑÈ°4(€€€€¤€´øÍÑÈè4(€€€€€€€€ˆˆ‰I•ÍÁ½¹Ñ¼„Á•¹‘¥¹œ…ÁÁÉ½Ù…°É•ÅÕ•ÍÐ¸4(4(€€€€€€€ÉÌè4(€€€€€€€€€€€¥èQ¡”…ÁÁÉ½Ù…°%™É½´Á•Éµ¥ÍÍ¥½¹Í}±¥ÍÑ}½Á•¸4(€€€€€€€€€€€‘•¥Í¥½¸è=¹”½˜€‰…±±½Üµ½¹”ˆ°€‰…±±½Üµ…±Ý…åÌˆ°½È€‰‘•¹äˆ4(€€€€€€€€ˆˆˆ4(€€€€€€€¥˜‘•¥Í¥½¸¹½Ð¥¸ì‰…±±½Üµ½¹”ˆ°€‰…±±½Üµ…±Ý…åÌˆ°€‰‘•¹ä‰ôè4(€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡ì4(€€€€€€€€€€€€€€€€‰•ÉÉ½Èˆè˜‰%¹Ù…±¥‘•¥Í¥½¸èí‘•¥Í¥½¹ô¸€ˆ4(€€€€€€€€€€€€€€€€€€€€€€€€˜‰5ÕÍÐ‰”…±±½Üµ½¹”°…±±½Üµ…±Ý…åÌ°½È‘•¹äˆ4(€€€€€€€€€€€ô¤4(4(€€€€€€€É•ÍÕ±Ð€ô‰É¥‘”¹É•ÍÁ½¹‘}Ñ½}…ÁÁÉ½Ù…°¡¥°‘•¥Í¥½¸¤4(€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡É•ÍÕ±Ð°¥¹‘•¹ÐôÈ¤4(4(€€€É•ÑÕÉ¸µÀ4(4(4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(Œ¹ÑÉäÁ½¥¹Ð4(Œ€´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´´4(4)‘•˜ÉÕ¹}µÁ}Í•ÉÙ•È¡Ù•É‰½Í”è‰½½°€ô…±Í”¤€´ø9½¹”è4(€€€€ˆˆ‰MÑ…ÉÐÑ¡”!•Éµ•Ì5@Í•ÉÙ•È½¸ÍÑ‘¥¼¸ˆˆˆ4(€€€¥˜¹½Ð}5A}MIYI}Y%1	1è4(€€€€€€€ÁÉ¥¹Ð 4(€€€€€€€€€€€€‰ÉÉ½Èè5@Í•ÉÙ•ÈÉ•ÅÕ¥É•ÌÑ¡”€µÀœÁ…­…”¹q¸ˆ4(€€€€€€€€€€€˜‰%¹ÍÑ…±°Ý¥Ñ èíÍåÌ¹•á•ÕÑ…‰±•ô€µ´Á¥À¥¹ÍÑ…±°€µÀœˆ°4(€€€€€€€€€€€™¥±”õÍåÌ¹ÍÑ‘•ÉÈ°4(€€€€€€€€¤4(€€€€€€€ÍåÌ¹•á¥Ð Ä¤4(4(€€€¥˜Ù•É‰½Í”è4(€€€€€€€±½¥¹œ¹‰…Í¥½¹™¥œ¡±•Ù•°õ±½¥¹œ¹	U°ÍÑÉ•…´õÍåÌ¹ÍÑ‘•ÉÈ¤4(€€€•±Í”è4(€€€€€€€±½¥¹œ¹‰…Í¥½¹™¥œ¡±•Ù•°õ±½¥¹œ¹]I9%9°ÍÑÉ•…´õÍåÌ¹ÍÑ‘•ÉÈ¤4(4(€€€‰É¥‘”€ôÙ•¹Ñ	É¥‘” ¤4(€€€‰É¥‘”¹ÍÑ…ÉÐ ¤4(4(€€€Í•ÉÙ•È€ôÉ•…Ñ•}µÁ}Í•ÉÙ•È¡•Ù•¹Ñ}‰É¥‘”õ‰É¥‘”¤4(4(€€€¥µÁ½ÉÐ…Íå¹¥¼4(4(€€€…Íå¹Œ‘•˜}ÉÕ¸ ¤è4(€€€€€€€ÑÉäè4(€€€€€€€€€€€…Ý…¥ÐÍ•ÉÙ•È¹ÉÕ¹}ÍÑ‘¥½}…Íå¹Œ ¤4(€€€€€€€™¥¹…±±äè4(€€€€€€€€€€€‰É¥‘”¹ÍÑ½À ¤4(4(€€€ÑÉäè4(€€€€€€€…Íå¹¥¼¹ÉÕ¸¡}ÉÕ¸ ¤¤4(€€€•á•ÁÐ-•å‰½…É‘%¹Ñ•ÉÉÕÁÐè4(€€€€€€€‰É¥‘”¹ÍÑ½À ¤4(