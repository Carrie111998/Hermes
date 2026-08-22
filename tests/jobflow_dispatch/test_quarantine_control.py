"""Cross-process dispatch exclusion for the JobFlow quarantine fence."""

from __future__ import annotations

import threading

import pytest


def _store(tmp_path):
    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    return QuarantineControlStore(tmp_path / "dispatch.db", poll_interval=0.005)


def test_default_control_path_is_independent_of_activation_ledger(monkeypatch, tmp_path):
    import jobflow_dispatch.quarantine_control as control
    import jobflow_dispatch.store as activation

    monkeypatch.setattr(activation, "default_ledger_path", lambda: tmp_path / "jobflow_dispatch.db")
    monkeypatch.setattr(control, "_canonical_hermes_root", lambda: tmp_path)

    assert control.default_control_path() == tmp_path / "telemetry" / "jobflow_quarantine_fence.db"
    assert control.default_control_path() != activation.default_ledger_path()
    assert control.default_control_store() is control.default_control_store()


def test_default_control_store_refuses_database_replacement(monkeypatch, tmp_path):
    import jobflow_dispatch.quarantine_control as control

    monkeypatch.setattr(control, "_canonical_hermes_root", lambda: tmp_path)
    first = control.default_control_store()
    first._database_identity = ("replaced",)

    with pytest.raises(RuntimeError, match="control database identity changed"):
        control.default_control_store()


def test_store_refuses_database_disappearance_without_recreating_it(
    monkeypatch, tmp_path
):
    import jobflow_dispatch.quarantine_control as control

    store = _store(tmp_path)
    original_identity = store._database_identity

    def missing_identity(_path):
        raise FileNotFoundError(store.db_path)

    monkeypatch.setattr(control.QuarantineControlStore, "_path_identity", staticmethod(missing_identity))

    with pytest.raises(RuntimeError, match="control database disappeared"):
        store.fence_state()

    assert store._database_identity == original_identity


def test_default_control_store_does_not_heal_semantic_corruption(monkeypatch, tmp_path):
    import sqlite3

    import jobflow_dispatch.quarantine_control as control

    monkeypatch.setattr(control, "_canonical_hermes_root", lambda: tmp_path)
    first = control.default_control_store()
    with sqlite3.connect(first.db_path) as conn:
        conn.execute("DELETE FROM quarantine_dispatch_fence WHERE singleton=1")

    with pytest.raises(RuntimeError, match="fence row is missing"):
        control.default_control_store()

    with sqlite3.connect(first.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM quarantine_dispatch_fence"
        ).fetchone()[0] == 0


def test_concurrent_first_open_never_observes_partially_initialized_lock_file(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    db = tmp_path / "dispatch.db"
    stores = [QuarantineControlStore(db) for _ in range(8)]

    def actor(index):
        with stores[index % len(stores)].dispatch_section(boundary=f"actor-{index}"):
            return index

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sorted(pool.map(actor, range(64))) == list(range(64))


def test_independent_dispatch_sections_share_admission_concurrently(tmp_path):
    store = _store(tmp_path)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()

    def actor(entered):
        with store.dispatch_section(boundary="actor"):
            entered.set()
            assert release.wait(2)

    first = threading.Thread(target=actor, args=(first_entered,))
    second = threading.Thread(target=actor, args=(second_entered,))
    first.start()
    assert first_entered.wait(1)
    second.start()
    assert second_entered.wait(1), "acting boundaries were globally serialized"
    release.set()
    first.join(2)
    second.join(2)


def test_barrier_waits_for_claim_through_wake_section(tmp_path):
    store = _store(tmp_path)
    actor_entered = threading.Event()
    actor_release = threading.Event()
    barrier_entered = threading.Event()

    def actor():
        with store.dispatch_section(boundary="jobflow-dispatcher"):
            actor_entered.set()
            assert actor_release.wait(2)

    def barrier():
        with store.acquire_dispatch_barrier(reason="incident"):
            barrier_entered.set()

    actor_thread = threading.Thread(target=actor)
    barrier_thread = threading.Thread(target=barrier)
    actor_thread.start()
    assert actor_entered.wait(1)
    barrier_thread.start()

    assert not barrier_entered.wait(0.1), "barrier entered before the acting section drained"
    actor_release.set()
    actor_thread.join(2)
    barrier_thread.join(2)
    assert barrier_entered.is_set()


def test_barrier_blocks_new_due_capture_until_release(tmp_path):
    store = _store(tmp_path)
    attempted = threading.Event()
    entered = threading.Event()

    def actor():
        attempted.set()
        with store.dispatch_section(boundary="cron-tick"):
            entered.set()

    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        worker = threading.Thread(target=actor)
        worker.start()
        assert attempted.wait(1)
        assert not entered.wait(0.1)
        proof = barrier.assert_held()
        assert proof["coverage"] == "due_row_capture_through_submission"
        assert proof["complete"] is True

    worker.join(2)
    assert entered.is_set()


def test_activated_fence_survives_barrier_release_and_refuses_dispatch(tmp_path):
    store = _store(tmp_path)

    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        transition = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )
        token = transition["post"]["fence_token"]

    proof = store.verify_fence(token)
    assert proof["fenced"] is True
    assert proof["fence_token"] == token
    with pytest.raises(RuntimeError, match="dispatch fenced"):
        with store.dispatch_section(boundary="external-provider"):
            pass


