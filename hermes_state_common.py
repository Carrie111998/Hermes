"""Shared module-level constants for the SessionDB family of modules.

Extracted verbatim from hermes_state.py so the SessionDB mixin modules
(hermes_state_search / hermes_state_schema / hermes_state_portability) can
reference them without importing hermes_state (which would be a cycle).
hermes_state re-imports every name here for backward compatibility.
"""

from dataclasses import dataclass
from typing import Any, Optional

from agent.skill_commands import (
    SKILL_EXCERPT_JOINT,
    SKILL_SCAFFOLD_SQL_LIKE,
    describe_skill_invocation,
)


# Session preview = the head of the first user message, shown wherever a
# session has no title (sidebar rows, pickers, exports, the desktop's
# `sessionTitle` fallback).
#
# A /skill invocation expands into a message that embeds the whole skill body,
# so the plain head of it previews the SKILL's opening prose as if the user had
# written it. Scaffolded rows therefore carry a wider excerpt so
# ``_shape_preview`` can hand it to ``describe_skill_invocation`` and recover
# ``/work — fix the title leak``: the whole message while it stays under the
# budget, and head + tail (where the typed instruction lands) once it doesn't.
_PREVIEW_HEAD_CHARS = 63


_PREVIEW_SCAFFOLD_WINDOW = 400


_PREVIEW_MAX_CHARS = 60


