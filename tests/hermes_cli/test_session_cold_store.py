"""Contract tests for the first Store-only cold archive slice."""

import errno
import json
from datetime import UTC, datetime
import os
from pathlib import Path
import signal
import sqlite3
import stat
import threading

import pytest

import hermes_cli.session_cold_store as cold_store
from hermes_accounting_locks import PendingSessionAccountingError
from hermes_cli.session_cold_store import (
    plan_archived_lineage,
    purge_archived_lineage,
    store_archived_lineage,
    validate_purge_archived_lineage,
)
from hermes_state import SessionDB


def test_store_archived_compression_lineage_writes_one_terminal_id_snapshot(
    tmp_path: Path,
) -> None:
    """Store writes one current terminal-ID snapshot without deleting DB rows."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("root", source="cli")
        db.append_message("root", role="user", content="first")
        db.end_session("root", "compression")
        db.create_session("terminal", source="cli", parent_session_id="root")
        db.append_message("terminal", role="assistant", content="second")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", archive_root)

        assert result.terminal_id == "terminal"
        assert result.physical_ids == ("root", "terminal")
        terminal = db.get_session("terminal")
        assert terminal is not None
        started = datetime.fromtimestamp(float(terminal["started_at"]), UTC)
        expected_snapshot_parent = (
            archive_root
            / "sessions"
            / "started"
            / f"{started:%Y}"
            / f"{started:%m}"
            / f"{started:%d}"
        )
        assert result.snapshot_dir.parent == expected_snapshot_parent
        assert {path.name for path in result.snapshot_dir.iterdir()} == {
            "artifacts",
            "metadata.json",
            "session.jsonl",
        }
        assert (result.snapshot_dir / "artifacts").is_dir()
        assert (result.snapshot_dir / "metadata.json").is_file()
        assert (result.snapshot_dir / "session.jsonl").is_file()
        assert store_archived_lineage(db, "terminal", archive_root) == result
        assert db.get_session("root") is not None
        assert db.get_session("terminal") is not None
    finally:
        db.close()


@pytest.mark.parametrize("snapshot_state", ["missing", "mismatched"])
def test_purge_rejects_missing_or_mismatched_snapshot_and_retains_source_rows(
    tmp_path: Path,
    snapshot_state: str,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="keep me")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        if snapshot_state == "missing":
            archive_root = tmp_path / "missing-archive"
        else:
            metadata_path = stored.snapshot_dir / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["source_fingerprint"] = "0" * 64
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        with pytest.raises(ValueError, match="snapshot"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "keep me"
    finally:
        db.close()


def test_purge_finishes_filesystem_reads_before_final_write_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SessionDB(tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="stored")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)

        final_write_started = False
        external_reads: list[str] = []
        original_execute_write = db._execute_write
        original_message_limit = cold_store.resolved_max_export_messages
        original_source_store_key = cold_store._source_store_key
        original_verify_snapshot = cold_store._verify_plan_snapshot
        original_legacy_routes = cold_store._reject_legacy_routing_references

        def tracked_execute_write(callback):
            nonlocal final_write_started
            final_write_started = True
            return original_execute_write(callback)

        def tracked_message_limit():
            assert not final_write_started, "config read ran inside final write"
            external_reads.append("config")
            return original_message_limit()

        def tracked_source_store_key(*args, **kwargs):
            assert not final_write_started, "source-store stat ran inside final write"
            external_reads.append("source-store")
            return original_source_store_key(*args, **kwargs)

        def tracked_verify_snapshot(*args, **kwargs):
            assert not final_write_started, "snapshot verification ran inside final write"
            external_reads.append("snapshot")
            return original_verify_snapshot(*args, **kwargs)

        def tracked_legacy_routes(*args, **kwargs):
            assert not final_write_started, "legacy-route read ran inside final write"
            external_reads.append("legacy-routes")
            return original_legacy_routes(*args, **kwargs)

        monkeypatch.setattr(db, "_execute_write", tracked_execute_write)
        monkeypatch.setattr(
            cold_store, "resolved_max_export_messages", tracked_message_limit
        )
        monkeypatch.setattr(cold_store, "_source_store_key", tracked_source_store_key)
        monkeypatch.setattr(cold_store, "_verify_plan_snapshot", tracked_verify_snapshot)
        monkeypatch.setattr(
            cold_store, "_reject_legacy_routing_references", tracked_legacy_routes
        )

        result = purge_archived_lineage(db, "terminal", archive_root)

        assert external_reads == [
            "config",
            "source-store",
            "legacy-routes",
            "snapshot",
        ]
        assert result.snapshot_dir == stored.snapshot_dir
        assert db.get_session("terminal") is None
    finally:
        db.close()


def test_purge_flushes_pending_token_accounting_before_final_recheck(
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="stored")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)

        # Deterministically model a queued delta that the background writer has
        # not started applying yet. A post-delete apply would recreate the row
        # as source='unknown' and the snapshot would miss these counters.
        with db._token_queue_cond:
            assert db._token_writer_thread is None
            db._token_queue.append(
                (
                    "terminal",
                    {
                        "input_tokens": 7,
                        "model": "late-model",
                        "billing_provider": "late-provider",
                        "api_call_count": 1,
                    },
                )
            )

        with pytest.raises(ValueError, match="fingerprint"):
            purge_archived_lineage(db, "terminal", archive_root)

        row = db.get_session("terminal")
        assert row is not None
        assert row["source"] == "cli"
        assert row["input_tokens"] == 7
    finally:
        db.close()


def test_purge_tombstone_blocks_cross_instance_session_resurrection(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_root = tmp_path / "archive"
    purge_db = SessionDB(db_path)
    late_writer = SessionDB(db_path)
    try:
        purge_db.create_session("root", source="cli")
        purge_db.append_message("root", role="user", content="first")
        purge_db.end_session("root", "compression")
        purge_db.create_session(
            "terminal", source="cli", parent_session_id="root"
        )
        purge_db.append_message("terminal", role="user", content="stored")
        purge_db.end_session("terminal", "completed")
        assert purge_db.set_session_archived("terminal", True)
        store_archived_lineage(purge_db, "terminal", archive_root)

        purged = purge_archived_lineage(purge_db, "terminal", archive_root)
        assert purge_db.get_session("root") is None
        assert purge_db.get_session("terminal") is None
        assert late_writer._conn is not None
        tombstones = late_writer._conn.execute(
            "SELECT session_id, terminal_id, source_fingerprint "
            "FROM cold_archive_tombstones ORDER BY session_id"
        ).fetchall()
        assert [tuple(row) for row in tombstones] == [
            ("root", "terminal", purged.source_fingerprint),
            ("terminal", "terminal", purged.source_fingerprint),
        ]

        with pytest.raises(sqlite3.IntegrityError, match="cold-archived"):
            late_writer.update_token_counts(
                "terminal",
                input_tokens=7,
                model="late-model",
                billing_provider="late-provider",
                api_call_count=1,
            )
        assert late_writer.get_session("terminal") is None

        def _direct_insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO sessions (id, source, started_at) VALUES (?, ?, ?)",
                ("root", "gateway", 1.0),
            )

        with pytest.raises(sqlite3.IntegrityError, match="cold-archived"):
            late_writer._execute_write(_direct_insert)
        assert late_writer.get_session("root") is None
    finally:
        late_writer.close()
        purge_db.close()


def test_purge_tombstone_blocks_cross_instance_gateway_routes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_root = tmp_path / "archive"
    purge_db = SessionDB(db_path)
    late_writer = SessionDB(db_path)
    survivor_entry = json.dumps({"session_id": "survivor"})
    try:
        purge_db.create_session("terminal", source="cli")
        purge_db.append_message("terminal", role="user", content="stored")
        purge_db.end_session("terminal", "completed")
        assert purge_db.set_session_archived("terminal", True)
        late_writer.save_gateway_routing_entry(
            "existing-key", survivor_entry, scope="test"
        )
        store_archived_lineage(purge_db, "terminal", archive_root)
        purge_archived_lineage(purge_db, "terminal", archive_root)

        tombstoned_entry = json.dumps({"session_id": "terminal"})
        with pytest.raises(sqlite3.IntegrityError, match="cold-archived"):
            late_writer.save_gateway_routing_entry(
                "new-key", tombstoned_entry, scope="test"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cold-archived"):
            late_writer.save_gateway_routing_entry(
                "existing-key", tombstoned_entry, scope="test"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cold-archived"):
            late_writer.replace_gateway_routing_entries(
                {"replacement-key": tombstoned_entry}, scope="test"
            )

        assert late_writer.load_gateway_routing_entries(scope="test") == {
            "existing-key": survivor_entry
        }
    finally:
        late_writer.close()
        purge_db.close()


def test_lineage_accounting_lock_rejects_late_queue_and_releases_after_flush(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_db = SessionDB(db_path)
    accounting_db = SessionDB(db_path)
    try:
        archive_db.create_session("terminal", source="cli")
        archive_db.end_session("terminal", "completed")
        assert archive_db.set_session_archived("terminal", True)

        with cold_store._exclusive_lineage_accounting_locks(
            archive_db, "terminal"
        ):
            with pytest.raises(
                PendingSessionAccountingError, match="locked for cold archive"
            ):
                accounting_db.queue_token_counts(
                    "terminal", input_tokens=1, model="late-model"
                )
            assert not accounting_db._token_queue

        accounting_db.queue_token_counts(
            "terminal", input_tokens=2, model="settled-model"
        )
        assert accounting_db.flush_token_counts(timeout=10)
        with cold_store._exclusive_lineage_accounting_locks(
            archive_db, "terminal"
        ):
            pass
    finally:
        accounting_db.close()
        archive_db.close()


def test_failed_accounting_lock_survives_later_success_until_owner_close(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_db = SessionDB(db_path)
    accounting_db = SessionDB(db_path)
    accounting_closed = False
    try:
        archive_db.create_session("terminal", source="cli")
        archive_db.end_session("terminal", "completed")
        assert archive_db.set_session_archived("terminal", True)
        original_update = accounting_db.update_token_counts
        attempts = 0

        def fail_once(session_id: str, **kwargs) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("forced accounting failure")
            original_update(session_id, **kwargs)

        accounting_db.update_token_counts = fail_once  # type: ignore[method-assign]
        accounting_db.queue_token_counts(
            "terminal", input_tokens=1, model="failed-model"
        )
        assert not accounting_db.flush_token_counts(timeout=10)
        accounting_db.queue_token_counts(
            "terminal", input_tokens=2, model="successful-model"
        )
        assert not accounting_db.flush_token_counts(timeout=10)

        with pytest.raises(PendingSessionAccountingError, match="pending token"):
            with cold_store._exclusive_lineage_accounting_locks(
                archive_db, "terminal"
            ):
                pass

        accounting_db.close()
        accounting_closed = True
        with cold_store._exclusive_lineage_accounting_locks(
            archive_db, "terminal"
        ):
            pass
    finally:
        if not accounting_closed:
            accounting_db.close()
        archive_db.close()


def test_timed_out_stopped_writer_releases_retained_accounting_lock_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "state.db"
    archive_db = SessionDB(db_path)
    accounting_db = SessionDB(db_path)
    entered_apply = threading.Event()
    release_apply = threading.Event()
    try:
        archive_db.create_session("terminal", source="cli")
        archive_db.end_session("terminal", "completed")
        assert archive_db.set_session_archived("terminal", True)

        def blocked_failed_apply(_batch):
            entered_apply.set()
            if not release_apply.wait(10):
                raise AssertionError("test did not release blocked writer")
            return {"terminal"}

        monkeypatch.setattr(accounting_db, "_apply_token_batch", blocked_failed_apply)
        accounting_db.queue_token_counts(
            "terminal", input_tokens=1, model="failed-model"
        )
        assert entered_apply.wait(5)
        writer = accounting_db._token_writer_thread
        assert writer is not None

        accounting_db._stop_token_writer(join_timeout=0.01)
        with pytest.raises(PendingSessionAccountingError, match="pending token"):
            with cold_store._exclusive_lineage_accounting_locks(
                archive_db, "terminal"
            ):
                pass

        release_apply.set()
        writer.join(timeout=10)
        assert not writer.is_alive()
        with cold_store._exclusive_lineage_accounting_locks(
            archive_db, "terminal"
        ):
            pass
    finally:
        release_apply.set()
        accounting_db.close()
        archive_db.close()


def test_purge_rolls_back_tombstone_when_source_delete_fails(
    tmp_path: Path,
) -> None:
    db = SessionDB(tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="stored")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        assert db._conn is not None
        db._conn.executescript(
            """
            CREATE TRIGGER force_cold_purge_delete_failure
            BEFORE DELETE ON sessions
            WHEN OLD.id = 'terminal'
            BEGIN
                SELECT RAISE(ABORT, 'forced cold purge delete failure');
            END;
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="forced cold purge"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "stored"
        assert db._conn.execute(
            "SELECT 1 FROM cold_archive_tombstones WHERE session_id = ?",
            ("terminal",),
        ).fetchone() is None
    finally:
        db.close()


