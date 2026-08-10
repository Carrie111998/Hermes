#!/usr/bin/env python3
"""
Session Search Tool - Long-Term Conversation Recall

Single-shape tool with three calling modes (inferred from args, no explicit
mode parameter):

  1. DISCOVERY â€” pass ``query``. Runs FTS5, dedupes hits by session lineage,
     returns top N sessions each with: snippet, Â±5 message window around the
     match, plus bookend_start (first 3 user+assistant msgs of session) and
     bookend_end (last 3). Zero LLM cost.

  2. SCROLL â€” pass ``session_id`` + ``around_message_id``. Returns a window
     of Â±window messages centered on the anchor, no FTS5, no bookends. To
     scroll forward / backward, re-anchor on the last / first message id of
     the returned window.

  3. BROWSE â€” no args. Returns recent sessions chronologically (titles,
     previews, timestamps).

All three modes operate on the SQLite session DB via the FTS5 index and
the get_anchored_view / get_messages_around primitives in hermes_state.
No LLM calls anywhere â€” every shape returns actual messages from the DB.

History: PR #20238 (JabberELF) seeded a fast/summary dual-mode split; the
toolkit expansion in PR #26419 (yoniebans) added the anchored drill-down,
bookends, and sort. This module merges all of that into a single calling
shape with no mode parameter, no summary LLM path, and explicit scroll
support.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Union

# Sources that are excluded from session browsing/searching by default.
# Third-party integrations tag their sessions with HERMES_SESSION_SOURCE=tool;
# delegate subagent runs are tagged "subagent"; kanban dispatcher workers are
# tagged "kanban" â€” none belongs in the user's session history.
_HIDDEN_SESSION_SOURCES = ("kanban", "subagent", "tool")

# Automation sources that are kept searchable but DEMOTED below interactive
# sessions in discover ranking. Cron jobs run on a schedule and accumulate
# large volumes of repetitive vocabulary (recurring project names, dates,
# "session", summaries); under bare BM25 they dominate the top-N FTS rows and
# starve out the user's own interactive sessions, producing "recall blindness"
# where only cron sessions surface (#19434). Demoting â€” not excluding â€” keeps
# cron content reachable when it's the only match, while interactive sessions
# always win when both match.
_DEMOTED_SESSION_SOURCES = ("cron",)

# How many FTS rows discover scans before dedup-by-lineage. The interactive
# vs automation split below only helps if enough rows are in hand to find
# interactive matches buried under a wall of cron hits, so this is well above
# the handful of distinct sessions a typical query returns.
_DISCOVER_SCAN_LIMIT = 300

# Raw FTS rows are only a discovery-plan input. The final response hydrates
# its own anchored message window and bookends after lineage deduplication.
_DISCOVER_SEARCH_FIELDS = (
    "id",
    "session_id",
    "role",
    "snippet",
    "source",
    "model",
    "session_started",
)

# Prefixes that identify generated context-compaction handoff summaries.
# These are inserted by agent/context_compressor.py as normal user/assistant
# messages but contain machine-generated summary metadata â€” not user content.
# They must be excluded from discovery bookends to avoid re-introducing huge
# compaction payloads into fresh sessions via session_search.  (#43175)
_COMPACTION_PREFIXES = (
    "[CONTEXT COMPACTION",
    "[CONTEXT SUMMARY]:",
)


def _format_timestamp(ts: Union[int, float, str, None]) -> str:
    """Convert a Unix timestamp (float/int) or ISO string to a human-readable date.

    Returns "unknown" for None, str(ts) if conversion fails.
    """
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, (int, float)):
            from datetime import datetime
            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, str):
            if ts.replace(".", "").replace("-", "").isdigit():
                from datetime import datetime
                dt = datetime.fromtimestamp(float(ts))
                return dt.strftime("%B %d, %Y at %I:%M %p")
            return ts
    except (ValueError, OSError, OverflowError) as e:
        logging.debug("Failed to format timestamp %s: %s", ts, e, exc_info=True)
    except Exception as e:
        logging.debug("Unexpected error formatting timestamp %s: %s", ts, e, exc_info=True)
    return str(ts)


def _is_compaction_summary(content: str) -> bool:
    """Return True if *content* looks like a generated compaction handoff."""
    if not content:
        return False
    stripped = content.lstrip()
    return any(stripped.startswith(p) for p in _COMPACTION_PREFIXES)


def _resolve_to_parent(db, session_id: str) -> tuple[str, bool]:
    """Walk parent_session_id chain to the lineage root.

    Returns ``(root_id, has_compression_hop)`` where ``has_compression_hop`` is
    True if any session along the chain ended with ``end_reason = 'compression'``
    â€” i.e. at least one parent/ancestor was compression-rotated into this
    lineage. That flag lets callers distinguish a compression-split lineage
    (parent content summarised away, no longer in live context) from a
    delegation lineage (child content still visible to the parent agent).

    Falls back to ``(session_id, False)`` on errors.
    """
    if not session_id:
        return session_id, False
    visited: set[str] = set()
    cur = session_id
    has_compression = False
    while cur and cur not in visited:
        visited.add(cur)
        try:
            s = db.get_session(cur)
            if not s:
                break
            if s.get("end_reason") == "compression":
                has_compression = True
            parent = s.get("parent_session_id")
            if not parent:
                break
            cur = parent
        except Exception as e:
            logging.debug("Error resolving parent for %s: %s", cur, e, exc_info=True)
            break
    return cur, has_compression


def _resolve_lineage(db, session_id: str) -> str:
    """Convenience: return only the lineage root (ignores compression hop)."""
    return _resolve_to_parent(db, session_id)[0]


def _is_compression_ended(db, session_id: str) -> bool:
    """Return True if *session_id* itself ended with ``end_reason='compression'``.

    Unlike the ``has_compression_hop`` flag from :func:`_resolve_to_parent`
    (which is True for any descendant of a compression-ended ancestor), this
    checks only the session's own ``end_reason``. A delegation child created
    under a compression continuation has ``parent_session_id`` set but its own
    ``end_reason`` is ``None`` â€” its content is still live to the parent agent,
    so it must stay excluded from discovery.
    """
    if not session_id:
        return False
    try:
        s = db.get_session(session_id)
        if not s:
            return False
        return s.get("end_reason") == "compression"
    except Exception:
        return False


def _get_message_storage_state(db, message_id) -> Optional[Dict[str, Any]]:
    """Return the owning session and visibility flags for *message_id*."""
    if not message_id:
        return None
    try:
        with db._lock:
            cursor = db._conn.execute(
                "SELECT session_id, active, compacted FROM messages WHERE id = ?",
                (message_id,),
            )
            row = cursor.fetchone()
    except Exception:
        logging.debug(
            "message storage-state lookup failed for %s", message_id, exc_info=True
        )
        return None
    return dict(row) if row is not None else None


def _is_compacted_message(db, message_id) -> bool:
    """Return True if *message_id* is a compaction-archived row.

    Compaction archives are ``active=0, compacted=1`` â€” the content was
    summarised away from live context by :meth:`archive_and_compact`.
    Rewind/undo rows are ``active=0, compacted=0`` and must stay hidden.

    Used by ``_discover`` to distinguish a compaction-archived FTS hit on the
    current session (pre-compaction content no longer in live context â€” should
    stay discoverable) from an active live hit (already in context â€” skip).
    Returns False on any error so the caller falls back to the safe default
    (skip the current session).
    """
    state = _get_message_storage_state(db, message_id)
    return state is not None and state["active"] == 0 and state["compacted"] == 1


def _annotate_rebuild_status(db, payload: Dict[str, Any]) -> None:
    """Add a rebuild-progress note when the deferred FTS backfill (schema
    v23) is still running, so the agent can tell the user why older results
    may be incomplete/slower instead of treating a thin result set as
    ground truth. No-op (and never raises) when no rebuild is pending."""
    try:
        status = db.fts_rebuild_status()
    except Exception:
        return
    if status is None:
        return
    payload["index_rebuild"] = {
        "percent": status["percent"],
        "note": (
            f"The search index is rebuilding in the background "
            f"({status['percent']}% done, {status['indexed']:,} of "
            f"{status['total']:,} messages). Results from older messages "
            f"may be incomplete until it finishes."
        ),
    }


def _order_for_recall(raw_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stable-sort FTS rows so interactive sessions rank above automation.

    Within each class (interactive vs demoted) the original BM25 ``rank``
    order is preserved â€” Python's sort is stable, and rows arrive already
    ranked by relevance. This only changes cross-class ordering: a cron hit
    never displaces an interactive hit during lineage dedup, so the user's
    own conversations surface first even when cron rows out-rank them under
    bare BM25 (#19434). Demoted rows still appear when they're the only
    matches.
    """
    return sorted(
        raw_results,
        key=lambda r: 1 if (r.get("source") or "") in _DEMOTED_SESSION_SOURCES else 0,
    )


