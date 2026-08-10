#!/usr/bin/env python3
"""
SQLite State Store for Hermes Agent.

Provides persistent session storage with FTS5 full-text search, replacing
the per-session JSONL file approach. Stores session metadata, full message
history, and model configuration for CLI and gateway sessions.

Key design decisions:
- WAL mode for concurrent readers + one writer (gateway multi-platform)
- FTS5 virtual table for fast text search across all session messages
- Compression-triggered session splitting via parent_session_id chains
- Batch runner and RL trajectories are NOT stored here (separate systems)
- Session source tagging ('cli', 'telegram', 'discord', etc.) for filtering
"""

import asyncio
import atexit
import errno
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from agent.memory_manager import sanitize_context
from agent.session_activity import ActivityProvenance
from agent.message_sanitization import _sanitize_surrogates
from agent.skill_commands import (
    SKILL_EXCERPT_JOINT,
    SKILL_SCAFFOLD_SQL_LIKE,
    describe_skill_invocation,
)
from hermes_constants import get_hermes_home
from hermes_cli.sqlite_runtime import (
    is_sqlite_wal_reset_vulnerable as _is_sqlite_wal_reset_vulnerable,
)
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from hermes_state_common import (  # noqa: F401  (re-exported for back-compat)
    _BRANCH_CHILD_SQL,
    _COMPRESSION_CHILD_SQL,
    _FTS_CJK_TRIGGERS,
    _FTS_TRIGGERS,
    _LISTABLE_CHILD_SQL,
    _PREVIEW_RAW_SELECT,
    _ephemeral_child_sql,
    _shape_preview,
    _sql_session_last_active,
    _sql_session_last_active_by_id,
    escape_like as _escape_like,
    DEFERRED_INDEX_SQL,
    FTS_CJK_STALE_KEY,
    FTS_SQL,
    FTS_STALE_KEY,
    FTS_STORAGE_VERSION,
    FTS_TRIGRAM_SQL,
    LEGACY_FTS_SQL,
    LEGACY_FTS_TRIGRAM_SQL,
    MAX_FTS5_QUERY_CHARS,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    _PREVIEW_CONTENT_SQL,
    _PREVIEW_HEAD_CHARS,
    _PREVIEW_MAX_CHARS,
    _PREVIEW_SCAFFOLD_WINDOW,
    _PREVIEW_SCAFFOLDED_SQL,
)
from hermes_state_portability import SessionPortabilityMixin
from hermes_state_schema import SessionSchemaMixin
from hermes_state_search import SessionSearchMixin

try:  # Hard dependency, but tolerate scaffold-phase imports before pip install.
    import psutil
except ImportError:  # pragma: no cover - stripped/scaffold installs only
    psutil = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

MAX_SAFE_RESUME_MESSAGES = 20_000
MAX_SAFE_EXPORT_MESSAGES = 20_000


def _configured_transcript_limit(key: str, fallback: int) -> int:
    """Resolve a transcript safety limit from config at call time.

    Reads ``sessions.<key>`` from config.yaml lazily (avoiding a circular
    import at module load) and falls back to the module constant when the
    config subsystem is unavailable (scaffold installs, stripped test
    environments). A value of 0 disables the guard entirely. No caching:
    ``load_config_readonly`` is already mtime-cached, and resolving fresh
    keeps tests that monkeypatch config or the module constants working.
    """
    try:
        from hermes_cli.config import load_config_readonly

        sessions_cfg = load_config_readonly().get("sessions") or {}
        value = sessions_cfg.get(key)
        if value is None:
            return fallback
        limit = int(value)
        return limit if limit >= 0 else fallback
    except Exception:
        return fallback


def resolved_max_resume_messages() -> int:
    """Config-resolved resume guard limit (0 disables the guard)."""
    return _configured_transcript_limit(
        "max_resume_messages", MAX_SAFE_RESUME_MESSAGES
    )


def resolved_max_export_messages() -> int:
    """Config-resolved in-memory export guard limit (0 disables the guard)."""
    return _configured_transcript_limit(
        "max_export_messages", MAX_SAFE_EXPORT_MESSAGES
    )


class SessionResumeTooLargeError(ValueError):
    def __init__(
        self,
        message_count: int,
        limit: int = MAX_SAFE_RESUME_MESSAGES,
        scope: str = "across its lineage",
    ):
        self.message_count = message_count
        self.limit = limit
        super().__init__(
            f"session has at least {message_count} active messages {scope}; "
            f"safe resume limit is {limit}. Export the session instead, or set "
            "sessions.max_resume_messages: 0 in config.yaml to disable the guard."
        )


class SessionExportTooLargeError(ValueError):
    def __init__(
        self,
        session_id: str,
        message_count: int,
        limit: int = MAX_SAFE_EXPORT_MESSAGES,
    ):
        self.session_id = session_id
        self.message_count = message_count
        self.limit = limit
        super().__init__(
            f"session '{session_id}' has at least {message_count} active messages; "
            f"safe in-memory export limit is {limit}"
        )


_COMPRESSION_LOCK_HOLDER_PID_RE = re.compile(r"(?:^|:)pid=(\d+)(?::|$)")


def _system_prompt_hash(system_prompt: str) -> str:
    return hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()