def test_purge_rejects_source_mutation_after_store_and_retains_source_rows(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="stored")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        db.append_message("terminal", role="assistant", content="changed after Store")

        with pytest.raises(ValueError, match="fingerprint"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert [message["content"] for message in db.get_messages("terminal")] == [
            "stored",
            "changed after Store",
        ]
    finally:
        db.close()


def test_purge_rechecks_source_after_pretransaction_snapshot_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="stored")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        original_execute_write = db._execute_write

        def mutate_before_final_transaction(callback):
            with sqlite3.connect(db.db_path) as mutator:
                mutator.execute(
                    "INSERT INTO messages (session_id, role, content, timestamp) "
                    "VALUES (?, ?, ?, ?)",
                    ("terminal", "assistant", "late source change", 1.0),
                )
            return original_execute_write(callback)

        monkeypatch.setattr(
            db, "_execute_write", mutate_before_final_transaction
        )

        with pytest.raises(
            ValueError, match="source lineage changed after snapshot verification"
        ):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert [message["content"] for message in db.get_messages("terminal")] == [
            "stored",
            "late source change",
        ]
        assert db._conn is not None
        assert db._conn.execute(
            "SELECT 1 FROM cold_archive_tombstones WHERE session_id = ?",
            ("terminal",),
        ).fetchone() is None
    finally:
        db.close()


