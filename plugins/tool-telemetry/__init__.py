"""
Tool Telemetry Plugin for Hermes Agent
=======================================

Passive observability: records structured telemetry on every tool call
without affecting agent behavior. Named for the Moskstraumen — the Lofoten
maelstrom that makes invisible tidal forces visible as surface patterns.

Hooks: pre_tool_call, post_tool_call, on_session_start
Tools: telemetry_summary, telemetry_failures, telemetry_export
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MAX_ARG_LENGTH = 500
DEFAULT_RETENTION_DAYS = 30

# Patterns that look like secrets — redacted before storage
DEFAULT_REDACT_PATTERNS = [
    r"ghp_\w+",           # GitHub PAT
    r"gho_\w+",           # GitHub OAuth
    r"sk-[a-zA-Z0-9]+",   # OpenAI key
    r"AKIA[A-Z0-9_]+",  # AWS access key (includes underscores)
    r"xox[bpoa]-[\w-]+",  # Slack token
    r"AIza[a-zA-Z0-9_-]+",# Google API key
    r"hf_[a-zA-Z0-9]+",   # HuggingFace token
]


def _get_hermes_home() -> Path:
    """Resolve HERMES_HOME or fall back to ~/.hermes."""
    return Path(os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")))


def _get_telemetry_db_path() -> Path:
    """Telemetry DB lives inside the profile directory."""
    return _get_hermes_home() / "telemetry.db"


# Thread-local storage for tracking in-flight tool calls
_local = threading.local()

# Global config (populated in register())
_config: Dict[str, Any] = {}

# Session tracking
_current_session_id: Optional[str] = None


def _get_config_value(key: str, default: Any = None) -> Any:
    """Read a config value from the global _config dict."""
    return _config.get(key, default)


def _redact_string(s: str, patterns: List[str], max_length: int) -> str:
    """Redact secrets and truncate a string for safe storage."""
    if not isinstance(s, str):
        s = str(s)
    for pattern in patterns:
        s = re.sub(pattern, "[REDACTED]", s)
    if len(s) > max_length:
        s = s[:max_length] + "...[truncated]"
    return s


def _redact_args(args: Dict[str, Any], patterns: List[str], max_length: int) -> str:
    """Safely serialize and redact tool arguments."""
    try:
        # Convert all values to strings for storage, redacting secrets
        safe = {}
        for k, v in args.items():
            if isinstance(v, str):
                safe[k] = _redact_string(v, patterns, max_length)
            elif isinstance(v, (dict, list)):
                safe[k] = _redact_string(json.dumps(v, default=str), patterns, max_length)
            else:
                safe[k] = _redact_string(str(v), patterns, max_length)
        return json.dumps(safe, ensure_ascii=False)
    except Exception:
        return "[redaction error]"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    """Get a thread-safe SQLite connection to the telemetry database."""
    db_path = _get_telemetry_db_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except (PermissionError, OSError):
        pass  # Handled by callers via try/except
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_get_db() -> Optional[sqlite3.Connection]:
    """Try to get a DB connection, return None on failure (unwritable path, etc)."""
    try:
        db_path = _get_telemetry_db_path()
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            pass
        conn = sqlite3.connect(str(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id TEXT NOT NULL,
            session_id TEXT,
            profile_name TEXT,
            tool_name TEXT NOT NULL,
            toolset TEXT,
            args_redacted TEXT,
            duration_ms REAL,
            success INTEGER,
            error_message TEXT,
            timestamp REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_name);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_timestamp ON tool_calls(timestamp);
        CREATE INDEX IF NOT EXISTS idx_tool_calls_success ON tool_calls(success);

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            profile_name TEXT,
            started_at REAL,
            tool_call_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0
        );
    """)
    conn.commit()


def _enforce_retention(conn: sqlite3.Connection, retention_days: int) -> None:
    """Delete records older than retention_days. 0 = keep forever."""
    if retention_days <= 0:
        return
    cutoff = time.time() - (retention_days * 86400)
    conn.execute("DELETE FROM tool_calls WHERE timestamp < ?", (cutoff,))
    conn.execute("DELETE FROM sessions WHERE started_at < ?", (cutoff,))
    conn.commit()