def _compression_lock_holder_process_is_dead(holder: str) -> bool:
    """Return True only when a structured lock holder's local PID is gone.

    Compression locks are stored in a host-local SQLite database and holder
    IDs created by ``conversation_compression`` start with ``pid=<n>``. A
    process killed during gateway shutdown cannot release its lease, so waiting
    for the full TTL makes every new turn repeatedly attempt compaction. Reclaim
    only when the kernel proves that PID no longer exists; legacy/unstructured
    holders, same-process holders, permission errors, and any probe doubt
    remain protected until normal TTL expiry (conservative: PID reuse must
    never steal a live lease, and a wrongly-kept lease self-heals via TTL).
    """
    match = _COMPRESSION_LOCK_HOLDER_PID_RE.search(holder or "")
    if match is None:
        return False
    try:
        pid = int(match.group(1))
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        # Same-process holder (e.g. another thread's live lease): never
        # self-reclaim â€” the lease refresher and release path own it.
        return False
    if psutil is not None:
        try:
            # psutil is the canonical cross-platform liveness answer
            # (CONTRIBUTING.md "Critical rules" #1). pid_exists() reports
            # recycled PIDs as alive â€” conservative, the TTL still applies.
            return not psutil.pid_exists(pid)
        except Exception:
            return False  # any doubt â†’ keep the lease until TTL expiry
    # Scaffold-phase fallback only (psutil missing), and POSIX-only: stdlib
    # os.kill(pid, 0) is NOT a no-op probe on Windows (bpo-14484 â€” sig=0 maps
    # to CTRL_C_EVENT and can kill the target's console group). Without psutil
    # a Windows host stays TTL-only; the lease TTL remains the recovery path.
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)  # windows-footgun: ok â€” nt early-returns just above
    except ProcessLookupError:
        return True
    except (PermissionError, OSError, OverflowError):
        return False
    return False


def _scrub_surrogates(value: Any) -> Any:
    """Replace lone surrogates when *value* is text; pass anything else through.

    sqlite3 encodes bound ``str`` parameters as UTF-8 and raises
    ``UnicodeEncodeError`` on lone surrogates (U+D800..U+DFFF), so a single
    such code point anywhere in a message aborts the whole write. No-op for
    well-formed text.
    """
    return _sanitize_surrogates(value) if isinstance(value, str) else value


def workspace_key(row: Dict[str, Any]) -> Optional[str]:
    """A session's workspace grouping key: its git repo root when known, else
    its cwd.

    Branch is deliberately excluded so checking out a new branch doesn't
    fragment a workspace's session history. Returns None for cwd-less (unbound)
    sessions. Both fields are already recorded on ``sessions`` â€” this just picks
    the coarser identity for grouping/filtering.
    """
    root = (row.get("git_repo_root") or "").strip()
    if root:
        return root

    cwd = (row.get("cwd") or "").strip()
    return cwd or None


def _delegate_from_json(col: str = "model_config") -> str:
    return f"json_extract(COALESCE({col}, '{{}}'), '$._delegate_from')"


# Sentinel returned by SessionDB._merge_model_config_json when the session row
# doesn't exist and on_missing="skip" â€” distinguishes "no row" from the legal
# None result ("merged config is empty â†’ store NULL").
_MODEL_CONFIG_ROW_MISSING = object()


def _cwd_prefix_clause(cwd_prefix: str) -> Tuple[str, List[str]]:
    prefix = cwd_prefix.rstrip("/\\") or cwd_prefix
    # ``_`` and ``%`` are LIKE wildcards but ordinary characters in a path
    # (``my_project``), so an unescaped prefix also matches sibling directories.
    # Escape the needle and pair it with ESCAPE; the literal separator
    # backslash in the Windows pattern needs escaping for the same reason. The
    # ``=`` arm is an exact compare and keeps the raw prefix.
    esc = _escape_like(prefix)
    return (
        "(s.cwd = ? OR s.cwd LIKE ? ESCAPE '\\' OR s.cwd LIKE ? ESCAPE '\\')",
        [prefix, f"{esc}/%", f"{esc}\\\\%"],
    )


def _workspace_key_clause(key: str) -> Tuple[str, List[str]]:
    """Match sessions whose ``workspace_key(row)`` equals ``key``.

    Mirrors :func:`workspace_key`: a session belongs to workspace ``key``
    when its recorded ``git_repo_root`` equals ``key``, or â€” for rows that
    predate per-session git metadata â€” when its ``cwd`` is at or under
    ``key`` (so a session started in ``repo/src`` still groups with ``repo``).
    Used by ``hermes -c``/``--resume`` to continue the most recent session in
    the *current* workspace rather than the global MRU.
    """
    prefix = key.rstrip("/\\") or key
    cwd_clause, cwd_params = _cwd_prefix_clause(prefix)
    return (
        f"(s.git_repo_root = ? OR (COALESCE(s.git_repo_root, '') = '' AND {cwd_clause}))",
        [prefix, *cwd_params],
    )


def _collect_delegate_child_ids(conn, parent_ids: List[str]) -> List[str]:
    """Delegate-subagent ids to cascade-delete with *parent_ids*.

    Only rows carrying the ``_delegate_from`` marker (set at creation, and
    backfilled by the v16 migration) â€” generic untagged children keep the
    orphan-don't-delete contract. Walks marker chains recursively so an
    orchestrator subagent's own delegate children go too (FK safety).
    """
    df = _delegate_from_json()
    seeds = {sid for sid in parent_ids if sid}
    # Seed the visited set with the parents themselves. A delegation marker
    # chain can loop back onto a parent â€” a cycle, or a parent that is also
    # another parent's delegate child when several ids are deleted at once â€”
    # and without this guard that parent would be collected as one of its own
    # descendants and cascade-deleted along with all of its messages. Callers
    # delete the parents separately, so parents must never appear in the
    # returned child set. (#49148)
    found: set[str] = set(seeds)
    frontier = list(seeds)
    while frontier:
        ph = ",".join("?" * len(frontier))
        cursor = conn.execute(
            f"SELECT id FROM sessions WHERE {df} IN ({ph}) "
            f"OR (parent_session_id IN ({ph}) AND {df} IS NOT NULL)",
            frontier + frontier,
        )
        frontier = [row["id"] for row in cursor.fetchall() if row["id"] not in found]
        found.update(frontier)
    # Return only the discovered children â€” never the parents themselves.
    return [sid for sid in found if sid not in seeds]