def test_purge_rechecks_pin_inside_delete_transaction(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="stored")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        assert db.set_session_pinned("terminal", True)

        with pytest.raises(ValueError, match="pinned"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "stored"
    finally:
        db.close()


def test_purge_rejects_uncovered_child_and_preserves_foreign_key_rows(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session("terminal", source="cli", parent_session_id="root")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        db.create_session(
            "branch",
            source="cli",
            parent_session_id="root",
            model_config={"_branched_from": "root"},
        )

        with pytest.raises(ValueError, match="uncovered child"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("root") is not None
        assert db.get_session("terminal") is not None
        branch = db.get_session("branch")
        assert branch is not None
        assert branch["parent_session_id"] == "root"
    finally:
        db.close()


@pytest.mark.parametrize("delegate_parent_id", [None, "different-parent"])
def test_purge_rejects_uncovered_delegate_marker_and_retains_source_rows(
    tmp_path: Path,
    delegate_parent_id: str | None,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("source", source="cli")
        db.append_message("source", role="user", content="keep source")
        db.end_session("source", "completed")
        assert db.set_session_archived("source", True)
        store_archived_lineage(db, "source", archive_root)
        if delegate_parent_id is not None:
            db.create_session(delegate_parent_id, source="cli")
        db.create_session(
            "delegate",
            source="delegate",
            parent_session_id=delegate_parent_id,
            model_config={"_delegate_from": "source"},
        )
        db.append_message("delegate", role="assistant", content="keep delegate")

        with pytest.raises(ValueError, match="uncovered child"):
            purge_archived_lineage(db, "source", archive_root)

        assert db.get_session("source") is not None
        assert db.get_messages("source")[0]["content"] == "keep source"
        delegate = db.get_session("delegate")
        assert delegate is not None
        assert delegate["parent_session_id"] == delegate_parent_id
        assert db.get_messages("delegate")[0]["content"] == "keep delegate"
    finally:
        db.close()


@pytest.mark.parametrize(
    "entry_json",
    ["{malformed", "[]", '{"session_id": 123}'],
)
def test_purge_rejects_unverifiable_routing_entries_and_retains_all_rows(
    tmp_path: Path,
    entry_json: str,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="keep me")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        db.save_gateway_routing_entry(
            "route-key", entry_json, scope="test-routing-scope"
        )

        with pytest.raises(ValueError, match="gateway_routing.*cannot be verified"):
            purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "keep me"
        assert db.load_gateway_routing_entries(scope="test-routing-scope") == {
            "route-key": entry_json
        }
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["preflight", "purge"])
def test_purge_accepts_absent_pre_upgrade_delegation_reference_column(
    tmp_path: Path,
    operation: str,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="keep me")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        assert db._conn is not None
        db._conn.execute(
            "ALTER TABLE async_delegations DROP COLUMN origin_session_id"
        )
        db._conn.commit()
        before_meta = db._conn.execute(
            "SELECT key, value FROM state_meta ORDER BY key"
        ).fetchall()
        before_delegations = db._conn.execute(
            "SELECT * FROM async_delegations ORDER BY delegation_id"
        ).fetchall()

        if operation == "preflight":
            result = validate_purge_archived_lineage(db, "terminal", archive_root)
            assert db.get_session("terminal") is not None
            assert db.get_messages("terminal")[0]["content"] == "keep me"
        else:
            result = purge_archived_lineage(db, "terminal", archive_root)
            assert db.get_session("terminal") is None
            assert db.get_messages("terminal") == []

        assert result.terminal_id == "terminal"
        assert db._conn.execute(
            "SELECT key, value FROM state_meta ORDER BY key"
        ).fetchall() == before_meta
        assert db._conn.execute(
            "SELECT * FROM async_delegations ORDER BY delegation_id"
        ).fetchall() == before_delegations
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["preflight", "purge"])
def test_purge_fails_closed_when_required_delegation_reference_column_is_absent(
    tmp_path: Path,
    operation: str,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="keep me")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        store_archived_lineage(db, "terminal", archive_root)
        assert db._conn is not None
        db._conn.execute(
            "ALTER TABLE async_delegations DROP COLUMN parent_session_id"
        )
        db._conn.commit()
        before_meta = db._conn.execute(
            "SELECT key, value FROM state_meta ORDER BY key"
        ).fetchall()
        before_delegations = db._conn.execute(
            "SELECT * FROM async_delegations ORDER BY delegation_id"
        ).fetchall()

        with pytest.raises(
            ValueError,
            match=r"schema is missing async_delegations\.parent_session_id",
        ):
            if operation == "preflight":
                validate_purge_archived_lineage(db, "terminal", archive_root)
            else:
                purge_archived_lineage(db, "terminal", archive_root)

        assert db.get_session("terminal") is not None
        assert db.get_messages("terminal")[0]["content"] == "keep me"
        assert db._conn.execute(
            "SELECT key, value FROM state_meta ORDER BY key"
        ).fetchall() == before_meta
        assert db._conn.execute(
            "SELECT * FROM async_delegations ORDER BY delegation_id"
        ).fetchall() == before_delegations
    finally:
        db.close()


def test_verify_rejects_live_source_change_after_store(tmp_path: Path) -> None:
    """Verify compares the current read-only Store plan with the snapshot."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="original")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        db.append_message("terminal", role="assistant", content="changed after Store")
        before_changes = db._conn.total_changes if db._conn is not None else -1

        with pytest.raises(ValueError, match="fingerprint"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert db._conn is not None
        assert db._conn.total_changes == before_changes
        assert stored.snapshot_dir.is_dir()
        assert "changed after Store" not in (
            stored.snapshot_dir / "session.jsonl"
        ).read_text(encoding="utf-8")
    finally:
        db.close()


def test_verify_missing_snapshot_does_not_create_archive_root(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "missing-archive"
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        before_changes = db._conn.total_changes if db._conn is not None else -1

        with pytest.raises(ValueError, match="snapshot not found"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert not archive_root.exists()
        assert db._conn is not None
        assert db._conn.total_changes == before_changes
    finally:
        db.close()


def test_verify_rejects_corrupt_snapshot_without_replacing_it(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        metadata = stored.snapshot_dir / "metadata.json"
        metadata.write_text("{corrupt\n", encoding="utf-8")
        before_files = sorted(
            path.relative_to(archive_root) for path in archive_root.rglob("*")
        )

        with pytest.raises(ValueError, match="snapshot is corrupt"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert metadata.read_text(encoding="utf-8") == "{corrupt\n"
        assert sorted(
            path.relative_to(archive_root) for path in archive_root.rglob("*")
        ) == before_files
    finally:
        db.close()


def test_verify_rejects_symlinked_snapshot_payload(tmp_path: Path) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    outside = tmp_path / "outside-metadata.json"
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        metadata = stored.snapshot_dir / "metadata.json"
        original = metadata.read_text(encoding="utf-8")
        outside.write_text(original, encoding="utf-8")
        metadata.unlink()
        metadata.symlink_to(outside)

        with pytest.raises(ValueError, match="snapshot is corrupt"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert metadata.is_symlink()
        assert outside.read_text(encoding="utf-8") == original
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["store", "preflight"])
def test_store_rejects_blob_message_content_before_writing(
    tmp_path: Path,
    operation: str,
) -> None:
    """Archive v1 fails closed on SQLite BLOBs without changing its source."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        message_id = db.append_message("terminal", role="user", content="placeholder")
        assert db._conn is not None
        db._conn.execute(
            "UPDATE messages SET content = ? WHERE id = ?",
            (b"future-blob", message_id),
        )
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        with pytest.raises(
            ValueError,
            match=r"cold store v1 does not support SQLite BLOB/bytes values.*message\.content",
        ):
            if operation == "store":
                store_archived_lineage(db, "terminal", archive_root)
            else:
                plan_archived_lineage(db, "terminal")

        assert not archive_root.exists()
        assert db._conn is not None
        session = db._conn.execute(
            "SELECT archived FROM sessions WHERE id = ?", ("terminal",)
        ).fetchone()
        message = db._conn.execute(
            "SELECT content FROM messages WHERE session_id = ?", ("terminal",)
        ).fetchone()
        assert session is not None and session["archived"] == 1
        assert message is not None and message["content"] == b"future-blob"
    finally:
        db.close()


def test_store_reports_the_same_canonical_path_it_writes(tmp_path: Path) -> None:
    """A lexical ROOT alias cannot make the reported snapshot path misleading."""
    db = SessionDB(db_path=tmp_path / "state.db")
    canonical_root = tmp_path / "archive"
    root_spelling = canonical_root / "not-created" / ".."
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", root_spelling)

        assert result.snapshot_dir.is_dir()
        assert result.snapshot_dir.is_relative_to(canonical_root)
        assert "not-created" not in result.snapshot_dir.parts
    finally:
        db.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
def test_store_rejects_rename_unsafe_archive_parent_before_creating_root(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    shared_parent = tmp_path / "shared"
    archive_root = shared_parent / "archive"
    shared_parent.mkdir(mode=0o770)
    shared_parent.chmod(0o770)
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        with pytest.raises(ValueError, match="unsafe archive parent path"):
            store_archived_lineage(db, "terminal", archive_root)

        assert not archive_root.exists()
    finally:
        db.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership is required")
def test_root_lock_rejects_foreign_owned_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_root = tmp_path / "archive"
    lock_path = cold_store._cold_archive_lock_path(archive_root)
    lock_path.write_text("", encoding="utf-8")
    lock_path.chmod(0o600)
    real_fstat = os.fstat

    def foreign_regular_owner(fd: int) -> os.stat_result:
        opened = real_fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            return opened
        values = list(opened)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", foreign_regular_owner)
    with pytest.raises(ValueError, match="unsafe cold archive lock sidecar"):
        with cold_store._exclusive_cold_archive_root_lock(archive_root):
            pass


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership is required")
def test_directory_authority_rejects_foreign_owned_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "foreign-owned"
    directory.mkdir(mode=0o700)
    descriptor = os.open(directory, cold_store._directory_open_flags())
    real_fstat = os.fstat

    def foreign_owner_fstat(fd: int) -> os.stat_result:
        opened = real_fstat(fd)
        if fd != descriptor:
            return opened
        values = list(opened)
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "fstat", foreign_owner_fstat)
    try:
        with pytest.raises(ValueError, match="unsafe archive parent path"):
            cold_store._validate_directory_authority(descriptor, directory)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
@pytest.mark.parametrize(
    ("member", "insecure_mode"),
    [
        pytest.param(".", 0o750, id="snapshot-directory"),
        pytest.param("artifacts", 0o750, id="artifacts-directory"),
        pytest.param("metadata.json", 0o640, id="metadata-file"),
        pytest.param("session.jsonl", 0o640, id="payload-file"),
    ],
)
def test_verify_rejects_insecure_snapshot_permissions_and_store_repairs_them(
    tmp_path: Path,
    member: str,
    insecure_mode: int,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="private")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        target = stored.snapshot_dir if member == "." else stored.snapshot_dir / member
        target.chmod(insecure_mode)

        with pytest.raises(ValueError, match="snapshot"):
            cold_store.verify_archived_lineage(db, "terminal", archive_root)
        assert db.get_session("terminal") is not None

        repaired = store_archived_lineage(db, "terminal", archive_root)
        assert repaired.snapshot_dir == stored.snapshot_dir
        repaired_target = (
            repaired.snapshot_dir if member == "." else repaired.snapshot_dir / member
        )
        assert stat.S_IMODE(repaired_target.stat().st_mode) & 0o077 == 0
        cold_store.verify_archived_lineage(db, "terminal", archive_root)
    finally:
        db.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are required")
def test_store_creates_archive_hierarchy_directories_with_private_modes(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    original_umask = os.umask(0)
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", archive_root)

        hierarchy = [archive_root]
        relative_parent = result.snapshot_dir.parent.relative_to(archive_root)
        for part in relative_parent.parts:
            hierarchy.append(hierarchy[-1] / part)
        assert all(path.stat().st_mode & 0o777 == 0o700 for path in hierarchy)
    finally:
        os.umask(original_umask)
        db.close()


def test_store_keeps_distinct_ids_that_only_differ_by_trailing_space(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    started_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    try:
        for session_id, marker in (("foo", "plain"), ("foo ", "trailing-space")):
            db.create_session(session_id, source="cli")
            db.append_message(session_id, role="user", content=marker)
            db.end_session(session_id, "completed")
            assert db.set_session_archived(session_id, True)
            assert db._conn is not None
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (started_at, session_id),
            )

        plain = store_archived_lineage(db, "foo", archive_root)
        trailing_space = store_archived_lineage(db, "foo ", archive_root)

        assert plain.snapshot_dir != trailing_space.snapshot_dir
        assert plain.snapshot_dir.is_dir()
        assert trailing_space.snapshot_dir.is_dir()
        assert '"content":"plain"' in (
            plain.snapshot_dir / "session.jsonl"
        ).read_text(encoding="utf-8")
        assert '"content":"trailing-space"' in (
            trailing_space.snapshot_dir / "session.jsonl"
        ).read_text(encoding="utf-8")
    finally:
        db.close()


def test_safe_component_reserves_the_hash_suffix_namespace() -> None:
    """A generated component cannot collide with an already-safe literal ID."""
    generated = cold_store._safe_component("foo ")

    assert cold_store._safe_component(generated) != generated


def test_store_rejects_a_compression_ancestor_when_a_tip_exists(tmp_path: Path) -> None:
    """An archive snapshot must be keyed to the complete chain terminal, never a prefix."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session("terminal", source="cli", parent_session_id="root")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        with pytest.raises(ValueError, match="terminal"):
            store_archived_lineage(db, "root", tmp_path / "archive")
    finally:
        db.close()


def test_store_keeps_a_compression_child_with_an_inherited_old_delegate_marker(tmp_path: Path) -> None:
    """Inherited markers are not forks unless they name the direct parent."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("root", source="cli")
        db.end_session("root", "compression")
        db.create_session(
            "terminal",
            source="cli",
            parent_session_id="root",
            model_config={"_delegate_from": "older-delegate-parent"},
        )
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", tmp_path / "archive")

        assert result.physical_ids == ("root", "terminal")
    finally:
        db.close()


def test_store_and_purge_isolate_a_direct_reset_from_its_compression_parent(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("compression-parent", source="cli")
        db.end_session("compression-parent", "compression")
        db.create_session(
            "reset-child",
            source="cli",
            parent_session_id="compression-parent",
            model_config={"_reset_from": "compression-parent"},
        )
        db.end_session("reset-child", "completed")
        assert db.set_session_archived("reset-child", True)

        stored = store_archived_lineage(db, "reset-child", archive_root)
        purged = purge_archived_lineage(db, "reset-child", archive_root)

        assert stored.physical_ids == ("reset-child",)
        assert purged.physical_ids == ("reset-child",)
        assert db.get_session("reset-child") is None
        assert db.get_session("compression-parent") is not None
    finally:
        db.close()


def test_store_keeps_a_compression_child_with_an_inherited_old_reset_marker(
    tmp_path: Path,
) -> None:
    """An inherited reset marker names an older parent, not a new reset."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("reset-root", source="cli")
        db.end_session("reset-root", "compression")
        db.create_session(
            "terminal",
            source="cli",
            parent_session_id="reset-root",
            model_config={"_reset_from": "pre-reset-parent"},
        )
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        result = store_archived_lineage(db, "terminal", tmp_path / "archive")

        assert result.physical_ids == ("reset-root", "terminal")
    finally:
        db.close()


def test_store_reads_raw_rows_without_flushing_and_preserves_system_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Store neither flushes pending accounting nor drops normalized prompt rows."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli", system_prompt="be helpful")
        db.append_message("terminal", role="user", content="hello")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        def unexpected_flush() -> None:
            raise AssertionError("Store must not flush accounting")

        monkeypatch.setattr(db, "flush_token_counts", unexpected_flush)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")

        records = [
            json.loads(line)
            for line in (result.snapshot_dir / "session.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
        ]
        assert any(record["kind"] == "system-prompt" for record in records)
    finally:
        db.close()


def test_store_replaces_current_snapshot_when_archived_source_changes(
    tmp_path: Path,
) -> None:
    """A changed archived source replaces the snapshot and leaves source rows intact."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="original")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        first = store_archived_lineage(db, "terminal", archive_root)
        session_before = (first.snapshot_dir / "session.jsonl").read_bytes()

        db.append_message("terminal", role="assistant", content="changed after Store")
        changed_messages = db.get_messages("terminal")

        replacement = store_archived_lineage(db, "terminal", archive_root)

        assert db.get_messages("terminal") == changed_messages
        assert replacement.snapshot_dir == first.snapshot_dir
        assert replacement.source_fingerprint != first.source_fingerprint
        replacement_payload = (replacement.snapshot_dir / "session.jsonl").read_bytes()
        assert replacement_payload != session_before
        assert b"changed after Store" in replacement_payload
        assert {path.name for path in replacement.snapshot_dir.iterdir()} == {
            "artifacts",
            "metadata.json",
            "session.jsonl",
        }
    finally:
        db.close()


def test_store_restores_current_snapshot_when_replacement_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="original")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        first = store_archived_lineage(db, "terminal", archive_root)
        original_payload = (first.snapshot_dir / "session.jsonl").read_bytes()

        db.append_message("terminal", role="assistant", content="changed")
        changed_messages = db.get_messages("terminal")
        real_rename = os.rename

        def fail_staging_publication(
            src: str,
            dst: str,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            if src.startswith(".staging-") and dst == first.snapshot_dir.name:
                raise OSError(errno.EIO, "injected snapshot publication failure")
            real_rename(
                src,
                dst,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )

        monkeypatch.setattr(os, "rename", fail_staging_publication)

        with pytest.raises(OSError, match="injected snapshot publication failure"):
            store_archived_lineage(db, "terminal", archive_root)

        assert first.snapshot_dir.is_dir()
        assert (first.snapshot_dir / "session.jsonl").read_bytes() == original_payload
        assert db.get_messages("terminal") == changed_messages
        leftovers = {
            path.name
            for path in first.snapshot_dir.parent.iterdir()
            if path.name.startswith((".stale-", ".staging-"))
        }
        assert leftovers == set()
    finally:
        db.close()


def test_store_retry_requires_successful_parent_fsync_after_publication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="durable payload")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        real_rename = os.rename
        real_fsync = os.fsync
        published = False
        fail_publication_fsync = True
        retrying = False
        retry_fsyncs = 0

        def track_publication_rename(
            src: str,
            dst: str,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            nonlocal published
            real_rename(
                src,
                dst,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            if src.startswith(".staging-"):
                published = True

        def fail_once_after_publication(descriptor: int) -> None:
            nonlocal fail_publication_fsync, retry_fsyncs
            if published and fail_publication_fsync:
                fail_publication_fsync = False
                raise OSError(errno.EIO, "injected snapshot parent fsync failure")
            if retrying:
                retry_fsyncs += 1
            real_fsync(descriptor)

        monkeypatch.setattr(os, "rename", track_publication_rename)
        monkeypatch.setattr(os, "fsync", fail_once_after_publication)

        with pytest.raises(OSError, match="injected snapshot parent fsync failure"):
            store_archived_lineage(db, "terminal", archive_root)

        assert published
        visible_snapshot = next(archive_root.rglob("metadata.json")).parent
        assert visible_snapshot.is_dir()

        retrying = True
        result = store_archived_lineage(db, "terminal", archive_root)

        assert result.snapshot_dir == visible_snapshot
        assert retry_fsyncs == 1
    finally:
        db.close()


def test_store_same_fingerprint_is_idempotent_without_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="unchanged")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        first = store_archived_lineage(db, "terminal", archive_root)

        def unexpected_staging(_snapshot_parent_fd: int) -> tuple[str, int]:
            raise AssertionError("same fingerprint must not create a staged replacement")

        monkeypatch.setattr(cold_store, "_create_staging_directory", unexpected_staging)

        assert store_archived_lineage(db, "terminal", archive_root) == first
        assert first.snapshot_dir.is_dir()
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["store", "verify"])
def test_existing_snapshot_rejects_replaced_open_parent_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """A valid snapshot is not accepted after its opened parent is displaced."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        stored = store_archived_lineage(db, "terminal", archive_root)
        snapshot_parent = stored.snapshot_dir.parent
        displaced_parent = snapshot_parent.with_name("day-displaced")
        real_validate = cold_store._valid_existing_snapshot_at
        swapped = False

        def validate_then_replace_parent(*args, **kwargs):
            nonlocal swapped
            valid = real_validate(*args, **kwargs)
            if valid is True and not swapped:
                snapshot_parent.rename(displaced_parent)
                snapshot_parent.mkdir()
                swapped = True
            return valid

        monkeypatch.setattr(
            cold_store,
            "_valid_existing_snapshot_at",
            validate_then_replace_parent,
        )

        with pytest.raises(ValueError, match="unsafe archive parent path"):
            if operation == "store":
                store_archived_lineage(db, "terminal", archive_root)
            else:
                cold_store.verify_archived_lineage(db, "terminal", archive_root)

        assert swapped
        assert not stored.snapshot_dir.exists()
        assert (displaced_parent / stored.snapshot_dir.name).is_dir()
    finally:
        db.close()


def test_store_does_not_use_old_snapshot_after_replacement(tmp_path: Path) -> None:
    """Only the replacement payload remains at the logical session's current path."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    started_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    first_ended_at = datetime(2026, 2, 3, 4, 5, tzinfo=UTC).timestamp()
    second_ended_at = datetime(2026, 3, 4, 5, 6, tzinfo=UTC).timestamp()
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="old payload marker")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        conn = db._conn
        assert conn is not None
        conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (started_at, first_ended_at, "terminal"),
        )

        first = store_archived_lineage(db, "terminal", archive_root)
        conn.execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ?",
            (second_ended_at, "terminal"),
        )
        db.append_message("terminal", role="assistant", content="new payload marker")

        replacement = store_archived_lineage(db, "terminal", archive_root)

        assert replacement.snapshot_dir == first.snapshot_dir
        assert replacement.snapshot_dir.parent == (
            archive_root / "sessions" / "started" / "2026" / "01" / "02"
        )
        current_metadata = json.loads(
            (replacement.snapshot_dir / "metadata.json").read_text(encoding="utf-8")
        )
        assert current_metadata["source_fingerprint"] == replacement.source_fingerprint
        assert current_metadata["source_fingerprint"] != first.source_fingerprint
        payloads = list(archive_root.rglob("session.jsonl"))
        assert payloads == [replacement.snapshot_dir / "session.jsonl"]
        assert "new payload marker" in payloads[0].read_text(encoding="utf-8")
    finally:
        db.close()