def _shape_message(
    m: Dict[str, Any],
    anchor_id: Optional[int] = None,
    max_content_len: Optional[int] = None,
) -> Dict[str, Any]:
    """Slim a message row for the tool response. Keeps content even if empty.

    When *max_content_len* is set, ``content`` is truncated to that many
    characters and ``content_truncated`` / ``original_content_chars`` metadata
    is added so callers know the payload was bounded.
    """
    raw_content = m.get("content")
    if isinstance(raw_content, str) and "\x1b" in raw_content:
        # Recalled messages can carry ANSI escape sequences (e.g. archived
        # terminal output). Strip them before returning content to the model.
        from tools.ansi_strip import strip_ansi

        raw_content = strip_ansi(raw_content)
    if max_content_len and raw_content and len(raw_content) > max_content_len:
        content = raw_content[:max_content_len] + "â€¦"
        truncated = True
        original_chars = len(raw_content)
    else:
        content = raw_content
        truncated = False
        original_chars = None
    entry = {
        "id": m.get("id"),
        "role": m.get("role"),
        "content": content,
        "timestamp": m.get("timestamp"),
    }
    if m.get("tool_name"):
        entry["tool_name"] = m.get("tool_name")
    if m.get("tool_calls"):
        entry["tool_calls"] = m.get("tool_calls")
    if m.get("tool_call_id"):
        entry["tool_call_id"] = m.get("tool_call_id")
    if anchor_id is not None and m.get("id") == anchor_id:
        entry["anchor"] = True
    if truncated:
        entry["content_truncated"] = True
        entry["original_content_chars"] = original_chars
    # Strip None values to keep payload tight, but always keep content
    # (absent content is meaningful â€” tool-call-only assistant turns).
    return {k: v for k, v in entry.items() if v is not None or k in ("content",)}


def _resolve_profile_db(profile: str):
    """Open another profile's ``state.db`` read-only, or None for the current one.

    The desktop's ``@session:<profile>/<id>`` links always carry the source
    profile, so a linked session from profile B can be read while the agent
    runs in profile A. ``read_only=True`` (mode=ro) takes no write lock â€” safe
    to point at a live profile's DB, including our own. Returns None when no
    profile is given (use the caller's default db).
    """
    if profile is None or not str(profile).strip():
        return None

    from hermes_cli import profiles as profiles_mod
    from hermes_state import SessionDB

    canon = profiles_mod.normalize_profile_name(profile)
    profiles_mod.validate_profile_name(canon)
    if not profiles_mod.profile_exists(canon):
        raise ValueError(f"profile '{canon}' does not exist")

    return SessionDB(db_path=profiles_mod.get_profile_dir(canon) / "state.db", read_only=True)


def _session_link(session_id: str, profile: str = None) -> str:
    """The reference the agent writes to point the user at a session.

    Same value the desktop composer emits when a session is dragged into a
    message, so the desktop renders it as a link carrying the session's title.
    The profile segment is omitted when we can't name it confidently â€” a bare
    id still resolves, it just can't disambiguate across profiles.
    """
    name = (profile or "").strip()
    if not name:
        try:
            from hermes_cli.profiles import get_active_profile_name

            resolved = get_active_profile_name()
            name = "" if resolved == "custom" else resolved
        except Exception:
            logging.debug("get_active_profile_name failed for session link", exc_info=True)
            name = ""

    return f"@session:{name}/{session_id}" if name else f"@session:{session_id}"