def _delete_delegate_children(conn, parent_ids: List[str]) -> List[str]:
    ids = _collect_delegate_child_ids(conn, parent_ids)
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", ids)
        # FK safety: orphan any untagged stragglers pointing at a doomed row.
        conn.execute(
            f"UPDATE sessions SET parent_session_id = NULL "
            f"WHERE parent_session_id IN ({ph})",
            ids,
        )
        conn.execute(f"DELETE FROM sessions WHERE id IN ({ph})", ids)
    return ids

T = TypeVar("T")

DEFAULT_DB_PATH = get_hermes_home() / "state.db"

# Import-time snapshot used by _default_db_path() to detect a deliberately
# re-pointed DEFAULT_DB_PATH (tests monkeypatch the constant directly).
_IMPORT_DEFAULT_DB_PATH = DEFAULT_DB_PATH


def _default_db_path() -> Path:
    """Resolve the default state DB path at call time.

    ``DEFAULT_DB_PATH`` is computed when this module is first imported, which
    freezes the developer's real ``~/.hermes`` even when a test fixture later
    redirects ``HERMES_HOME`` â€” importing this module during collection was
    enough to point every default ``SessionDB()`` at the real state.db.

    Precedence:

    1. A deliberately re-pointed ``DEFAULT_DB_PATH`` (differs from the
       import-time snapshot â€” the established test escape hatch) wins.
    2. Otherwise resolve ``get_hermes_home()`` fresh so a runtime
       ``HERMES_HOME`` redirect takes effect regardless of import order.
    """
    if DEFAULT_DB_PATH != _IMPORT_DEFAULT_DB_PATH:
        return DEFAULT_DB_PATH
    return get_hermes_home() / "state.db"