def test_store_keeps_purged_snapshot_when_same_id_is_reused_in_new_generation(
    tmp_path: Path,
) -> None:
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    first_started_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    second_started_at = datetime(2026, 1, 2, 3, 5, tzinfo=UTC).timestamp()
    try:
        db.create_session("reused", source="cli")
        db.append_message("reused", role="user", content="first generation")
        db.end_session("reused", "completed")
        assert db.set_session_archived("reused", True)
        assert db._conn is not None
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (first_started_at, "reused"),
        )
        db._conn.commit()
        first = store_archived_lineage(db, "reused", archive_root)
        purge_archived_lineage(db, "reused", archive_root)

        # Cold Purge deliberately fences this ID from implicit resurrection.
        # Simulate a future explicit restore/new-generation authorization; v1
        # exposes no public untombstone operation.
        untombstoned = db._conn.execute(
            "DELETE FROM cold_archive_tombstones WHERE session_id = ?",
            ("reused",),
        )
        assert untombstoned.rowcount == 1
        db._conn.commit()

        db.create_session("reused", source="cli")
        db.append_message("reused", role="user", content="second generation")
        db.end_session("reused", "completed")
        assert db.set_session_archived("reused", True)
        db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (second_started_at, "reused"),
        )
        db._conn.commit()
        second = store_archived_lineage(db, "reused", archive_root)

        assert first.snapshot_dir != second.snapshot_dir
        assert "first generation" in (first.snapshot_dir / "session.jsonl").read_text(
            encoding="utf-8"
        )
        assert "second generation" in (second.snapshot_dir / "session.jsonl").read_text(
            encoding="utf-8"
        )
    finally:
        db.close()