def test_actor_waiting_during_activation_refuses_after_fence_is_committed(tmp_path):
    store = _store(tmp_path)
    attempted = threading.Event()
    entered = threading.Event()
    refused = threading.Event()

    def actor():
        attempted.set()
        try:
            with store.dispatch_section(boundary="cron-tick"):
                entered.set()
        except RuntimeError as exc:
            if "dispatch fenced" in str(exc):
                refused.set()

    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        worker = threading.Thread(target=actor)
        worker.start()
        assert attempted.wait(1)
        transition = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )

    worker.join(2)
    assert transition["post"]["fenced"] is True
    assert refused.is_set()
    assert not entered.is_set()


def test_stale_token_cannot_verify_or_release_current_fence(tmp_path):
    store = _store(tmp_path)
    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        first = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )["post"]["fence_token"]
        store.release_fence(
            barrier_token=barrier.token,
            expected_fence_token=first,
        )
        second = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-2",
            required=True,
        )["post"]["fence_token"]

        with pytest.raises(RuntimeError, match="fence token"):
            store.release_fence(
                barrier_token=barrier.token,
                expected_fence_token=first,
            )

    with pytest.raises(RuntimeError, match="fence token"):
        store.verify_fence(first)
    assert store.verify_fence(second)["fence_token"] == second


def test_fence_transition_drains_exact_durable_wake_set_under_barrier(tmp_path):
    store = _store(tmp_path)
    assert store.request_wake("job-1", caller="dispatcher", reason="mailbox") is True
    assert store.request_wake("job-2", caller="dispatcher", reason="mailbox") is True

    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        transition = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )

    assert [row["job_id"] for row in transition["pre"]["wakes"]] == ["job-1", "job-2"]
    assert transition["post"]["wakes"] == []
    assert store.pending_wakes() == ()


def test_control_store_never_claims_activation_ledger_completeness(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """CREATE TABLE activations (
                 message_key TEXT NOT NULL, activity_id TEXT NOT NULL,
                 correlation_id TEXT, state TEXT NOT NULL, claimed_at REAL,
                 completed_at REAL, outcome TEXT,
                 PRIMARY KEY (message_key, activity_id)
               )"""
        )
        conn.execute(
            "INSERT INTO activations VALUES (?, ?, ?, 'claimed', ?, NULL, NULL)",
            ("message-1", "cron.jobflow.matcher", "correlation-1", 1000.0),
        )

    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        transition = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )

    assert "durable_claims" not in transition["pre"]
    assert "durable_claims" not in transition["post"]
    proof = store.verify_fence(transition["post"]["fence_token"])
    assert "durable_claims" not in proof
    assert proof["claims"] == []


def test_active_fence_refuses_direct_wake_drain_and_clear(tmp_path):
    store = _store(tmp_path)
    store.request_wake("job-1", caller="dispatcher")
    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        transition = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )

    with pytest.raises(RuntimeError, match="dispatch fenced"):
        store.drain_wakes()
    with pytest.raises(RuntimeError, match="dispatch fenced"):
        store.clear_wakes()
    assert store.verify_fence(transition["post"]["fence_token"])["fenced"] is True