# ---------------------------------------------------------------------------
# Live-DB test-isolation guard
# ---------------------------------------------------------------------------
# Forensic evidence (Aug 2026, live developer machine): the production
# ~/.hermes/state.db accumulated pytest fixture rows â€” sessions with
# chat_id='chat-1'/'123'/'wx-chat' and gateway_routing scopes literally under
# /tmp/pytest-of-*/ â€” and a pytest-spawned process flipped the journal mode
# out from under the WAL-mode gateway writer, destroying committed
# transcripts ("Persisted transcript lagged live cached history ... possible
# FTS write corruption").  The hermetic conftest redirects HERMES_HOME per
# test, but any escape (a session-scoped fixture running before the autouse
# fixture, a subprocess child launched without HERMES_HOME, a stale worktree
# without the re-pin, or a developer shell that exports HERMES_HOME to the
# real home so the conftest session sandbox is×Ÿ6×»h‘éì¶»§q«^tÜÚ[ÛˆHÙ[‹—ÜÙ\ÜÚ[Û—Ü›İ×ÙXİ
›İÊCBˆÙ\ÜÚ[Û–Èœ™]šY]È—HHÜÚ\WÜ™]šY]ÊÙ\ÜÚ[Û‹œÜ
—Ü™]šY]×Ü˜]È‹ˆŠJCBˆÙ\ÜÚ[ÛœË˜\[™
Ù\ÜÚ[ÛŠCBˆ™]\›ˆÙ\ÜÚ[ÛœÃBƒBˆÈ8¥ 8¥ ÜXÙH™XÛ[X][Ûˆ8¥ 8¥ BƒBˆÈ•ÍHš\X[X›\ÈÚÜÙH‹]™YHÙYÛY[ÈÙHY\™ÙHÛˆÜ[Z^™KˆCBˆÈšYÜ˜[HX›H\ÈÜ™X]Y^š[HÈX^H™H\ØX›Y[™HÚšËXšYÜ˜[CBˆÈX›HÛ›H^\İÈ
[™\ÈÛ›H]Y\XX›JHÚ[ˆHØYX›HÚÙ[š^™\ƒBˆÈ\È™\Ù[8 %ÛÈÙH›Ø™HXXÚ™Y›Ü™HİXÚ[™È]
ÙYHÜ[Z^™WÙÊKƒBˆÑ•×ÕP“TÈH
›Y\ÜØYÙ\×ÙÈ‹›Y\ÜØYÙ\×Ù×İšYÜ˜[H‹›Y\ÜØYÙ\×Ù×ØÚšÈŠCBƒBˆYˆÙÚXØ[ÜÚ^™WØ]\ÊÙ[ŠHOˆÜ[Û˜[Ú[NƒBˆˆˆ‘]X˜\ÙHÚ^™H[ˆ]\È\ÈÔS]H]Ù[ˆXØÛİ[È›Üˆ]ƒBƒBˆYÙWØÛİ[
ˆYÙWÜÚ^™X8 %HÚ^™HHXZ[ˆˆš[HÚ[]™HÛ˜ÙCBˆHĞS\ÈÚXÚÜÚ[Y˜XÚÈ[È]ƒBƒBˆ™Y™\ˆ\Èİ™\ˆÜËœ]™Ù]Ú^™J—Ü]
XÚ[ˆ™\Ü[™ÈHY™™XİBˆÙˆHPÕUSKˆ[ˆĞS[ÙHHPÕUSIÜÈ™]Üš]H[™È[ˆH]Ø[š[KBˆ[™HÚXÚÜÚ[]›ÛÈ]˜XÚÈ\È™Y\ÙYÚ[H[Hİ\ƒBˆÛÛ›™Xİ[Ûˆ
H]™HØ]]Ø^JHÛÈH™XY[X\šËˆ[[]\[œÈCBˆXZ[ˆš[HÛˆ\ÚÈİ[Ø\œšY\È]È™KUPÕUSHÚ^™H[™ÙY\ÈÜ›İÚ[™ËBˆÛÈHİ]

KX˜\ÙY™Y›Ü™KØY\ˆ[H[™\œİ]\ÈHÚ[ˆ[™Ø[ˆÛÃBˆ™YØ]]™H8 %Hœ™XÛZ[YYLÎŒŒHPˆˆ™\ÜÛˆH]X˜\ÙH]YBˆXİX[HÚ[šÈŒ	KƒBƒBˆ™]\›œÈ›Û™HYˆH˜YÛX\ÈØ[››İ™H™XYƒBˆˆˆƒBˆNƒBˆÚ]Ù[‹—ÛØÚÎƒBˆYˆÙ[‹—ØÛÛ›ˆ\È›Û™NƒBˆ™]\›ˆ›Û™CBˆYÙWØÛİ[HÙ[‹—ØÛÛ›‹™^Xİ]J”QÓPHYÙWØÛİ[ŠK™™]ÚÛ™J
VÌCBˆYÙWÜÚ^™HHÙ[‹—ØÛÛ›‹™^Xİ]J”QÓPHYÙWÜÚ^™HŠK™™]ÚÛ™J
VÌCBˆ™]\›ˆ[
YÙWØÛİ[
H
ˆ[
YÙWÜÚ^™JCBˆ^Ù\^Ù\[Ûˆ\È^ÎƒBˆÙÙÙ\‹™XYÊÛİ[›İ™XYÙÚXØ[ˆÚ^™Nˆ	\È‹^ÊCBˆ™]\›ˆ›Û™CBƒBˆYˆ˜Xİ][JÙ[ŠHOˆ[ƒBˆˆˆ”[ˆPÕUSHÈ™XÛZ[H\ÚÈÜXÙHY\ˆ\™ÙH[]\ËƒBƒBˆÔS]HÙ\È›İÚš[šÈH]X˜\ÙHš[HÚ[ˆ›İÜÈ\™H[]Y8 %Bˆœ™YYYÙ\È\İÙ]™]\ÙYÛˆH™^[œÙ\ˆY\ˆH[™H]Bˆ™[[İ™Y[™™YÈÙˆÙ\ÜÚ[ÛœËHš[Hİ^\È›Ø]Y[›\ÜÈÙCBˆ^XÚ]HPÕUSKƒBƒBˆPÕUSH™]Üš]\ÈH[\™H‹ÛÈ]	ÜÈ^[œÚ]™H
ÙXÛÛ™È\ƒBˆLPŠH[™Ø[››İ[ˆ[œÚYHH˜[œØXİ[Û‹ˆ][ÛÈXÜ]Z\™\È[ƒBˆ^Û\Ú]™HØÚËÛÈØ[\œÈ]\İ[œİ\™H›Èİ\ˆÜš]\œÈ\™CBˆXİ]™KˆØY™HÈØ[]İ\\™Y›Ü™HHØ]]Ø^KĞÓHİ\ÃBˆÙ\š[™È˜Y™šXËƒBƒBˆ•ÍHÙYÛY[È\™HY\™ÙYš\œİšXH›Y]˜Ü[Z^™WÙØÛÈCBˆİXœÙ\]Y[PÕUSH™XÛZ[\ÈHYÙ\Èœ™YYHHY\™ÙKˆ\È\ÈCBˆ^[İ][Û›HÜ[Z^˜][Ûˆ8 %ÙX\˜Ú™\İ[È\™H[˜Ú[™ÙYƒBƒBˆ™]\›œÈH[X™\ˆÙˆ•È[™^\È]Ù\™HÜ[Z^™Y
YˆCBˆY\™ÙHİ\˜Z[YÜˆ›È•ÈX›\È^\İ
KƒBˆˆˆƒBˆÈY\™ÙH•ÍHÙYÛY[È™Y›Ü™HPÕUSHÛÈHœ™YYYÙ\È\™H™]\›™YBˆÈÈHÔÈ[ˆHØ[YH\ÜËˆÜ[Z^™WÙÊ
HX[˜YÙ\È]ÈİÛˆØÚËƒBˆÜ[Z^™YHBˆNƒBˆÜ[Z^™YHÙ[‹›Ü[Z^™WÙÊ
CBˆ^Ù\^Ù\[Ûˆ\È^ÎƒBˆÙÙÙ\‹Ø\›š[™Ê‘•ÈÜ[Z^™H™Y›Ü™HPÕUSH˜Z[Yˆ	\È‹^ÊCBˆÈPÕUSHØ[››İ™H^Xİ]Y[œÚYHH˜[œØXİ[Û‹ƒBˆÚ]Ù[‹—ÛØÚÎƒBˆÈ™\İYY™›ÜĞSÚXÚÜÚ[š\œİ[ˆPÕUSKƒBˆNƒBˆÙ[‹—ØÛÛ›‹™^Xİ]J”QÓPHØ[ØÚXÚÜÚ[
•SĞUJHŠCBˆ^Ù\^Ù\[Ûˆ\È^ÎƒBˆÙÙÙ\‹™XYÊ•ĞSÚXÚÜÚ[
•SĞUJH™Y›Ü™HPÕUSH˜Z[Yˆ	\È‹^ÊCBˆÙ[‹—ØÛÛ›‹™^Xİ]J•PÕUSHŠCBˆ™]\›ˆÜ[Z^™YBƒBˆYˆX^X™WØ]]×Ü[™WØ[™İ˜Xİ][JBˆÙ[‹Bˆ™][[Û—Ù^\Îˆ[HLBˆZ[—Ú[\˜[Úİ\œÎˆ[HBˆ˜Xİ][Nˆ›ÛÛHYKBˆÙ\ÜÚ[Ûœ×Ù\ˆÜ[Û˜[Ô]HH›Û™KBˆZ[—İ˜Xİ][WÚ[\˜[Ù^\Îˆ[HÌBˆ
HOˆXİÜİ‹[WNƒBˆˆˆ’Y[\İ[]]Ë[XZ[[˜[˜ÙNˆ[™H[˜Xİ]™HÙ\ÜÚ[ÛœÈ
ÈÜ[Û˜[PÕUSKƒBƒBˆ™XÛÜ™ÈH\İ[ˆ[Y\İ[\[ˆİ]WÛY]HÛÈİXœÙ\]Y[Ø[ÃBˆÚ][ˆZ[—Ú[\˜[Úİ\œØ›Ë[ÜˆPÕUSH\È]ÈİÛ‹\XØ[CBˆÛ™Ù\‹›İHÛÛ›ÛYHZ[—İ˜Xİ][WÚ[\˜[Ù^\ØÛÈ›İ][™CBˆ[š[™ÈÙ\È›İ™\X]YH™]Üš]HH]X˜\ÙKˆ\ÚYÛ™YÈ™CBˆØ[YÛ˜ÙH]İ\\œ›ÛHÛ™Ë[]™Y[\Ú[È
ÓKØ]]Ø^KÜ›ÛƒBˆØÚY[\ŠKƒBƒBˆÚ[ˆ
œÙ\ÜÚ[Ûœ×Ù\Šˆ\È›İšYYÛ‹Y\ÚÈ˜[œØÜš\š[\ÃBˆ
šœÛÛ˜ÈšœÛÛ›È™\]Y\İÙ[\Ê˜
H›Üˆ[™YÙ\ÜÚ[ÛœÃBˆ\™H™[[İ™Y\È\ÙˆHØ[YHİÙY\
\ÜİYHÌÌMJKƒBƒBˆ™]™\ˆ˜Z\Ù\ËˆÛˆ[H˜Z[\™KÙÜÈHØ\›š[™È[™™]\›œÈHXİBˆÚ]™\œ›Üˆ˜Ù]ƒBƒBˆ™]\›œÈHXİÚ]Ù^\ÎƒBˆHœÚÚ\Y˜
›ÛÛ
H8 %YHYˆÚ][ˆZ[—Ú[\˜[Úİ\œÈÙˆ\İ[ƒBˆHœ[™Y˜
[
H8 %[X™\ˆÙˆÙ\ÜÚ[ÛœÈ[]YBˆH˜Xİ][YY˜
›ÛÛ
H8 %YHYˆPÕUSH˜[ƒBˆH™\œ›Üˆ˜
İ‹Ü[Û˜[
H8 %™\Ù[Û›HÛˆ˜Z[\™CBˆˆˆƒBˆ™\İ[ˆXİÜİ‹[WHHÈœÚÚ\Yˆ˜[ÙKœ[™Yˆ˜Xİ][YYˆ˜[Ù_CBˆNƒBˆÈÚÚ\Yˆ[›İ\ˆ›ØÙ\ÜËØØ[YXZ[[˜[˜ÙH™XÙ[KƒBˆ\İÜ˜]ÈHÙ[‹™Ù]ÛY]J›\İØ]]×Ü[™HŠCBˆ›İÈH[YK[YJ
CBˆYˆ\İÜ˜]ÎƒBˆNƒBˆ\İİÈH›Ø]
\İÜ˜]ÊCBˆYˆ›İÈH\İİÈZ[—Ú[\˜[Úİ\œÈ
ˆÍŒƒBˆ™\İ[ÈœÚÚ\Y—HHYCBˆ™]\›ˆ™\İ[Bˆ^Ù\
\Q\œ›Ü‹˜[YQ\œ›ÜŠNƒBˆ\ÜÈÈÛÜœ\Y]NÈ™X]\È›Èš[Üˆ[ƒBƒBˆ[™YHÙ[‹œ[™WÜÙ\ÜÚ[ÛœÊBˆÛ\—İ[—Ù^\Ï\™][[Û—Ù^\ËBˆÙ\ÜÚ[Ûœ×Ù\\Ù\ÜÚ[Ûœ×Ù\‹Bˆ
CBˆ™\İ[Èœ[™Y—HH[™YBƒBˆÈÛ›HPÕUSHYˆÙHXİX[Hœ™YY›İÜË[™›È[Ü™HÙ[ˆ[ƒBˆÈÛ˜ÙH]™\HZ[—İ˜Xİ][WÚ[\˜[Ù^\ÈKHH\™ÙH[™H
K™ËˆCBˆÈš\œİÛ™HÈÜ›ÜÜÈ™][[Û—Ù^\ÈÛˆHˆÚ][œÈÙƒBˆÈİ\Ø[™ÈÙˆ›İÜÊHØ[ˆœ™YH[›İYÚYÙ\È][™Yˆš\™\ÃBˆÈÛˆ]™\HİXœÙ\]Y[İ\\]™[ˆİYÚHPÕUSH[™XYH˜[ƒBˆÈ™XÙ[KˆPÕUSHÛˆ\È‰ÜÈÚ^™H
•ÍHÚYİÈX›\ÊH\È›İBˆÈÚX\KH]ÛÈ[ˆ^Û\Ú]™HØÚÈ›ÜˆH[™]Üš]KƒBˆ\İİ˜Xİ][WÜ˜]ÈHÙ[‹™Ù]ÛY]J›\İİ˜Xİ][HŠCBˆ˜Xİ][WÙYHHYCBˆYˆ\İİ˜Xİ][WÜ˜]ÎƒBˆNƒBˆ˜Xİ][WÙYHH
›İÈH›Ø]
\İİ˜Xİ][WÜ˜]ÊJHHZ[—İ˜Xİ][WÚ[\˜[Ù^\È
ˆBˆ^Ù\
\Q\œ›Ü‹˜[YQ\œ›ÜŠNƒBˆ˜Xİ][WÙYHHYCBˆYˆ˜Xİ][H[™[™Yˆ[™˜Xİ][WÙYNƒBˆNƒBˆÙ[‹˜Xİ][J
CBˆ™\İ[È˜Xİ][YY—HHYCBˆÙ[‹œÙ]ÛY]J›\İİ˜Xİ][H‹İŠ›İÊJCBˆ^Ù\^Ù\[Ûˆ\È^ÎƒBˆÙÙÙ\‹Ø\›š[™Êœİ]K™ˆPÕUSH˜Z[Yˆ	\È‹^ÊCBƒBˆÈ™XÛÜ™H][\]™[ˆYˆ[™YOHÛÈÙHÛ‰İ™]CBˆÈ]™\Hİ\\Ú][ˆHZ[—Ú[\˜[Úİ\œÈÚ[™İËƒBˆÙ[‹œÙ]ÛY]J›\İØ]]×Ü[™H‹İŠ›İÊJCBƒBˆYˆ[™YˆƒBˆÙÙÙ\‹š[™›ÊBˆœİ]K™ˆ]]Ë[XZ[[˜[˜ÙNˆ[™Y	YÙ\ÜÚ[ÛŠÊH[˜Xİ]™H›Üˆ	Y^\É\È‹Bˆ[™YBˆ™][[Û—Ù^\ËBˆˆ
ÈPÕUSHˆYˆ™\İ[È˜Xİ][YY—H[ÙHˆ‹Bˆ
CBˆ^Ù\^Ù\[Ûˆ\È^ÎƒBˆÈXZ[[˜[˜ÙH]\İ™]™\ˆ›ØÚÈİ\\ˆÙÈ[™™]\›ˆ\œ›ÜˆX\šÙ\‹ƒBˆÙÙÙ\‹Ø\›š[™Êœİ]K™ˆ]]Ë[XZ[[˜[˜ÙH˜Z[Yˆ	\È‹^ÊCBˆ™\İ[È™\œ›Üˆ—HHİŠ^ÊCBƒBˆ™]\›ˆ™\İ[BƒBˆYˆX^X™WØ]]×Ø\˜Ú]™JBˆÙ[‹BˆYWÙ^\Îˆ›Ø]HËBˆZ[—Ú[\˜[Úİ\œÎˆ[HBˆ^ÛYWÜ[›™Yˆ›ÛÛHYKBˆ
HOˆXİÜİ‹[WNƒBˆˆˆ’Y[\İ[]]ËX\˜Ú]™NˆÛÙZYHÙ\ÜÚ[ÛœÈYH›ÜˆYWÙ^\ØƒBƒBˆÚX›[™ÈÙˆ›Y]˜X^X™WØ]]×Ü[™WØ[™İ˜Xİ][X]›Û‹Y\İXİ]™H8 %Bˆ]\˜Ú]™\È
Y\ÊH˜]\ˆ[ˆ[]\Ë[™YÙ\ÈÛˆ\İXİ]š]CBˆ
ÙYH›Y]˜\˜Ú]™WÜİ[WÜÙ\ÜÚ[ÛœØ
H˜]\ˆ[ˆÜ™X][Û‹ˆ™XÛÜ™ÈCBˆ\İ[ˆ[ˆİ]WÛY]VÉÛ\İØ]]×Ø\˜Ú]™I×XÛÈØ[ÈÚ][ƒBˆZ[—Ú[\˜[Úİ\œØ›Ë[ÜÈØY™HÈØ[ÜÜ[š\İXØ[H
İ\\BˆÛÚÜËÜˆÚ[ˆH\ÚİÜ˜XÚÙ[™\İÈÙ\ÜÚ[ÛœÊKƒBƒBˆ™]™\ˆ˜Z\Ù\Ëˆ™]\›œÈHXİÚ]ƒBˆHœÚÚ\Y˜
›ÛÛ
H8 %Ú][ˆZ[—Ú[\˜[Úİ\œÈÙˆ\İ[ƒBˆH˜\˜Ú]™Y˜
[
H8 %Ù\ÜÚ[ÛœÈ\˜Ú]™Y\È[ƒBˆH™\œ›Üˆ˜
İ‹Ü[Û˜[
H8 %™\Ù[Û›HÛˆ˜Z[\™CBˆˆˆƒBˆ™\İ[ˆXİÜİ‹[WHHÈœÚÚ\Yˆ˜[ÙK˜\˜Ú]™YˆCBˆNƒBˆ\İÜ˜]ÈHÙ[‹™Ù]ÛY]J›\İØ]]×Ø\˜Ú]™HŠCBˆ›İÈH[YK[YJ
CBˆYˆ\İÜ˜]ÎƒBˆNƒBˆYˆ›İÈH›Ø]
\İÜ˜]ÊHZ[—Ú[\˜[Úİ\œÈ
ˆÍŒƒBˆ™\İ[ÈœÚÚ\Y—HHYCBˆ™]\›ˆ™\İ[Bˆ^Ù\
\Q\œ›Ü‹˜[YQ\œ›ÜŠNƒBˆ\ÜÈÈÛÜœ\Y]NÈ™X]\È›Èš[Üˆ[ƒBƒBˆ\˜Ú]™YHÙ[‹˜\˜Ú]™WÜİ[WÜÙ\ÜÚ[ÛœÊBˆYWÙ^\Ë^ÛYWÜ[›™YY^ÛYWÜ[›™YBˆ
CBˆ™\İ[È˜\˜Ú]™Y—HH\˜Ú]™YBƒBˆÈ™XÛÜ™]™[ˆH™\›ËX\˜Ú]™H[ˆÛÈÙHÛ‰İ™K\İÙY\]™\HØ[BˆÈÚ][ˆH[\˜[Ú[™İËƒBˆÙ[‹œÙ]ÛY]J›\İØ]]×Ø\˜Ú]™H‹İŠ›İÊJCBƒBˆYˆ\˜Ú]™YˆƒBˆÙÙÙ\‹š[™›ÊBˆœİ]K™ˆ]]ËX\˜Ú]™Nˆ\˜Ú]™Y	YÙ\ÜÚ[ÛŠÊHYHH	\È^\È‹Bˆ\˜Ú]™YBˆYWÙ^\ËBˆ
CBˆ^Ù\^Ù\[Ûˆ\È^ÎƒBˆÙÙÙ\‹Ø\›š[™Êœİ]K™ˆ]]ËX\˜Ú]™H˜Z[Yˆ	\È‹^ÊCBˆ™\İ[È™\œ›Üˆ—HHİŠ^ÊCBƒBˆ™]\›ˆ™\İ[BƒBˆÈ8¥ 8¥ [™Ù™ˆ
Ü›ÜÜË\]›Ü›HÙ\ÜÚ[Ûˆ˜[œÙ™\ŠH8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ 8¥ BˆÃBˆÈİ]HXXÚ[™NƒBˆÈ›Û™H8 %›È[™Ù™ˆ[ˆ›YÚBˆÈœ[™[™Èˆ8 %ÓH™\]Y\İY[™Ù™‹Ø]]Ø^H\Û‰İXÚÙY]\Y]BˆÈœ[›š[™Èˆ8 %Ø]]Ø^H\È›ØÙ\ÜÚ[™È
Ù\ÜÚ[ÛˆİÚ]Ú
ÈŞ[]XÈ\›ŠCBˆÈ˜ÛÛ\]Y¸ %Ø]]Ø^HİXØÙ\ÜÙ[H[]™\™YHŞ[]XÈ\›ƒBˆÈ™˜Z[Yˆ8 %Ø]]Ø^H][ˆ\œ›ÜÈ™X\ÛÛˆ[ˆ[™Ù™—Ù\œ›ÜƒBˆÃBˆÈHÓHÜš]\Èœ[™[™Èˆ[ˆÛ]ØZ]È›Üˆ\›Z[˜[İ]KˆHØ]]Ø^CBˆÈØ]Ú\ˆ˜[œÚ][ÛœÈ[™[™ø¡¤œ[›š[™ø¡¤ØÛÛ\]Y˜Z[YKƒBƒBˆYˆ™\]Y\İÚ[™Ù™ŠÙ[‹Ù\ÜÚ[Û—ÚYˆİ‹]›Ü›NˆİŠHOˆ›ÛÛƒBˆˆˆ“X\šÈHÙ\ÜÚ[Ûˆ\È[™[™È[™Ù™ˆÈHÚ]™[ˆ]›Ü›KƒBƒBˆ™]\›œÈYHYˆH›İÈØ\È›İ[™[™›İ[™XYH[ˆ›YÚÈ˜[ÙHYƒBˆHÙ\ÜÚ[Ûˆ\È[™XYH[ˆH›Û‹]\›Z[˜[[™Ù™ˆİ]KƒBˆˆˆƒBˆYˆÙÊÛÛ›ŠNƒBˆİ\ˆHÛÛ›‹™^Xİ]JBˆ•TUHÙ\ÜÚ[ÛœÈƒBˆ”ÑU[™Ù™—Üİ]HH	Ü[™[™ÉËƒBˆˆ[™Ù™—Ü]›Ü›HHËƒBˆˆ[™Ù™—Ù\œ›ÜˆH•SƒBˆ•ÒT‘HYHÈS‘
[™Ù™—Üİ]HTÈ•SƒBˆˆÔˆ[™Ù™—Üİ]HSˆ
	ØÛÛ\]Y	Ë	Ù˜Z[Y	ÊJH‹Bˆ
]›Ü›KÙ\ÜÚ[Û—ÚY
KBˆ
CBˆ™]\›ˆİ\‹œ›İØÛİ[ˆBˆ™]\›ˆÙ[‹—Ù^Xİ]WİÜš]JÙÊCBƒBˆYˆÙ]Ú[™Ù™—Üİ]JÙ[‹Ù\ÜÚ[Û—ÚYˆİŠHOˆÜ[Û˜[ÑXİÜİ‹[WWNƒBˆˆˆ”™XYHİ\œ™[[™Ù™ˆİ]H›ÜˆHÙ\ÜÚ[Û‹ƒBƒBˆ™]\›œÈÈœİ]H‹œ]›Ü›H‹™\œ›ÜˆŸXÜˆ›Û™HYˆHÙ\ÜÚ[Ûˆ\ÃBˆ›È[™Ù™ˆ™XÛÜ™ƒBˆˆˆƒBˆNƒBˆİ\ˆHÙ[‹—ØÛÛ›‹™^Xİ]JBˆ”ÑSPÕ[™Ù™—Üİ]K[™Ù™—Ü]›Ü›K[™Ù™—Ù\œ›ÜˆƒBˆ‘”“ÓHÙ\ÜÚ[ÛœÈÒT‘HYHÈ‹Bˆ
Ù\ÜÚ[Û—ÚY
KBˆ
CBˆ›İÈHİ\‹™™]ÚÛ™J
CBˆYˆ›İ›İÎƒBˆ™]\›ˆ›Û™CBˆ™]\›ˆÃBˆœİ]Hˆ›İÖÈš[™Ù™—Üİ]H—KBˆœ]›Ü›Hˆ›İÖÈš[™Ù™—Ü]›Ü›H—KBˆ™\œ›Üˆˆ›İÖÈš[™Ù™—Ù\œ›Üˆ—KBˆCBˆ^Ù\^Ù\[ÛƒBˆ™]\›ˆ›Û™CBƒBˆYˆ\İÜ[™[™×Ú[™Ù™œÊÙ[ŠHOˆ\İÑXİÜİ‹[WWNƒBˆˆˆ”™]\›ˆ[Ù\ÜÚ[ÛœÈ[ˆ[™Ù™—Üİ]OIÜ[™[™ÉËÛ\İš\œİƒBƒBˆ\ÙYHHØ]]Ø^IÜÈ[™Ù™ˆØ]Ú\‹ƒBˆˆˆƒBˆNƒBˆİ\ˆHÙ[‹—ØÛÛ›‹™^Xİ]JBˆ”ÑSPÕËŠ‹ƒBˆÓĞSTĞÑJÜœ›Û\ËœŞ\İ[WÜ›Û\
HTÈÜŞ\İ[WÜ›Û\Ü™\ÛÛ™YƒBˆ‘”“ÓHÙ\ÜÚ[ÛœÈÈƒBˆ“Q•“ÒSˆŞ\İ[WÜ›Û\ÈÜÓˆÜš\ÚHËœŞ\İ[WÜ›Û\Ú\ÚƒBˆ•ÒT‘HËš[™Ù™—Üİ]HH	Ü[™[™ÉÈƒBˆ“Ô‘Tˆ–HËœİ\YØ]TĞÈƒBˆ
CBˆ™]\›ˆÜÙ[‹—ÜÙ\ÜÚ[Û—Ü›İ×ÙXİ
ŠH›Üˆˆ[ˆİ\‹™™]Ú[

WCBˆ^Ù\^Ù\[ÛƒBˆ™]\›ˆ×CBƒBˆYˆÛZ[WÚ[™Ù™ŠÙ[‹Ù\ÜÚ[Û—ÚYˆİŠHOˆ›ÛÛƒBˆˆˆ]ÛZXØ[H˜[œÚ][Ûˆ[™[™È8¡¤ˆ[›š[™Ëˆ™]\›œÈYHYˆÛZ[YYˆˆˆƒBˆYˆÙÊÛÛ›ŠNƒBˆİ\ˆHÛÛ›‹™^Xİ]JBˆ•TUHÙ\ÜÚ[ÛœÈÑU[™Ù™—Üİ]HH	Ü[›š[™ÉÈƒBˆ•ÒT‘HYHÈS‘[™Ù™—Üİ]HH	Ü[™[™ÉÈ‹Bˆ
Ù\ÜÚ[Û—ÚY
KBˆ
CBˆ™]\›ˆİ\‹œ›İØÛİ[ˆBˆ™]\›ˆÙ[‹—Ù^Xİ]WİÜš]JÙÊCBƒBˆYˆÛÛ\]WÚ[™Ù™ŠÙ[‹Ù\ÜÚ[Û—ÚYˆİŠHOˆ›Û™NƒBˆˆˆ“X\šÈH[™Ù™ˆ\ÈÛÛ\]YˆˆˆƒBˆYˆÙÊÛÛ›ŠNƒBˆÛÛ›‹™^Xİ]JBˆ•TUHÙ\ÜÚ[ÛœÈÑU[™Ù™—Üİ]HH	ØÛÛ\]Y	ËƒBˆš[™Ù™—Ù\œ›ÜˆH•SÒT‘HYHÈ‹Bˆ
Ù\ÜÚ[Û—ÚY
KBˆ
CBˆÙ[‹—Ù^Xİ]WİÜš]JÙÊCBƒBˆYˆ˜Z[Ú[™Ù™ŠÙ[‹Ù\ÜÚ[Û—ÚYˆİ‹\œ›ÜˆİŠHOˆ›Û™NƒBˆˆˆ“X\šÈH[™Ù™ˆ\È˜Z[Y[™™XÛÜ™H™X\ÛÛ‹ˆˆˆƒBˆYˆÙÊÛÛ›ŠNƒBˆÛÛ›‹™^Xİ]JBˆ•TUHÙ\ÜÚ[ÛœÈÑU[™Ù™—Üİ]HH	Ù˜Z[Y	ËƒBˆš[™Ù™—Ù\œ›ÜˆHÈÒT‘HYHÈ‹Bˆ
\œ›Ü–ÎLKÙ\ÜÚ[Û—ÚY
KBˆ
CBˆÙ[‹—Ù^Xİ]WİÜš]JÙÊCBƒBƒB˜Û\ÜÈ\Ş[˜ÔÙ\ÜÚ[Û‘ƒBˆˆˆ\Ş[˜ÈÛÜˆÛÈÙ\ÜÚ[Û‘ˆÙ™›ØYÈXXÚØ[šXH\Ş[˜Ú[Ë×İ™XYÛÈH›ØÚÚ[™ÈÔS]HØ[™]™\ˆœ™Y^™\ÈH]™[ÛÜˆÙ[™\šXÈ›ÜØ\™\ˆ8 %H]Y]ÛÛ™š\›\È›ÈY]Ù™]\›œÈH]™Hİ\œÛÜ‹ÙÙ[™\˜]Ü‹ˆˆˆƒBƒBˆYˆ×Ú[š]×ÊÙ[‹ˆ”Ù\ÜÚ[Û‘ˆŠHOˆ›Û™NƒBˆÙ[‹—ÙˆHƒBƒBˆYˆ×ÙÙ]]—×ÊÙ[‹˜[YNˆİŠNƒBˆ]ˆHÙ]]ŠÙ[‹—Ù‹˜[YJCBˆYˆ›İØ[X›J]ŠNƒBˆ™]\›ˆ]ƒBƒBˆ\Ş[˜ÈYˆÛÙ™›ØYY

˜\™ÜË
ŠšİØ\™ÜÊNƒBˆ™]\›ˆ]ØZ]\Ş[˜Ú[Ë×İ™XY
]‹
˜\™ÜË
ŠšİØ\™ÜÊCBƒBˆ™]\›ˆÛÙ™›ØYYB