from __future__ import annotations

import json
import sqlite3

import pytest

import hermes_state
import session_fallback_spool as spool
from hermes_state import SessionDB, SessionDBBatchMessage


@pytest.fixture()
def db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    session_db = SessionDB(db_path=home / "state.db")
    try:
        yield session_db
    finally:
        session_db.close()


def _bootstrap(
    *,
    session_id: str = "replay-session",
    parent_session_id: str | None = None,
) -> spool.SessionSpoolBootstrap:
    return spool.SessionSpoolBootstrap(
        session_id=session_id,
        source="cli",
        started_at=123.456,
        model="gpt-test",
        model_config={"mode": "replay"},
        system_prompt="persist me exactly",
        parent_session_id=parent_session_id,
        cwd="/tmp/project",
        profile_name="profile-a",
        user_id="user-1",
        session_key="session-key",
        chat_id="chat-1",
        chat_type="group",
        thread_id="thread-1",
    )


def _batch_messages(
    unit_id: str = "unit-1",
    *,
    content: str = "hello replay",
) -> list[SessionDBBatchMessage]:
    return [
        SessionDBBatchMessage(
            persistence_unit_id=unit_id,
            persistence_message_key=f"{unit_id}-key-0",
            persistence_ordinal=0,
            role="user",
            content=content,
            timestamp=100.0,
        )
    ]


def _record(
    unit_id: str = "unit-1",
    *,
    session_id: str = "replay-session",
    content: str = "hello replay",
) -> spool.SessionSpoolRecord:
    return spool.SessionSpoolRecord(
        bootstrap=_bootstrap(session_id=session_id),
        persist_attempt_id="a" * 32,
        persist_attempt_unit_index=0,
        canonical_failure={
            "stage": "append_messages_batch",
            "error_class": "RuntimeError",
            "error_message": "db down",
            "session_row_created": True,
        },
        batch_messages=tuple(_batch_messages(unit_id=unit_id, content=content)),
    )


def _write_sealed_segment(home, sequence: int, *records: spool.SessionSpoolRecord):
    root = home / spool.SPOOL_ROOT_NAME
    sealed = root / spool.SEALED_DIR_NAME
    sealed.mkdir(parents=True, exist_ok=True)
    segment_path = sealed / f"{sequence:020d}.spool"
    segment_path.write_bytes(b"".join(spool._frame_bytes_for_record(record) for record in records))
    return segment_path


def _backfill_record(
    unit_id: str,
    *,
    content: str,
    parent_session_id: str,
) -> spool.SessionSpoolRecord:
    return spool.SessionSpoolRecord(
        bootstrap=_bootstrap(parent_session_id=parent_session_id),
        persist_attempt_id="b" * 32,
        persist_attempt_unit_index=0,
        canonical_failure={
            "stage": "append_messages_batch",
            "error_class": "RuntimeError",
            "error_message": "db down",
            "session_row_created": True,
        },
        batch_messages=tuple(_batch_messages(unit_id=unit_id, content=content)),
    )


def _fts_message_count(db) -> int:
    with db._lock:
        return int(db._conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0])


def _assert_existing_row_backfilled(session, *, parent_session_id: str) -> None:
    assert session["source"] == "cli"
    assert session["parent_session_id"] == parent_session_id
    assert session["profile_name"] == "profile-a"
    assert session["user_id"] == "user-1"
    assert session["session_key"] == "session-key"
    assert session["chat_id"] == "chat-1"
    assert session["chat_type"] == "group"
    assert session["thread_id"] == "thread-1"