def test_capacity_exhaustion_is_distinct_from_duplicate_collapse(tmp_path, monkeypatch):
    import jobflow_dispatch.quarantine_control as control

    store = _store(tmp_path)
    monkeypatch.setattr(control, "MAX_PENDING_WAKES", 1)
    assert store.request_wake("job-1", caller="dispatcher") is True
    assert store.request_wake("job-1", caller="dispatcher") is False
    with pytest.raises(control.WakeQueueFullError, match="capacity"):
        store.request_wake("job-2", caller="dispatcher")


def test_fence_transition_without_drain_authorization_refuses_pending_wakes(tmp_path):
    store = _store(tmp_path)
    store.request_wake("job-1", caller="dispatcher", reason="mailbox")

    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        with pytest.raises(RuntimeError, match="pending wake"):
            store.activate_fence(
                barrier_token=barrier.token,
                authorization_request_id="auth-1",
                required=False,
            )

    assert [row["job_id"] for row in store.pending_wakes()] == ["job-1"]


def test_truncated_lock_file_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.lock_path.write_bytes(b"0")

    with pytest.raises(RuntimeError, match="truncated or corrupt"):
        with store.dispatch_section(boundary="actor"):
            pass
    with pytest.raises(RuntimeError, match="truncated or corrupt"):
        with store.acquire_dispatch_barrier(reason="incident"):
            pass


def test_unix_uses_shared_flock_for_actors_and_exclusive_flock_for_barrier(
    tmp_path, monkeypatch
):
    import jobflow_dispatch.quarantine_control as control

    calls = []

    class FakeFcntl:
        LOCK_SH = 1
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8

        @staticmethod
        def flock(_fd, operation):
            calls.append(operation)

    store = _store(tmp_path)
    monkeypatch.setattr(control, "fcntl", FakeFcntl)
    monkeypatch.setattr(control, "msvcrt", None)

    with store.dispatch_section(boundary="actor"):
        pass
    with store.acquire_dispatch_barrier(reason="incident"):
        pass

    assert calls == [
        FakeFcntl.LOCK_SH | FakeFcntl.LOCK_NB,
        FakeFcntl.LOCK_UN,
        FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB,
        FakeFcntl.LOCK_UN,
    ]


def test_unavailable_kernel_lock_backend_fails_closed(tmp_path, monkeypatch):
    import jobflow_dispatch.quarantine_control as control

    store = _store(tmp_path)
    monkeypatch.setattr(control, "fcntl", None)
    monkeypatch.setattr(control, "msvcrt", None)

    with pytest.raises(RuntimeError, match="cross-process dispatch locking unavailable"):
        with store.dispatch_section(boundary="actor"):
            pass


def test_missing_singleton_row_fails_closed_without_recreating_it(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM quarantine_dispatch_fence WHERE singleton=1")

    with pytest.raises(RuntimeError, match="fence row is missing"):
        store.fence_state()
    with pytest.raises(RuntimeError, match="fence row is missing"):
        with store.dispatch_section(boundary="actor"):
            pass


@pytest.mark.parametrize(
    "assignment",
    (
        "generation=-1",
        "fence_token='orphan-token'",
        "authorization_request_id='orphan-auth'",
        "changed_at=''",
    ),
)
def test_corrupt_singleton_fields_fail_closed(tmp_path, assignment):
    import sqlite3

    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            f"UPDATE quarantine_dispatch_fence SET {assignment} WHERE singleton=1"
        )

    with pytest.raises(RuntimeError, match="semantically invalid"):
        store.fence_state()
    with pytest.raises(RuntimeError, match="semantically invalid"):
        with store.dispatch_section(boundary="actor"):
            pass


def test_activate_fence_sqlite_failure_rolls_back_wake_drain_and_fence(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    store.request_wake("job-1", caller="dispatcher")
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """CREATE TRIGGER refuse_fence_activation
               BEFORE UPDATE OF fenced ON quarantine_dispatch_fence
               WHEN NEW.fenced=1 BEGIN SELECT RAISE(ABORT, 'injected activation failure'); END"""
        )

    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        with pytest.raises(sqlite3.IntegrityError, match="activation failure"):
            store.activate_fence(
                barrier_token=barrier.token,
                authorization_request_id="auth-1",
                required=True,
            )

    assert store.fence_state()["fenced"] is False
    assert [row["job_id"] for row in store.pending_wakes()] == ["job-1"]