def test_store_isolates_same_id_and_generation_from_distinct_source_stores(
    tmp_path: Path,
) -> None:
    first_db = SessionDB(db_path=tmp_path / "first" / "state.db")
    second_db = SessionDB(db_path=tmp_path / "second" / "state.db")
    archive_root = tmp_path / "archive"
    started_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    try:
        for db, content in (
            (first_db, "first source store"),
            (second_db, "second source store"),
        ):
            db.create_session("same-id", source="cli")
            db.append_message("same-id", role="user", content=content)
            db.end_session("same-id", "completed")
            assert db.set_session_archived("same-id", True)
            assert db._conn is not None
            db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (started_at, "same-id"),
            )
            db._conn.commit()

        first = store_archived_lineage(first_db, "same-id", archive_root)
        second = store_archived_lineage(second_db, "same-id", archive_root)

        assert first.snapshot_dir != second.snapshot_dir
        assert "first source store" in (first.snapshot_dir / "session.jsonl").read_text(
            encoding="utf-8"
        )
        assert "second source store" in (second.snapshot_dir / "session.jsonl").read_text(
            encoding="utf-8"
        )
    finally:
        first_db.close()
        second_db.close()


def test_store_keeps_purged_snapshot_when_source_db_is_replaced_at_same_path(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    archive_root = tmp_path / "archive"
    started_at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC).timestamp()
    first_db = SessionDB(db_path=db_path)
    second_db: SessionDB | None = None
    try:
        first_db.create_session("same-id", source="cli")
        first_db.append_message("same-id", role="user", content="first database")
        first_db.end_session("same-id", "completed")
        assert first_db.set_session_archived("same-id", True)
        assert first_db._conn is not None
        first_db._conn.execute(
            "UPDATE sessions SET started_at = ? WHERE id = ?",
            (started_at, "same-id"),
        )
        first_db._conn.commit()
        first = store_archived_lineage(first_db, "same-id", archive_root)
        purge_archived_lineage(first_db, "same-id", archive_root)
        first_db.close()
        first_file_identity = (os.stat(db_path).st_dev, os.stat(db_path).st_ino)

        replacement_path = tmp_path / "replacement.db"
        replacement_db = SessionDB(db_path=replacement_path)
        try:
            replacement_db.create_session("same-id", source="cli")
            replacement_db.append_message(
                "same-id", role="user", content="replacement database"
            )
            replacement_db.end_session("same-id", "completed")
            assert replacement_db.set_session_archived("same-id", True)
            assert replacement_db._conn is not None
            replacement_db._conn.execute(
                "UPDATE sessions SET started_at = ? WHERE id = ?",
                (started_at, "same-id"),
            )
            replacement_db._conn.commit()
        finally:
            replacement_db.close()

        for suffix in ("-wal", "-shm"):
            (db_path.parent / f"{db_path.name}{suffix}").unlink(missing_ok=True)
        os.replace(replacement_path, db_path)
        assert (os.stat(db_path).st_dev, os.stat(db_path).st_ino) != first_file_identity

        second_db = SessionDB(db_path=db_path)
        second = store_archived_lineage(second_db, "same-id", archive_root)

        assert first.snapshot_dir != second.snapshot_dir
        assert "first database" in (first.snapshot_dir / "session.jsonl").read_text(
            encoding="utf-8"
        )
        assert "replacement database" in (
            second.snapshot_dir / "session.jsonl"
        ).read_text(encoding="utf-8")
    finally:
        if second_db is not None:
            second_db.close()
        first_db.close()


