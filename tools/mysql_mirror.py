"""MySQL mirror for state.db — dual-write plugin.

Reads connection info from the agent profile's .env:
  MYSQL_MIRROR_HOST, MYSQL_MIRROR_PORT, MYSQL_MIRROR_USER,
  MYSQL_MIRROR_PASSWORD, MYSQL_MIRROR_MACHINE

If MYSQL_MIRROR_HOST is absent (or pymysql unavailable), the mirror is
disabled and every public call becomes a silent no-op — state.db writes
are NEVER blocked or failed by this module.

Database name is derived from HERMES_HOME:
  ~/.hermes/profiles/<name>  ->  database <name>
  ~/.hermes (default)        ->  database "hermes"
"""

import json
import logging
import os
import socket
import threading

logger = logging.getLogger(__name__)

_MIRROR = None
_MIRROR_INIT_LOCK = threading.Lock()
_DISABLE_REASONS = set()  # one-time log reasons


def _log_once(reason: str) -> None:
    if reason not in _DISABLE_REASONS:
        _DISABLE_REASONS.add(reason)
        logger.info("[mysql-mirror] disabled: %s", reason)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _machine_id() -> str:
    mid = _env("MYSQL_MIRROR_MACHINE")
    if mid:
        return mid
    return socket.gethostname().split(".")[0]


def _database_name() -> str:
    home = _env("HERMES_HOME") or os.path.expanduser("~/.hermes")
    # ~/.hermes/profiles/<name> -> <name>
    parts = os.path.normpath(home).rstrip("/").split(os.sep)
    if len(parts) >= 2 and parts[-2] == "profiles":
        return parts[-1]
    return "hermes"


class _Mirror:
    """Lazily-connected pymysql writer with thread-safe access."""

    def __init__(self, host: str, port: int, user: str, password: str,
                 db: str, machine: str):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._db = db
        self.machine = machine
        self._lock = threading.Lock()
        self._conn = None

    def _connect(self):
        import pymysql
        return pymysql.connect(
            host=self._host, port=self._port, user=self._user,
            password=self._password, database=self._db,
            charset="utf8mb4", autocommit=True,
            connect_timeout=3, read_timeout=10, write_timeout=10,
        )

    def _conn_or_none(self):
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        try:
            self._conn = self._connect()
            return self._conn
        except Exception as e:
            logger.warning("[mysql-mirror] connect failed: %s", e)
            return None

    def execute(self, sql: str, params: tuple):
        """Execute one statement. Thread-safe. Returns True on success."""
        with self._lock:
            conn = self._conn_or_none()
            if conn is None:
                return False
            try:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                return True
            except Exception as e:
                logger.warning("[mysql-mirror] write failed: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass
                self._conn = None
                return False

    def executemany(self, sql: str, rows: list):
        with self._lock:
            conn = self._conn_or_none()
            if conn is None:
                return False
            try:
                with conn.cursor() as cur:
                    cur.executemany(sql, rows)
                return True
            except Exception as e:
                logger.warning("[mysql-mirror] batch write failed: %s", e)
                try:
                    conn.close()
                except Exception:
                    pass
                self._conn = None
                return False


def _get_mirror():
    global _MIRROR
    if _MIRROR is not None:
        return _MIRROR
    with _MIRROR_INIT_LOCK:
        if _MIRROR is not None:
            return _MIRROR
        host = _env("MYSQL_MIRROR_HOST")
        if not host:
            _log_once("MYSQL_MIRROR_HOST not set in env")
            _MIRROR = False  # sentinel: disabled, don't retry
            return False
        try:
            import pymysql  # noqa: F401
        except ImportError:
            _log_once("pymysql not installed")
            _MIRROR = False
            return False
        try:
            port = int(_env("MYSQL_MIRROR_PORT", "3306"))
        except ValueError:
            port = 3306
        _MIRROR = _Mirror(
            host=host,
            port=port,
            user=_env("MYSQL_MIRROR_USER", "root"),
            password=_env("MYSQL_MIRROR_PASSWORD"),
            db=_database_name(),
            machine=_machine_id(),
        )
        return _MIRROR


# ── Public API — every function is a safe no-op when disabled ──────────────

def _j(v):
    """Serialize structured fields to JSON (None-safe)."""
    return json.dumps(v) if v else None


def mirror_message(msg_id, session_id, role, content=None, tool_call_id=None,
                   tool_calls=None, tool_name=None, timestamp=None,
                   token_count=None, finish_reason=None, reasoning=None,
                   reasoning_content=None, reasoning_details=None,
                   codex_reasoning_items=None, codex_message_items=None,
                   platform_message_id=None, observed=False,
                   active=1, effect_disposition=None, compacted=0,
                   api_content=None, display_kind=None, display_metadata=None):
    """INSERT one message row into MySQL (REPLACE for idempotence)."""
    m = _get_mirror()
    if not m:
        return
    def _jj(v):
        return json.dumps(v) if v else None
    # tool_calls may be a Python list or a JSON string already
    tc = tool_calls if isinstance(tool_calls, str) else _jj(tool_calls)
    m.execute(
        "REPLACE INTO messages (machine, id, session_id, role, content, "
        "tool_call_id, tool_calls, tool_name, timestamp, token_count, "
        "finish_reason, reasoning, reasoning_content, reasoning_details, "
        "codex_reasoning_items, codex_message_items, platform_message_id, "
        "observed, active, effect_disposition, compacted, api_content, "
        "display_kind, display_metadata) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (m.machine, msg_id, session_id, role, content, tool_call_id, tc,
         tool_name, timestamp or 0, token_count, finish_reason, reasoning,
         reasoning_content, _j(reasoning_details), _j(codex_reasoning_items),
         _j(codex_message_items), platform_message_id,
         1 if observed else 0, active if active is not None else 1,
         effect_disposition, compacted if compacted else 0, api_content,
         display_kind,
         _j(display_metadata) if not isinstance(display_metadata, str) else display_metadata),
    )


