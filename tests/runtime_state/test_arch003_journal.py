"""Focused ARCH-003 journal, counter, custody, and generation contracts."""

from pathlib import Path

from runtime_state import (
    RuntimeStateDB,
    WRITE_ABORT,
    cas_claim_owner,
    cas_update_columns,
    create_session_state,
    verify_entity,
)
from runtime_state.journal import JournalWriteError
from runtime_state.key_custody import AuthJsonKeyCustody
from runtime_state.locking import LockTimeout, MaintenanceLock


def _open(tmp_path: Path, *, epoch: int = 0):
    return RuntimeStateDB(tmp_path / "state.db", writer_epoch=epoch)


def test_successful_mutations_and_noop_are_journaled_once(tmp_path):
    with _open(tmp_path) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
        assert cas_claim_owner(db.connection, "session_state", "p", "s", "owner", 0).success
        first = cas_update_columns(db.connection, "session_state", "p", "s", "owner", 1, {"status": "active"})
        retry = cas_update_columns(db.connection, "session_state", "p", "s", "owner", 1, {"status": "active"})
        assert first.success and retry.success
        rows = db.connection.execute(
            "SELECT entity_seq, materialized_write_counter_before, materialized_write_counter_after "
            "FROM runtime_state_journal ORDER BY entity_seq"
        ).fetchall()
        assert rows == [(1, 0, 1), (2, 1, 2)]
    assert verify_entity(tmp_path / "state.db", "p", "session_state", "s").code == "OK"


def test_direct_materialized_write_is_unknown_counter_gap(tmp_path):
    with _open(tmp_path) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
        assert cas_claim_owner(db.connection, "session_state", "p", "s", "owner", 0).success
        db.connection.execute(
            "UPDATE session_state SET status='degraded' WHERE profile_name='p' AND session_id='s'"
        )
        db.connection.commit()
    result = verify_entity(tmp_path / "state.db", "p", "session_state", "s")
    assert result.status == "UNKNOWN"
    assert result.code == "WRITE_COUNTER_GAP"


def test_missing_key_fails_closed_for_verify_and_mutation(tmp_path):
    path = tmp_path / "state.db"
    auth = tmp_path / "auth.json"
    with RuntimeStateDB(path) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
        auth.unlink()
        auth.with_name("auth.json.lock").unlink()
        result = cas_claim_owner(db.connection, "session_state", "p", "s", "owner", 0)
        assert not result.success and result.error == "KEY_UNAVAILABLE"
    assert verify_entity(path, "p", "session_state", "s").code == "KEY_UNAVAILABLE"


def test_downgrade_epoch_blocks_writes_and_verification(tmp_path):
    path = tmp_path / "state.db"
    with RuntimeStateDB(path, writer_epoch=5) as db:
        assert create_session_state(db.connection, "p", "s", user_id="u", workspace="w").success
    with RuntimeStateDB(path, writer_epoch=0) as db:
        result = cas_claim_owner(db.connection, "session_state", "p", "s", "owner", 0)
        assert not result.success and result.error == WRITE_ABORT
    assert verify_entity(path, "p", "session_state", "s").code == "GENERATION_MISMATCH"


def test_maintenance_lock_has_bounded_timeout_and_kernel_release(tmp_path):
    path = tmp_path / "maintenance.lock"
    first = MaintenanceLock(path, exclusive=True, timeout=0.1)
    first.acquire()
    try:
        second = MaintenanceLock(path, exclusive=True, timeout=0.1)
        try:
            second.acquire()
        except LockTimeout:
            pass
        else:
            raise AssertionError("second maintenance writer unexpectedly acquired lock")
    finally:
        first.release()
    with MaintenanceLock(path, exclusive=True, timeout=0.1):
        pass