def escape_like(text: str) -> str:
    """Escape SQL LIKE wildcards so operator/session-derived text matches
    literally.  Pair with ``ESCAPE '\\'`` in the clause.

    ``%`` and ``_`` are wildcards to LIKE, and ``_`` in particular is common
    in the values these patterns run against (branch names, session titles,
    filesystem paths).  A match documented as substring/prefix must not
    silently widen.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_PREVIEW_CONTENT_SQL = "REPLACE(REPLACE(m.content, X'0A', ' '), X'0D', ' ')"


_PREVIEW_SCAFFOLDED_SQL = f"m.content LIKE '{SKILL_SCAFFOLD_SQL_LIKE}'"


# The shared ``_preview_raw`` SELECT expression, interpolated by every listing
# query. A scaffolded row gets a wider excerpt: the whole message while it fits
# the budget, else head + tail (where the typed instruction lands) spliced
# around SKILL_EXCERPT_JOINT.
_PREVIEW_RAW_SELECT = (
    f"CASE WHEN {_PREVIEW_SCAFFOLDED_SQL}"
    f" AND LENGTH(m.content) > {_PREVIEW_SCAFFOLD_WINDOW * 2}"
    f" THEN SUBSTR({_PREVIEW_CONTENT_SQL}, 1, {_PREVIEW_SCAFFOLD_WINDOW})"
    f" || '{SKILL_EXCERPT_JOINT}'"
    f" || SUBSTR({_PREVIEW_CONTENT_SQL}, -{_PREVIEW_SCAFFOLD_WINDOW})"
    f" WHEN {_PREVIEW_SCAFFOLDED_SQL}"
    f" THEN SUBSTR({_PREVIEW_CONTENT_SQL}, 1, {_PREVIEW_SCAFFOLD_WINDOW * 2})"
    f" ELSE SUBSTR({_PREVIEW_CONTENT_SQL}, 1, {_PREVIEW_HEAD_CHARS}) END"
)


def _shape_preview(raw: Any) -> str:
    """Turn a ``_preview_raw`` column into the short preview callers show."""
    text = str(raw or "").strip()
    if not text:
        return ""
    described = describe_skill_invocation(text)
    text = described if described is not None else text.split(SKILL_EXCERPT_JOINT)[0]
    if len(text) > _PREVIEW_MAX_CHARS:
        return text[:_PREVIEW_MAX_CHARS] + "..."
    return text


# A child session counts as a /branch (kept visible, never cascade-deleted) if
# it carries the stable marker OR the legacy end_reason heuristic holds.
_BRANCH_CHILD_SQL = (
    "json_extract(COALESCE({a}.model_config, '{{}}'), '$._branched_from') IS NOT NULL"
    " OR EXISTS (SELECT 1 FROM sessions p"
    "            WHERE p.id = {a}.parent_session_id"
    "            AND p.end_reason = 'branched'"
    "            AND {a}.started_at >= p.ended_at)"
)


_COMPRESSION_CHILD_SQL = (
    "EXISTS (SELECT 1 FROM sessions p"
    "        WHERE p.id = {a}.parent_session_id"
    "        AND p.end_reason = 'compression')"
)


_RESET_END_REASONS = (
    "session_reset",
    # switch_session() never creates a child row, but pre-marker DBs can hold
    # legacy reset children whose parent later ended with 'session_switch'
    # (resumed then switched away before reopen-time stamping existed). Also
    # keeps this set identical to the recovery fence in
    # find_latest_gateway_session_for_peer, which interpolates
    # _RESET_END_REASONS_SQL so the two cannot drift.
    "session_switch",
    "idle",
    "daily",
    "suspended",
    "resume_pending_expired",
)
_RESET_END_REASONS_SQL = ", ".join(f"'{reason}'" for reason in _RESET_END_REASONS)


def _legacy_reset_child_sql(alias: str, reasons_sql: str) -> str:
    """Pre-marker reset-continuation heuristic.

    A child is a legacy reset continuation when it rides its parent's exact
    non-empty routing key and the parent ended at a reset boundary. Shared by
    the listing predicate (``_RESET_CHILD_SQL``) and ``reopen_session()``'s
    marker-stamping UPDATE so the two sites cannot drift; ``reasons_sql`` is
    either the literal ``_RESET_END_REASONS_SQL`` or a bound-placeholder list.
    """
    return (
        f"EXISTS (SELECT 1 FROM sessions p"
        f"            WHERE p.id = {alias}.parent_session_id"
        f"            AND p.end_reason IN ({reasons_sql})"
        f"            AND {alias}.session_key IS NOT NULL"
        f"            AND {alias}.session_key != ''"
        f"            AND {alias}.session_key = p.session_key)"
    )


# A reset starts a separate user-visible conversation even though gateway rows
# retain parent_session_id for durable lineage. New rows carry the stable
# marker; the same-key fallback recovers rows written before the marker existed.
# Requiring the exact non-empty routing key keeps ordinary child/subagent rows
# out even when their parent is later reset.
_RESET_CHILD_SQL = (
    "json_extract(COALESCE({a}.model_config, '{{}}'), '$._reset_from') IS NOT NULL"
    " OR " + _legacy_reset_child_sql("{a}", _RESET_END_REASONS_SQL)
)


# Rows that surface in pickers: roots + branch/reset children. Subagent runs
# and compression continuations stay hidden.
_LISTABLE_CHILD_SQL = (
    f"(s.parent_session_id IS NULL OR {_BRANCH_CHILD_SQL.format(a='s')}"
    f" OR {_RESET_CHILD_SQL.format(a='s')})"
)


def _ephemeral_child_sql(alias: str = "s") -> str:
    """Subagent runs, not branch, reset, or compression children."""
    branch = _BRANCH_CHILD_SQL.format(a=alias)
    compression = _COMPRESSION_CHILD_SQL.format(a=alias)
    reset = _RESET_CHILD_SQL.format(a=alias)
    return (
        f"({alias}.parent_session_id IS NOT NULL"
        f" AND NOT ({branch})"
        f" AND NOT ({compression})"
        f" AND NOT ({reset}))"
    )


def _sql_session_last_active(alias: str = "s") -> str:
    """SQL expression for session recency used by list/status surfaces.

    Freshest of ``last_activity_at`` (mid-turn agent activity heartbeat) and
    the latest message timestamp, then fall back to ``started_at``.

    Must not prefer a stale heartbeat over a newer message: durable
    heartbeats are rate-limited (~60s), so after a turn writes messages
    ``last_activity_at`` can lag ``MAX(messages.timestamp)``.
    """
    msg_max = (
        f"(SELECT MAX(_act_m.timestamp) FROM messages _act_m "
        f"WHERE _act_m.session_id = {alias}.id)"
    )
    return (
        f"COALESCE("
        f"(SELECT MAX(_act_v.v) FROM ("
        f"SELECT {alias}.last_activity_at AS v "
        f"UNION ALL "
        f"SELECT {msg_max}"
        f") _act_v), "
        f"{alias}.started_at)"
    )


def _sql_session_last_active_by_id(session_id_expr: str) -> str:
    """Same freshest-of expression keyed by a session-id SQL expression."""
    msg_max = (
        f"(SELECT MAX(_act_m.timestamp) FROM messages _act_m "
        f"WHERE _act_m.session_id = {session_id_expr})"
    )
    activity = (
        f"(SELECT last_activity_at FROM sessions _act_s "
        f"WHERE _act_s.id = {session_id_expr})"
    )
    started = (
        f"(SELECT started_at FROM sessions _act_s "
        f"WHERE _act_s.id = {session_id_expr})"
    )
    return (
        f"COALESCE("
        f"(SELECT MAX(_act_v.v) FROM ("
        f"SELECT {activity} AS v "
        f"UNION ALL "
        f"SELECT {msg_max}"
        f") _act_v), "
        f"{started})"
    )


SCHEMA_VERSION = 27


# FTS storage-layout version, tracked INDEPENDENTLY of SCHEMA_VERSION in the
# state_meta key ``fts_storage_version``. The main schema version advances
# freely on open (so future migrations always land); the FTS *layout* only
# reaches the current version when a DB is either born fresh or explicitly
# optimized via ``hermes sessions optimize-storage``. A legacy DB sits at
# layout 0 (marker absent) with a working inline index until the user opts in.
#   1 = v23 external-content layout (content/tool_name/tool_calls,
#       tool-row-excluded trigram)
FTS_STORAGE_VERSION = 1


# Cap on user-controlled FTS5 query input before regex/sanitizer processing.
# Search queries do not need to be arbitrarily large, and bounding them keeps
# sanitizer/runtime behavior predictable under adversarial input.
MAX_FTS5_QUERY_CHARS = 2_048


_FTS_TRIGGERS = (
    "messages_fts_insert",
    "messages_fts_delete",
    "messages_fts_update",
    "messages_fts_trigram_insert",
    "messages_fts_trigram_delete",
    "messages_fts_trigram_update",
)


# ── Session metadata FTS substrate (#128 / fork #25) ──────────────────
# ``sessions`` gains a named stable ``row_id`` so external-content FTS can
# key documents on a stable integer identity. ``id`` stays the logical /
# public identity every API, relationship and ``messages.session_id``
# reference continues to use.
SESSION_TABLE_REBUILD_SQL = """
CREATE TABLE sessions_new (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    system_prompt_hash TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    git_metadata_generation INTEGER NOT NULL DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    title_source TEXT,
    last_activity_at REAL,
    last_activity_description TEXT,
    last_activity_provenance TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    last_read_at REAL,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id),
    FOREIGN KEY (system_prompt_hash) REFERENCES system_prompts(hash)
)
"""


# Indexes on ``sessions`` that DROP TABLE removes during the row_id swap;
# the migration recreates them inside the same transaction (all IF NOT EXISTS
# so the later SCHEMA_SQL / DEFERRED_INDEX_SQL passes no-op on them).
SESSION_INDEX_SQL_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_source_id ON sessions(source, id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_session_key "
    "ON sessions(session_key, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_gateway_peer ON sessions("
    "source, user_id, chat_id, chat_type, thread_id, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_handoff_state "
    "ON sessions(handoff_state, started_at)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_system_prompt_hash "
    "ON sessions(system_prompt_hash)",
)


# Unicode session-metadata external-content FTS over raw
# (title, id, display_name), keyed by named ``sessions.row_id``. Triggers are
# gated on a dedicated H/P marker pair (``fts_session_rebuild_*``) so an
# empty external index is never served as complete — historical rows are
# backfilled by the resumable chunk engine, and rows committing after the
# high-water mark are live-indexed by the triggers.
SESSIONS_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    title,
    id,
    display_name,
    content='sessions',
    content_rowid='row_id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS sessions_fts_insert AFTER INSERT ON sessions
WHEN (new.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                             WHERE key = 'fts_session_rebuild_high_water'), -1)
   OR new.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                              WHERE key = 'fts_session_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts(rowid, title, id, display_name)
    VALUES (new.row_id, new.title, new.id, new.display_name);
END;

CREATE TRIGGER IF NOT EXISTS sessions_fts_delete AFTER DELETE ON sessions
WHEN (old.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                             WHERE key = 'fts_session_rebuild_high_water'), -1)
   OR old.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                              WHERE key = 'fts_session_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, title, id, display_name)
    VALUES ('delete', old.row_id, old.title, old.id, old.display_name);
END;

CREATE TRIGGER IF NOT EXISTS sessions_fts_update
AFTER UPDATE OF title, id, display_name ON sessions
WHEN (old.title IS NOT new.title
   OR old.id IS NOT new.id
   OR old.display_name IS NOT new.display_name)
   AND (old.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                               WHERE key = 'fts_session_rebuild_high_water'), -1)
     OR old.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                                WHERE key = 'fts_session_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, title, id, display_name)
    VALUES ('delete', old.row_id, old.title, old.id, old.display_name);
    INSERT INTO sessions_fts(rowid, title, id, display_name)
    VALUES (new.row_id, new.title, new.id, new.display_name);
END;
"""