def _record_call(
    call_id: str,
    session_id: Optional[str],
    profile_name: Optional[str],
    tool_name: str,
    toolset: Optional[str],
    args_redacted: str,
    duration_ms: Optional[float],
    success: bool,
    error_message: Optional[str],
    timestamp: float,
) -> None:
    """Insert a tool call record into the telemetry database."""
    with _db_lock:
        conn = _safe_get_db()
        if conn is None:
            return  # Can't write — fail silently
        try:
            _init_db(conn)
            conn.execute(
                """INSERT INTO tool_calls
                   (call_id, session_id, profile_name, tool_name, toolset,
                    args_redacted, duration_ms, success, error_message, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (call_id, session_id, profile_name, tool_name, toolset,
                 args_redacted, duration_ms, int(success), error_message, timestamp),
            )
            # Update session stats
            if session_id:
                conn.execute(
                    """INSERT INTO sessions (id, profile_name, started_at, tool_call_count, error_count)
                       VALUES (?, ?, ?, 1, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           tool_call_count = tool_call_count + 1,
                           error_count = error_count + ?""",
                    (session_id, profile_name, timestamp, int(not success), int(not success)),
                )
            conn.commit()
            # Enforce retention
            retention = _get_config_value("retention_days", DEFAULT_RETENTION_DAYS)
            _enforce_retention(conn, retention)
        except Exception:
            pass  # Never let telemetry failure break the agent
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Hook handlers
# ---------------------------------------------------------------------------

def _on_pre_tool_call(**kwargs: Any) -> None:
    """Record the start of a tool call."""
    tool_name = kwargs.get("tool_name", "unknown")
    args = kwargs.get("args", {})
    toolset = kwargs.get("toolset", "")

    call_id = str(uuid.uuid4())
    patterns = _get_config_value("redact_patterns", DEFAULT_REDACT_PATTERNS)
    max_arg_len = _get_config_value("max_arg_length", DEFAULT_MAX_ARG_LENGTH)

    _local.call_id = call_id
    _local.start_time = time.time()
    _local.tool_name = tool_name
    _local.toolset = toolset
    _local.args_redacted = _redact_args(args, patterns, max_arg_len)


def _on_post_tool_call(**kwargs: Any) -> None:
    """Record the completion of a tool call."""
    tool_name = kwargs.get("tool_name", getattr(_local, "tool_name", "unknown"))
    success = kwargs.get("success", True)
    error = kwargs.get("error", None)
    toolset = kwargs.get("toolset", getattr(_local, "toolset", ""))

    call_id = getattr(_local, "call_id", str(uuid.uuid4()))
    start_time = getattr(_local, "start_time", time.time())
    duration_ms = (time.time() - start_time) * 1000
    args_redacted = getattr(_local, "args_redacted", "{}")

    _record_call(
        call_id=call_id,
        session_id=_current_session_id,
        profile_name=os.environ.get("HERMES_HOME", "").split("/")[-1] or "default",
        tool_name=tool_name,
        toolset=toolset,
        args_redacted=args_redacted,
        duration_ms=duration_ms,
        success=success,
        error_message=str(error)[:500] if error else None,
        timestamp=start_time,
    )

    # Clean up thread-local
    for attr in ("call_id", "start_time", "tool_name", "toolset", "args_redacted"):
        if hasattr(_local, attr):
            delattr(_local, attr)


def _on_session_start(**kwargs: Any) -> None:
    """Track the current session ID for correlating tool calls."""
    global _current_session_id
    _current_session_id = kwargs.get("session_id") or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Tool handlers (exposed to the agent)
# ---------------------------------------------------------------------------

def _tool_telemetry_summary(args: Dict[str, Any], **kw: Any) -> str:
    """Provide aggregate telemetry statistics over a time window.

    Args:
        hours: Look-back window in hours (default: 24)
        group_by: Group results by 'tool', 'toolset', or 'session' (default: 'tool')
    """
    hours = args.get("hours", 24)
    group_by = args.get("group_by", "tool")
    cutoff = time.time() - (hours * 3600)

    valid_groups = {"tool": "tool_name", "toolset": "toolset", "session": "session_id"}
    group_col = valid_groups.get(group_by, "tool_name")

    with _db_lock:
        conn = _safe_get_db()
        if conn is None:
            return json.dumps({"error": "telemetry database unavailable"})
        try:
            _init_db(conn)
            rows = conn.execute(
                f"""SELECT {group_col} as grp,
                           COUNT(*) as total_calls,
                           SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                           SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures,
                           AVG(duration_ms) as avg_duration_ms,
                           MAX(duration_ms) as max_duration_ms,
                           MIN(duration_ms) as min_duration_ms
                    FROM tool_calls
                    WHERE timestamp >= ?
                    GROUP BY {group_col}
                    ORDER BY total_calls DESC""",
                (cutoff,),
            ).fetchall()

            total = conn.execute(
                "SELECT COUNT(*) FROM tool_calls WHERE timestamp >= ?", (cutoff,)
            ).fetchone()[0]

            result = {
                "time_window_hours": hours,
                "group_by": group_by,
                "total_calls": total,
                "groups": [dict(row) for row in rows],
            }
        except Exception as e:
            result = {"error": str(e)}
        finally:
            conn.close()

    return json.dumps(result, indent=2)


def _tool_telemetry_failures(args: Dict[str, Any], **kw: Any) -> str:
    """Show recent tool call failures with error clustering.

    Args:
        hours: Look-back window in hours (default: 24)
        limit: Maximum number of failure records to return (default: 50)
    """
    hours = args.get("hours", 24)
    limit = min(args.get("limit", 50), 200)
    cutoff = time.time() - (hours * 3600)

    with _db_lock:
        conn = _safe_get_db()
        if conn is None:
            return json.dumps({"error": "telemetry database unavailable"})
        try:
            _init_db(conn)
            # Recent failures
            failures = conn.execute(
                """SELECT tool_name, toolset, error_message, timestamp, session_id
                   FROM tool_calls
                   WHERE success = 0 AND timestamp >= ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()

            # Error clustering — group by tool + error pattern
            clusters = conn.execute(
                """SELECT tool_name,
                          error_message,
                          COUNT(*) as occurrence_count,
                          MAX(timestamp) as last_seen
                   FROM tool_calls
                   WHERE success = 0 AND timestamp >= ?
                   GROUP BY tool_name, error_message
                   ORDER BY occurrence_count DESC
                   LIMIT 20""",
                (cutoff,),
            ).fetchall()

            result = {
                "time_window_hours": hours,
                "total_failures": len(failures),
                "recent_failures": [dict(row) for row in failures],
                "error_clusters": [dict(row) for row in clusters],
            }
        except Exception as e:
            result = {"error": str(e)}
        finally:
            conn.close()

    return json.dumps(result, indent=2)


def _tool_telemetry_export(args: Dict[str, Any], **kw: Any) -> str:
    """Export telemetry data as JSON.

    Args:
        hours: Look-back window in hours (default: 24)
        format: Output format — 'summary' or 'full' (default: 'summary')
    """
    hours = args.get("hours", 24)
    fmt = args.get("format", "summary")
    cutoff = time.time() - (hours * 3600)

    with _db_lock:
        conn = _safe_get_db()
        if conn is None:
            return json.dumps({"error": "telemetry database unavailable"})
        try:
            _init_db(conn)
            if fmt == "full":
                rows = conn.execute(
                    """SELECT * FROM tool_calls WHERE timestamp >= ?
                       ORDER BY timestamp DESC""",
                    (cutoff,),
                ).fetchall()
                result = {
                    "format": "full",
                    "hours": hours,
                    "records": [dict(row) for row in rows],
                }
            else:
                # Summary export — aggregate stats
                total = conn.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE timestamp >= ?", (cutoff,)
                ).fetchone()[0]
                success_count = conn.execute(
                    "SELECT COUNT(*) FROM tool_calls WHERE timestamp >= ? AND success = 1",
                    (cutoff,),
                ).fetchone()[0]
                avg_dur = conn.execute(
                    "SELECT AVG(duration_ms) FROM tool_calls WHERE timestamp >= ?",
                    (cutoff,),
                ).fetchone()[0]

                # Per-tool breakdown
                tools = conn.execute(
                    """SELECT tool_name,
                              COUNT(*) as calls,
                              SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as failures,
                              AVG(duration_ms) as avg_ms
                       FROM tool_calls WHERE timestamp >= ?
                       GROUP BY tool_name ORDER BY calls DESC""",
                    (cutoff,),
                ).fetchall()

                result = {
                    "format": "summary",
                    "hours": hours,
                    "total_calls": total,
                    "success_rate": (success_count / total * 100) if total > 0 else 0,
                    "avg_duration_ms": round(avg_dur, 2) if avg_dur else 0,
                    "per_tool": [dict(row) for row in tools],
                }
        except Exception as e:
            result = {"error": str(e)}
        finally:
            conn.close()

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register telemetry hooks and tools with the Hermes plugin context."""
    global _config

    # Load plugin configuration from config.yaml
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        plugin_cfg = (cfg.get("plugins") or {}).get("entries") or {}
        telemetry_cfg = plugin_cfg.get("tool-telemetry") or {}
        _config = {
            "max_arg_length": telemetry_cfg.get("max_arg_length", DEFAULT_MAX_ARG_LENGTH),
            "redact_patterns": telemetry_cfg.get("redact_patterns", DEFAULT_REDACT_PATTERNS),
            "retention_days": telemetry_cfg.get("retention_days", DEFAULT_RETENTION_DAYS),
        }
    except Exception:
        _config = {
            "max_arg_length": DEFAULT_MAX_ARG_LENGTH,
            "redact_patterns": DEFAULT_REDACT_PATTERNS,
            "retention_days": DEFAULT_RETENTION_DAYS,
        }

    # Register hooks (passive observers — never block or transform)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("on_session_start", _on_session_start)

    # Register tools for agent self-diagnosis
    ctx.register_tool(
        name="telemetry_summary",
        toolset="telemetry",
        schema={
            "name": "telemetry_summary",
            "description": (
                "Get aggregate tool call telemetry statistics. Shows call counts, "
                "success rates, and duration metrics grouped by tool, toolset, or "
                "session over a specified time window. Use this to understand tool "
                "usage patterns and identify degraded tools."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours (default: 24)",
                        "default": 24,
                    },
                    "group_by": {
                        "type": "string",
                        "enum": ["tool", "toolset", "session"],
                        "description": "Group results by tool name, toolset, or session (default: tool)",
                        "default": "tool",
                    },
                },
            },
        },
        handler=lambda args, **kw: _tool_telemetry_summary(args, **kw),
        description="Aggregate tool call telemetry statistics",
        emoji="📊",
    )

    ctx.register_tool(
        name="telemetry_failures",
        toolset="telemetry",
        schema={
            "name": "telemetry_failures",
            "description": (
                "Show recent tool call failures with error clustering. Lists recent "
                "failed calls and groups recurring errors by tool and message pattern "
                "to surface chronic issues. Use this when diagnosing tool reliability "
                "problems."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours (default: 24)",
                        "default": 24,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of recent failures to return (default: 50, max: 200)",
                        "default": 50,
                    },
                },
            },
        },
        handler=lambda args, **kw: _tool_telemetry_failures(args, **kw),
        description="Recent tool call failures with error clustering",
        emoji="⚠️",
    )

    ctx.register_tool(
        name="telemetry_export",
        toolset="telemetry",
        schema={
            "name": "telemetry_export",
            "description": (
                "Export telemetry data as JSON. Supports 'summary' (aggregate stats) "
                "and 'full' (individual call records) formats. Use this when you need "
                "to share telemetry data or perform external analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Look-back window in hours (default: 24)",
                        "default": 24,
                    },
                    "format": {
                        "type": "string",
                        "enum": ["summary", "full"],
                        "description": "Output format — summary (aggregate) or full (individual records). Default: summary",
                        "default": "summary",
                    },
                },
            },
        },
        handler=lambda args, **kw: _tool_telemetry_export(args, **kw),
        description="Export telemetry data as JSON",
        emoji="📤",
    )

    # Initialize the database on load
    try:
        conn = _safe_get_db()
        if conn:
            _init_db(conn)
            conn.close()
    except Exception:
        pass  # Will be created on first tool call