def test_existing_row_bootstrap_backfill_manual_replay_fills_null_fields_and_duplicate_safe(
    db, tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    db.create_session("real-parent", "cli")
    db.create_session(
        "replay-session",
        "cli",
        chat_type="group",
        thread_id="thread-1",
    )
    record = _backfill_record(
        "existing-row-bootstrap-backfill-manual",
        content="backfill manual transcript",
        parent_session_id="real-parent",
    )
    _write_sealed_segment(home, 1, record)

    result = spool.replay_to_session_db(db, trigger="manual")
    session = db.get_session("replay-session")

    assert result.state is spool.ReplayRunState.REPLAYED
    assert session is not None
    _assert_existing_row_backfilled(session, parent_session_id="real-parent")
    assert session["message_count"] == 1
    assert [row["content"] for row in db.get_messages("replay-session")] == [
        "backfill manual transcript"
    ]
    assert _fts_message_count(db) == 1

    duplicate = db.reconcile_bootstrap_and_append_messages_batch(
        record.bootstrap,
        record.batch_messages,
        replay_patience_s=2.0,
    )

    session_after = db.get_session("replay-session")
    assert duplicate.inserted_count == 0
    assert duplicate.duplicate_count == 1
    assert session_after is not None
    _assert_existing_row_backfilled(session_after, parent_session_id="real-parent")
    assert session_after["message_count"] == 1
    assert [row["content"] for row in db.get_messages("replay-session")] == [
        "backfill manual transcript"
    ]
    assert _fts_message_count(db) == 1


def test_existing_row_bootstrap_backfill_startup_replay_fills_null_fields_after_restart(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    setup_db = SessionDB(db_path=home / "state.db")
    try:
        setup_db.create_session("real-parent", "cli")
        setup_db.create_session("replay-session", "cli")
    finally:
        setup_db.close()

    record = _backfill_record(
        "existing-row-bootstrap-backfill-startup",
        content="backfill startup transcript",
        parent_session_id="real-parent",
    )
    _write_sealed_segment(home, 1, record)
    monkeypatch.setattr(hermes_state, "_SESSION_SPOOL_STARTUP_ONCE", set(), raising=False)

    startup_db = SessionDB(db_path=home / "state.db")
    try:
        session = startup_db.get_session("replay-session")
        assert session is not None
        _assert_existing_row_backfilled(session, parent_session_id="real-parent")
        assert session["message_count"] == 1
        assert [row["content"] for row in startup_db.get_messages("replay-session")] == [
            "backfill startup transcript"
        ]
        assert _fts_message_count(startup_db) == 1
    finally:
        startup_db.close()

    monkeypatch.setattr(hermes_state, "_SESSION_SPOOL_STARTUP_ONCE", set(), raising=False)
    restarted = SessionDB(db_path=home / "state.db")
    try:
        session = restarted.get_session("replay-session")
        assert session is not None
        _assert_existing_row_backfilled(session, parent_session_id="real-parent")
        assert session["message_count"] == 1
        assert [row["content"] for row in restarted.get_messages("replay-session")] == [
            "backfill startup transcript"
        ]
        assert _fts_message_count(restarted) == 1
    finally:
        restarted.close()


def test_existing_row_bootstrap_backfill_conflict_rolls_back_metadata_messages_counters_and_fts(
    db,
):
    db.create_session("real-parent", "cli")
    db.create_session(
        "replay-session",
        "cli",
        thread_id="wrong-thread",
    )
    record = _backfill_record(
        "existing-row-bootstrap-backfill-conflict",
        content="backfill conflict transcript",
        parent_session_id="real-parent",
    )

    with pytest.raises(hermes_state.AppendMessagesBatchConflictError, match="thread_id"):
        db.reconcile_bootstrap_and_append_messages_batch(
            record.bootstrap,
            record.batch_messages,
            replay_patience_s=2.0,
        )

    session = db.get_session("replay-session")
    assert session is not None
    assert session["source"] == "cli"
    assert session["thread_id"] == "wrong-thread"
    assert session["parent_session_id"] is None
    assert session["profile_name"] is None
    assert session["user_id"] is None
    assert session["session_key"] is None
    assert session["chat_id"] is None
    assert session["chat_type"] is None
    assert session["message_count"] == 0
    assert db.get_messages("replay-session") == []
    assert _fts_message_count(db) == 0


def test_reconcile_bootstrap_and_append_messages_batch_creates_missing_session_row(db):
    bootstrap = _bootstrap()

    result = db.reconcile_bootstrap_and_append_messages_batch(
        bootstrap,
        _batch_messages(),
        replay_patience_s=2.0,
    )

    assert result.inserted_count == 1
    assert result.duplicate_count == 0
    session = db.get_session("replay-session")
    assert session is not None
    assert session["source"] == "cli"
    assert session["model"] == "gpt-test"
    assert session["system_prompt"] == "persist me exactly"
    assert session["parent_session_id"] is None
    assert session["cwd"] == "/tmp/project"
    assert session["profile_name"] == "profile-a"
    assert session["user_id"] == "user-1"
    assert session["session_key"] == "session-key"
    assert session["chat_id"] == "chat-1"
    assert session["chat_type"] == "group"
    assert session["thread_id"] == "thread-1"
    assert [row["content"] for row in db.get_messages("replay-session")] == ["hello replay"]


def test_reconcile_bootstrap_and_append_messages_batch_rejects_missing_parent(db):
    bootstrap = _bootstrap(parent_session_id="missing-parent")

    with pytest.raises(hermes_state.AppendMessagesBatchConflictError, match="parent"):
        db.reconcile_bootstrap_and_append_messages_batch(
            bootstrap,
            _batch_messages(),
            replay_patience_s=2.0,
        )

    assert db.get_session("replay-session") is None
    assert db.get_messages("replay-session") == []


def test_missing_parent_manual_replay_blocks_and_preserves_later_fifo_head(
    db, tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_sealed_segment(
        home,
        1,
        spool.SessionSpoolRecord(
            bootstrap=_bootstrap(parent_session_id="missing-parent"),
            persist_attempt_id="b" * 32,
            persist_attempt_unit_index=0,
            canonical_failure={
                "stage": "append_messages_batch",
                "error_class": "RuntimeError",
                "error_message": "db down",
                "session_row_created": True,
            },
            batch_messages=tuple(_batch_messages(unit_id="unit-missing", content="alpha")),
        ),
    )
    later = _write_sealed_segment(home, 2, _record(unit_id="unit-later", content="later"))

    result = spool.replay_to_session_db(db, trigger="manual")

    assert result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert result.first_blocked_segment == 1
    assert result.first_blocked_offset == 0
    assert result.error_class == "AppendMessagesBatchConflictError"
    assert db.get_messages("replay-session") == []
    assert later.exists()


def test_missing_parent_startup_replay_opens_and_preserves_sealed_head(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_state, "_SESSION_SPOOL_STARTUP_ONCE", set(), raising=False)
    head = _write_sealed_segment(
        home,
        1,
        spool.SessionSpoolRecord(
            bootstrap=_bootstrap(parent_session_id="missing-parent"),
            persist_attempt_id="c" * 32,
            persist_attempt_unit_index=0,
            canonical_failure={
                "stage": "append_messages_batch",
                "error_class": "RuntimeError",
                "error_message": "db down",
                "session_row_created": True,
            },
            batch_messages=tuple(_batch_messages(unit_id="unit-startup", content="alpha")),
        ),
    )

    startup_db = SessionDB(db_path=home / "state.db")
    try:
        assert startup_db.get_messages("replay-session") == []
        assert head.exists()
    finally:
        startup_db.close()


def test_compression_busy_returns_retry_pending_and_preserves_later_fifo(
    db, tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_sealed_segment(home, 1, _record(unit_id="unit-a", content="alpha"))
    later = _write_sealed_segment(home, 2, _record(unit_id="unit-b", content="later"))

    def _busy(*_args, **_kwargs):
        raise hermes_state.CompressionSessionBusyError("busy")

    monkeypatch.setattr(db, "reconcile_bootstrap_and_append_messages_batch", _busy)

    result = spool.replay_to_session_db(db, trigger="manual")

    assert result.state is spool.ReplayRunState.RETRY_PENDING
    assert result.retry_class is not None
    assert result.cooldown_seconds > 0
    assert result.frames_acked == 0
    assert db.get_messages("replay-session") == []
    assert later.exists()


def test_compression_closed_returns_blocked_integrity(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_sealed_segment(home, 1, _record(unit_id="unit-a", content="alpha"))

    def _closed(*_args, **_kwargs):
        raise hermes_state.CompressionSessionClosedError("replay-session")

    monkeypatch.setattr(db, "reconcile_bootstrap_and_append_messages_batch", _closed)

    result = spool.replay_to_session_db(db, trigger="manual")

    assert result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert result.first_blocked_segment == 1
    assert result.first_blocked_offset == 0
    assert result.error_class == "CompressionSessionClosedError"


def test_sqlite_locked_returns_retry_pending(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_sealed_segment(home, 1, _record(unit_id="unit-a", content="alpha"))

    def _locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "reconcile_bootstrap_and_append_messages_batch", _locked)

    result = spool.replay_to_session_db(db, trigger="manual")

    assert result.state is spool.ReplayRunState.RETRY_PENDING
    assert result.retry_class is not None
    assert result.cooldown_seconds > 0


def test_sqlite_busy_unrelated_operational_error_propagates(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    _write_sealed_segment(home, 1, _record(unit_id="unit-a", content="alpha"))

    def _busy(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is busy")

    monkeypatch.setattr(db, "reconcile_bootstrap_and_append_messages_batch", _busy)
    busy_result = spool.replay_to_session_db(db, trigger="manual")
    assert busy_result.state is spool.ReplayRunState.RETRY_PENDING
    assert busy_result.retry_class is not None

    other_home = tmp_path / ".hermes-other"
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    _write_sealed_segment(other_home, 1, _record(unit_id="unit-b", content="beta"))

    def _other(*_args, **_kwargs):
        raise sqlite3.OperationalError("syntax error")

    monkeypatch.setattr(db, "reconcile_bootstrap_and_append_messages_batch", _other)
    with pytest.raises(sqlite3.OperationalError, match="syntax error"):
        spool.replay_to_session_db(db, trigger="manual")


def test_replay_to_session_db_replays_clean_segment_and_compacts_it(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    segment_path = _write_sealed_segment(home, 1, _record())

    result = spool.replay_to_session_db(db, trigger="startup")

    assert result.state is spool.ReplayRunState.REPLAYED
    assert result.frames_committed == 1
    assert result.frames_duplicated == 0
    assert [row["content"] for row in db.get_messages("replay-session")] == ["hello replay"]
    assert not segment_path.exists()


def test_sessiondb_startup_replay_runs_once_only_for_writable_canonical_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_state, "_SESSION_SPOOL_STARTUP_ONCE", set(), raising=False)
    calls = []

    def _fake_replay(session_db, *, trigger):
        calls.append((session_db.db_path, trigger))
        return spool.ReplayRunResult(state=spool.ReplayRunState.EMPTY, trigger=trigger)

    monkeypatch.setattr(spool, "replay_to_session_db", _fake_replay)

    canonical = SessionDB(db_path=home / "state.db")
    canonical.close()
    second = SessionDB(db_path=home / "state.db")
    second.close()
    other = SessionDB(db_path=home / "other.db")
    other.close()
    readonly = SessionDB(db_path=home / "state.db", read_only=True)
    readonly.close()

    assert calls == [(home / "state.db", "startup")]


def test_corrupt_active_public_replay_publishes_evidence_and_blocker(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    spool.append_records((_record(unit_id="corrupt-active-direct"),))

    active_path = home / spool.SPOOL_ROOT_NAME / spool.ACTIVE_SPOOL_NAME
    corrupted = bytearray(active_path.read_bytes())
    corrupted[0] = 0
    active_path.write_bytes(bytes(corrupted))

    result = spool.replay_to_session_db(db, trigger="startup")

    blockers = sorted((home / spool.SPOOL_ROOT_NAME / spool.SEALED_DIR_NAME / spool.BLOCKERS_DIR_NAME).glob("*.blocker.json"))
    quarantine_spools = sorted((home / spool.SPOOL_ROOT_NAME / spool.QUARANTINE_DIR_NAME).glob("*.spool"))
    quarantine_sidecars = sorted((home / spool.SPOOL_ROOT_NAME / spool.QUARANTINE_DIR_NAME).glob("*.json"))

    assert result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert result.first_blocked_segment == 1
    assert len(blockers) == 1
    assert len(quarantine_spools) == 1
    assert len(quarantine_sidecars) == 1
    assert active_path.read_bytes() == b""


def test_corrupt_active_startup_replay_publishes_evidence_and_keeps_messages_empty(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_state, "_SESSION_SPOOL_STARTUP_ONCE", set(), raising=False)

    spool.append_records((_record(unit_id="corrupt-active-startup"),))

    active_path = home / spool.SPOOL_ROOT_NAME / spool.ACTIVE_SPOOL_NAME
    corrupted = bytearray(active_path.read_bytes())
    corrupted[0] = 0
    active_path.write_bytes(bytes(corrupted))

    startup_db = SessionDB(db_path=home / "state.db")
    try:
        blockers = sorted((home / spool.SPOOL_ROOT_NAME / spool.SEALED_DIR_NAME / spool.BLOCKERS_DIR_NAME).glob("*.blocker.json"))
        quarantine_spools = sorted((home / spool.SPOOL_ROOT_NAME / spool.QUARANTINE_DIR_NAME).glob("*.spool"))
        quarantine_sidecars = sorted((home / spool.SPOOL_ROOT_NAME / spool.QUARANTINE_DIR_NAME).glob("*.json"))

        assert startup_db.get_messages("replay-session") == []
        assert len(blockers) == 1
        assert len(quarantine_spools) == 1
        assert len(quarantine_sidecars) == 1
        assert active_path.read_bytes() == b""
    finally:
        startup_db.close()


def _assert_metadata_only_replay_evidence(
    home,
    *,
    sequence: int,
    expected_source_kind: str,
    expected_tail_status: str,
    expected_valid_prefix_bytes: int,
    expected_original_size_bytes: int,
):
    quarantine = home / spool.SPOOL_ROOT_NAME / spool.QUARANTINE_DIR_NAME
    evidence_spools = sorted(
        quarantine.glob(f"seq-{sequence:020d}-{expected_tail_status}-vp{expected_valid_prefix_bytes}.spool")
    )
    evidence_sidecars = sorted(
        quarantine.glob(f"seq-{sequence:020d}-{expected_tail_status}-vp{expected_valid_prefix_bytes}.json")
    )
    assert len(evidence_spools) == 1
    assert len(evidence_sidecars) == 1

    payload = json.loads(evidence_sidecars[0].read_text(encoding="utf-8"))
    assert set(payload.keys()) == {
        "schema_version",
        "segment_sequence",
        "source_kind",
        "tail_status",
        "valid_prefix_bytes",
        "original_size_bytes",
        "evidence_spool_name",
    }
    assert payload == {
        "schema_version": 1,
        "segment_sequence": f"{sequence:020d}",
        "source_kind": expected_source_kind,
        "tail_status": expected_tail_status,
        "valid_prefix_bytes": expected_valid_prefix_bytes,
        "original_size_bytes": expected_original_size_bytes,
        "evidence_spool_name": evidence_spools[0].name,
    }
    return evidence_spools[0], evidence_sidecars[0]


def _write_blocker_backed_prefix_state(
    home,
    *,
    sequence: int,
    source_kind: str,
    prefix_bytes: bytes,
    original_bytes: bytes,
    tail_status: str,
):
    root = home / spool.SPOOL_ROOT_NAME
    sealed = root / spool.SEALED_DIR_NAME
    blockers = sealed / spool.BLOCKERS_DIR_NAME
    quarantine = root / spool.QUARANTINE_DIR_NAME
    sealed.mkdir(parents=True, exist_ok=True)
    blockers.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)

    prefix_name = f"{sequence:020d}.prefix.spool"
    (sealed / prefix_name).write_bytes(prefix_bytes)
    evidence_base = f"seq-{sequence:020d}-{tail_status}-vp{len(prefix_bytes)}"
    evidence_spool_name = f"{evidence_base}.spool"
    evidence_sidecar_name = f"{evidence_base}.json"
    (quarantine / evidence_spool_name).write_bytes(original_bytes)
    (quarantine / evidence_sidecar_name).write_bytes(
        spool._canonical_json_bytes(
            {
                "schema_version": 1,
                "segment_sequence": f"{sequence:020d}",
                "source_kind": source_kind,
                "tail_status": tail_status,
                "valid_prefix_bytes": len(prefix_bytes),
                "original_size_bytes": len(original_bytes),
                "evidence_spool_name": evidence_spool_name,
            }
        )
    )
    (blockers / f"{sequence:020d}.blocker.json").write_bytes(
        spool._canonical_json_bytes(
            {
                "schema_version": 1,
                "segment_sequence": f"{sequence:020d}",
                "source_kind": source_kind,
                "tail_status": tail_status,
                "valid_prefix_bytes": len(prefix_bytes),
                "acked_prefix_bytes": 0,
                "blocking_offset": len(prefix_bytes),
                "prefix_segment_name": prefix_name,
                "evidence_spool_name": evidence_spool_name,
                "evidence_sidecar_name": evidence_sidecar_name,
                "original_size_bytes": len(original_bytes),
            }
        )
    )
    return sealed / prefix_name


def _build_blocker_crash_state(home, case_name: str):
    first = _record(unit_id=f"{case_name}-a", content="alpha")
    second = _record(unit_id=f"{case_name}-b", content="beta")
    clean_frame = spool._frame_bytes_for_record(first)
    corrupt_frame = bytearray(spool._frame_bytes_for_record(second))
    corrupt_frame[-1] ^= 0x01
    prefix_path = _write_blocker_backed_prefix_state(
        home,
        sequence=1,
        source_kind="sealed",
        prefix_bytes=clean_frame,
        original_bytes=clean_frame + bytes(corrupt_frame),
        tail_status="checksum_mismatch",
    )
    _write_sealed_segment(home, 2, _record(unit_id=f"{case_name}-later", content="gamma"))
    root = home / spool.SPOOL_ROOT_NAME
    quarantine = root / spool.QUARANTINE_DIR_NAME
    evidence_spool = quarantine / f"seq-{1:020d}-checksum_mismatch-vp{len(clean_frame)}.spool"
    evidence_sidecar = quarantine / f"seq-{1:020d}-checksum_mismatch-vp{len(clean_frame)}.json"
    blocker_path = root / spool.SEALED_DIR_NAME / spool.BLOCKERS_DIR_NAME / f"{1:020d}.blocker.json"

    if case_name == "missing_evidence_sidecar":
        evidence_sidecar.unlink()
        expected_error_class = "missing_replay_evidence_sidecar"
    elif case_name == "missing_evidence_spool":
        evidence_spool.unlink()
        expected_error_class = "missing_replay_evidence_spool"
    elif case_name == "malformed_evidence_sidecar":
        evidence_sidecar.write_text('{"schema_version":1}\n', encoding="utf-8")
        expected_error_class = "invalid_replay_evidence_sidecar"
    elif case_name == "mismatched_evidence_relationship":
        payload = json.loads(evidence_sidecar.read_text(encoding="utf-8"))
        payload["valid_prefix_bytes"] = payload["valid_prefix_bytes"] + 1
        evidence_sidecar.write_bytes(spool._canonical_json_bytes(payload))
        expected_error_class = "invalid_blocker_relationship"
    elif case_name == "missing_prefix_required":
        prefix_path.unlink()
        expected_error_class = "invalid_blocker_relationship"
    elif case_name == "mismatched_prefix_required":
        prefix_path.write_bytes(clean_frame[:-1])
        expected_error_class = "invalid_blocker_relationship"
    else:
        raise AssertionError(f"unknown blocker crash-state case: {case_name}")

    return {
        "blocked_sequence": 1,
        "blocking_offset": len(clean_frame),
        "blocker_path": blocker_path,
        "evidence_spool": evidence_spool,
        "evidence_sidecar": evidence_sidecar,
        "expected_error_class": expected_error_class,
        "prefix_path": prefix_path,
    }


@pytest.mark.parametrize(
    ("case_name", "expected_error_class"),
    [
        ("missing_evidence_sidecar", "missing_replay_evidence_sidecar"),
        ("missing_evidence_spool", "missing_replay_evidence_spool"),
        ("malformed_evidence_sidecar", "invalid_replay_evidence_sidecar"),
        ("mismatched_evidence_relationship", "invalid_blocker_relationship"),
        ("missing_prefix_required", "invalid_blocker_relationship"),
        ("mismatched_prefix_required", "invalid_blocker_relationship"),
    ],
)
def test_blocker_crash_state_outcome_manual_replay_returns_blocked_integrity_and_fd_stable(
    db, tmp_path, monkeypatch, case_name, expected_error_class
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state = _build_blocker_crash_state(home, case_name)

    import psutil

    baseline_fds = psutil.Process().num_fds()

    first = spool.replay_to_session_db(db, trigger="manual")
    second = spool.replay_to_session_db(db, trigger="manual")

    assert first.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert first.first_blocked_segment == state["blocked_sequence"]
    assert first.first_blocked_offset == state["blocking_offset"]
    assert first.error_class == expected_error_class
    assert db.get_messages("replay-session") == []
    assert [row["content"] for row in db.get_messages("replay-session") if row["content"] == "gamma"] == []
    assert second.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert second.first_blocked_segment == state["blocked_sequence"]
    assert second.first_blocked_offset == state["blocking_offset"]
    assert second.error_class == expected_error_class
    assert state["blocker_path"].exists()
    assert psutil.Process().num_fds() == baseline_fds


@pytest.mark.parametrize(
    ("case_name", "expected_error_class"),
    [
        ("missing_evidence_sidecar", "missing_replay_evidence_sidecar"),
        ("missing_evidence_spool", "missing_replay_evidence_spool"),
        ("malformed_evidence_sidecar", "invalid_replay_evidence_sidecar"),
        ("mismatched_evidence_relationship", "invalid_blocker_relationship"),
        ("missing_prefix_required", "invalid_blocker_relationship"),
        ("mismatched_prefix_required", "invalid_blocker_relationship"),
    ],
)
def test_blocker_crash_state_outcome_startup_remains_available_and_fail_closed(
    tmp_path, monkeypatch, case_name, expected_error_class
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    state = _build_blocker_crash_state(home, case_name)
    monkeypatch.setattr(hermes_state, "_SESSION_SPOOL_STARTUP_ONCE", set(), raising=False)

    startup_db = SessionDB(db_path=home / "state.db")
    try:
        assert startup_db.get_messages("replay-session") == []
        assert state["blocker_path"].exists()
    finally:
        startup_db.close()

    replay_db = SessionDB(db_path=home / "state.db")
    try:
        replay_result = spool.replay_to_session_db(replay_db, trigger="startup")
        assert replay_result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
        assert replay_result.first_blocked_segment == state["blocked_sequence"]
        assert replay_result.first_blocked_offset == state["blocking_offset"]
        assert replay_result.error_class == expected_error_class
        assert replay_db.get_messages("replay-session") == []
        assert [row["content"] for row in replay_db.get_messages("replay-session") if row["content"] == "gamma"] == []
    finally:
        replay_db.close()


def test_blocker_backed_valid_prefix_active_manual_replays_prefix_once_then_blocks_restart_duplicate_safe(
    db, tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    first = _record(unit_id="unit-a", content="alpha")
    second = _record(unit_id="unit-b", content="beta")
    clean_frame = spool._frame_bytes_for_record(first)
    corrupt_frame = bytearray(spool._frame_bytes_for_record(second))
    corrupt_frame[-1] ^= 0x01
    expected_sequence = 1

    spool.append_records((first, second))
    active_path = home / spool.SPOOL_ROOT_NAME / spool.ACTIVE_SPOOL_NAME
    active_path.write_bytes(clean_frame + bytes(corrupt_frame))

    first_result = spool.replay_to_session_db(db, trigger="manual")

    blocker_path = (
        home
        / spool.SPOOL_ROOT_NAME
        / spool.SEALED_DIR_NAME
        / spool.BLOCKERS_DIR_NAME
        / f"{expected_sequence:020d}.blocker.json"
    )
    ack_path = (
        home
        / spool.SPOOL_ROOT_NAME
        / spool.SEALED_DIR_NAME
        / spool.ACKS_DIR_NAME
        / f"{expected_sequence:020d}.prefix.spool.ap{len(clean_frame):020d}.json"
    )

    assert first_result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert [row["content"] for row in db.get_messages("replay-session")] == ["alpha"]
    assert first_result.first_blocked_segment == expected_sequence
    assert first_result.first_blocked_offset == len(clean_frame)
    assert blocker_path.exists()
    assert ack_path.exists()
    evidence_spool, _evidence_sidecar = _assert_metadata_only_replay_evidence(
        home,
        sequence=expected_sequence,
        expected_source_kind="active",
        expected_tail_status="checksum_mismatch",
        expected_valid_prefix_bytes=len(clean_frame),
        expected_original_size_bytes=len(clean_frame) + len(corrupt_frame),
    )
    assert evidence_spool.read_bytes() == clean_frame + bytes(corrupt_frame)

    restarted = SessionDB(db_path=home / "state.db")
    try:
        second_result = spool.replay_to_session_db(restarted, trigger="manual")
        assert second_result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
        assert [row["content"] for row in restarted.get_messages("replay-session")] == [
            "alpha"
        ]
        assert second_result.first_blocked_segment == expected_sequence
        assert second_result.first_blocked_offset == len(clean_frame)
        assert restarted.get_messages("replay-session") == db.get_messages("replay-session")
        assert blocker_path.exists()
        assert ack_path.exists()
    finally:
        restarted.close()


def test_blocker_backed_valid_prefix_sealed_startup_replays_prefix_once_then_blocks_restart_duplicate_safe(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_state, "_SESSION_SPOOL_STARTUP_ONCE", set(), raising=False)

    first = _record(unit_id="unit-a", content="alpha")
    second = _record(unit_id="unit-b", content="beta")
    clean_frame = spool._frame_bytes_for_record(first)
    corrupt_frame = bytearray(spool._frame_bytes_for_record(second))
    corrupt_frame[-1] ^= 0x01
    _write_blocker_backed_prefix_state(
        home,
        sequence=1,
        source_kind="sealed",
        prefix_bytes=clean_frame,
        original_bytes=clean_frame + bytes(corrupt_frame),
        tail_status="checksum_mismatch",
    )
    _write_sealed_segment(home, 2, _record(unit_id="unit-c", content="gamma"))

    startup_db = SessionDB(db_path=home / "state.db")
    try:
        blocker_path = (
            home
            / spool.SPOOL_ROOT_NAME
            / spool.SEALED_DIR_NAME
            / spool.BLOCKERS_DIR_NAME
            / "00000000000000000001.blocker.json"
        )
        ack_path = (
            home
            / spool.SPOOL_ROOT_NAME
            / spool.SEALED_DIR_NAME
            / spool.ACKS_DIR_NAME
            / f"00000000000000000001.prefix.spool.ap{len(clean_frame):020d}.json"
        )
        assert [row["content"] for row in startup_db.get_messages("replay-session")] == [
            "alpha"
        ]
        assert [row["content"] for row in startup_db.get_messages("replay-session") if row["content"] == "gamma"] == []
        assert blocker_path.exists()
        assert ack_path.exists()
        evidence_spool, _evidence_sidecar = _assert_metadata_only_replay_evidence(
            home,
            sequence=1,
            expected_source_kind="sealed",
            expected_tail_status="checksum_mismatch",
            expected_valid_prefix_bytes=len(clean_frame),
            expected_original_size_bytes=len(clean_frame) + len(corrupt_frame),
        )
        assert evidence_spool.read_bytes() == clean_frame + bytes(corrupt_frame)
    finally:
        startup_db.close()

    monkeypatch.setattr(hermes_state, "_SESSION_SPOOL_STARTUP_ONCE", set(), raising=False)
    restarted = SessionDB(db_path=home / "state.db")
    try:
        replay_result = spool.replay_to_session_db(restarted, trigger="startup")
        assert replay_result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
        assert replay_result.first_blocked_segment == 1
        assert replay_result.first_blocked_offset == len(clean_frame)
        assert [row["content"] for row in restarted.get_messages("replay-session")] == [
            "alpha"
        ]
    finally:
        restarted.close()


def test_blocker_backed_valid_prefix_zero_prefix_active_remains_blocked_without_messages(
    db, tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    spool.append_records((_record(unit_id="zero-prefix-active"),))
    active_path = home / spool.SPOOL_ROOT_NAME / spool.ACTIVE_SPOOL_NAME
    corrupted = bytearray(active_path.read_bytes())
    corrupted[0] = 0
    active_path.write_bytes(bytes(corrupted))

    result = spool.replay_to_session_db(db, trigger="manual")

    assert result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert db.get_messages("replay-session") == []


def test_blocker_backed_valid_prefix_zero_prefix_sealed_remains_blocked_without_messages(
    db, tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / spool.SPOOL_ROOT_NAME
    sealed = root / spool.SEALED_DIR_NAME
    sealed.mkdir(parents=True, exist_ok=True)

    corrupt_frame = bytearray(spool._frame_bytes_for_record(_record(unit_id="unit-bad", content="beta")))
    corrupt_frame[0] = 0
    (sealed / "00000000000000000001.spool").write_bytes(bytes(corrupt_frame))
    (sealed / "00000000000000000002.spool").write_bytes(
        spool._frame_bytes_for_record(_record(unit_id="unit-c", content="gamma"))
    )

    result = spool.replay_to_session_db(db, trigger="manual")

    assert result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert db.get_messages("replay-session") == []
    assert [row["content"] for row in db.get_messages("replay-session") if row["content"] == "gamma"] == []


def test_replay_stops_at_blocker_and_does_not_advance_later_sequences_after_restart(
    db, tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / spool.SPOOL_ROOT_NAME
    sealed = root / spool.SEALED_DIR_NAME
    blockers = sealed / spool.BLOCKERS_DIR_NAME
    quarantine = root / spool.QUARANTINE_DIR_NAME
    sealed.mkdir(parents=True, exist_ok=True)
    blockers.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)

    clean_frame = spool._frame_bytes_for_record(_record(unit_id="unit-a", content="alpha"))
    corrupt_frame = bytearray(
        spool._frame_bytes_for_record(_record(unit_id="unit-b", content="beta"))
    )
    corrupt_frame[-1] ^= 0x01
    (sealed / "00000000000000000001.spool").write_bytes(clean_frame + bytes(corrupt_frame))
    (sealed / "00000000000000000002.spool").write_bytes(
        spool._frame_bytes_for_record(_record(unit_id="unit-c", content="gamma"))
    )

    first = spool.replay_to_session_db(db, trigger="startup")

    assert first.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert [row["content"] for row in db.get_messages("replay-session")] == ["alpha"]

    restarted = SessionDB(db_path=home / "state.db")
    try:
        second = spool.replay_to_session_db(restarted, trigger="startup")
        assert second.state is spool.ReplayRunState.BLOCKED_INTEGRITY
        assert [row["content"] for row in restarted.get_messages("replay-session")] == [
            "alpha"
        ]
        assert restarted.get_messages("replay-session") == db.get_messages("replay-session")
    finally:
        restarted.close()


def test_corrupt_sealed_segment_replays_only_prefix_then_blocks_fifo(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / spool.SPOOL_ROOT_NAME
    sealed = root / spool.SEALED_DIR_NAME
    sealed.mkdir(parents=True, exist_ok=True)

    clean_frame = spool._frame_bytes_for_record(_record(unit_id="unit-a", content="alpha"))
    corrupt_frame = bytearray(
        spool._frame_bytes_for_record(_record(unit_id="unit-b", content="beta"))
    )
    corrupt_frame[-1] ^= 0x01
    (sealed / "00000000000000000001.spool").write_bytes(clean_frame + bytes(corrupt_frame))
    (sealed / "00000000000000000002.spool").write_bytes(
        spool._frame_bytes_for_record(_record(unit_id="unit-c", content="gamma"))
    )

    result = spool.replay_to_session_db(db, trigger="startup")

    assert result.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert result.first_blocked_segment == 1
    assert [row["content"] for row in db.get_messages("replay-session")] == ["alpha"]
    assert [row["content"] for row in db.get_messages("replay-session") if row["content"] == "gamma"] == []


def _write_ack_sidecar(home, segment_name: str, acked_prefix_bytes: int, valid_prefix_bytes: int, *, sequence: int = 1):
    acks = home / spool.SPOOL_ROOT_NAME / spool.SEALED_DIR_NAME / spool.ACKS_DIR_NAME
    acks.mkdir(parents=True, exist_ok=True)
    ack_name = f"{segment_name}.ap{acked_prefix_bytes:020d}.json"
    payload = {
        "schema_version": 1,
        "segment_sequence": f"{sequence:020d}",
        "segment_name": segment_name,
        "segment_kind": "prefix" if segment_name.endswith(".prefix.spool") else "clean",
        "segment_size_bytes": valid_prefix_bytes,
        "acked_prefix_bytes": acked_prefix_bytes,
        "valid_prefix_bytes": valid_prefix_bytes,
        "tail_status": "clean",
        "last_frame_offset": 0,
        "last_frame_length": acked_prefix_bytes,
        "last_frame_checksum_hex": "1" * 32,
    }
    (acks / ack_name).write_bytes(spool._canonical_json_bytes(payload))
    return acks / ack_name


def test_full_ack_tombstone_suppresses_replay_but_partial_ack_tombstone_blocks(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / spool.SPOOL_ROOT_NAME
    sealed = root / spool.SEALED_DIR_NAME
    sealed.mkdir(parents=True, exist_ok=True)

    full = _write_ack_sidecar(home, "00000000000000000001.spool", 5, 5, sequence=1)
    empty_result = spool.replay_to_session_db(db, trigger="startup")

    assert empty_result.state is spool.ReplayRunState.EMPTY
    assert db.get_messages("replay-session") == []

    full.unlink()
    _write_ack_sidecar(home, "00000000000000000001.spool", 3, 5, sequence=1)
    blocked = spool.replay_to_session_db(db, trigger="startup")

    assert blocked.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert blocked.first_blocked_segment == 1


def test_blocker_held_full_ack_tombstone_keeps_fifo_stopped_after_restart(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / spool.SPOOL_ROOT_NAME
    sealed = root / spool.SEALED_DIR_NAME
    blockers = sealed / spool.BLOCKERS_DIR_NAME
    sealed.mkdir(parents=True, exist_ok=True)
    blockers.mkdir(parents=True, exist_ok=True)
    _write_ack_sidecar(home, "00000000000000000001.prefix.spool", 5, 5, sequence=1)
    (blockers / "00000000000000000001.blocker.json").write_bytes(
        spool._canonical_json_bytes(
            {
                "schema_version": 1,
                "segment_sequence": "00000000000000000001",
                "source_kind": "sealed",
                "tail_status": "checksum_mismatch",
                "valid_prefix_bytes": 5,
                "acked_prefix_bytes": 5,
                "blocking_offset": 5,
                "prefix_segment_name": "00000000000000000001.prefix.spool",
                "evidence_spool_name": "seq-00000000000000000001-checksum_mismatch-vp5.spool",
                "evidence_sidecar_name": "seq-00000000000000000001-checksum_mismatch-vp5.json",
                "original_size_bytes": 10,
            }
        )
    )
    (sealed / "00000000000000000002.spool").write_bytes(
        spool._frame_bytes_for_record(_record(unit_id="unit-c", content="gamma"))
    )

    first = spool.replay_to_session_db(db, trigger="startup")
    assert first.state is spool.ReplayRunState.BLOCKED_INTEGRITY
    assert db.get_messages("replay-session") == []

    restarted = SessionDB(db_path=home / "state.db")
    try:
        second = spool.replay_to_session_db(restarted, trigger="startup")
        assert second.state is spool.ReplayRunState.BLOCKED_INTEGRITY
        assert restarted.get_messages("replay-session") == []
    finally:
        restarted.close()


def test_replay_respects_startup_pre_persist_and_manual_budgets(tmp_path, monkeypatch):
    def _run(trigger: str):
        home = tmp_path / trigger
        home.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        db = SessionDB(db_path=home / "state.db")
        try:
            for idx in range(20):
                _write_sealed_segment(
                    home,
                    idx + 1,
                    _record(unit_id=f"unit-{idx}", content=f"msg-{idx}"),
                )
            result = spool.replay_to_session_db(db, trigger=trigger)
            return db, result
        except Exception:
            db.close()
            raise

    startup_db, startup = _run("startup")
    try:
        assert startup.state is spool.ReplayRunState.REPLAYED
        assert startup.frames_committed == 20
    finally:
        startup_db.close()

    pre_db, pre = _run("pre_persist")
    try:
        assert pre.state is spool.ReplayRunState.PARTIALLY_REPLAYED
        assert pre.frames_committed == 16
        assert pre.pending_bytes_after > 0
        assert len(pre_db.get_messages("replay-session")) == 16
    finally:
        pre_db.close()

    manual_db, manual = _run("manual")
    try:
        assert manual.state is spool.ReplayRunState.REPLAYED
        assert manual.frames_committed == 20
    finally:
        manual_db.close()


def test_retryable_ack_cleanup_returns_retry_pending_with_ack_pending_true(
    tmp_path, monkeypatch
):
    home = tmp_path / "ack-cleanup-retry"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    try:
        _write_sealed_segment(home, 1, _record(unit_id="unit-a", content="alpha"))
        _write_ack_sidecar(home, "00000000000000000001.spool", 3, 5, sequence=1)

        def _busy(**_kwargs):
            raise OSError(16, "busy")

        monkeypatch.setattr(spool, "_cleanup_stale_lower_ack_sidecars", _busy)

        result = spool.replay_to_session_db(db, trigger="manual")

        assert result.state is spool.ReplayRunState.RETRY_PENDING
        assert result.ack_pending is True
        assert result.frames_committed == 1
        assert result.frames_acked == 0
        assert result.retry_class == "ack_cleanup_busy"
        assert [row["content"] for row in db.get_messages("replay-session")] == ["alpha"]
    finally:
        db.close()


def test_retry_cooldown_skips_early_trigger_and_allows_later_takeover(
    tmp_path, monkeypatch
):
    home = tmp_path / "retry-cooldown"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    db = SessionDB(db_path=home / "state.db")
    try:
        _write_sealed_segment(home, 1, _record(unit_id="unit-a", content="alpha"))
        original = spool._publish_ack_sidecar_strict
        calls = {"count": 0}
        clock = {"now": 100.0}

        monkeypatch.setattr(spool.time, "monotonic", lambda: clock["now"])

        def _flaky(runtime, *, segment_sequence, segment_path, ack_payload):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError(16, "busy")
            return original(
                runtime,
                segment_sequence=segment_sequence,
                segment_path=segment_path,
                ack_payload=ack_payload,
            )

        monkeypatch.setattr(spool, "_publish_ack_sidecar_strict", _flaky)

        first = spool.replay_to_session_db(db, trigger="manual")
        assert first.state is spool.ReplayRunState.RETRY_PENDING
        assert first.ack_pending is True
        assert calls["count"] == 1

        second = spool.replay_to_session_db(db, trigger="manual")
        assert second.state is spool.ReplayRunState.RETRY_PENDING
        assert second.cooldown_seconds > 0
        assert calls["count"] == 1

        clock["now"] += second.cooldown_seconds + 0.01
        third = spool.replay_to_session_db(db, trigger="manual")
        assert third.state is spool.ReplayRunState.REPLAYED
        assert calls["count"] == 2
    finally:
        db.close()


def test_replay_to_session_db_seals_clean_active_spool_before_replaying(db, tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    root = home / spool.SPOOL_ROOT_NAME
    root.mkdir(parents=True, exist_ok=True)
    active_path = root / spool.ACTIVE_SPOOL_NAME
    active_path.write_bytes(spool._frame_bytes_for_record(_record()))

    result = spool.replay_to_session_db(db, trigger="startup")

    assert result.state is spool.ReplayRunState.REPLAYED
    assert result.frames_committed == 1
    assert [row["content"] for row in db.get_messages("replay-session")] == ["hello replay"]
    sealed_entries = sorted((root / spool.SEALED_DIR_NAME).glob("*.spool"))
    assert sealed_entries == []
    assert active_path.exists()
    assert active_path.read_bytes() == b""