# ── Optional CJK session-metadata index (#128 / fork #26) ────────────────
# Same external-content raw (title, id, display_name) document keyed by named
# row_id, but tokenized with the loadable ``cjk_unicode61`` bigram tokenizer.
# It owns an independent marker pair (``fts_session_cjk_rebuild_*``) and its
# own stale key (``fts_session_cjk_stale``) so optional tokenizer
# availability can never gate or corrupt the complete Unicode index. Split
# table/trigger DDL so a stale optional index can exist while unsafe triggers
# remain absent.
SESSIONS_FTS_CJK_TABLE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts_cjk USING fts5(
    title,
    id,
    display_name,
    content='sessions',
    content_rowid='row_id',
    tokenize='cjk_unicode61'
);
"""

SESSIONS_FTS_CJK_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS sessions_fts_cjk_insert AFTER INSERT ON sessions
WHEN (new.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                             WHERE key = 'fts_session_cjk_rebuild_high_water'), -1)
   OR new.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                              WHERE key = 'fts_session_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts_cjk(rowid, title, id, display_name)
    VALUES (new.row_id, new.title, new.id, new.display_name);
END;

CREATE TRIGGER IF NOT EXISTS sessions_fts_cjk_delete AFTER DELETE ON sessions
WHEN (old.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                             WHERE key = 'fts_session_cjk_rebuild_high_water'), -1)
   OR old.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                              WHERE key = 'fts_session_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts_cjk(sessions_fts_cjk, rowid, title, id, display_name)
    VALUES ('delete', old.row_id, old.title, old.id, old.display_name);
END;

CREATE TRIGGER IF NOT EXISTS sessions_fts_cjk_update
AFTER UPDATE OF title, id, display_name ON sessions
WHEN (old.title IS NOT new.title
   OR old.id IS NOT new.id
   OR old.display_name IS NOT new.display_name)
   AND (old.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                               WHERE key = 'fts_session_cjk_rebuild_high_water'), -1)
     OR old.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                                WHERE key = 'fts_session_cjk_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts_cjk(sessions_fts_cjk, rowid, title, id, display_name)
    VALUES ('delete', old.row_id, old.title, old.id, old.display_name);
    INSERT INTO sessions_fts_cjk(rowid, title, id, display_name)
    VALUES (new.row_id, new.title, new.id, new.display_name);
END;
"""