def _locate_session_db(session_id: str):
    """Scan every profile's ``state.db`` (read-only) for a session id.

    Returns ``(db, profile_name)`` for the first profile that owns the id, or
    ``(None, None)``. Session ids are globally unique (timestamp + random hex),
    so the first hit is authoritative. This is the safety net for linked-session
    reads where the model dropped the owning profile from the link and passed a
    bare id â€” we find it wherever it actually lives instead of failing.
    """
    from pathlib import Path

    try:
        from hermes_cli import profiles as profiles_mod
        from hermes_state import SessionDB
    except Exception:
        return None, None

    targets = [("default", profiles_mod.ïNù¶‰žËkºwµçM•…ÌÍ•ÍÍ¥½¹}¥¸(€€€€ŒM•ÍÍ¥½¸¥‘Ì¹•Ù•È½¹Ñ…¥¸€ˆ¼ˆ°Í¼„Í±…Í Õ¹…µ‰¥Õ½ÕÍ±äµ•…¹ÌÁÉ½™¥±”½¥ƒŠP4(€€€€Œ…±Ý…åÌÍÑÉ¥ÀÑ¡”ÁÉ•™¥à½™˜Ñ¡”¥°…¹…‘½ÁÐÑ¡”•µ‰•‘‘•ÁÉ½™¥±”½¹±ä4(€€€€ŒÝ¡•¸½¹”Ý…Í¸ÐÁ…ÍÍ••áÁ±¥¥Ñ±ä¸!…¹‘±•Ì•Ù•ÉäÁ•ÉµÕÑ…Ñ¥½¸Ñ¡”µ½‘•°4(€€€€Œµ¥¡ÐÍ•¹€¡™Õ±°Ù…±Õ”…Ì¥°Ý¥Ñ ½ÈÝ¥Ñ¡½ÕÐ„Í•Á…É…Ñ”ÁÉ½™¥±”ô¤¸4(€€€¥˜¥Í¥¹ÍÑ…¹”¡Í•ÍÍ¥½¹}¥°ÍÑÈ¤…¹€ˆ¼ˆ¥¸Í•ÍÍ¥½¹}¥è4(€€€€€€€•µ‰}ÁÉ½™¥±”°|°•µ‰}¥€ôÍ•ÍÍ¥½¹}¥¹Á…ÉÑ¥Ñ¥½¸ ˆ¼ˆ¤4(€€€€€€€¥˜•µ‰}¥è4(€€€€€€€€€€€Í•ÍÍ¥½¹}¥€ô•µ‰}¥4(€€€€€€€€€€€¥˜•µ‰}ÁÉ½™¥±”…¹€¡ÁÉ½™¥±”¥Ì9½¹”½È¹½ÐÍÑÈ¡ÁÉ½™¥±”¤¹ÍÑÉ¥À ¤¤è4(€€€€€€€€€€€€€€€ÁÉ½™¥±”€ô•µ‰}ÁÉ½™¥±”4(4(€€€€ŒÉ½ÍÌµÁÉ½™¥±”É•…èÍÝ…À¥¸Ñ¡”¹…µ•ÁÉ½™¥±”Ì€¡É•…µ½¹±ä¤™½È•Ù•Éä4(€€€€ŒÍ¡…Á”‰•±½Ü¸Q¡”ÕÉÉ•¹ÐµÍ•ÍÍ¥½¸µ±¥¹•…”Õ…É‘Ì¹¼±½¹•È…ÁÁ±ä…É½ÍÌ4(€€€€ŒÁÉ½™¥±•Ì°‰ÕÐÑ¡•ä­•ä½™˜¥‘ÌÑ¡…ÐÝ½¸Ð½±±¥‘”°Í¼Ñ¡•äÍÑ…ä¥¹•ÉÐ¸4(€€€¥˜ÁÉ½™¥±”¥Ì¹½Ð9½¹”…¹ÍÑÈ¡ÁÉ½™¥±”¤¹ÍÑÉ¥À ¤è4(€€€€€€€ÑÉäè4(€€€€€€€€€€€ÁÉ½™¥±•}‘ˆ€ô}É•Í½±Ù•}ÁÉ½™¥±•}‘ˆ¡ÁÉ½™¥±”¤4(€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è4(€€€€€€€€€€€É•ÑÕÉ¸Ñ½½±}•ÉÉ½È¡˜‰ÁÉ½™¥±”€íÁÉ½™¥±•ôœèí•ôˆ°ÍÕ•ÍÌõ…±Í”¤4(€€€€€€€¥˜ÁÉ½™¥±•}‘ˆ¥Ì¹½Ð9½¹”è(€€€€€€€€€€€‘ˆ€ôÁÉ½™¥±•}‘ˆ(€€€€€€€€€€€¥˜}½Ý¹•‘}‘‰Ì¥Ì¹½Ð9½¹”è(€€€€€€€€€€€€€€€}½Ý¹•‘}‘‰Ì¹…ÁÁ•¹¡ÁÉ½™¥±•}‘ˆ¤(€€€€€€€€€€€ÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥€ô9½¹”(4(€€€€ŒMÉ½±°Í¡…Á”Ñ…­•ÌÁÉ••‘•¹”ƒŠP•áÁ±¥¥Ð…¹¡½È‰•…ÑÌ…¹äÅÕ•Éä¸4(€€€¥˜€¡¥Í¥¹ÍÑ…¹”¡Í•ÍÍ¥½¹}¥°ÍÑÈ¤…¹Í•ÍÍ¥½¹}¥¹ÍÑÉ¥À ¤¤…¹…É½Õ¹‘}µ•ÍÍ…•}¥¥Ì¹½Ð9½¹”è4(€€€€€€€É•ÑÕÉ¸}ÍÉ½±° 4(€€€€€€€€€€€‘ˆõ‘ˆ°4(€€€€€€€€€€€Í•ÍÍ¥½¹}¥õÍ•ÍÍ¥½¹}¥°4(€€€€€€€€€€€…É½Õ¹‘}µ•ÍÍ…•}¥õ…É½Õ¹‘}µ•ÍÍ…•}¥°4(€€€€€€€€€€€Ý¥¹‘½ÜõÝ¥¹‘½Ü°4(€€€€€€€€€€€ÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥õÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥°4(€€€€€€€€¤4(4(€€€€ŒI•…Í¡…Á”è„Í•ÍÍ¥½¹}¥Ý¥Ñ ¹¼…¹¡½ÈƒŠH‘ÕµÀÑ¡”Ý¡½±”Í•ÍÍ¥½¸¸4(€€€¥˜¥Í¥¹ÍÑ…¹”¡Í•ÍÍ¥½¹}¥°ÍÑÈ¤…¹Í•ÍÍ¥½¹}¥¹ÍÑÉ¥À ¤è4(€€€€€€€Í¥€ôÍ•ÍÍ¥½¹}¥¹ÍÑÉ¥À ¤4(€€€€€€€É•ÍÕ±Ð€ô}É•…‘}Í•ÍÍ¥½¸¡‘ˆ°Í¥°±¥¹­}ÁÉ½™¥±”õÁÉ½™¥±”¤4(€€€€€€€¥˜©Í½¸¹±½…‘Ì¡É•ÍÕ±Ð¤¹•Ð ‰ÍÕ•ÍÌˆ¤è4(€€€€€€€€€€€É•ÑÕÉ¸É•ÍÕ±Ð4(4(€€€€€€€€Œ5¥ÍÌ¥¸Ñ¡”Ñ…É•ÐÁÉ½™¥±”ƒŠPÑ¡”µ½‘•°µ…ä¡…Ù”‘É½ÁÁ•Ñ¡”½Ý¹¥¹œ4(€€€€€€€€ŒÁÉ½™¥±”™É½´Ñ¡”±¥¹¬¸M…¸•Ù•ÉäÁÉ½™¥±”…¹É•…¥Ð™É½´Ý¡•É•Ù•È4(€€€€€€€€Œ¥Ð±¥Ù•Ì°Ñ…¥¹œÑ¡”ÁÉ½™¥±”¥ÐÝ…Ì™½Õ¹¥¸¸4(€€€€€€€±½…Ñ•°½Ý¹•È€ô}±½…Ñ•}Í•ÍÍ¥½¹}‘ˆ¡Í¥¤4(€€€€€€€¥˜±½…Ñ•¥Ì¹½Ð9½¹”è4(€€€€€€€€€€€ÑÉäè4(€€€€€€€€€€€€€€€™½Õ¹€ô©Í½¸¹±½…‘Ì¡}É•…‘}Í•ÍÍ¥½¸¡±½…Ñ•°Í¥°±¥¹­}ÁÉ½™¥±”õ½Ý¹•È¤¤4(€€€€€€€€€€€™¥¹…±±äè4(€€€€€€€€€€€€€€€±½…Ñ•¹±½Í” ¤4(€€€€€€€€€€€¥˜™½Õ¹¹•Ð ‰ÍÕ•ÍÌˆ¤è4(€€€€€€€€€€€€€€€™½Õ¹‘l‰ÁÉ½™¥±”‰t€ô½Ý¹•È4(€€€€€€€€€€€€€€€É•ÑÕÉ¸©Í½¸¹‘ÕµÁÌ¡™½Õ¹°•¹ÍÕÉ•}…Í¥¤õ…±Í”¤4(€€€€€€€É•ÑÕÉ¸É•ÍÕ±Ð4(4(€€€€Œ1¥µ¥Ð±…µÀlÄ°€ÄÁt4(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡±¥µ¥Ð°¥¹Ð¤è4(€€€€€€€ÑÉäè4(€€€€€€€€€€€±¥µ¥Ð€ô¥¹Ð¡±¥µ¥Ð¤4(€€€€€€€•á•ÁÐ€¡QåÁ•ÉÉ½È°Y…±Õ•ÉÉ½È¤è4(€€€€€€€€€€€±¥µ¥Ð€ô€Ì4(€€€±¥µ¥Ð€ôµ…à Ä°µ¥¸¡±¥µ¥Ð°€ÄÀ¤¤4(4(€€€€Œ	É½ÝÍ”Í¡…Á”è¹¼ÅÕ•ÉäƒŠHÉ••¹ÐÍ•ÍÍ¥½¹Ì¸4(€€€¥˜¹½ÐÅÕ•Éä½È¹½Ð¥Í¥¹ÍÑ…¹”¡ÅÕ•Éä°ÍÑÈ¤½È¹½ÐÅÕ•Éä¹ÍÑÉ¥À ¤è4(€€€€€€€É•ÑÕÉ¸}±¥ÍÑ}É••¹Ñ}Í•ÍÍ¥½¹Ì¡‘ˆ°±¥µ¥Ð°ÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥°±¥¹­}ÁÉ½™¥±”õÁÉ½™¥±”¤4(4(€€€€ŒA…ÉÍ”É½±•}™¥±Ñ•È4(€€€É½±•}±¥ÍÐè=ÁÑ¥½¹…±m1¥ÍÑmÍÑÉut€ô9½¹”4(€€€¥˜¥Í¥¹ÍÑ…¹”¡É½±•}™¥±Ñ•È°ÍÑÈ¤…¹É½±•}™¥±Ñ•È¹ÍÑÉ¥À ¤è4(€€€€€€€É½±•}±¥ÍÐ€ômÈ¹ÍÑÉ¥À ¤™½ÈÈ¥¸É½±•}™¥±Ñ•È¹ÍÁ±¥Ð ˆ°ˆ¤¥˜È¹ÍÑÉ¥À ¥t4(4(€€€€Œ9½Éµ…±¥Í”Í½ÉÐ4(€€€Í½ÉÑ}¹½É´è=ÁÑ¥½¹…±mÍÑÉt€ô9½¹”4(€€€¥˜¥Í¥¹ÍÑ…¹”¡Í½ÉÐ°ÍÑÈ¤è4(€€€€€€€…¹‘¥‘…Ñ”€ôÍ½ÉÐ¹ÍÑÉ¥À ¤¹±½Ý•È ¤4(€€€€€€€¥˜…¹‘¥‘…Ñ”¥¸€ ‰¹•Ý•ÍÐˆ°€‰½±‘•ÍÐˆ¤è4(€€€€€€€€€€€Í½ÉÑ}¹½É´€ô…¹‘¥‘…Ñ”4(4(€€€É•ÑÕÉ¸}‘¥Í½Ù•È (€€€€€€€‘ˆõ‘ˆ°(€€€€€€€ÅÕ•ÉäõÅÕ•Éä¹ÍÑÉ¥À ¤°(€€€€€€€É½±•}™¥±Ñ•ÈõÉ½±•}±¥ÍÐ°(€€€€€€€±¥µ¥Ðõ±¥µ¥Ð°(€€€€€€€Í½ÉÐõÍ½ÉÑ}¹½É´°4(€€€€€€€ÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥õÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥°4(€€€€€€€±¥¹­}ÁÉ½™¥±”õÁÉ½™¥±”°(€€€€¤(()‘•˜Í•ÍÍ¥½¹}Í•…É  (€€€ÅÕ•ÉäèÍÑÈ€ô€ˆˆ°(€€€É½±•}™¥±Ñ•ÈèÍÑÈ€ô9½¹”°(€€€±¥µ¥Ðè¥¹Ð€ô€Ì°(€€€‘ˆõ9½¹”°(€€€ÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥èÍÑÈ€ô9½¹”°(€€€€ŒMÉ½±°Í¡…Á”(€€€Í•ÍÍ¥½¹}¥èÍÑÈ€ô9½¹”°(€€€…É½Õ¹‘}µ•ÍÍ…•}¥è¥¹Ð€ô9½¹”°(€€€Ý¥¹‘½Üè¥¹Ð€ô€Ô°(€€€€Œ¥Í½Ù•ÉäÍ¡…Á”(€€€Í½ÉÐèÍÑÈ€ô9½¹”°(€€€€ŒÉ½ÍÌµÁÉ½™¥±”€¡…¹äÍ¡…Á”¤(€€€ÁÉ½™¥±”èÍÑÈ€ô9½¹”°(¤€´øÍÑÈè(€€€€ˆˆ‰IÕ¸Í•ÍÍ¥½¸Í•…É …¹±½Í”‘…Ñ…‰…Í•Ì½Á•¹•‰äÑ¡¥Ì¥¹Ù½…Ñ¥½¸¸ˆˆˆ(€€€½Ý¹•‘}‘‰Ìè1¥ÍÑm¹åt€ômt(€€€¥˜‘ˆ¥Ì9½¹”è(€€€€€€€ÑÉäè(€€€€€€€€€€€™É½´¡•Éµ•Í}ÍÑ…Ñ”¥µÁ½ÉÐM•ÍÍ¥½¹((€€€€€€€€€€€‘ˆ€ôM•ÍÍ¥½¹ ¤(€€€€€€€€€€€½Ý¹•‘}‘‰Ì¹…ÁÁ•¹¡‘ˆ¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€±½¥¹œ¹‘•‰Õœ ‰M•ÍÍ¥½¹Õ¹…Ù…¥±…‰±”™½ÈÍ•ÍÍ¥½¹}Í•…É ˆ°•á}¥¹™¼õQÉÕ”¤(€€€€€€€€€€€™É½´¡•Éµ•Í}ÍÑ…Ñ”¥µÁ½ÉÐ™½Éµ…Ñ}Í•ÍÍ¥½¹}‘‰}Õ¹…Ù…¥±…‰±”((€€€€€€€€€€€É•ÑÕÉ¸Ñ½½±}•ÉÉ½È¡™½Éµ…Ñ}Í•ÍÍ¥½¹}‘‰}Õ¹…Ù…¥±…‰±” ¤°ÍÕ•ÍÌõ…±Í”¤((€€€ÑÉäè(€€€€€€€É•ÑÕÉ¸}Í•ÍÍ¥½¹}Í•…É¡}¥µÁ° (€€€€€€€€€€€ÅÕ•ÉäõÅÕ•Éä°(€€€€€€€€€€€É½±•}™¥±Ñ•ÈõÉ½±•}™¥±Ñ•È°(€€€€€€€€€€€±¥µ¥Ðõ±¥µ¥Ð°(€€€€€€€€€€€‘ˆõ‘ˆ°(€€€€€€€€€€€ÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥õÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥°(€€€€€€€€€€€Í•ÍÍ¥½¹}¥õÍ•ÍÍ¥½¹}¥°(€€€€€€€€€€€…É½Õ¹‘}µ•ÍÍ…•}¥õ…É½Õ¹‘}µ•ÍÍ…•}¥°(€€€€€€€€€€€Ý¥¹‘½ÜõÝ¥¹‘½Ü°(€€€€€€€€€€€Í½ÉÐõÍ½ÉÐ°(€€€€€€€€€€€ÁÉ½™¥±”õÁÉ½™¥±”°(€€€€€€€€€€€}½Ý¹•‘}‘‰Ìõ½Ý¹•‘}‘‰Ì°(€€€€€€€€¤(€€€™¥¹…±±äè(€€€€€€€™½È½Ý¹•‘}‘ˆ¥¸É•Ù•ÉÍ•¡½Ý¹•‘}‘‰Ì¤è(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€½Ý¹•‘}‘ˆ¹±½Í” ¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€€€€€±½¥¹œ¹‘•‰Õœ ‰…¥±•Ñ¼±½Í”Í•ÍÍ¥½¹}Í•…É M•ÍÍ¥½¹ˆ°•á}¥¹™¼õQÉÕ”¤(()‘•˜¡•­}Í•ÍÍ¥½¹}Í•…É¡}É•ÅÕ¥É•µ•¹ÑÌ ¤€´ø‰½½°è(€€€€ˆˆ‰I•ÅÕ¥É•ÌÑ¡”ME1¥Ñ”ÍÑ…Ñ”‘…Ñ…‰…Í”¸ˆˆˆ4(€€€ÑÉäè4(€€€€€€€™É½´¡•Éµ•Í}ÍÑ…Ñ”¥µÁ½ÉÐ}‘•™…Õ±Ñ}‘‰}Á…Ñ 4(€€€€€€€É•ÑÕÉ¸}‘•™…Õ±Ñ}‘‰}Á…Ñ  ¤¹Á…É•¹Ð¹•á¥ÍÑÌ ¤4(€€€•á•ÁÐ%µÁ½ÉÑÉÉ½Èè4(€€€€€€€É•ÑÕÉ¸…±Í”4(4(4)MMM%=9}MI!}M!5€ôì4(€€€€‰¹…µ”ˆè€‰Í•ÍÍ¥½¹}Í•…É ˆ°4(€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€‰M•…É Á…ÍÐÍ•ÍÍ¥½¹ÌÍÑ½É•¥¸Ñ¡”±½…°Í•ÍÍ¥½¸°½ÈÍÉ½±°¥¹Í¥‘”½¹”¸€ˆ4(€€€€€€€€‰QLÔµ‰…­•É•ÑÉ¥•Ù…°½Ù•ÈÑ¡”ME1¥Ñ”µ•ÍÍ…”ÍÑ½É”¸9¼114…±±ÌƒŠP•Ù•Éä€ˆ4(€€€€€€€€‰Í¡…Á”É•ÑÕÉ¹Ì…ÑÕ…°µ•ÍÍ…•Ì™É½´Ñ¡”¹q¹q¸ˆ4(€€€€€€€€‰M=UIµ%IMP1%5%Qq¹q¸ˆ4(€€€€€€€€ˆ€Q¡¥ÌÑ½½°Í•…É¡•Ì!•Éµ•Ì½¹Ù•ÉÍ…Ñ¥½¸¡¥ÍÑ½Éä½¹±ä¸%Ð¥Ì¹½Ð•Ù¥‘•¹”€ˆ4(€€€€€€€€‰…‰½ÕÐÑ¡”ÕÉÉ•¹Ð½¹Ñ•¹ÑÌ½˜•áÑ•É¹…°Í½ÕÉ•Ì¸%˜Ñ¡”ÕÍ•ÈÁÉ½Ù¥‘•„€ˆ4(€€€€€€€€‰‘¥É•ÐÍ½ÕÉ”ÍÕ …Ì„UI0°Á¡½¹”¹Õµ‰•È½½¹Ñ…Ð°…ÁÀ½Ñ¡É•…°™¥±”Á…Ñ °€ˆ4(€€€€€€€€‰…½Õ¹Ð°Ý•‰Í¥Ñ”°½È±¥Ù”ÍåÍÑ•´°¥¹ÍÁ•ÐÑ¡…Ð½É¥¥¹…°Í½ÕÉ”‰•™½É”½È€ˆ4(€€€€€€€€‰¥¹ÍÑ•…½˜Í•ÍÍ¥½¹}Í•…É Ý¡•¸…•ÍÍ¥‰±”¸UÍ”Í•ÍÍ¥½¹}Í•…É …ÌÍ•½¹‘…Éä€ˆ4(€€€€€€€€‰½¹Ñ•áÐ™½ÈÝ¡…ÐÝ…ÌÁÉ•Ù¥½ÕÍ±äÍ…¥°¹½Ð…ÌÁÉ¥µ…ÉäÁÉ½½˜½˜Ý¡…ÐÑ¡”€ˆ4(€€€€€€€€‰Í½ÕÉ”ÕÉÉ•¹Ñ±ä½¹Ñ…¥¹Ì¸%˜Ñ¡”½É¥¥¹…°Í½ÕÉ”¥Ì¥¹…•ÍÍ¥‰±”°Í…äÍ¼€ˆ4(€€€€€€€€‰…¹Ý¡ä‰•™½É”™…±±¥¹œ‰…¬Ñ¼Í•ÍÍ¥½¸¡¥ÍÑ½Éä¸¼¹½Ð½¹±Õ‘”€¹½Ð™½Õ¹œ€ˆ4(€€€€€€€€‰½È€¹¼ÁÉ¥½È½ÉÉ•ÍÁ½¹‘•¹”œ™É½´Í•ÍÍ¥½¹}Í•…É …±½¹”Ý¡•¸„‘¥É•ÐÍ½ÕÉ”€ˆ4(€€€€€€€€‰Ý…ÌÁÉ½Ù¥‘•¹q¹q¸ˆ4(€€€€€€€€‰=UH11%9M!AMq¹q¸ˆ4(€€€€€€€€ˆ€€Ä¤%M=YIdƒŠPÁ…ÍÌÅÕ•Éå€éq¸ˆ4(€€€€€€€€ˆ€€€€Í•ÍÍ¥½¹}Í•…É ¡ÅÕ•Éäõp‰…ÕÑ É•™…Ñ½Épˆ°±¥µ¥ÐôÌ¥q¸ˆ4(€€€€€€€€ˆ€€€€IÕ¹ÌQLÔ°‘•‘ÕÁ•Ì¡¥ÑÌ‰äÍ•ÍÍ¥½¸±¥¹•…”°É•ÑÕÉ¹ÌÑ¡”Ñ½À8Í•ÍÍ¥½¹Ì¸€ˆ4(€€€€€€€€‰… É•ÍÕ±Ð…ÉÉ¥•Ìéq¸ˆ4(€€€€€€€€ˆ€€€€€€€´Í•ÍÍ¥½¹}¥°Ñ¥Ñ±”°Ý¡•¸°Í½ÕÉ•q¸ˆ4(€€€€€€€€ˆ€€€€€€€´Í¹¥ÁÁ•ÐèQLÔµ¡¥¡±¥¡Ñ•µ…Ñ •á•ÉÁÑq¸ˆ4(€€€€€€€€ˆ€€€€€€€´‰½½­•¹‘}ÍÑ…ÉÐè™¥ÉÍÐ€ÌÕÍ•È­…ÍÍ¥ÍÑ…¹Ðµ•ÍÍ…•Ì½˜Ñ¡”Í•ÍÍ¥½¸€ˆ4(€€€€€€€€ˆ¡Ñ¡”½…°€¼­¥­½™˜¥q¸ˆ4(€€€€€€€€ˆ€€€€€€€´µ•ÍÍ…•Ìèƒ
ÄÔµ•ÍÍ…•Ì…É½Õ¹Ñ¡”QLÔµ…Ñ °Ý¥Ñ Ñ¡”…¹¡½Èµ•ÍÍ…”€ˆ4(€€€€€€€€‰™±…•€¡Ñ¡”¡¥Ð¥¸½¹Ñ•áÐ¥q¸ˆ4(€€€€€€€€ˆ€€€€€€€´‰½½­•¹‘}•¹è±…ÍÐ€ÌÕÍ•È­…ÍÍ¥ÍÑ…¹Ðµ•ÍÍ…•Ì½˜Ñ¡”Í•ÍÍ¥½¸€ˆ4(€€€€€€€€ˆ¡Ñ¡”É•Í½±ÕÑ¥½¸€¼‘•¥Í¥½¹Ì¥q¸ˆ4(€€€€€€€€ˆ€€€€€€€´µ…Ñ¡}µ•ÍÍ…•}¥°µ•ÍÍ…•Í}‰•™½É”°µ•ÍÍ…•Í}…™Ñ•Éq¸ˆ4(€€€€€€€€ˆ€€€€	½½­•¹‘Ì€¬Ý¥¹‘½ÜÑ½•Ñ¡•È±•Ðå½ÔÉ•½¹ÍÑÉÕÐ½…°ƒŠHµ…Ñ ƒŠHÉ•Í½±ÕÑ¥½¸€ˆ4(€€€€€€€€‰Ý¥Ñ¡½ÕÐÁ…å¥¹œ™½ÈÑ¡”Ý¡½±”ÑÉ…¹ÍÉ¥ÁÐ¹q¹q¸ˆ4(€€€€€€€€ˆ€€È¤MI=10ƒŠPÁ…ÍÌÍ•ÍÍ¥½¹}¥‘€€¬…É½Õ¹‘}µ•ÍÍ…•}¥‘€éq¸ˆ4(€€€€€€€€ˆ€€€€Í•ÍÍ¥½¹}Í•…É ¡Í•ÍÍ¥½¹}¥õpˆ¸¸¹pˆ°…É½Õ¹‘}µ•ÍÍ…•}¥ôÄÈÌÐÔ°Ý¥¹‘½ÜôÄÀ¥q¸ˆ4(€€€€€€€€ˆ€€€€I•ÑÕÉ¹Ì„Ý¥¹‘½Ü½˜ƒ
ÅÝ¥¹‘½Ý€µ•ÍÍ…•Ì•¹Ñ•É•½¸Ñ¡”…¹¡½È¸9¼QLÔ°€ˆ4(€€€€€€€€‰¹¼‰½½­•¹‘ÌƒŠP©ÕÍÐÑ¡”Í±¥”¸UÍ”…™Ñ•È„‘¥Í½Ù•Éä…±°Ý¡•¸å½Ô¹••µ½É”€ˆ4(€€€€€€€€‰½¹Ñ•áÐÑ¡…¸Ñ¡”ƒ
ÄÔ‘•™…Õ±ÐÝ¥¹‘½Ü¹q¸ˆ4(€€€€€€€€ˆ€€€€€€€´Q¼ÍÉ½±°=I]IèÁ…ÍÌµ•ÍÍ…•Íl´Åt¹¥‰…¬…Ì…É½Õ¹‘}µ•ÍÍ…•}¥¹q¸ˆ4(€€€€€€€€ˆ€€€€€€€´Q¼ÍÉ½±°	-]IèÁ…ÍÌµ•ÍÍ…•ÍlÁt¹¥‰…¬…Ì…É½Õ¹‘}µ•ÍÍ…•}¥¹q¸ˆ4(€€€€€€€€ˆ€€€€€€€´Q¡”‰½Õ¹‘…Éäµ•ÍÍ…”…ÁÁ•…ÉÌ¥¸‰½Ñ Ý¥¹‘½ÝÌƒŠP½É¥•¹Ñ…Ñ¥½¸µ…É­•È¹q¸ˆ4(€€€€€€€€ˆ€€€€€€€´]¡•¸µ•ÍÍ…•Í}‰•™½É”½Èµ•ÍÍ…•Í}…™Ñ•È¥Ì€ðÝ¥¹‘½Ü°å½ÔÉ”…ÐÑ¡”€ˆ4(€€€€€€€€‰ÍÑ…ÉÐ½È•¹½˜Ñ¡”Í•ÍÍ¥½¸¹q¹q¸ˆ4(€€€€€€€€ˆ€€Ì¤IƒŠPÁ…ÍÌÍ•ÍÍ¥½¹}¥‘€½¹±ä€¡¹¼…É½Õ¹‘}µ•ÍÍ…•}¥¤éq¸ˆ4(€€€€€€€€ˆ€€€€Í•ÍÍ¥½¹}Í•…É ¡Í•ÍÍ¥½¹}¥õpˆ¸¸¹pˆ°ÁÉ½™¥±”õp‰Ý½É­pˆ¥q¸ˆ4(€€€€€€€€ˆ€€€€ÕµÁÌÑ¡”Ý¡½±”Í•ÍÍ¥½¸‰ä¥€¡™¥ÉÍÐ€ÈÀ€¬±…ÍÐ€ÄÀµ•ÍÍ…•ÌÝ¡•¸€ˆ4(€€€€€€€€‰±…É”¤¸Q¡¥Ì¥Ì¡½Üå½ÔÉ•Í½±Ù”…¸Í•ÍÍ¥½¸èñÁÉ½™¥±”ø¼ñ¥ù€±¥¹¬Ñ¡”€ˆ4(€€€€€€€€‰ÕÍ•È‘É½ÁÁ•¥¹Ñ¼Ñ¡”¡…ÐèÍÁ±¥ÐÑ¡”Ù…±Õ”½¸€½€¥¹Ñ¼ÁÉ½™¥±”€¬¥€ˆ4(€€€€€€€€‰…¹…±°Í•ÍÍ¥½¹}Í•…É ¡Í•ÍÍ¥½¹}¥õ¥°ÁÉ½™¥±”õÁÉ½™¥±”¤¹q¹q¸ˆ4(€€€€€€€€ˆ€€Ð¤	I=]MƒŠP¹¼…ÉÌéq¸ˆ4(€€€€€€€€ˆ€€€€Í•ÍÍ¥½¹}Í•…É  ¥q¸ˆ4(€€€€€€€€ˆ€€€€I•ÑÕÉ¹ÌÉ••¹ÐÍ•ÍÍ¥½¹Ì¡É½¹½±½¥…±±äèÑ¥Ñ±•Ì°ÁÉ•Ù¥•ÝÌ°Ñ¥µ•ÍÑ…µÁÌ¸€ˆ4(€€€€€€€€‰UÍ”Ý¡•¸Ñ¡”ÕÍ•È…Í­Ìp‰Ý¡…ÐÝ…Ì$Ý½É­¥¹œ½¹pˆÝ¥Ñ¡½ÕÐ¹…µ¥¹œ„Ñ½Á¥Œ¹q¹q¸ˆ4(€€€€€€€€‰1%9-%9Q!UMHQ<MMM%=9q¹q¸ˆ4(€€€€€€€€ˆ€]¡•¸å½ÔÉ•™•ÈÑ¡”ÕÍ•ÈÑ¼„Í•ÍÍ¥½¸°ÝÉ¥Ñ”¥ÑÌ±¥¹­€Ù…±Õ”¥¹±¥¹”¥¸€ˆ4(€€€€€€€€‰å½ÕÈÉ•Á±äƒŠP•Ù•ÉäÉ•ÍÕ±Ð…ÉÉ¥•Ì½¹”°”¹œ¸€ˆ4(€€€€€€€€‰Í•ÍÍ¥½¸é‘•™…Õ±Ð¼ÈÀÈØÀÜÈÉ|ÈÀÐÌÌÕ}ØÉŒÄÙ€¸½Áä¥ÐÙ•É‰…Ñ¥´ì‘¼¹½Ð€ˆ4(€€€€€€€€‰É•™½Éµ…Ð¥Ð…Ì„µ…É­‘½Ý¸±¥¹¬½ÈÝÉ…À¥Ð¥¸‰…­Ñ¥­Ì¸!•Éµ•ÌÉ•¹‘•ÉÌ€ˆ4(€€€€€€€€‰¥Ð…Ì„±¥¹¬Í¡½Ý¥¹œÑ¡”Í•ÍÍ¥½¸ÌÑ¥Ñ±”°Í¼Ñ¡”±¥¹¬%LÑ¡”Ñ¥Ñ±”è€ˆ4(€€€€€€€€‰ÕÍ”¥Ð…Ì„¹½Õ¸µ¥µÍ•¹Ñ•¹”€¡p‰Ñ¡…ÐÌÍ•ÍÍ¥½¸é‘•™…Õ±Ð¼¸¸¸ƒŠPÝ…¹Ðµ”€ˆ4(€€€€€€€€‰Ñ¼Á¥¬¥ÐÕÀýpˆ¤°¹•Ù•È…±½¹”½¸¥ÑÌ½Ý¸±¥¹”°…¹¹•Ù•È…±½¹Í¥‘”Ñ¡”€ˆ4(€€€€€€€€‰Ñ¥Ñ±”°¥°½È‘…Ñ”ÍÁ•±±•½ÕÐƒŠPÑ¡…ÐÍ¡½ÝÌÑ¡”ÕÍ•ÈÑ¡”Í…µ”Í•ÍÍ¥½¸€ˆ4(€€€€€€€€‰ÑÝ¥”¹q¹q¸ˆ4(€€€€€€€€‰QLÔMe9Qaq¹q¸ˆ4(€€€€€€€€ˆ€9¥ÌÑ¡”‘•™…Õ±ÐƒŠPµÕ±Ñ¤µÝ½ÉÅÕ•É¥•ÌÉ•ÅÕ¥É”…±°Ñ•ÉµÌ¸UÍ”=H•áÁ±¥¥Ñ±ä€ˆ4(€€€€€€€€‰™½È‰É½…‘•ÈÉ•…±°€¡…±Á¡„=H‰•Ñ„=H…µµ…€¤°ÅÕ½Ñ•Á¡É…Í•Ì™½È•á…Ðµ…Ñ €ˆ4(€€€€€€€€ˆ¡p‰‘½­•È¹•ÑÝ½É­¥¹p‰€¤°‰½½±•…¸€¡ÁåÑ¡½¸9=P©…Ù…€¤°½ÈÁÉ•™¥àÝ¥±‘…É‘Ì€ˆ4(€€€€€€€€ˆ¡‘•Á±½ä©€¤¹q¹q¸ˆ4(€€€€€€€€‰]!8Q<UMq¹q¸ˆ4(€€€€€€€€ˆ€I•… ™½ÈÑ¡¥Ì½¸ÅÕ•ÍÑ¥½¹Ì…‰½ÕÐ!•Éµ•Ì½¹Ù•ÉÍ…Ñ¥½¸¡¥ÍÑ½Éä¥ÑÍ•±˜°ÍÕ €ˆ4(€€€€€€€€‰…Ìp‰Ý¡…Ð‘¥Ý”‘¼…‰½ÕÐapˆ°p‰Ý¡•É”‘¥Ý”±•…Ù”epˆ°½Èp‰™¥¹Ñ¡”€ˆ4(€€€€€€€€‰Í•ÍÍ¥½¸Ý¡•É”ipˆ¸%˜Ñ¡”ÕÍ•ÈÁÉ½Ù¥‘•„‘¥É•ÐÍ½ÕÉ”¥‘•¹Ñ¥™¥•È°¥¹ÍÁ•Ð€ˆ4(€€€€€€€€‰Ñ¡…ÐÍ½ÕÉ”™¥ÉÍÐÝ¡•¸…•ÍÍ¥‰±”ìÍ•ÍÍ¥½¹}Í•…É …¸Ñ¡•¸ÍÕÁÁ±ä¡¥ÍÑ½É¥…°€ˆ4(€€€€€€€€‰½¹Ñ•áÐ¸Q¡”Í•ÍÍ¥½¸…ÉÉ¥•ÌÝ¡…ÐÝ…ÌÍ…¥Ý¡•¸ì•áÑ•É¹…°Ñ½½±ÌÍ¡½Ü€ˆ4(€€€€€€€€‰ÕÉÉ•¹ÐÍ½ÕÉ”½Ý½É±ÍÑ…Ñ”¸ˆ4(€€€€¤°4(€€€€‰Á…É…µ•Ñ•ÉÌˆèì4(€€€€€€€€‰ÑåÁ”ˆè€‰½‰©•Ðˆ°4(€€€€€€€€‰ÁÉ½Á•ÉÑ¥•Ìˆèì4(€€€€€€€€€€€€‰ÅÕ•Éäˆèì4(€€€€€€€€€€€€€€€€‰ÑåÁ”ˆè€‰ÍÑÉ¥¹œˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€€€€€€€€€€€€€‰M•…É ÅÕ•Éä€¡‘¥Í½Ù•ÉäÍ¡…Á”¤¸-•åÝ½É‘Ì°Á¡É…Í•Ì°½È‰½½±•…¸€ˆ4(€€€€€€€€€€€€€€€€€€€€‰•áÁÉ•ÍÍ¥½¹ÌÑ¼™¥¹¥¸Á…ÍÐÍ•ÍÍ¥½¹Ì¸=µ¥ÐÑ¼‰É½ÝÍ”É••¹Ð€ˆ4(€€€€€€€€€€€€€€€€€€€€‰Í•ÍÍ¥½¹Ì¸%¹½É•Ý¡•¸Í•ÍÍ¥½¹}¥€¬…É½Õ¹‘}µ•ÍÍ…•}¥…É”Í•Ð€ˆ4(€€€€€€€€€€€€€€€€€€€€ˆ¡ÍÉ½±°Í¡…Á”¤¸ˆ4(€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€€‰±¥µ¥Ðˆèì4(€€€€€€€€€€€€€€€€‰ÑåÁ”ˆè€‰¥¹Ñ••Èˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€€€€€€€€€€€€€‰¥Í½Ù•ÉäÍ¡…Á”½¹±ä¸5…àÍ•ÍÍ¥½¹ÌÑ¼É•ÑÕÉ¸€¡‘•™…Õ±Ð€Ì°µ…à€ÄÀ¤¸€ˆ4(€€€€€€€€€€€€€€€€€€€€‰	ÕµÀÑ¼€×ŠLÄÀÝ¡•¸Ñ¡”Ñ½Á¥Œ±¥­•±äÍÁ…¹ÌÍ•Ù•É…°Í•ÍÍ¥½¹Ì…¹å½Ô€ˆ4(€€€€€€€€€€€€€€€€€€€€‰Ý…¹ÐÑ¼Á¥¬Ñ¡”É¥¡Ð½¹”Ñ¼ÍÉ½±°¥¹Ñ¼¸ˆ4(€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€€€€€€‰‘•™…Õ±Ðˆè€Ì°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€€‰Í½ÉÐˆèì4(€€€€€€€€€€€€€€€€‰ÑåÁ”ˆè€‰ÍÑÉ¥¹œˆ°4(€€€€€€€€€€€€€€€€‰•¹Õ´ˆèl‰¹•Ý•ÍÐˆ°€‰½±‘•ÍÐ‰t°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€€€€€€€€€€€€€‰¥Í½Ù•ÉäÍ¡…Á”½¹±ä¸Q•µÁ½É…°‰¥…Ì½¸Ñ½À½˜QLÔÉ…¹­¥¹œ¸=µ¥Ð€ˆ4(€€€€€€€€€€€€€€€€€€€€‰Ñ¼­••ÀÉ•±•Ù…¹”µ½¹±ä½É‘•É¥¹œ€¡ÍÕ¥Ñ…‰±”™½È•áÁ±½É…Ñ½ÉäÉ•…±°ƒŠP€ˆ4(€€€€€€€€€€€€€€€€€€€€‰p‰Ý¡…Ð‘¼Ý”­¹½Ü…‰½ÕÐapˆ¤¸M•Ð€¹•Ý•ÍÐœ™½ÈÉ••¹äµÍ¡…Á•€ˆ4(€€€€€€€€€€€€€€€€€€€€‰ÅÕ•ÍÑ¥½¹Ì€¡p‰Ý¡•É”‘¥Ý”±•…Ù”apˆ¤¸M•Ð€½±‘•ÍÐœ™½È€ˆ4(€€€€€€€€€€€€€€€€€€€€‰½É¥¥¸µÍ¡…Á•ÅÕ•ÍÑ¥½¹Ì€¡p‰¡½Ü‘¥`ÍÑ…ÉÑpˆ¤¸%¹½É•¥¸ÍÉ½±°€ˆ4(€€€€€€€€€€€€€€€€€€€€‰…¹‰É½ÝÍ”Í¡…Á•Ì¸ˆ4(€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€€‰Í•ÍÍ¥½¹}¥ˆèì4(€€€€€€€€€€€€€€€€‰ÑåÁ”ˆè€‰ÍÑÉ¥¹œˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€€€€€€€€€€€€€‰MÉ½±°Í¡…Á”¸M•ÍÍ¥½¸Ñ¼É•…¥¹Í¥‘”¸UÍ”Ñ¡”Í•ÍÍ¥½¹}¥É•ÑÕÉ¹•€ˆ4(€€€€€€€€€€€€€€€€€€€€‰™É½´„ÁÉ¥½È‘¥Í½Ù•Éä…±°¸5ÕÍÐ‰”Á…¥É•Ý¥Ñ €ˆ4(€€€€€€€€€€€€€€€€€€€€‰…É½Õ¹‘}µ•ÍÍ…•}¥¸ˆ4(€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€€‰…É½Õ¹‘}µ•ÍÍ…•}¥ˆèì4(€€€€€€€€€€€€€€€€‰ÑåÁ”ˆè€‰¥¹Ñ••Èˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€€€€€€€€€€€€€‰MÉ½±°Í¡…Á”¸5•ÍÍ…”¥Ñ¼•¹Ñ•ÈÑ¡”Ý¥¹‘½Ü½¸¸É½´„‘¥Í½Ù•Éä€ˆ4(€€€€€€€€€€€€€€€€€€€€‰É•ÍÕ±ÐÕÍ”µ…Ñ¡}µ•ÍÍ…•}¥°½È…¹ä¥Í••¸¥¸„ÁÉ¥½ÈÝ¥¹‘½Ü¸Q¼€ˆ4(€€€€€€€€€€€€€€€€€€€€‰ÍÉ½±°™½ÉÝ…ÉÁ…ÍÌÑ¡”±…ÍÐÝ¥¹‘½Üµ•ÍÍ…”Ì¥ìÑ¼ÍÉ½±°€ˆ4(€€€€€€€€€€€€€€€€€€€€‰‰…­Ý…ÉÁ…ÍÌÑ¡”™¥ÉÍÐ¸ˆ4(€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€€‰Ý¥¹‘½Üˆèì4(€€€€€€€€€€€€€€€€‰ÑåÁ”ˆè€‰¥¹Ñ••Èˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€€€€€€€€€€€€€‰MÉ½±°Í¡…Á”½¹±ä¸5•ÍÍ…•ÌÑ¼É•ÑÕÉ¸½¸•… Í¥‘”½˜Ñ¡”…¹¡½È€ˆ4(€€€€€€€€€€€€€€€€€€€€ˆ¡…¹¡½È¥ÑÍ•±˜…±Ý…åÌ¥¹±Õ‘•¤¸±…µÁ•Ñ¼lÄ°€ÈÁt¸•™…Õ±Ð€Ô¸ˆ4(€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€€€€€€‰‘•™…Õ±Ðˆè€Ô°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€€‰É½±•}™¥±Ñ•Èˆèì4(€€€€€€€€€€€€€€€€‰ÑåÁ”ˆè€‰ÍÑÉ¥¹œˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€€€€€€€€€€€€€‰=ÁÑ¥½¹…°¸½µµ„µÍ•Á…É…Ñ•É½±•ÌÑ¼¥¹±Õ‘”¸¥Í½Ù•Éä‘•™…Õ±ÑÌÑ¼€ˆ4(€€€€€€€€€€€€€€€€€€€€ˆÕÍ•È±…ÍÍ¥ÍÑ…¹Ðœ€¡Ñ½½°½ÕÑÁÕÐ¥ÌÕÍÕ…±±ä¹½¥Í”¤¸A…ÍÌ€ˆ4(€€€€€€€€€€€€€€€€€€€€ˆÕÍ•È±…ÍÍ¥ÍÑ…¹Ð±Ñ½½°œÑ¼¥¹±Õ‘”Ñ½½°½ÕÑÁÕÐ€¡‘•‰Õ¥¹œÑ½½°€ˆ4(€€€€€€€€€€€€€€€€€€€€‰‰•¡…Ù¥½ÕÈ¤½È€Ñ½½°œÑ¼Í•…É Ñ½½°½ÕÑÁÕÐ½¹±ä¸ˆ4(€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€ô°4(€€€€€€€€€€€€‰ÁÉ½™¥±”ˆèì4(€€€€€€€€€€€€€€€€‰ÑåÁ”ˆè€‰ÍÑÉ¥¹œˆ°4(€€€€€€€€€€€€€€€€‰‘•ÍÉ¥ÁÑ¥½¸ˆè€ 4(€€€€€€€€€€€€€€€€€€€€‰=ÁÑ¥½¹…°¸I•…Í•ÍÍ¥½¹Ì™É½´…¹½Ñ¡•È!•Éµ•ÌÁÉ½™¥±”Ì‘…Ñ…‰…Í”€ˆ4(€€€€€€€€€€€€€€€€€€€€ˆ¡É•…µ½¹±ä¤¸UÍ”Ý¡•¸É•Í½±Ù¥¹œ…¸Í•ÍÍ¥½¸èñÁÉ½™¥±”ø¼ñ¥ù€±¥¹¬è€ˆ4(€€€€€€€€€€€€€€€€€€€€‰Á…ÍÌÑ¡”ÁÉ½™¥±”Í•µ•¹Ð¡•É”Ý¥Ñ Í•ÍÍ¥½¹}¥…ÌÑ¡”¥Í•µ•¹Ð¸€ˆ4(€€€€€€€€€€€€€€€€€€€€‰=µ¥ÐÑ¼ÕÍ”Ñ¡”ÕÉÉ•¹ÐÁÉ½™¥±”¸ˆ4(€€€€€€€€€€€€€€€€¤°4(€€€€€€€€€€€ô°4(€€€€€€€ô°4(€€€€€€€€‰É•ÅÕ¥É•ˆèmt°4(€€€ô°4)ô4(4(4(Œ€´´´I•¥ÍÑÉä€´´´4)™É½´Ñ½½±Ì¹É•¥ÍÑÉä¥µÁ½ÉÐÉ•¥ÍÑÉä°Ñ½½±}•ÉÉ½È4(4)É•¥ÍÑÉä¹É•¥ÍÑ•È 4(€€€¹…µ”ô‰Í•ÍÍ¥½¹}Í•…É ˆ°4(€€€Ñ½½±Í•Ðô‰Í•ÍÍ¥½¹}Í•…É ˆ°4(€€€Í¡•µ„õMMM%=9}MI!}M!5°4(€€€¡…¹‘±•Èõ±…µ‰‘„…ÉÌ°€¨©­ÜèÍ•ÍÍ¥½¹}Í•…É  4(€€€€€€€ÅÕ•Éäõ…ÉÌ¹•Ð ‰ÅÕ•Éäˆ¤½È€ˆˆ°4(€€€€€€€É½±•}™¥±Ñ•Èõ…ÉÌ¹•Ð ‰É½±•}™¥±Ñ•Èˆ¤°4(€€€€€€€±¥µ¥Ðõ…ÉÌ¹•Ð ‰±¥µ¥Ðˆ°€Ì¤°4(€€€€€€€Í•ÍÍ¥½¹}¥õ…ÉÌ¹•Ð ‰Í•ÍÍ¥½¹}¥ˆ¤°4(€€€€€€€…É½Õ¹‘}µ•ÍÍ…•}¥õ…ÉÌ¹•Ð ‰…É½Õ¹‘}µ•ÍÍ…•}¥ˆ¤°4(€€€€€€€Ý¥¹‘½Üõ…ÉÌ¹•Ð ‰Ý¥¹‘½Üˆ°€Ô¤°4(€€€€€€€Í½ÉÐõ…ÉÌ¹•Ð ‰Í½ÉÐˆ¤°4(€€€€€€€ÁÉ½™¥±”õ…ÉÌ¹•Ð ‰ÁÉ½™¥±”ˆ¤°4(€€€€€€€‘ˆõ­Ü¹•Ð ‰‘ˆˆ¤°4(€€€€€€€ÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥õ­Ü¹•Ð ‰ÕÉÉ•¹Ñ}Í•ÍÍ¥½¹}¥ˆ¤°4(€€€€¤°4(€€€¡•­}™¸õ¡•­}Í•ÍÍ¥½¹}Í•…É¡}É•ÅÕ¥É•µ•¹ÑÌ°4(€€€•µ½©¤ô‹Â~R4ˆ°4(¤4