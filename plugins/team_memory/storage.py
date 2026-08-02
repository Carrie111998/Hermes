"""Scoped SQLite storage for the team-memory plugin.

This module is intentionally dependency-light. SQLite is used for the first
rollout because it is already part of Hermes and can be replaced behind this
module without changing the tool or CLI contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 3
DEFAULT_DB_RELATIVE = Path("plugins") / "shared_memory.db"
DEFAULT_RESULT_LIMIT = 5
MAX_RESULT_LIMIT = 20
DEFAULT_CONTENT_LIMIT = 4_000
MAX_CONTENT_LIMIT = 12_000
DEFAULT_TOTAL_RESULT_LIMIT = 24_000
MAX_TOTAL_RESULT_LIMIT = 64_000
_UTC_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff\uf900-\ufaff]+")
_SAFE_WORKSPACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SECRET_RE = re.compile(
    r"(?i)(api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*[^\s,;]+"
)


def _utc_timestamp(value: Optional[datetime] = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime(_UTC_TIMESTAMP_FORMAT)


def _normalize_valid_until(value: Optional[str]) -> Optional[str]:
    """Normalize an expiry to UTC so SQLite comparisons are chronological.

    Storing ISO strings with a ``T`` separator and an offset looks reasonable,
    but lexical comparison against SQLite's ``CURRENT_TIMESTAMP`` (which uses
    a space separator) makes entries expiring later today appear unexpired.
    Canonical UTC timestamps avoid that class of same-day expiry bug.
    """
    if value is None or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "valid_until must be an ISO-8601 timestamp, for example "
            "2026-12-31T23:59:59Z"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return _utc_timestamp(parsed)


def _config() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        value = load_config_readonly()
    except Exception:
        try:
            from hermes_cli.config import load_config

            value = load_config()
        except Exception:
            value = {}
    return value if isinstance(value, dict) else {}


def _team_config(config: Optional[dict] = None) -> dict:
    cfg = _config() if config is None else config
    value = cfg.get("team_memory", {})
    return value if isinstance(value, dict) else {}


def _configured_workspace(config: Optional[dict] = None) -> str:
    cfg = _config() if config is None else config
    team_cfg = _team_config(cfg)
    value = team_cfg.get("workspace_id", team_cfg.get("workspace", ""))
    return str(value or "").strip()


def validate_workspace_id(workspace_id: str) -> str:
    value = str(workspace_id or "").strip()
    if not value or not _SAFE_WORKSPACE_RE.fullmatch(value):
        raise ValueError(
            "workspace_id must be 1-128 characters using letters, digits, "
            "dot, underscore, colon, slash, or hyphen"
        )
    return value


def _expand_config_path(value: Any, base_dir: Path) -> Optional[Path]:
    if value is None or str(value).strip() == "":
        return None
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else base_dir / path


def get_db_path(config: Optional[dict] = None) -> Path:
    """Resolve the configured database without silently crossing profiles.

    ``team_memory.database_path`` is the explicit sharing boundary. If it is
    absent, the legacy profile-local path is retained. To share one store
    across Frontend/Backend/DevOps profiles, point every profile at the same
    absolute path and set the same workspace id.
    """
    from hermes_constants import get_hermes_home

    home = Path(get_hermes_home())
    team_cfg = _team_config(config)
    configured = _expand_config_path(team_cfg.get("database_path"), home)
    return configured or (home / DEFAULT_DB_RELATIVE)


def get_metrics_path(
    config: Optional[dict] = None, *, db_path: Optional[Path] = None
) -> Path:
    cfg = _config() if config is None else config
    team_cfg = _team_config(cfg)
    resolved_db_path = Path(db_path) if db_path is not None else get_db_path(cfg)
    configured = (
        None
        if db_path is not None
        else _expand_config_path(team_cfg.get("metrics_path"), resolved_db_path.parent)
    )
    path = configured or resolved_db_path.with_name(
        f"{resolved_db_path.stem}.metrics.db"
    )
    if path.resolve() == resolved_db_path.resolve():
        raise ValueError("team_memory.metrics_path must be different from database_path")
    return path


def get_agent_variant(config: Optional[dict] = None) -> str:
    value = _team_config(config).get("agent_variant", "")
    return str(value or "").strip()[:64]


def _secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _secure_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if not read_only:
        _secure_dir(path.parent)
        conn = sqlite3.connect(path, timeout=5.0)
    else:
        # A read-only search must not create a new database when the file is
        # removed between the existence check and the connection attempt.
        # ``mode=ro`` also makes accidental writes from a search path fail
        # closed instead of silently changing the store.
        uri = f"file:{quote(str(path.resolve()), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    if not read_only:
        # WAL keeps search readers from blocking short CLI writes. It is not
        # required for correctness and can be disabled by the storage backend.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _secure_file(path)
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column_if_missing(
    conn: sqlite3.Connection, columns: set[str], name: str, definition: str
) -> None:
    if name not in columns:
        conn.execute(f"ALTER TABLE shared_memory ADD COLUMN {name} {definition}")
        columns.add(name)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shared_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id TEXT NOT NULL DEFAULT 'default',
            project_id TEXT NOT NULL DEFAULT '',
            memory_key TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            author TEXT NOT NULL,
            tags TEXT NOT NULL DEFAULT '[]',
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_ref TEXT NOT NULL DEFAULT '',
            review_status TEXT NOT NULL DEFAULT 'approved',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            valid_until TEXT
        )
        """
    )
    columns = _table_columns(conn, "shared_memory")
    _add_column_if_missing(conn, columns, "workspace_id", "TEXT NOT NULL DEFAULT 'default'")
    _add_column_if_missing(conn, columns, "project_id", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(conn, columns, "memory_key", "TEXT NOT NULL DEFAULT ''")
    had_created_by = "created_by" in columns
    _add_column_if_missing(conn, columns, "author", "TEXT NOT NULL DEFAULT 'legacy'")
    _add_column_if_missing(conn, columns, "source_type", "TEXT NOT NULL DEFAULT 'manual'")
    _add_column_if_missing(conn, columns, "source_ref", "TEXT NOT NULL DEFAULT ''")
    _add_column_if_missing(
        conn, columns, "review_status", "TEXT NOT NULL DEFAULT 'approved'"
    )
    _add_column_if_missing(conn, columns, "valid_until", "TEXT")

    conn.execute(
        "UPDATE shared_memory SET memory_key = 'legacy-' || id WHERE memory_key = ''"
    )
    if had_created_by:
        conn.execute(
            "UPDATE shared_memory SET author = COALESCE(NULLIF(created_by, ''), 'legacy') "
            "WHERE author = 'legacy'"
        )
    for row in conn.execute(
        "SELECT id, valid_until FROM shared_memory "
        "WHERE valid_until IS NOT NULL AND valid_until <> ''"
    ).fetchall():
        try:
            normalized = _normalize_valid_until(row["valid_until"])
        except ValueError:
            # An invalid legacy expiry must not make the whole database
            # unavailable. It remains excluded by datetime() in searches and
            # is surfaced to the operator by list --include-expired.
            logger.warning(
                "Leaving invalid team-memory expiry unchanged for row %s",
                row["id"],
            )
            continue
        if normalized != row["valid_until"]:
            conn.execute(
                "UPDATE shared_memory SET valid_until = ? WHERE id = ?",
                (normalized, row["id"]),
            )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_memory_scope_key "
        "ON shared_memory(workspace_id, memory_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_memory_scope_category "
        "ON shared_memory(workspace_id, category, updated_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_shared_memory_scope_project "
        "ON shared_memory(workspace_id, project_id)"
    )

    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS shared_memory_fts
        USING fts5(
            title,
            content,
            tags,
            content='shared_memory',
            content_rowid='id'
        )
        """
    )

    # FTS5 external-content tables require delete+insert on UPDATE. A plain
    # UPDATE leaves stale terms behind, which is especially damaging for API
    # contracts that change over time.
    for trigger in ("shared_memory_ai", "shared_memory_au", "shared_memory_ad"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute(
        """
        CREATE TRIGGER shared_memory_ai
        AFTER INSERT ON shared_memory
        BEGIN
            INSERT INTO shared_memory_fts(rowid, title, content, tags)
            VALUES (new.id, new.title, new.content, new.tags);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER shared_memory_au
        AFTER UPDATE ON shared_memory
        BEGIN
            INSERT INTO shared_memory_fts(shared_memory_fts, rowid, title, content, tags)
            VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO shared_memory_fts(rowid, title, content, tags)
            VALUES (new.id, new.title, new.content, new.tags);
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER shared_memory_ad
        AFTER DELETE ON shared_memory
        BEGIN
            INSERT INTO shared_memory_fts(shared_memory_fts, rowid, title, content, tags)
            VALUES ('delete', old.id, old.title, old.content, old.tags);
        END
        """
    )
    # Rebuild is idempotent and repairs databases created by the original
    # candidate implementation, including stale external-content indexes.
    conn.execute(
        "INSERT INTO shared_memory_fts(shared_memory_fts) VALUES ('rebuild')"
    )
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


def init_database(
    db_path: Optional[Path] = None,
    *,
    workspace_id: Optional[str] = None,
) -> Path:
    """Create or migrate a team-memory database and repair its FTS index."""
    cfg = _config()
    path = Path(db_path) if db_path is not None else get_db_path(cfg)
    workspace = workspace_id or _configured_workspace(cfg) or "default"
    validate_workspace_id(workspace)
    conn = _connect(path)
    try:
        with conn:
            _create_schema(conn)
            # Old candidate databases had no explicit scope. Keep them usable
            # under the workspace selected for this migration.
            conn.execute(
                "UPDATE shared_memory SET workspace_id = ? "
                "WHERE workspace_id = 'default' AND ? <> 'default'",
                (workspace, workspace),
            )
    finally:
        conn.close()
    _secure_file(path)
    return path


def _require_db(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(
            f"Team-memory database is not initialized: {path}. "
            "Run `hermes team-memory init --workspace <id>`."
        )


def _resolve_workspace(workspace_id: Optional[str], config: Optional[dict] = None) -> str:
    value = workspace_id or _configured_workspace(config)
    if not value:
        raise ValueError(
            "team_memory.workspace_id is required; use the same explicit "
            "workspace id in every cooperating profile"
        )
    return validate_workspace_id(value)


def _normalize_tags(tags: Iterable[str] | str | None) -> str:
    if isinstance(tags, str):
        values = [part.strip() for part in tags.split(",")]
    else:
        values = [str(part).strip() for part in (tags or [])]
    values = sorted({value for value in values if value})
    return json.dumps(values, ensure_ascii=False)


def _memory_key(category: str, title: str, source_ref: str = "") -> str:
    raw = "\x1f".join((category.strip(), title.strip(), source_ref.strip()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def add_memory(
    category: str,
    title: str,
    content: str,
    author: str,
    tags: Iterable[str] | str | None = None,
    *,
    workspace_id: Optional[str] = None,
    project_id: str = "",
    source_type: str = "manual",
    source_ref: str = "",
    review_status: str = "approved",
    valid_until: Optional[str] = None,
    memory_key: Optional[str] = None,
    replace: bool = False,
    db_path: Optional[Path] = None,
) -> int:
    """Insert a reviewed memory entry, optionally replacing the same key."""
    cfg = _config()
    path = Path(db_path) if db_path is not None else get_db_path(cfg)
    _require_db(path)
    workspace = _resolve_workspace(workspace_id, cfg)
    category = str(category or "").strip()
    title = str(title or "").strip()
    content = str(content or "").strip()
    author = str(author or "").strip()
    review_status = str(review_status or "approved").strip().lower()
    if not category or not title or not content or not author:
        raise ValueError("category, title, content, and author are required")
    if review_status not in {"approved", "draft", "archived"}:
        raise ValueError("review_status must be approved, draft, or archived")
    if len(content) > MAX_CONTENT_LIMIT * 4:
        raise ValueError("content is too large for Stage 1 shared memory")
    key = str(memory_key or _memory_key(category, title, source_ref)).strip()
    if not key or len(key) > 128:
        raise ValueError("memory_key must be 1-128 characters")
    tags_json = _normalize_tags(tags)
    now = _utc_timestamp()
    valid_until = _normalize_valid_until(valid_until)
    conn = _connect(path)
    try:
        with conn:
            existing = conn.execute(
                "SELECT id FROM shared_memory WHERE workspace_id = ? AND memory_key = ?",
                (workspace, key),
            ).fetchone()
            if existing and not replace:
                return int(existing["id"])
            if existing:
                conn.execute(
                    """
                    UPDATE shared_memory
                    SET project_id=?, category=?, title=?, content=?, author=?, tags=?,
                        source_type=?, source_ref=?, review_status=?, updated_at=?, valid_until=?
                    WHERE id=? AND workspace_id=?
                    """,
                    (
                        str(project_id or "").strip(),
                        category,
                        title,
                        content,
                        author,
                        tags_json,
                        str(source_type or "manual").strip(),
                        str(source_ref or "").strip(),
                        review_status,
                        now,
                        valid_until,
                        existing["id"],
                        workspace,
                    ),
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO shared_memory(
                    workspace_id, project_id, memory_key, category, title, content,
                    author, tags, source_type, source_ref, review_status, updated_at,
                    valid_until
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace,
                    str(project_id or "").strip(),
                    key,
                    category,
                    title,
                    content,
                    author,
                    tags_json,
                    str(source_type or "manual").strip(),
                    str(source_ref or "").strip(),
                    review_status,
                    now,
                    valid_until,
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def _safe_like(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(query: str) -> str:
    tokens = _TOKEN_RE.findall(str(query or "").strip())
    # unicode61 does not tokenize unspaced Chinese reliably. Search Latin /
    # numeric tokens through FTS and use LIKE fallback for CJK below.
    terms = []
    for token in tokens:
        if _CJK_RE.search(token):
            continue
        escaped = token.replace('"', '""')
        terms.append(f'"{escaped}"*')
    return " AND ".join(terms)


def _row_to_dict(row: sqlite3.Row, *, max_content_chars: int) -> dict[str, Any]:
    value = dict(row)
    try:
        value["tags"] = json.loads(value.get("tags") or "[]")
    except (TypeError, json.JSONDecodeError):
        value["tags"] = []
    content = str(value.get("content") or "")
    value["content_truncated"] = len(content) > max_content_chars
    value["content"] = content[:max_content_chars]
    return value


_SELECT_COLUMNS = (
    "m.id, m.workspace_id, m.project_id, m.memory_key, m.category, m.title, "
    "m.content, m.author, m.tags, m.source_type, m.source_ref, m.review_status, "
    "m.created_at, m.updated_at, m.valid_until"
)


def _base_filters(
    workspace: str,
    category: Optional[str],
    project_id: Optional[str],
    include_drafts: bool,
    include_expired: bool,
) -> tuple[list[str], list[Any]]:
    where = ["m.workspace_id = ?"]
    params: list[Any] = [workspace]
    if not include_expired:
        where.append(
            "(m.valid_until IS NULL OR datetime(m.valid_until) > CURRENT_TIMESTAMP)"
        )
    if not include_drafts:
        where.append("m.review_status = 'approved'")
    if category:
        where.append("m.category = ?")
        params.append(category)
    if project_id:
        where.append("(m.project_id = ? OR m.project_id = '')")
        params.append(project_id)
    return where, params


def search_memory(
    query: str,
    category: Optional[str] = None,
    limit: int = DEFAULT_RESULT_LIMIT,
    *,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    include_drafts: bool = False,
    include_expired: bool = False,
    db_path: Optional[Path] = None,
    max_content_chars: int = DEFAULT_CONTENT_LIMIT,
    max_total_chars: int = DEFAULT_TOTAL_RESULT_LIMIT,
) -> list[dict[str, Any]]:
    """Search one explicit workspace with FTS5 and a bounded CJK fallback."""
    cfg = _config()
    path = Path(db_path) if db_path is not None else get_db_path(cfg)
    _require_db(path)
    workspace = _resolve_workspace(workspace_id, cfg)
    query = str(query or "").strip()
    if not query:
        return []
    limit = max(1, min(int(limit), MAX_RESULT_LIMIT))
    max_content_chars = max(256, min(int(max_content_chars), MAX_CONTENT_LIMIT))
    max_total_chars = max(1_024, min(int(max_total_chars), MAX_TOTAL_RESULT_LIMIT))
    where, params = _base_filters(
        workspace, category, project_id, include_drafts, include_expired
    )
    conn = _connect(path, read_only=True)
    try:
        fts = _fts_query(query)
        results: list[sqlite3.Row] = []
        if fts:
            sql = (
                f"SELECT {_SELECT_COLUMNS}, bm25(shared_memory_fts) AS rank "
                "FROM shared_memory_fts "
                "JOIN shared_memory AS m ON m.id = shared_memory_fts.rowid "
                f"WHERE shared_memory_fts MATCH ? AND {' AND '.join(where)} "
                "ORDER BY rank ASC, m.updated_at DESC LIMIT ?"
            )
            results = conn.execute(sql, [fts, *params, limit]).fetchall()

        # CJK and mixed-language fallback. It is intentionally bounded and
        # scoped, so a malformed FTS query cannot turn into a full-table leak.
        if not results and _CJK_RE.search(query):
            cjk_tokens = [token for token in _TOKEN_RE.findall(query) if _CJK_RE.search(token)]
            needles = list(dict.fromkeys([query, *cjk_tokens]))
            like_parts: list[str] = []
            like_params: list[Any] = []
            for needle in needles:
                escaped = f"%{_safe_like(needle)}%"
                like_parts.append("(m.title LIKE ? ESCAPE '\\' OR m.content LIKE ? ESCAPE '\\' OR m.tags LIKE ? ESCAPE '\\')")
                like_params.extend([escaped, escaped, escaped])
            fallback_where = [*where, f"({' OR '.join(like_parts)})"]
            title_needle = f"%{_safe_like(query)}%"
            sql = (
                f"SELECT {_SELECT_COLUMNS}, 0.0 AS rank FROM shared_memory AS m "
                f"WHERE {' AND '.join(fallback_where)} "
                "ORDER BY CASE WHEN m.title LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, "
                "m.updated_at DESC LIMIT ?"
            )
            results = conn.execute(
                sql, [*params, *like_params, title_needle, limit]
            ).fetchall()
        bounded: list[dict[str, Any]] = []
        total_chars = 2
        for row in results:
            item = _row_to_dict(row, max_content_chars=max_content_chars)
            item_chars = len(json.dumps(item, ensure_ascii=False))
            separator_chars = 2 if bounded else 0  # comma + space in JSON list
            remaining = max_total_chars - total_chars - separator_chars
            if item_chars > remaining:
                if bounded:
                    break
                # Keep the first useful hit even when its title/metadata is
                # larger than the total envelope. Content is the only field
                # that is safe to shorten; if fixed metadata alone is too
                # large, omit the row rather than violating the bound.
                content = str(item.get("content") or "")
                while content and item_chars > remaining:
                    content = content[: max(0, len(content) - (item_chars - remaining))]
                    item["content"] = content
                    item["content_truncated"] = True
                    item_chars = len(json.dumps(item, ensure_ascii=False))
                if item_chars > remaining:
                    break
            bounded.append(item)
            total_chars += item_chars + separator_chars
        return bounded
    finally:
        conn.close()


def list_all_memories(
    category: Optional[str] = None,
    *,
    workspace_id: Optional[str] = None,
    project_id: Optional[str] = None,
    include_drafts: bool = True,
    include_expired: bool = False,
    db_path: Optional[Path] = None,
    max_content_chars: int = DEFAULT_CONTENT_LIMIT,
) -> list[dict[str, Any]]:
    cfg = _config()
    path = Path(db_path) if db_path is not None else get_db_path(cfg)
    _require_db(path)
    workspace = _resolve_workspace(workspace_id, cfg)
    where, params = _base_filters(
        workspace, category, project_id, include_drafts, include_expired
    )
    conn = _connect(path, read_only=True)
    try:
        rows = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM shared_memory AS m "
            f"WHERE {' AND '.join(where)} ORDER BY m.updated_at DESC",
            params,
        ).fetchall()
        return [_row_to_dict(row, max_content_chars=max_content_chars) for row in rows]
    finally:
        conn.close()


def delete_memory(
    memory_id: int,
    *,
    workspace_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> bool:
    cfg = _config()
    path = Path(db_path) if db_path is not None else get_db_path(cfg)
    _require_db(path)
    workspace = _resolve_workspace(workspace_id, cfg)
    conn = _connect(path)
    try:
        with conn:
            cursor = conn.execute(
                "DELETE FROM shared_memory WHERE id = ? AND workspace_id = ?",
                (int(memory_id), workspace),
            )
            return cursor.rowcount > 0
    finally:
        conn.close()


def _redact_metric_query(query: str) -> str:
    return _SECRET_RE.sub(r"\1=[REDACTED]", str(query or ""))[:512]


def _init_metrics_db(path: Path) -> None:
    conn = _connect(path)
    try:
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS query_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    category TEXT,
                    results_count INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    agent_variant TEXT,
                    profile_name TEXT,
                    session_id TEXT,
                    task_id TEXT
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_query_metrics_variant_time "
                "ON query_metrics(agent_variant, timestamp)"
            )
    finally:
        conn.close()


def log_query_metric(
    query: str,
    category: Optional[str],
    results_count: int,
    latency_ms: float,
    *,
    workspace_id: Optional[str] = None,
    agent_variant: Optional[str] = None,
    profile_name: Optional[str] = None,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    metrics_path: Optional[Path] = None,
) -> None:
    """Best-effort metrics write to a separate SQLite file."""
    cfg = _config()
    workspace = _resolve_workspace(workspace_id, cfg)
    path = (
        Path(metrics_path)
        if metrics_path is not None
        else get_metrics_path(cfg)
    )
    _init_metrics_db(path)
    conn = _connect(path)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO query_metrics(
                    timestamp, workspace_id, query, category, results_count,
                    latency_ms, agent_variant, profile_name, session_id, task_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_timestamp(),
                    workspace,
                    _redact_metric_query(query),
                    category,
                    int(results_count),
                    float(latency_ms),
                    (agent_variant or get_agent_variant(cfg) or None),
                    profile_name,
                    session_id,
                    task_id,
                ),
            )
    finally:
        conn.close()


def get_query_metrics(
    agent_variant: Optional[str] = None,
    *,
    workspace_id: Optional[str] = None,
    metrics_path: Optional[Path] = None,
) -> list[dict[str, Any]]:
    cfg = _config()
    path = Path(metrics_path) if metrics_path is not None else get_metrics_path(cfg)
    if not path.exists():
        return []
    workspace = _resolve_workspace(workspace_id, cfg)
    conn = _connect(path, read_only=True)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'query_metrics'"
        ).fetchone()
        if not table_exists:
            return []
        if agent_variant:
            rows = conn.execute(
                "SELECT * FROM query_metrics WHERE workspace_id = ? AND agent_variant = ? ORDER BY timestamp",
                (workspace, agent_variant),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM query_metrics WHERE workspace_id = ? ORDER BY timestamp",
                (workspace,),
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def uninstall_database(
    db_path: Optional[Path] = None,
    *,
    metrics_path: Optional[Path] = None,
) -> bool:
    """Remove only the resolved Stage 1 files; callers must confirm intent."""
    cfg = _config()
    db = Path(db_path) if db_path is not None else get_db_path(cfg)
    # Keep an explicitly selected database and its default metrics sidecar
    # together. This matters for experiment homes and operator rollback: a
    # custom ``--db`` must not fall back to the active profile's configured
    # metrics database.
    if metrics_path is not None:
        metrics = Path(metrics_path)
    elif db_path is not None:
        metrics = get_metrics_path(cfg, db_path=db)
    else:
        metrics = get_metrics_path(cfg)
    removed = False
    for path in (db, metrics):
        if path.exists() and path.is_file():
            path.unlink()
            removed = True
        for sidecar in (Path(f"{path}-wal"), Path(f"{path}-shm")):
            if sidecar.exists() and sidecar.is_file():
                sidecar.unlink()
                removed = True
    return removed