_FTS_SESSION_CJK_TRIGGERS = (
    "sessions_fts_cjk_insert",
    "sessions_fts_cjk_delete",
    "sessions_fts_cjk_update",
)


# Breadcrumb set when a tokenizer-less process had to drop the session-CJK
# triggers to keep canonical session writes alive: rows written from that
# moment on are missing from the CJK index, so it must not serve reads until
# a capable host resets and rebuilds. Distinct from the message
# ``FTS_CJK_STALE_KEY`` and from the Unicode-session markers.
FTS_SESSION_CJK_STALE_KEY = "fts_session_cjk_stale"


# ── Optional trigram session-metadata index (#128 / fork #30) ───────────
# Same canonical-source principle as the Unicode sessions_fts: metadata text
# is canonical ONLY in ``sessions``. The trigram document reads through the
# ``sessions_fts_trigram_src`` VIEW so the INDEXED text is
# ``compact(title)``, RAW ``id``, ``compact(display_name)`` while ``sessions``
# keeps raw canonical values — no persistent normalized columns are added.
#
# The compact transform deletes EXACTLY the separator set below
# (``- _ .`` and ASCII space); ``id`` stays raw so punctuation-bearing id
# substrings preserve the #16 contract. The separator policy is defined ONCE
# here and drives both the Python query compacting
# (``compact_session_metadata_text``) and the SQL expression embedded in the
# VIEW. Case-insensitivity comes from the trigram tokenizer's normal default
# behavior.
#
# The trigram lane owns its own durable H/P marker pair
# (``fts_session_trigram_rebuild_*``): P means target-specific processed
# completeness, so the Unicode and trigram lanes never share a claim. The
# UPDATE is split into BEFORE/AFTER triggers reading the VIEW projection.
SESSION_METADATA_COMPACT_SEPARATORS = ("-", "_", ".", " ")