def test_release_fence_sqlite_failure_leaves_exact_fence_active(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        token = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )["post"]["fence_token"]
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                """CREATE TRIGGER refuse_fence_release
                   BEFORE UPDATE OF fenced ON quarantine_dispatch_fence
                   WHEN NEW.fenced=0 BEGIN SELECT RAISE(ABORT, 'injected release failure'); END"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="release failure"):
            store.release_fence(
                barrier_token=barrier.token,
                expected_fence_token=token,
            )

    assert store.verify_fence(token)["fenced"] is True


def test_wake_insert_sqlite_failure_rolls_back_without_phantom_row(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """CREATE TRIGGER refuse_wake_insert
               BEFORE INSERT ON quarantine_wakes
               BEGIN SELECT RAISE(ABORT, 'injected wake failure'); END"""
        )

    with pytest.raises(sqlite3.IntegrityError, match="wake failure"):
        store.request_wake("job-1", caller="dispatcher")

    assert store.pending_wakes() == ()


def test_wake_started_before_barrier_is_committed_then_drained(monkeypatch, tmp_path):
    import jobflow_dispatch.quarantine_control as control

    store = _store(tmp_path)
    wake_inside_section = threading.Event()
    release_wake = threading.Event()
    barrier_entered = threading.Event()
    original_now = control._now

    def blocking_now():
        wake_inside_section.set()
        assert release_wake.wait(2)
        return original_now()

    monkeypatch.setattr(control, "_now", blocking_now)
    wake = threading.Thread(
        target=lambda: store.request_wake("job-1", caller="dispatcher")
    )
    transition = {}

    def fence():
        with store.acquire_dispatch_barrier(reason="incident") as barrier:
            barrier_entered.set()
            transition.update(
                store.activate_fence(
                    barrier_token=barrier.token,
                    authorization_request_id="auth-1",
                    required=True,
                )
            )

    wake.start()
    assert wake_inside_section.wait(1)
    barrier = threading.Thread(target=fence)
    barrier.start()
    assert not barrier_entered.wait(0.1)
    release_wake.set()
    wake.join(2)
    barrier.join(2)

    assert [row["job_id"] for row in transition["pre"]["wakes"]] == ["job-1"]
    assert transition["post"]["wakes"] == []


def test_windows_actor_slot_exhaustion_fails_closed(tmp_path, monkeypatch):
    import jobflow_dispatch.quarantine_control as control

    class FullMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_fd, operation, _size):
            if operation == FullMsvcrt.LK_NBLCK:
                raise OSError("slot occupied")

    monkeypatch.setattr(control, "fcntl", None)
    monkeypatch.setattr(control, "msvcrt", FullMsvcrt)
    lock_path = tmp_path / "dispatch.lock"
    lock_path.write_bytes(b"0" * control._LOCK_FILE_SIZE)
    lock = control._KernelLock(
        lock_path,
        timeout=0.02,
        poll_interval=0.001,
        exclusive=False,
    )

    with pytest.raises(TimeoutError, match="admission slot"):
        lock.acquire()
    assert lock.handle is None and lock.held is False


def test_default_store_detects_lock_file_replacement(monkeypatch, tmp_path):
    import os

    import jobflow_dispatch.quarantine_control as control

    monkeypatch.setattr(control, "_canonical_hermes_root", lambda: tmp_path)
    first = control.default_control_store()
    original_identity = first._lock_identity
    replacement = first.lock_path.with_suffix(".replacement")
    replacement.write_bytes(b"0" * control._LOCK_FILE_SIZE)
    os.replace(replacement, first.lock_path)

    with pytest.raises(RuntimeError, match="lock file identity changed"):
        control.default_control_store()
    assert first._lock_identity == original_identity


def test_fresh_store_refuses_replaced_lock_against_durable_identity(tmp_path):
    import os

    import jobflow_dispatch.quarantine_control as control

    first = _store(tmp_path)
    replacement = first.lock_path.with_suffix(".replacement")
    replacement.write_bytes(b"0" * control._CONTROL_LOCK_FILE_SIZE)
    os.replace(replacement, first.lock_path)

    with pytest.raises(RuntimeError, match="control identity changed"):
        control.QuarantineControlStore(
            first.db_path, lock_path=first.lock_path, poll_interval=0.005
        )


def test_established_store_refuses_deleted_identity_row(tmp_path):
    import sqlite3

    import jobflow_dispatch.quarantine_control as control

    first = _store(tmp_path)
    with sqlite3.connect(first.db_path) as conn:
        conn.execute(
            "DELETE FROM quarantine_control_identity WHERE singleton=1"
        )

    with pytest.raises(RuntimeError, match="identity row is missing"):
        control.QuarantineControlStore(
            first.db_path, lock_path=first.lock_path, poll_interval=0.005
        )
    with sqlite3.connect(first.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM quarantine_control_identity"
        ).fetchone()[0] == 0


def test_corrupt_singleton_fails_closed_inside_activate_and_release(tmp_path):
    import sqlite3

    store = _store(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE quarantine_dispatch_fence SET generation=-1 WHERE singleton=1"
        )
    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        with pytest.raises(RuntimeError, match="semantically invalid"):
            store.activate_fence(
                barrier_token=barrier.token,
                authorization_request_id="auth-1",
                required=True,
            )

    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE quarantine_dispatch_fence SET fenced=1, generation=1, "
            "fence_token='token', authorization_request_id=NULL WHERE singleton=1"
        )
    with store.acquire_dispatch_barrier(reason="release") as barrier:
        with pytest.raises(RuntimeError, match="semantically invalid"):
            store.release_fence(
                barrier_token=barrier.token,
                expected_fence_token="token",
            )


def test_active_fence_survives_store_reopen(tmp_path):
    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    first = _store(tmp_path)
    with first.acquire_dispatch_barrier(reason="incident") as barrier:
        transition = first.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )

    reopened = QuarantineControlStore(
        first.db_path, lock_path=first.lock_path, poll_interval=0.005
    )
    assert reopened.verify_fence(transition["post"]["fence_token"])["generation"] == 1
    with pytest.raises(RuntimeError, match="dispatch fenced"):
        with reopened.dispatch_section(boundary="actor"):
            pass


def test_barrier_timeout_leaves_no_phantom_holder(tmp_path):
    store = _store(tmp_path)
    actor = store.dispatch_section(boundary="stuck")
    actor.__enter__()
    try:
        with pytest.raises(TimeoutError, match="dispatch sections"):
            with store.acquire_dispatch_barrier(reason="incident", timeout=0.03):
                pass
    finally:
        actor.__exit__(None, None, None)

    with store.acquire_dispatch_barrier(reason="retry", timeout=0.2) as barrier:
        assert barrier.assert_held()["complete"] is True


def test_release_requires_held_barrier_and_exact_live_generation(tmp_path):
    store = _store(tmp_path)
    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        token = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )["post"]["fence_token"]

    with pytest.raises(RuntimeError, match="barrier"):
        store.release_fence(
            barrier_token="not-held",
            expected_fence_token=token,
        )

    assert store.verify_fence(token)["fenced"] is True


def test_nested_dispatch_section_does_not_inherit_different_store_admission(tmp_path):
    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    first = _store(tmp_path / "first")
    second = QuarantineControlStore(
        tmp_path / "second" / "dispatch.db", timeout=0.03, poll_interval=0.005
    )
    with second.acquire_dispatch_barrier(reason="other-store"):
        with first.dispatch_section(boundary="first-store"):
            with pytest.raises(TimeoutError, match="dispatch admission"):
                with second.dispatch_section(boundary="second-store"):
                    pass


def test_fence_transition_rejects_barrier_from_different_store(tmp_path):
    first = _store(tmp_path / "first")
    second = _store(tmp_path / "second")
    with first.acquire_dispatch_barrier(reason="first-store") as barrier:
        with pytest.raises(RuntimeError, match="same control store"):
            second.activate_fence(
                barrier_token=barrier.token,
                authorization_request_id="auth-1",
                required=True,
            )


def test_release_rejects_barrier_from_different_store(tmp_path):
    first = _store(tmp_path / "first")
    second = _store(tmp_path / "second")
    with second.acquire_dispatch_barrier(reason="second-store") as barrier:
        token = second.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )["post"]["fence_token"]

    with first.acquire_dispatch_barrier(reason="first-store") as wrong_barrier:
        with pytest.raises(RuntimeError, match="same control store"):
            second.release_fence(
                barrier_token=wrong_barrier.token,
                expected_fence_token=token,
            )

    assert second.verify_fence(token)["fenced"] is True
