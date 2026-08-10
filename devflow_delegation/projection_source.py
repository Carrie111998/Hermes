"""Read-only, stable paging over the canonical DDP SQLite ledger."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from devflow_delegation.public_summary import (
    public_artifact_summary,
    public_decision_summary,
    public_evidence_summary,
    public_lease_summary,
    public_request_summary,
    public_transition_summary,
)


@dataclass(frozen=True)
class SourcePage:
    stream: str
    rows: tuple[dict[str, object], ...]
    next_position: dict[str, object] | None
    high_watermark: dict[str, object]


@dataclass(frozen=True)
class _Stream:
    table: str
    transform: Callable[[dict[str, object]], dict[str, object] | None]
    public_id: str | None = None


_STREAMS = {
    "requests": _Stream("requests", public_request_summary),
    "transitions": _Stream("transitions", public_transition_summary, "transition_id"),
    "evidence": _Stream("evidence_log", public_evidence_summary, "evidence_id"),
    "decisions": _Stream("human_decisions", public_decision_summary, "decision_id"),
    "leases": _Stream("leases", public_lease_summary),
    "artifacts": _Stream("artifacts", public_artifact_summary, "artifact_id"),
}


class DdpProjectionSource:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._snapshots: dict[str, tuple[dict[str, object], sqlite3.Connection]] = {}

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        uri = f"file:{quote(self.db_path.resolve().as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    @staticmethod
    def _stream(stream: str) -> _Stream:
        try:
            return _STREAMS[stream]
        except KeyError as exc:
            raise ValueError(f"unsupported stream: {stream}") from exc

    def close(self) -> None:
        for _watermark, conn in self._snapshots.values():
            conn.rollback()
            conn.close()
        self._snapshots.clear()

    def _snapshot(
        self,
        stream: str,
        high_watermark: dict[str, object] | None,
    ) -> tuple[sqlite3.Connection, dict[str, object], bool]:
        current = self._snapshots.get(stream)
        if current is not None:
            stored_watermark, conn = current
            if high_watermark is None or high_watermark == stored_watermark:
                return conn, dict(stored_watermark), False
            raise ValueError(f"high watermark does not match active {stream} scan")
        if high_watermark is not None:
            raise ValueError(f"no active {stream} scan for supplied high watermark")

        conn = self._connect()
        conn.execute("BEGIN")
        # Establish the WAL read snapshot before a concurrent writer can change
        # mutable request or lease rows between source pages.
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        if stream == "requests":
            row = conn.execute(
                "SELECT updated_at, request_id FROM requests "
                "ORDER BY updated_at DESC, request_id DESC LIMIT 1"
            ).fetchone()
            watermark: dict[str, object] = (
                {"updated_at": row["updated_at"], "request_id": row["request_id"]}
                if row is not None
                else {"updated_at": "", "request_id": ""}
            )
        else:
            table = self._stream(stream).table
            maximum = conn.execute(
                f"SELECT COALESCE(MAX(rowid), 0) AS id FROM {table}"
            ).fetchone()["id"]
            watermark = {"id": int(maximum)}
        self._snapshots[stream] = (dict(watermark), conn)
        return conn, watermark, True

    @staticmethod
    def _request_cursor(value: dict[str, object] | None) -> dict[str, str]:
        if value is None:
            return {"updated_at": "", "request_id": ""}
        if set(value) != {"updated_at", "request_id"} or not all(
            isinstance(value[key], str) for key in ("updated_at", "request_id")
        ):
            raise ValueError("invalid requests cursor")
        return {"updated_at": value["updated_at"], "request_id": value["request_id"]}

    @staticmethod
    def _integer_cursor(stream: str, value: dict[str, object] | None) -> int:
        if value is None:
            return 0
        if set(value) != {"id"} or not isinstance(value["id"], int) or value["id"] < 0:
            raise ValueError(f"invalid {stream} cursor")
        return value["id"]

    def read_page(
        self,
        stream: str,
        *,
        after: dict[str, object] | None,
        high_watermark: dict[str, object] | None,
        limit: int,
    ) -> SourcePage:
        if not 1 <= limit <= 500:
            raise ValueError("limit must satisfy 1 <= limit <= 500")
        definition = self._stream(stream)
        conn, snapshot_watermark, _created = self._snapshot(stream, high_watermark)
        if stream == "requests":
            page = self._read_requests(
                conn,
                definition,
                after=after,
                high_watermark=snapshot_watermark,
                limit=limit,
            )
        else:
            page = self._read_integer_stream(
                conn,
                stream,
                definition,
                after=after,
                high_watermark=snapshot_watermark,
                limit=limit,
            )
        if page.next_position is None:
            _watermark, finished = self._snapshots.pop(stream)
            finished.rollback()
            finished.close()
        return page

    def _read_requests(
        self,
        conn: sqlite3.Connection,
        definition: _Stream,
        *,
        after: dict[str, object] | None,
        high_watermark: dict[str, object] | None,
        limit: int,
    ) -> SourcePage:
        if high_watermark is None:
            raise ValueError("requests high watermark is required")
        after = self._request_cursor(after)
        high_watermark = self._request_cursor(high_watermark)
        sql = """
            SELECT * FROM requests
            WHERE (updated_at > ? OR (updated_at = ? AND request_id > ?))
              AND (updated_at < ? OR (updated_at = ? AND request_id <= ?))
            ORDER BY updated_at, request_id
            LIMIT ?
        """
        raw_rows = conn.execute(
            sql,
            (
                after["updated_at"],
                after["updated_at"],
                after["request_id"],
                high_watermark["updated_at"],
                high_watermark["updated_at"],
                high_watermark["request_id"],
                limit,
            ),
        ).fetchall()
        rows = tuple(definition.transform(dict(row)) for row in raw_rows)
        next_position = (
            {
                "updated_at": raw_rows[-1]["updated_at"],
                "request_id": raw_rows[-1]["request_id"],
            }
            if raw_rows
            else None
        )
        return SourcePage("requests", rows, next_position, dict(high_watermark))

    def _read_integer_stream(
        self,
        conn: sqlite3.Connection,
        stream: str,
        definition: _Stream,
        *,
        after: dict[str, object] | None,
        high_watermark: dict[str, object] | None,
        limit: int,
    ) -> SourcePage:
        if high_watermark is None:
            raise ValueError(f"{stream} high watermark is required")
        after_id = self._integer_cursor(stream, after)
        high_id = self._integer_cursor(stream, high_watermark)
        raw_rows = conn.execute(
            f"SELECT rowid AS _source_id, * FROM {definition.table} "
            "WHERE rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
            (after_id, high_id, limit),
        ).fetchall()

        output: list[dict[str, object]] = []
        for raw in raw_rows:
            source_row = dict(raw)
            source_id = int(source_row.pop("_source_id"))
            if definition.public_id is not None and "id" not in source_row:
                source_row["id"] = source_id
            transformed = definition.transform(source_row)
            if transformed is not None:
                output.append(transformed)
        next_position = {"id": int(raw_rows[-1]["_source_id"])} if raw_rows else None
        return SourcePage(stream, tuple(output), next_position, dict(high_watermark))

    def source_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            return {
                stream: int(
                    conn.execute(f"SELECT COUNT(*) AS n FROM {definition.table}").fetchone()["n"]
                )
                for stream, definition in _STREAMS.items()
            }