def compact_session_metadata_text(text: Optional[str]) -> str:
    """Delete the canonical compact separators from *text*.

    Mirrors the SQL expression generated by ``_session_metadata_compact_sql``
    so search-query compacting and the stored-side VIEW derive from ONE
    policy (the canonical ``- _ . space`` set — never broadened to arbitrary
    punctuation).
    """
    text = text or ""
    for sep in SESSION_METADATA_COMPACT_SEPARATORS:
        text = text.replace(sep, "")
    return text


def _session_metadata_compact_sql(column: str) -> str:
    """Pure-SQL expression removing the canonical compact separators from
    ``column`` (nested REPLACE) — no application-defined SQL function."""
    expr = f"COALESCE({column}, '')"
    for sep in SESSION_METADATA_COMPACT_SEPARATORS:
        expr = f"REPLACE({expr}, '{sep}', '')"
    return expr


SESSIONS_FTS_TRIGRAM_SQL = f"""
CREATE VIEW IF NOT EXISTS sessions_fts_trigram_src AS
SELECT
    row_id,
    {_session_metadata_compact_sql('title')} AS title,
    id AS id,
    {_session_metadata_compact_sql('display_name')} AS display_name
FROM sessions;

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts_trigram USING fts5(
    title,
    id,
    display_name,
    content='sessions_fts_trigram_src',
    content_rowid='row_id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS sessions_fts_trigram_insert AFTER INSERT ON sessions
WHEN (new.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                             WHERE key = 'fts_session_trigram_rebuild_high_water'), -1)
   OR new.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                              WHERE key = 'fts_session_trigram_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts_trigram(rowid, title, id, display_name)
    SELECT row_id, title, id, display_name FROM sessions_fts_trigram_src
    WHERE row_id = new.row_id;
END;

CREATE TRIGGER IF NOT EXISTS sessions_fts_trigram_delete BEFORE DELETE ON sessions
WHEN (old.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                             WHERE key = 'fts_session_trigram_rebuild_high_water'), -1)
   OR old.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                              WHERE key = 'fts_session_trigram_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts_trigram(sessions_fts_trigram, rowid, title, id, display_name)
    SELECT 'delete', row_id, title, id, display_name FROM sessions_fts_trigram_src
    WHERE row_id = old.row_id;
END;

CREATE TRIGGER IF NOT EXISTS sessions_fts_trigram_update_before
BEFORE UPDATE OF title, id, display_name ON sessions
WHEN (old.title IS NOT new.title
   OR old.id IS NOT new.id
   OR old.display_name IS NOT new.display_name)
   AND (old.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                               WHERE key = 'fts_session_trigram_rebuild_high_water'), -1)
     OR old.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                                WHERE key = 'fts_session_trigram_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts_trigram(sessions_fts_trigram, rowid, title, id, display_name)
    SELECT 'delete', row_id, title, id, display_name FROM sessions_fts_trigram_src
    WHERE row_id = old.row_id;
END;

CREATE TRIGGER IF NOT EXISTS sessions_fts_trigram_update_after
AFTER UPDATE OF title, id, display_name ON sessions
WHEN (old.title IS NOT new.title
   OR old.id IS NOT new.id
   OR old.display_name IS NOT new.display_name)
   AND (new.row_id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                               WHERE key = 'fts_session_trigram_rebuild_high_water'), -1)
     OR new.row_id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                                WHERE key = 'fts_session_trigram_rebuild_progress'), -1))
BEGIN
    INSERT INTO sessions_fts_trigram(rowid, title, id, display_name)
    SELECT row_id, title, id, display_name FROM sessions_fts_trigram_src
    WHERE row_id = new.row_id;
END;
"""


_FTS_SESSION_TRIGRAM_TRIGGERS = (
    "sessions_fts_trigram_insert",
    "sessions_fts_trigram_delete",
    "sessions_fts_trigram_update_before",
    "sessions_fts_trigram_update_after",
)