@pytest.mark.skipif(
    not all(
        hasattr(module, name)
        for module, name in ((os, "mkfifo"), (signal, "setitimer"), (signal, "SIGALRM"))
    ),
    reason="requires POSIX FIFOs and interval timers",
)
@pytest.mark.parametrize("payload_name", ["metadata.json", "session.jsonl"])
def test_store_replaces_fifo_payload_without_blocking(
    tmp_path: Path,
    payload_name: str,
) -> None:
    """A damaged current payload is safely replaced without blocking on a FIFO."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")
        payload = result.snapshot_dir / payload_name
        payload.unlink()
        os.mkfifo(payload)

        def fail_if_blocked(*_args: object) -> None:
            raise AssertionError("cold-store validation blocked while opening a FIFO")

        sigalrm = getattr(signal, "SIGALRM", None)
        assert sigalrm is not None
        previous_handler = signal.signal(sigalrm, fail_if_blocked)
        signal.setitimer(signal.ITIMER_REAL, 2.0)
        try:
            replacement = store_archived_lineage(db, "terminal", tmp_path / "archive")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(sigalrm, previous_handler)

        assert (replacement.snapshot_dir / payload_name).is_file()
    finally:
        db.close()


def test_store_replaces_non_object_snapshot_metadata(tmp_path: Path) -> None:
    """A malformed current metadata envelope is replaced by a valid snapshot."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")
        (result.snapshot_dir / "metadata.json").write_text("[]\n", encoding="utf-8")

        replacement = store_archived_lineage(db, "terminal", tmp_path / "archive")

        metadata = json.loads(
            (replacement.snapshot_dir / "metadata.json").read_text(encoding="utf-8")
        )
        assert metadata["source_fingerprint"] == replacement.source_fingerprint
    finally:
        db.close()


