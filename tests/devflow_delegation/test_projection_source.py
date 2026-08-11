from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from devflow_delegation.projection_source import DdpProjectionSource


SCHEMA = """
CREATE TABLE requests (
    request_id TEXT PRIMARY KEY, idempotency_key TEXT, fingerprint TEXT,
    envelope_json TEXT, state TEXT, terminal_reason TEXT, source_agent TEXT,
    source_kind TEXT, target_repo TEXT, target_subsystem TEXT, kind TEXT,
    severity TEXT, created_at TEXT, updated_at TEXT, lease_attempt_count INTEGER
);
CREATE TABLE transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, from_state TEXT,
    to_state TEXT, actor TEXT, policy_version TEXT, evidence_ref TEXT, created_at TEXT
);
CREATE TABLE evidence_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT,
    evidence_json TEXT, created_at TEXT
);
CREATE TABLE human_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, actor TEXT,
    decision TEXT, evidence_ref TEXT, confirmation_token TEXT, created_at TEXT
);
CREATE TABLE leases (
    request_id TEXT PRIMARY KEY, lease_id TEXT, holder TEXT, acquired_at TEXT,
    expires_at TEXT, heartbeat_at TEXT, worktree_path TEXT, branch TEXT,
    attempt_count INTEGER
);
CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT, kind TEXT,
    ref TEXT, created_at TEXT
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _request(request_id: str, updated_at: str, *, title: str = "Safe title") -> tuple:
    return (
        request_id,
        f"key:{request_id}",
        f"fp:{request_id}",
        json.dumps({"title": title, "prompt": "never project me"}),
        "REQUESTED",
        None,
        "critic",
        "finding",
        "hermes",
        "gateway",
        "bug",
        "high",
        "2026-08-10T09:00:00+00:00",
        updated_at,
        0,
    )


def _insert_request(conn: sqlite3.Connection, request_id: str, updated_at: str) -> None:
    conn.execute(
        "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        _request(request_id, updated_at),
    )
    conn.commit()


def test_missing_ledger_is_not_created(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "delegation_ledger.db"
    source = DdpProjectionSource(path)

    with pytest.raises(FileNotFoundError):
        source.read_page("requests", after=None, high_watermark=None, limit=10)

    assert not path.exists()
    assert not path.parent.exists()


def test_read_only_source_does_not_migrate_schema(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE requests (request_id TEXT PRIMARY KEY, updated_at TEXT)")
    conn.execute("INSERT INTO requests VALUES ('req_1', '2026-08-10T10:00:00+00:00')")
    conn.commit()
    conn.close()

    source = DdpProjectionSource(path)
    page = source.read_page("requests", after=None, high_watermark=None, limit=10)

    assert page.rows == ({"request_id": "req_1", "updated_at": "2026-08-10T10:00:00+00:00"},)
    check = sqlite3.connect(path)
    assert [row[1] for row in check.execute("PRAGMA table_info(requests)")] == [
        "request_id",
        "updated_at",
    ]
    assert check.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='human_decisions'"
    ).fetchone() is None
    check.close()


def test_request_scan_keeps_a_read_snapshot_across_pages(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.db"
    writer = _connect(path)
    _insert_request(writer, "req_a", "2026-08-10T10:00:00+00:00")
    _insert_request(writer, "req_b", "2026-08-10T10:01:00+00:00")

    source = DdpProjectionSource(path)
    first = source.read_page("requests", after=None, high_watermark=None, limit=1)
    assert [row["request_id"] for row in first.rows] == ["req_a"]

    writer.execute(
        "UPDATE requests SET updated_at='2026-08-10T10:03:00+00:00' WHERE request_id='req_b'"
    )
    writer.execute(
        "UPDATE requests SET updated_at='2026-08-10T10:00:30+00:00' WHERE request_id='req_a'"
    )
    writer.commit()

    second = source.read_page(
        "requests",
        after=first.next_position,
        high_watermark=first.high_watermark,
        limit=10,
    )
    assert [row["request_id"] for row in second.rows] == ["req_b"]
    assert second.rows[0]["updated_at"] == "2026-08-10T10:01:00+00:00"
    source.close()
    writer.close()


def test_request_pages_use_equal_timestamp_request_id_tiebreaker_and_frozen_watermark(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.db"
    conn = _connect(path)
    stamp = "2026-08-10T10:00:00+00:00"
    _insert_request(conn, "req_b", stamp)
    _insert_request(conn, "req_a", stamp)
    _insert_request(conn, "req_c", "2026-08-10T10:01:00+00:00")

    source = DdpProjectionSource(path)
    first = source.read_page("requests", after=None, high_watermark=None, limit=1)
    assert [row["request_id"] for row in first.rows] == ["req_a"]
    assert first.next_position == {"updated_at": stamp, "request_id": "req_a"}
    assert first.high_watermark == {
        "updated_at": "2026-08-10T10:01:00+00:00",
        "request_id": "req_c",
    }

    _insert_request(conn, "req_d", "2026-08-10T10:02:00+00:00")
    second = source.read_page(
        "requests",
        after=first.next_position,
        high_watermark=first.high_watermark,
        limit=10,
    )
    assert [row["request_id"] for row in second.rows] == ["req_b", "req_c"]
    assert all(row["request_id"] != "req_d" for row in second.rows)
    assert "envelope_json" not in second.rows[0]
    conn.close()


@pytest.mark.parametrize(
    ("stream", "table", "insert_sql", "values", "identity_key"),
    [
        (
            "transitions",
            "transitions",
            "INSERT INTO transitions (request_id, from_state, to_state, actor, policy_version, evidence_ref, created_at) VALUES (?,?,?,?,?,?,?)",
            ("req_1", "REQUESTED", "TRIAGED", "devflow-triage", "v1", "ev:1", "2026-08-10T10:00:00+00:00"),
            "transition_id",
        ),
        (
            "evidence",
            "evidence_log",
            "INSERT INTO evidence_log (request_id, evidence_json, created_at) VALUES (?,?,?)",
            ("req_1", '{"kind":"test","summary":"safe"}', "2026-08-10T10:00:00+00:00"),
            "evidence_id",
        ),
        (
            "decisions",
            "human_decisions",
            "INSERT INTO human_decisions (request_id, actor, decision, evidence_ref, confirmation_token, created_at) VALUES (?,?,?,?,?,?)",
            ("req_1", "admin-diego-42", "approve", "rationale:safe", "confirm-super-secret", "2026-08-10T10:00:00+00:00"),
            "decision_id",
        ),
        (
            "artifacts",
            "artifacts",
            "INSERT INTO artifacts (request_id, kind, ref, created_at) VALUES (?,?,?,?)",
            ("req_1", "pr", "https://github.com/acme/hermes/pull/42", "2026-08-10T10:00:00+00:00"),
            "artifact_id",
        ),
    ],
)
def test_append_only_streams_page_by_stable_integer_id(
    tmp_path: Path,
    stream: str,
    table: str,
    insert_sql: str,
    values: tuple,
    identity_key: str,
) -> None:
    path = tmp_path / f"{stream}.db"
    conn = _connect(path)
    conn.execute(insert_sql, values)
    conn.execute(insert_sql, values)
    conn.commit()

    source = DdpProjectionSource(path)
    first = source.read_page(stream, after=None, high_watermark=None, limit=1)
    assert first.rows[0][identity_key] == 1
    assert first.next_position == {"id": 1}
    assert first.high_watermark == {"id": 2}

    conn.execute(insert_sql, values)
    conn.commit()
    second = source.read_page(
        stream,
        after=first.next_position,
        high_watermark=first.high_watermark,
        limit=10,
    )
    assert [row[identity_key] for row in second.rows] == [2]
    conn.close()


def test_lease_stream_uses_internal_rowid_without_exposing_private_fields(tmp_path: Path) -> None:
    path = tmp_path / "leases.db"
    conn = _connect(path)
    conn.execute(
        "INSERT INTO leases VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "req_1",
            "lse_private",
            "admin-diego-42",
            "2026-08-10T10:00:00+00:00",
            "2026-08-10T10:02:00+00:00",
            "2026-08-10T10:01:00+00:00",
            r"C:\Users\diego\.hermes\worktrees\req_1",
            "devflow/req-1",
            1,
        ),
    )
    conn.commit()

    page = DdpProjectionSource(path).read_page(
        "leases", after=None, high_watermark=None, limit=10
    )
    assert page.rows == (
        {
            "request_id": "req_1",
            "acquired_at": "2026-08-10T10:00:00+00:00",
            "expires_at": "2026-08-10T10:02:00+00:00",
            "heartbeat_at": "2026-08-10T10:01:00+00:00",
            "branch": "devflow/req-1",
            "attempt_count": 1,
        },
    )
    assert page.next_position == {"id": 1}
    conn.close()


def test_lease_scan_keeps_a_read_snapshot_across_pages(tmp_path: Path) -> None:
    path = tmp_path / "lease-snapshot.db"
    writer = _connect(path)
    for request_id in ("req_1", "req_2"):
        writer.execute(
            "INSERT INTO leases VALUES (?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                f"lse_{request_id}",
                "executor-private",
                "2026-08-10T10:00:00+00:00",
                "2026-08-10T10:02:00+00:00",
                "2026-08-10T10:01:00+00:00",
                None,
                f"devflow/{request_id}",
                1,
            ),
        )
    writer.commit()

    source = DdpProjectionSource(path)
    first = source.read_page("leases", after=None, high_watermark=None, limit=1)
    assert [row["request_id"] for row in first.rows] == ["req_1"]

    writer.execute(
        "UPDATE leases SET heartbeat_at='2026-08-10T10:05:00+00:00' WHERE request_id='req_2'"
    )
    writer.execute("DELETE FROM leases WHERE request_id='req_1'")
    writer.commit()

    second = source.read_page(
        "leases",
        after=first.next_position,
        high_watermark=first.high_watermark,
        limit=10,
    )
    assert [row["request_id"] for row in second.rows] == ["req_2"]
    assert second.rows[0]["heartbeat_at"] == "2026-08-10T10:01:00+00:00"
    source.close()
    writer.close()


def test_filtered_artifact_page_still_advances_source_cursor(tmp_path: Path) -> None:
    path = tmp_path / "filtered-artifacts.db"
    conn = _connect(path)
    conn.execute(
        "INSERT INTO artifacts (request_id, kind, ref, created_at) VALUES (?,?,?,?)",
        ("req_1", "worktree", r"C:\\Users\\diego\\worktree", "2026-08-10T10:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    page = DdpProjectionSource(path).read_page(
        "artifacts", after=None, high_watermark=None, limit=1
    )
    assert page.rows == ()
    assert page.next_position == {"id": 1}


def test_malformed_cursor_is_rejected_explicitly(tmp_path: Path) -> None:
    path = tmp_path / "cursor.db"
    conn = _connect(path)
    conn.close()

    with pytest.raises(ValueError, match="invalid requests cursor"):
        DdpProjectionSource(path).read_page(
            "requests",
            after={"updated_at": "2026-08-10T10:00:00+00:00"},
            high_watermark=None,
            limit=10,
        )


def test_source_counts_cover_all_supported_streams(tmp_path: Path) -> None:
    path = tmp_path / "counts.db"
    conn = _connect(path)
    _insert_request(conn, "req_1", "2026-08-10T10:00:00+00:00")
    conn.execute(
        "INSERT INTO transitions (request_id, to_state, actor, policy_version, created_at) VALUES ('req_1', 'TRIAGED', 'triage', 'v1', '2026-08-10T10:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    assert DdpProjectionSource(path).source_counts() == {
        "requests": 1,
        "transitions": 1,
        "evidence": 0,
        "decisions": 0,
        "leases": 0,
        "artifacts": 0,
    }


@pytest.mark.parametrize("limit", [0, 501])
def test_page_limit_is_bounded(tmp_path: Path, limit: int) -> None:
    path = tmp_path / "limit.db"
    conn = _connect(path)
    conn.close()
    with pytest.raises(ValueError, match="1 <= limit <= 500"):
        DdpProjectionSource(path).read_page(
            "requests", after=None, high_watermark=None, limit=limit
        )


def test_unknown_stream_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unknown.db"
    conn = _connect(path)
    conn.close()
    with pytest.raises(ValueError, match="unsupported stream"):
        DdpProjectionSource(path).read_page(
            "secrets", after=None, high_watermark=None, limit=10
        )