# Breadcrumb set when a host could not build the trigram session index
# (tokenizer unavailable / capability loss); the index must not serve reads
# until a capable host resets and rebuilds. Distinct from the Unicode and
# CJK-session markers.
FTS_SESSION_TRIGRAM_STALE_KEY = "fts_session_trigram_stale"


@dataclass(frozen=True)
class _SessionFtsLane:
    """Identity and ensure-settings of one session-metadata FTS lane.

    ``marker_prefix`` names the lane's OWN H/P marker pair (so no lane can
    falsely certify another), ``fts_table`` is the external-content table the
    chunk backfill inserts into, ``src_from`` is the row source (``sessions``
    or the compact trigram VIEW), and ``available_attr`` names the
    search-serving flag to flip once the backfill completes (None for the
    Unicode lane, whose availability is implicit via the rebuild-gap check).
    ``stale_key`` / ``table_sql`` / ``trigger_sql`` drive the optional-lane
    schema ensure (split table/trigger DDL so a stale optional index can
    exist while unsafe triggers remain absent; ``trigger_sql`` None means the
    table SQL already includes the triggers).
    """

    marker_prefix: str
    fts_table: str
    src_from: str
    available_attr: Optional[str] = None
    stale_key: Optional[str] = None
    table_sql: Optional[str] = None
    trigger_sql: Optional[str] = None


_FTS_SESSION_LANES = (
    _SessionFtsLane("fts_session_rebuild", "sessions_fts", "sessions"),
    _SessionFtsLane(
        "fts_session_cjk_rebuild",
        "sessions_fts_cjk",
        "sessions",
        available_attr="_sessions_cjk_available",
        stale_key=FTS_SESSION_CJK_STALE_KEY,
        table_sql=SESSIONS_FTS_CJK_TABLE_SQL,
        trigger_sql=SESSIONS_FTS_CJK_TRIGGER_SQL,
    ),
    _SessionFtsLane(
        "fts_session_trigram_rebuild",
        "sessions_fts_trigram",
        "sessions_fts_trigram_src",
        available_attr="_sessions_trigram_available",
        stale_key=FTS_SESSION_TRIGRAM_STALE_KEY,
        table_sql=SESSIONS_FTS_TRIGRAM_SQL,
        trigger_sql=None,
    ),
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS system_prompts (
    hash TEXT PRIMARY KEY,
    prompt TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    user_id TEXT,
    session_key TEXT,
    chat_id TEXT,
    chat_type TEXT,
    thread_id TEXT,
    display_name TEXT,
    origin_json TEXT,
    expiry_finalized INTEGER DEFAULT 0,
    model TEXT,
    model_config TEXT,
    system_prompt TEXT,
    system_prompt_hash TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    end_reason TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    git_repo_root TEXT,
    git_metadata_generation INTEGER NOT NULL DEFAULT 0,
    billing_provider TEXT,
    billing_base_url TEXT,
    billing_mode TEXT,
    estimated_cost_usd REAL,
    actual_cost_usd REAL,
    cost_status TEXT,
    cost_source TEXT,
    pricing_version TEXT,
    title TEXT,
    title_source TEXT,
    last_activity_at REAL,
    last_activity_description TEXT,
    last_activity_provenance TEXT,
    api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT,
    handoff_platform TEXT,
    handoff_error TEXT,
    compression_failure_cooldown_until REAL,
    compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT,
    rewind_count INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0,
    last_read_at REAL,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id),
    FOREIGN KEY (system_prompt_hash) REFERENCES system_prompts(hash)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    effect_disposition TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER,
    finish_reason TEXT,
    reasoning TEXT,
    reasoning_content TEXT,
    reasoning_details TEXT,
    codex_reasoning_items TEXT,
    codex_message_items TEXT,
    platform_message_id TEXT,
    observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    api_content TEXT,
    display_kind TEXT,
    display_metadata TEXT
);

CREATE TABLE IF NOT EXISTS session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '',
    billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '',
    api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0,
    cost_status TEXT,
    cost_source TEXT,
    first_seen REAL,
    last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);

CREATE TABLE IF NOT EXISTS state_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS gateway_routing (
    scope TEXT NOT NULL DEFAULT '',
    session_key TEXT NOT NULL,
    entry_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (scope, session_key)
);

