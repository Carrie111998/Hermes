"""Canonical ordered events, bounded redaction, spool import, and cursors."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .database import write_txn
from .schema import meta_value
from .types import ContractError, EventRecord

_SECRET_KEY = re.compile(
    r"(?i)(authorization|cookie|credential|password|passwd|secret|token|api[_-]?key|private[_-]?key)"
)
_PATH_KEY = re.compile(r"(?i)(^|_)(path|cwd|home|directory|dir|workspace|root)$")
_MAX_DEPTH = 6
_MAX_ITEMS = 64
_MAX_STRING_BYTES = 4096
_MAX_PAYLOAD_BYTES = 32 * 1024


class CursorExpired(RuntimeError):
    def __init__(self, oldest_cursor: str) -> None:
        super().__init__("event cursor expired")
        self.oldest_cursor = oldest_cursor


def _truncate_utf8(value: str, limit: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= limit:
        return value
    clipped = raw[:limit]
    while clipped:
        try:
            return clipped.decode("utf-8") + "…"
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "…"


def redact_and_bound(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Return a persistence-safe copy before any spool or database write."""

    if key and _SECRET_KEY.search(key):
        return "***"
    if depth > _MAX_DEPTH:
        return "<max-depth>"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else "<non-finite>"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "bytes", "length": len(value), "sha256": hashlib.sha256(bytes(value)).hexdigest()}
    if isinstance(value, str):
        if key and _PATH_KEY.search(key):
            # Never persist a complete host path.  Preserve only a basename-like
            # hint, bounded to avoid turning paths into a second log channel.
            value = f"<path>/{Path(value).name}" if Path(value).name else "<path>"
        return _truncate_utf8(value, _MAX_STRING_BYTES)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (child_key, child) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                result["<truncated>"] = len(value) - _MAX_ITEMS
                break
            name = _truncate_utf8(str(child_key), 256)
            result[name] = redact_and_bound(child, key=name, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        bounded = [
            redact_and_bound(item, depth=depth + 1)
            for item in items[:_MAX_ITEMS]
        ]
        if len(items) > _MAX_ITEMS:
            bounded.append({"<truncated>": len(items) - _MAX_ITEMS})
        return bounded
    return _truncate_utf8(repr(value), _MAX_STRING_BYTES)


def safe_payload_json(payload: Mapping[str, Any]) -> str:
    safe = redact_and_bound(payload)
    raw = json.dumps(
        safe,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(raw) > _MAX_PAYLOAD_BYTES:
        safe = {
            "truncated": True,
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            "original_bytes": len(raw),
        }
        raw = json.dumps(safe, separators=(",", ":")).encode("utf-8")
    return raw.decode("utf-8")


def append_event(conn, event: EventRecord) -> int:
    payload_json = safe_payload_json(event.payload)
    now = int(time.time())
    sql = """
        INSERT OR IGNORE INTO task_events(
            event_uuid, task_id, run_id, claim_generation, schema_version,
            event_type, source, severity, retention_class, correlation_id,
            operation_id, stream, stream_seq, host_committed_at,
            producer_time, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        event.event_uuid,
        event.task_id,
        event.run_id,
        event.claim_generation,
        event.schema_version,
        event.event_type,
        event.source,
        event.severity,
        event.retention_class,
        event.correlation_id,
        event.operation_id,
        event.stream,
        event.stream_seq,
        now,
        event.producer_time,
        payload_json,
    )
    if conn.in_transaction:
        conn.execute(sql, params)
    else:
        with write_txn(conn):
            conn.execute(sql, params)
    row = conn.execute(
        "SELECT event_seq FROM task_events WHERE event_uuid=?", (event.event_uuid,)
    ).fetchone()
    if not row:
        raise RuntimeError("event insert was not observable")
    return int(row[0])


class EventSpoolWriter:
    """Line-flushed JSONL writer for high-volume worker output."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def write(self, event: EventRecord) -> None:
        data = asdict(event)
        data["payload"] = redact_and_bound(event.payload)
        line = json.dumps(
            data,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(line.encode("utf-8")) > _MAX_PAYLOAD_BYTES + 4096:
            raise ContractError("spool event exceeds bounded frame size")
        self._handle.write(line + "\n")
        self._handle.flush()
        os.fsync(self._handle.fileno())

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> "EventSpoolWriter":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def import_spool(conn, path: str | Path, *, limit: int = 1000) -> dict[str, int]:
    spool = Path(path)
    path_digest = hashlib.sha256(str(spool.resolve()).encode()).hexdigest()
    imported = 0
    duplicates = 0
    malformed = 0
    with spool.open("r", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= limit:
                break
            try:
                raw = json.loads(line)
                event = EventRecord(**raw)
            except Exception:
                malformed += 1
                continue
            with write_txn(conn):
                exists = conn.execute(
                    "SELECT 1 FROM event_spool_imports WHERE event_uuid=?",
                    (event.event_uuid,),
                ).fetchone()
                if exists:
                    duplicates += 1
                    continue
                append_event(conn, event)
                conn.execute(
                    "INSERT INTO event_spool_imports(event_uuid, spool_path_digest, imported_at) "
                    "VALUES (?, ?, ?)",
                    (event.event_uuid, path_digest, int(time.time())),
                )
                imported += 1
    return {"imported": imported, "duplicates": duplicates, "malformed": malformed}


def _cursor_key(conn) -> bytes:
    return bytes.fromhex(meta_value(conn, "cursor_hmac_key"))


def encode_cursor(conn, event_seq: int, *, task_id: str | None = None) -> str:
    payload = {
        "v": 1,
        "board": meta_value(conn, "board_id"),
        "database": meta_value(conn, "database_id"),
        "seq": int(event_seq),
        "task": task_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_cursor_key(conn), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")


def decode_cursor(conn, cursor: str, *, task_id: str | None = None) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        combined = base64.urlsafe_b64decode(padded.encode())
        raw, signature = combined[:-32], combined[-32:]
        expected = hmac.new(_cursor_key(conn), raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(raw)
    except Exception as exc:
        raise ContractError("invalid event cursor") from exc
    if payload.get("v") != 1:
        raise ContractError("unsupported event cursor version")
    if payload.get("board") != meta_value(conn, "board_id"):
        raise ContractError("event cursor belongs to another board")
    if payload.get("database") != meta_value(conn, "database_id"):
        raise ContractError("event cursor belongs to another database")
    if payload.get("task") != task_id:
        raise ContractError("event cursor belongs to another task scope")
    return int(payload["seq"])


def read_events(
    conn,
    *,
    task_id: str | None = None,
    cursor: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        raise ContractError("event limit must be 1..1000")
    after = decode_cursor(conn, cursor, task_id=task_id) if cursor else 0
    if task_id is None:
        oldest_row = conn.execute("SELECT MIN(event_seq) FROM task_events").fetchone()
        rows = conn.execute(
            "SELECT * FROM task_events WHERE event_seq > ? "
            "ORDER BY event_seq LIMIT ?",
            (after, limit + 1),
        ).fetchall()
    else:
        oldest_row = conn.execute(
            "SELECT MIN(event_seq) FROM task_events WHERE task_id=?", (task_id,)
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND event_seq > ? "
            "ORDER BY event_seq LIMIT ?",
            (task_id, after, limit + 1),
        ).fetchall()
    oldest = int(oldest_row[0]) if oldest_row and oldest_row[0] is not None else 0
    if after and oldest and after < oldest - 1:
        raise CursorExpired(encode_cursor(conn, oldest - 1, task_id=task_id))
    has_more = len(rows) > limit
    rows = rows[:limit]
    events = []
    last = after
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        events.append(item)
        last = int(row["event_seq"])
    return {
        "events": events,
        "cursor": encode_cursor(conn, last, task_id=task_id),
        "has_more": has_more,
    }