@pytest.mark.parametrize("payload_name", ["metadata.json", "session.jsonl"])
def test_store_replaces_snapshot_with_invalid_utf8(
    tmp_path: Path,
    payload_name: str,
) -> None:
    """Invalid UTF-8 is damaged snapshot state, just like malformed JSON."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("terminal", source="cli")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)
        result = store_archived_lineage(db, "terminal", tmp_path / "archive")
        (result.snapshot_dir / payload_name).write_bytes(b"\xff")

        replacement = store_archived_lineage(db, "terminal", tmp_path / "archive")

        assert replacement == result
        (replacement.snapshot_dir / payload_name).read_text(encoding="utf-8")
    finally:
        db.close()


def test_store_rejects_snapshot_parent_symlink_swap_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validated snapshot parent cannot be swapped to redirect transcript writes."""
    db = SessionDB(db_path=tmp_path / "state.db")
    archive_root = tmp_path / "archive"
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        db.create_session("terminal", source="cli")
        db.append_message("terminal", role="user", content="sensitive transcript")
        db.end_session("terminal", "completed")
        assert db.set_session_archived("terminal", True)

        real_mkdir = cold_store.os.mkdir
        swapped = False

        def racing_mkdir(
            path: str | Path,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal swapped
            if not swapped and Path(path).name.startswith(".staging-"):
                snapshot_parent = next(archive_root.glob("sessions/started/*/*/*"))
                snapshot_parent.rename(snapshot_parent.with_name("day-displaced"))
                snapshot_parent.symlink_to(outside, target_is_directory=True)
                swapped = True
            real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(cold_store.os, "mkdir", racing_mkdir)

        with pytest.raises(ValueError, match="unsafe archive parent path"):
            store_archived_lineage(db, "terminal", archive_root)

        assert swapped
        assert not any(outside.iterdir())
    finally:
        db.close()