CREATE TABLE IF NOT EXISTS gateway_hygiene_state (
    session_key TEXT PRIMARY KEY,
    failure_streak INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS compression_locks (
    session_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS session_turn_leases (
    conversation_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS async_delegations (
    delegation_id TEXT PRIMARY KEY,
    origin_session TEXT NOT NULL,
    origin_ui_session_id TEXT NOT NULL DEFAULT '',
    parent_session_id TEXT,
    state TEXT NOT NULL,
    dispatched_at REAL NOT NULL,
    completed_at REAL,
    updated_at REAL NOT NULL,
    event_json TEXT,
    result_json TEXT,
    delivery_state TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL,
    owner_pid INTEGER,
    owner_started_at INTEGER,
    task_json TEXT,
    delivery_claim TEXT,
    delivery_claimed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_sessions_source ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_source_id ON sessions(source, id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_session_id ON messages(session_id, id);
-- Partial index for the Insights assistant tool-call scan
-- (agent/insights.py _get_tool_usage / _get_skill_usage): those queries filter
-- messages by role='assistant' AND tool_calls IS NOT NULL, a small fraction of
-- rows on a large state.db. role and tool_calls are base columns, so this can
-- live in SCHEMA_SQL rather than DEFERRED_INDEX_SQL.
CREATE INDEX IF NOT EXISTS idx_messages_assistant_calls_by_session
    ON messages(session_id)
    WHERE role = 'assistant' AND tool_calls IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_compression_locks_expires ON compression_locks(expires_at);
CREATE INDEX IF NOT EXISTS idx_session_turn_leases_expires ON session_turn_leases(expires_at);
CREATE INDEX IF NOT EXISTS idx_session_model_usage_session ON session_model_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_session_model_usage_model ON session_model_usage(model);
CREATE INDEX IF NOT EXISTS idx_async_delegations_delivery
    ON async_delegations(delivery_state, completed_at);
"""


# Indexes that reference columns added in later schema versions must be
# created AFTER _reconcile_columns() has had a chance to ADD them on
# existing databases. SCHEMA_SQL above is run by sqlite executescript
# which would otherwise fail on legacy DBs ("no such column: active").
DEFERRED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_messages_session_active
    ON messages(session_id, active, timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_active_null
    ON messages(active) WHERE active IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_session_key
    ON sessions(session_key, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_gateway_peer
    ON sessions(source, user_id, chat_id, chat_type, thread_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_handoff_state
    ON sessions(handoff_state, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_system_prompt_hash
    ON sessions(system_prompt_hash);
"""


# ── Deferred FTS rebuild bookkeeping (schema v23) ──
# While a background index rebuild is pending, two state_meta keys define
# which message rows are currently IN the FTS indexes:
#
#   fts_rebuild_high_water  H — MAX(messages.id) at the moment the old
#                                indexes were dropped
#   fts_rebuild_progress    P — highest id the chunked backfill has indexed
#
# A row is indexed iff  id <= P  (backfilled)  OR  id > H  (inserted after
# the drop; ids are AUTOINCREMENT so new rows are always > H and the insert
# triggers index them live).  Rows in (P, H] are not yet indexed.
#
# Every trigger below gates on that same predicate: firing an FTS5
# external-content 'delete' for a row that is NOT in the index corrupts the
# index, and skipping it for a row that IS indexed leaves a stale entry.
# When no rebuild is pending both keys are absent and COALESCE turns the
# predicate into a tautology (id > -1 OR id <= -1), i.e. normal operation.
# The two state_meta PK probes per write are negligible next to the FTS
# insert itself.
FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages
WHEN (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                         WHERE key = 'fts_rebuild_high_water'), -1)
   OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                          WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages
WHEN (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                         WHERE key = 'fts_rebuild_high_water'), -1)
   OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                          WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

-- UPDATE OF skips the trigger entirely for non-content column writes
-- (status/compacted/observed/etc.), which is stronger than the WHEN gate
-- alone and avoids FTS I/O saturation on large state.db (#68858 / #73639).
CREATE TRIGGER IF NOT EXISTS messages_fts_update
AFTER UPDATE OF content, tool_name, tool_calls ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls)
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
    INSERT INTO messages_fts(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;
"""


# Trigram FTS5 table for CJK substring search.  The default unicode61
# tokenizer splits CJK characters into individual tokens, breaking phrase
# matching.  The trigram tokenizer creates overlapping 3-byte sequences so
# substring queries work natively for any script (CJK, Thai, etc.).
#
# The trigram index is the most expensive index in state.db (~2.6x the size
# of the text it covers), and ``role='tool'`` rows are ~90% of message bytes
# while being almost entirely machine noise (base64 payloads, file dumps,
# delegation transcripts).  The index therefore reads through
# ``messages_fts_trigram_src``, a view that excludes tool rows — they stay
# fully stored in ``messages`` and fully searchable via the standard
# ``messages_fts`` index; they just don't get trigram (CJK substring)
# treatment.  ``search_messages`` routes CJK queries that filter on
# ``role='tool'`` to the LIKE fallback for the same reason.
FTS_TRIGRAM_SQL = """
CREATE VIEW IF NOT EXISTS messages_fts_trigram_src AS
    SELECT id, role, content, tool_name, tool_calls
    FROM messages
    WHERE role <> 'tool';

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tool_name,
    tool_calls,
    content='messages_fts_trigram_src',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages
WHEN new.role <> 'tool'
   AND (new.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_rebuild_high_water'), -1)
     OR new.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_trigram(rowid, content, tool_name, tool_calls)
    VALUES (new.id, new.content, new.tool_name, new.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages
WHEN old.role <> 'tool'
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name, tool_calls)
    VALUES ('delete', old.id, old.content, old.tool_name, old.tool_calls);
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update
AFTER UPDATE OF content, tool_name, tool_calls, role ON messages
WHEN (old.content IS NOT new.content
    OR old.tool_name IS NOT new.tool_name
    OR old.tool_calls IS NOT new.tool_calls
    OR old.role IS NOT new.role)
   AND (old.id > COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                           WHERE key = 'fts_rebuild_high_water'), -1)
     OR old.id <= COALESCE((SELECT CAST(value AS INTEGER) FROM state_meta
                            WHERE key = 'fts_rebuild_progress'), -1))
BEGIN
    INSERT INTO messages_fts_trigram(messages_fts_trigram, rowid, content, tool_name, tool_calls)
    SELECT 'delete', old.id, old.content, old.tool_name, old.tool_calls
    WHERE old.role <> 'tool';
    INSERT INTO messages_fts_trigram(rowid, content, tool_name, tool_calls)
    SELECT new.id, new.content, new.tool_name, new.tool_calls
    WHERE new.role <> 'tool';
END;
"""


_FTS_CJK_TRIGGERS = (
    "messages_fts_cjk_insert",
    "messages_fts_cjk_delete",
    "messages_fts_cjk_update",
)


# state_meta breadcrumb set when a tokenizer-less process had to drop the
# cjk triggers to keep message writes alive: rows written from that moment
# on are missing from the cjk index, so it must not serve reads until
# `hermes sessions optimize-storage` rebuilds it on a capable host.
FTS_CJK_STALE_KEY = "fts_cjk_stale"


# Durable breadcrumb for a base/trigram FTS index that was detached from the
# canonical messages table after runtime corruption. While present, startup
# must rebuild the complete index before reinstalling sync triggers: rows may
# have been written while those triggers were absent, so merely recreating
# them would preserve an unknown index gap.
FTS_STALE_KEY = "fts_stale"


# ── Legacy (v22 / inline-content) FTS DDL ──────────────────────────────
# Used ONLY to keep an existing pre-v23 install's search working and its
# triggers repairable UNTIL the user opts into `hermes db optimize`. This is
# the exact inline shape v11..v22 shipped: each virtual table stores its own
# copy of ``content || tool_name || tool_calls`` and the trigram table indexes
# every row (including role='tool'). We never CREATE these on a fresh install —
# fresh installs are born on the v23 external-content schema above. These
# constants exist so a legacy DB is never accidentally handed the v23 DDL
# (which would create the external-content trigram source VIEW and leave the
# DB in a mixed, broken state). `optimize_fts_storage()` is what migrates a
# legacy DB to the v23 shape.
LEGACY_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content
);

CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update
AFTER UPDATE OF content, tool_name, tool_calls ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""


LEGACY_FTS_TRIGRAM_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update
AFTER UPDATE OF content, tool_name, tool_calls ON messages BEGIN
    DELETE FROM messages_fts_trigram WHERE rowid = old.id;
    INSERT INTO messages_fts_trigram(rowid, content) VALUES (
        new.id,
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
    );
END;
"""