def mirror_messages_batch(session_id, rows):
    """INSERT a batch of message dicts (same keys as append_messages_batch)."""
    m = _get_mirror()
    if not m or not rows:
        return
    payload = []
    for r in rows:
        payload.append((
            m.machine, r.get("id"), session_id, r.get("role"),
            r.get("content"), r.get("tool_call_id"),
            r.get("tool_calls") if isinstance(r.get("tool_calls"), str)
                else _j(r.get("tool_calls")) if "tool_calls" in r else None,
            r.get("tool_name"), r.get("timestamp") or 0,
            r.get("token_count"), r.get("finish_reason"),
            r.get("reasoning"), r.get("reasoning_content"),
            _j(r.get("reasoning_details")), _j(r.get("codex_reasoning_items")),
            _j(r.get("codex_message_items")), r.get("platform_message_id"),
            1 if r.get("observed") else 0,
            r.get("active") if r.get("active") is not None else 1,
            r.get("effect_disposition"),
            r.get("compacted") if r.get("compacted") else 0,
            r.get("api_content"), r.get("display_kind"),
            _j(r.get("display_metadata"))
            if not isinstance(r.get("display_metadata"), str)
            else r.get("display_metadata"),
        ))
    m.executemany(
        "REPLACE INTO messages (machine, id, session_id, role, content, "
        "tool_call_id, tool_calls, tool_name, timestamp, token_count, "
        "finish_reason, reasoning, reasoning_content, reasoning_details, "
        "codex_reasoning_items, codex_message_items, platform_message_id, "
        "observed, active, effect_disposition, compacted, api_content, "
        "display_kind, display_metadata) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        payload,
    )


def mirror_session(session_row):
    """UPSERT one sessions row. session_row: dict of column -> value."""
    m = _get_mirror()
    if not m or not session_row:
        return
    cols = [
        "id", "source", "user_id", "model", "model_config", "system_prompt",
        "parent_session_id", "started_at", "ended_at", "end_reason",
        "message_count", "tool_call_count", "input_tokens", "output_tokens",
        "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "billing_provider", "billing_base_url", "billing_mode",
        "estimated_cost_usd", "actual_cost_usd", "cost_status", "cost_source",
        "pricing_version", "title", "api_call_count", "handoff_state",
        "handoff_platform", "handoff_error", "cwd", "rewind_count",
        "archived", "session_key", "chat_id", "chat_type", "thread_id",
        "display_name", "origin_json", "expiry_finalized", "git_branch",
        "git_repo_root", "compression_failure_cooldown_until",
        "compression_failure_error", "compression_fallback_streak",
        "compression_ineffective_count", "profile_name", "pinned",
    ]
    vals = [session_row.get(c) for c in cols]
    updates = ", ".join(f"`{c}`=VALUES(`{c}`)" for c in cols[1:])
    m.execute(
        f"INSERT INTO sessions (machine, {', '.join('`'+c+'`' for c in cols)}) "
        f"VALUES ({','.join(['%s']*(len(cols)+1))}) "
        f"ON DUPLICATE KEY UPDATE {updates}",
        tuple([m.machine] + vals),
    )


def mirror_usage(session_id, model, usage_row):
    """UPSERT one session_model_usage row."""
    m = _get_mirror()
    if not m or not usage_row:
        return
    m.execute(
        "INSERT INTO session_model_usage (machine, session_id, model, "
        "billing_provider, billing_base_url, billing_mode, task, "
        "api_call_count, input_tokens, output_tokens, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens, estimated_cost_usd, "
        "actual_cost_usd, cost_status, cost_source, first_seen, last_seen) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE "
        "api_call_count=VALUES(api_call_count), input_tokens=VALUES(input_tokens), "
        "output_tokens=VALUES(output_tokens), cache_read_tokens=VALUES(cache_read_tokens), "
        "cache_write_tokens=VALUES(cache_write_tokens), reasoning_tokens=VALUES(reasoning_tokens), "
        "estimated_cost_usd=VALUES(estimated_cost_usd), actual_cost_usd=VALUES(actual_cost_usd), "
        "cost_status=VALUES(cost_status), cost_source=VALUES(cost_source), "
        "first_seen=VALUES(first_seen), last_seen=VALUES(last_seen)",
        (m.machine, session_id, model,
         usage_row.get("billing_provider", ""),
         usage_row.get("billing_base_url", ""),
         usage_row.get("billing_mode", ""),
         usage_row.get("task", ""),
         usage_row.get("api_call_count", 0),
         usage_row.get("input_tokens", 0),
         usage_row.get("output_tokens", 0),
         usage_row.get("cache_read_tokens", 0),
         usage_row.get("cache_write_tokens", 0),
         usage_row.get("reasoning_tokens", 0),
         usage_row.get("estimated_cost_usd", 0),
         usage_row.get("actual_cost_usd", 0),
         usage_row.get("cost_status"),
         usage_row.get("cost_source"),
         usage_row.get("first_seen"),
         usage_row.get("last_seen")),
    )
