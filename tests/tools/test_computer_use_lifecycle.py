from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_lifecycle():
    from tools.computer_use import tool as computer_use

    computer_use.reset_backend_for_tests()
    yield
    computer_use.reset_backend_for_tests()


class _Backend:
    def __init__(self, *, stop_entered=None, stop_continue=None, stop_error=None):
        self.stop_entered = stop_entered
        self.stop_continue = stop_continue
        self.stop_error = stop_error
        self.started = False
        self.stop_calls = 0

    def start(self):
        self.started = True

    def stop(self):
        self.stop_calls += 1
        if self.stop_entered is not None:
            self.stop_entered.set()
        if self.stop_continue is not None:
            assert self.stop_continue.wait(timeout=2)
        if self.stop_error is not None:
            raise self.stop_error
        self.started = False


def test_release_result_rejects_empty_or_generation_mismatch_without_stopping():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("session-a")

    generation = computer_use.get_computer_use_session_generation("session-a")
    assert generation is not None

    empty = computer_use.release_computer_use_session_result(
        "", expected_generation=generation, reason="dead_session"
    )
    mismatch = computer_use.release_computer_use_session_result(
        "session-a", expected_generation=generation + 1, reason="dead_session"
    )

    assert empty.status == "mismatch"
    assert mismatch.status == "mismatch"
    assert backend.stop_calls == 0
    assert computer_use.get_computer_use_session_generation("session-a") == generation


@pytest.mark.parametrize("invalid_generation", [None, True, False, 0, -1, "1", 1.0])
def test_release_result_rejects_invalid_generation_without_stopping(invalid_generation):
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("strict-generation")

    actual_generation = computer_use.get_computer_use_session_generation("strict-generation")
    assert actual_generation is not None

    result = computer_use.release_computer_use_session_result(
        "strict-generation",
        expected_generation=invalid_generation,
        reason="strict_validation",
    )

    assert result.status == "mismatch"
    assert result.error == "expected_generation must be a positive integer"
    assert backend.stop_calls == 0
    assert computer_use.get_computer_use_session_generation("strict-generation") == actual_generation


def test_release_result_omitted_generation_snapshots_exact_active_generation():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("result-compat")

    generation = computer_use.get_computer_use_session_generation("result-compat")
    assert generation is not None

    result = computer_use.release_computer_use_session_result("result-compat")

    assert result.status == "released"
    assert result.generation == generation
    assert backend.stop_calls == 1
    assert computer_use.get_computer_use_session_generation("result-compat") is None


def test_release_result_omitted_generation_is_idempotent_when_absent():
    from tools.computer_use import tool as computer_use

    result = computer_use.release_computer_use_session_result("result-compat-absent")

    assert result.status == "already_absent"
    assert result.generation is None


def test_terminal_transition_fence_blocks_same_session_backend_recreation():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("terminal-fence")
        generation = computer_use.get_computer_use_session_generation("terminal-fence")
        token = computer_use.begin_computer_use_terminal_transition(
            "terminal-fence"
        )
        try:
            result = computer_use.release_computer_use_session_result(
                "terminal-fence",
                expected_generation=generation,
                reason="test_terminal_fence",
            )
            assert result.released
            with pytest.raises(RuntimeError, match="terminal transition"):
                computer_use._get_backend("terminal-fence")
        finally:
            computer_use.end_computer_use_terminal_transition(token)

        replacement = computer_use._get_backend("terminal-fence")
        assert replacement is backend
        replacement_generation = computer_use.get_computer_use_session_generation(
            "terminal-fence"
        )
        assert replacement_generation > generation
        assert computer_use.release_computer_use_session_result(
            "terminal-fence",
            expected_generation=replacement_generation,
            reason="test_cleanup",
        ).released


def test_terminal_transition_rejects_action_that_looked_up_before_fence():
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from tools.computer_use import tool as computer_use

    backend = _Backend()
    lookup_complete = threading.Event()
    allow_revalidation = threading.Event()

    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("stale-terminal-call")
        generation = computer_use.get_computer_use_session_generation(
            "stale-terminal-call"
        )

        def stale_lookup(*, session_id):
            lookup_complete.set()
            assert allow_revalidation.wait(timeout=2)
            return backend

        with patch.object(computer_use, "_get_backend", side_effect=stale_lookup):
            with ThreadPoolExecutor(max_workers=1) as pool:
                acquire = pool.submit(
                    computer_use._acquire_backend_for_call,
                    "stale-terminal-call",
                )
                assert lookup_complete.wait(timeout=1)
                transition = computer_use.begin_computer_use_terminal_transition(
                    "stale-terminal-call"
                )
                allow_revalidation.set()
                with pytest.raises(RuntimeError, match="changed while acquiring"):
                    acquire.result(timeout=2)

        try:
            assert computer_use.release_computer_use_session_result(
                "stale-terminal-call",
                expected_generation=generation,
                reason="test_terminal_fence",
            ).released
        finally:
            computer_use.end_computer_use_terminal_transition(transition)


def test_successful_terminal_transition_retires_old_session_id():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("retired-route")
        transition = computer_use.begin_computer_use_terminal_transition(
            "retired-route"
        )
        released = computer_use.release_computer_use_session_result(
            "retired-route",
            expected_generation=transition.generation,
            timeout=1.0,
            reason="test-retire",
        )
        assert released.status == "released"
        computer_use.end_computer_use_terminal_transition(
            transition,
            retire=True,
        )
        with pytest.raises(RuntimeError, match="retired"):
            computer_use._get_backend("retired-route")


def test_managed_route_publication_registry_reclaims_all_idle_sessions():
    from tools.computer_use import tool as computer_use

    for index in range(4096):
        sid = f"managed-route-{index}"
        publication = computer_use.publish_computer_use_session(sid)
        computer_use.unpublish_computer_use_session(publication)

    assert computer_use._managed_session_publications == {}
    with pytest.raises(RuntimeError, match="retired or unpublished"):
        computer_use._get_backend("managed-route-0")


def test_managed_route_publication_is_reference_counted():
    from tools.computer_use import tool as computer_use

    sid = "concurrent-managed-route"
    first = computer_use.publish_computer_use_session(sid)
    second = computer_use.publish_computer_use_session(sid)
    computer_use.unpublish_computer_use_session(first)
    assert computer_use._managed_session_publications == {sid: {second}}
    computer_use.unpublish_computer_use_session(second)
    assert computer_use._managed_session_publications == {}


def test_failed_terminal_transition_restores_exact_publication_lease():
    from tools.computer_use import tool as computer_use

    sid = "rollback-publication"
    publication = computer_use.publish_computer_use_session(sid)
    transition = computer_use.begin_computer_use_terminal_transition(sid)
    assert publication.active is False

    computer_use.end_computer_use_terminal_transition(
        transition,
        retire=False,
    )
    assert publication.active is True
    computer_use.unpublish_computer_use_session(publication)
    assert computer_use._managed_session_publications == {}


def test_retired_session_reactivation_is_fenced_until_publication():
    from tools.computer_use import tool as computer_use

    first = _Backend()
    second = _Backend()
    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend",
        side_effect=[first, second],
    ):
        computer_use._get_backend("historical-route")
        first_generation = computer_use.get_computer_use_session_generation(
            "historical-route"
        )
        terminal = computer_use.begin_computer_use_terminal_transition(
            "historical-route"
        )
        released = computer_use.release_computer_use_session_result(
            "historical-route",
            expected_generation=first_generation,
            reason="switch_away",
        )
        assert released.released
        computer_use.end_computer_use_terminal_transition(terminal, retire=True)

        reactivation = computer_use.begin_computer_use_session_reactivation(
            "historical-route"
        )
        with pytest.raises(RuntimeError, match="fenced"):
            computer_use._get_backend("historical-route")
        computer_use.end_computer_use_session_reactivation(
            reactivation,
            succeeded=True,
        )
        publication = computer_use.publish_computer_use_session(
            "historical-route"
        )
        try:
            assert computer_use._get_backend("historical-route") is second
            second_generation = computer_use.get_computer_use_session_generation(
                "historical-route"
            )
        finally:
            computer_use.unpublish_computer_use_session(publication)

    assert second_generation > first_generation


def test_reactivation_completion_advances_scalar_route_epoch():
    from tools.computer_use import tool as computer_use

    reactivation = computer_use.begin_computer_use_session_reactivation(
        "reactivation-epoch"
    )
    before_completion = computer_use._session_route_epoch_sequence
    computer_use.end_computer_use_session_reactivation(
        reactivation,
        succeeded=True,
    )

    assert computer_use._session_route_epoch_sequence == before_completion + 1


def test_stale_lookup_cannot_hop_to_reactivated_backend_generation():
    from tools.computer_use import tool as computer_use

    first = _Backend()
    second = _Backend()
    looked_up = threading.Event()
    continue_lookup = threading.Event()
    original_get = computer_use._get_backend

    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend",
        side_effect=[first, second],
    ):
        original_get("historical-stale")
        generation = computer_use.get_computer_use_session_generation(
            "historical-stale"
        )

        def blocked_get(session_id=""):
            backend = original_get(session_id)
            looked_up.set()
            assert continue_lookup.wait(timeout=2)
            return backend

        with patch.object(computer_use, "_get_backend", side_effect=blocked_get):
            with ThreadPoolExecutor(max_workers=1) as pool:
                stale = pool.submit(
                    computer_use._acquire_backend_for_call,
                    "historical-stale",
                )
                assert looked_up.wait(timeout=1)

                terminal = computer_use.begin_computer_use_terminal_transition(
                    "historical-stale"
                )
                released = computer_use.release_computer_use_session_result(
                    "historical-stale",
                    expected_generation=generation,
                    reason="switch_away",
                )
                assert released.released
                computer_use.end_computer_use_terminal_transition(
                    terminal,
                    retire=True,
                )
                reactivation = computer_use.begin_computer_use_session_reactivation(
                    "historical-stale"
                )
                computer_use.end_computer_use_session_reactivation(
                    reactivation,
                    succeeded=True,
                )
                publication = computer_use.publish_computer_use_session(
                    "historical-stale"
                )
                try:
                    original_get("historical-stale")
                    continue_lookup.set()

                    with pytest.raises(RuntimeError, match="changed while acquiring"):
                        stale.result(timeout=2)
                finally:
                    computer_use.unpublish_computer_use_session(publication)


def test_public_bool_release_snapshots_exact_generation_before_teardown():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("bool-adapter")

    generation = computer_use.get_computer_use_session_generation("bool-adapter")
    assert generation is not None
    assert computer_use.release_computer_use_session("bool-adapter") is True
    assert backend.stop_calls == 1
    assert computer_use.get_computer_use_session_generation("bool-adapter") is None


def test_start_failure_attempts_cleanup_and_removes_confirmed_stopped_record():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    backend.start = MagicMock(side_effect=RuntimeError("partial startup"))
    backend.stop = MagicMock()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        with pytest.raises(RuntimeError, match="partial startup"):
            computer_use._get_backend("start-failed")

    backend.stop.assert_called_once_with()
    assert computer_use.get_computer_use_session_generation("start-failed") is None
    assert "start-failed" not in computer_use._backends


def test_same_sid_recreation_is_rejected_while_exact_generation_is_closing():
    from tools.computer_use import tool as computer_use

    stop_entered = threading.Event()
    stop_continue = threading.Event()
    backend = _Backend(stop_entered=stop_entered, stop_continue=stop_continue)
    created = [backend]

    def factory(permission_mode="standard"):
        if len(created) == 1:
            return created[0]
        replacement = _Backend()
        created.append(replacement)
        return replacement

    with patch("tools.computer_use.cua_backend.CuaDriverBackend", side_effect=factory):
        computer_use._get_backend("session-a")
        generation = computer_use.get_computer_use_session_generation("session-a")
        assert generation is not None

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                computer_use.release_computer_use_session_result,
                "session-a",
                expected_generation=generation,
                timeout=1.0,
                reason="dead_session",
            )
            assert stop_entered.wait(timeout=1)
            with pytest.raises(RuntimeError, match="closing"):
                computer_use._get_backend("session-a")
            assert len(created) == 1
            stop_continue.set()
            result = future.result(timeout=2)

    assert result.status == "released"
    again = computer_use.release_computer_use_session_result(
        "session-a",
        expected_generation=generation,
        timeout=0.2,
        reason="duplicate",
    )
    assert again.status == "already_absent"
    assert backend.stop_calls == 1
    assert computer_use.get_computer_use_session_generation("session-a") is None


def test_action_lease_revalidates_after_release_wins_post_lookup_race():
    from tools.computer_use import tool as computer_use

    old_backend = _Backend()
    replacement = _Backend()
    with patch(
        "tools.computer_use.cua_backend.CuaDriverBackend",
        side_effect=[old_backend, replacement],
    ):
        computer_use._get_backend("session-a")
        generation = computer_use.get_computer_use_session_generation("session-a")
        original_get = computer_use._get_backend
        looked_up = threading.Event()
        continue_lookup = threading.Event()
        calls = 0

        def delayed_get(session_id=""):
            nonlocal calls
            backend = original_get(session_id)
            calls += 1
            if calls == 1:
                looked_up.set()
                assert continue_lookup.wait(timeout=2)
            return backend

        with patch.object(computer_use, "_get_backend", side_effect=delayed_get):
            with ThreadPoolExecutor(max_workers=1) as pool:
                def acquire_and_release():
                    backend, lease = computer_use._acquire_backend_for_call(
                        "session-a"
                    )
                    lease.release()
                    return backend

                acquired = pool.submit(acquire_and_release)
                assert looked_up.wait(timeout=1)
                released = computer_use.release_computer_use_session_result(
                    "session-a",
                    expected_generation=generation,
                    reason="race_test",
                )
                assert released.status == "released"
                continue_lookup.set()
                leased_backend = acquired.result(timeout=2)

    assert leased_backend is replacement
    assert leased_backend is not old_backend
    assert old_backend.stop_calls == 1


def test_call_quiescence_timeout_is_truthful_and_blocks_recreation():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("session-a")

    generation = computer_use.get_computer_use_session_generation("session-a")
    call_lock = computer_use._backend_call_locks["session-a"]
    held = threading.Event()
    release_hold = threading.Event()

    def hold_call_lock():
        with call_lock:
            held.set()
            release_hold.wait(timeout=2)

    thread = threading.Thread(target=hold_call_lock)
    thread.start()
    assert held.wait(timeout=1)
    try:
        result = computer_use.release_computer_use_session_result(
            "session-a",
            expected_generation=generation,
            timeout=0.02,
            reason="dead_session",
        )
        assert result.status == "timed_out"
        assert backend.stop_calls == 0
        with pytest.raises(RuntimeError, match="failed"):
            computer_use._get_backend("session-a")
        snapshot = computer_use.computer_use_lifecycle_snapshot()
        assert snapshot["session-a"]["state"] == "FAILED"
    finally:
        release_hold.set()
        thread.join(timeout=2)


def test_stop_failure_is_not_reported_as_success_and_can_be_retried():
    from tools.computer_use import tool as computer_use

    backend = _Backend(stop_error=RuntimeError("driver teardown failed"))
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("session-a")

    generation = computer_use.get_computer_use_session_generation("session-a")
    failed = computer_use.release_computer_use_session_result(
        "session-a",
        expected_generation=generation,
        timeout=0.2,
        reason="dead_session",
    )
    assert failed.status == "failed"
    assert bool(failed) is False
    assert computer_use.release_computer_use_session("session-a") is False

    backend.stop_error = None
    released = computer_use.release_computer_use_session_result(
        "session-a",
        expected_generation=generation,
        timeout=0.2,
        reason="retry",
    )
    assert released.status == "released"
    assert backend.stop_calls >= 2
    assert computer_use.get_computer_use_session_generation("session-a") is None


def test_cleanup_supervisor_defers_and_deduplicates_when_active_capacity_is_full():
    from tools.computer_use import tool as computer_use

    supervisor = computer_use._CleanupSupervisor(
        max_workers=1,
        max_pending=1,
        max_deferred=2,
    )
    started = threading.Event()
    unblock = threading.Event()

    def blocking_release(sid, **kwargs):
        if sid == "active":
            started.set()
            assert unblock.wait(timeout=2)
        return computer_use.ComputerUseReleaseResult(
            session_id=sid,
            generation=kwargs.get("expected_generation"),
            status="released",
            reason=kwargs.get("reason", "test"),
        )

    with patch.object(
        computer_use,
        "_release_computer_use_session_with_retries",
        side_effect=blocking_release,
    ):
        active = supervisor.submit(
            "active",
            expected_generation=1,
            timeout=0.1,
            reason="active",
            allow_empty_session=False,
        )
        assert started.wait(timeout=1)
        deferred = supervisor.submit(
            "deferred",
            expected_generation=2,
            timeout=0.1,
            reason="deferred",
            allow_empty_session=False,
        )
        duplicate = supervisor.submit(
            "deferred",
            expected_generation=2,
            timeout=0.1,
            reason="duplicate",
            allow_empty_session=False,
        )
        assert duplicate is deferred
        assert deferred.done() is False
        assert supervisor.snapshot()["pending"] == 1
        assert supervisor.snapshot()["deferred"] == 1
        unblock.set()
        assert active.result(timeout=2).status == "released"
        assert deferred.result(timeout=2).status == "released"

    assert supervisor.drain(1.0) is True
    assert supervisor.snapshot()["deferred"] == 0
    supervisor.shutdown(0.1)


def test_cleanup_supervisor_overflow_fails_exact_owner_closed_for_manual_retry():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("retained")
    generation = computer_use.get_computer_use_session_generation("retained")

    supervisor = computer_use._CleanupSupervisor(
        max_workers=1,
        max_pending=1,
        max_deferred=0,
    )
    started = threading.Event()
    unblock = threading.Event()

    def blocking_release(sid, **kwargs):
        started.set()
        assert unblock.wait(timeout=2)
        return computer_use.ComputerUseReleaseResult(
            session_id=sid,
            generation=kwargs.get("expected_generation"),
            status="already_absent",
            reason="blocker",
        )

    with patch.object(
        computer_use,
        "_release_computer_use_session_with_retries",
        side_effect=blocking_release,
    ):
        blocker = supervisor.submit(
            "blocker",
            expected_generation=999,
            timeout=0.1,
            reason="blocker",
            allow_empty_session=False,
        )
        assert started.wait(timeout=1)
        overflow = supervisor.submit(
            "retained",
            expected_generation=generation,
            timeout=0.1,
            reason="overflow",
            allow_empty_session=False,
        )
        result = overflow.result(timeout=1)
        assert result.status == "queue_rejected"
        assert computer_use.computer_use_lifecycle_snapshot()["retained"]["state"] == "FAILED"
        unblock.set()
        blocker.result(timeout=2)

    supervisor.shutdown(0.5)
    retried = computer_use.release_computer_use_session_result(
        "retained",
        expected_generation=generation,
        timeout=0.2,
        reason="manual_retry",
    )
    assert retried.status == "released"


def test_cleanup_supervisor_submit_shutdown_race_is_rejected_without_slot_leak():
    from tools.computer_use import tool as computer_use

    supervisor = computer_use._CleanupSupervisor(max_workers=1, max_pending=1)
    with patch.object(
        supervisor._work_queue,
        "put_nowait",
        side_effect=queue.Full,
    ):
        future = supervisor.submit(
            "session-race",
            expected_generation=1,
            timeout=0.1,
            reason="shutdown_race",
            allow_empty_session=False,
        )
    result = future.result(timeout=1)
    assert result.status == "queue_rejected"
    assert supervisor.snapshot()["pending"] == 0
    assert supervisor._slots.acquire(blocking=False) is True
    supervisor._slots.release()
    supervisor.shutdown(0.1)


def test_cleanup_supervisor_shutdown_is_bounded_with_blocked_daemon_worker():
    from tools.computer_use import tool as computer_use

    supervisor = computer_use._CleanupSupervisor(max_workers=1, max_pending=1)
    started = threading.Event()
    unblock = threading.Event()

    def blocking_release(sid, **kwargs):
        started.set()
        assert unblock.wait(timeout=2)
        return computer_use.ComputerUseReleaseResult(
            session_id=sid,
            generation=kwargs.get("expected_generation"),
            status="released",
            reason="bounded_shutdown",
        )

    with patch.object(
        computer_use,
        "_release_computer_use_session_with_retries",
        side_effect=blocking_release,
    ):
        future = supervisor.submit(
            "blocked",
            expected_generation=1,
            timeout=0.1,
            reason="blocked",
            allow_empty_session=False,
        )
        assert started.wait(timeout=1)
        began = time.monotonic()
        assert supervisor.shutdown(0.05) is False
        assert time.monotonic() - began < 0.25
        assert all(worker.daemon for worker in supervisor._workers)
        unblock.set()
        assert future.result(timeout=2).status == "released"
        assert supervisor.shutdown(1.0) is True

    for worker in supervisor._workers:
        worker.join(timeout=1)
    assert not any(worker.is_alive() for worker in supervisor._workers)


@pytest.mark.parametrize("invalid_generation", [None, True, False, 0, -1, "1", 1.0])
def test_supervised_release_rejects_invalid_generation_before_enqueue(invalid_generation):
    from tools.computer_use import tool as computer_use

    with pytest.raises(ValueError, match="expected_generation must be a positive integer"):
        computer_use.submit_computer_use_session_release(
            "strict-submit",
            expected_generation=invalid_generation,
            reason="strict_submit",
        )


def test_tracked_cleanup_supervisor_returns_future_and_drains():
    from tools.computer_use import tool as computer_use

    backend = _Backend()
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("session-a")
    generation = computer_use.get_computer_use_session_generation("session-a")

    future = computer_use.submit_computer_use_session_release(
        "session-a",
        expected_generation=generation,
        timeout=0.5,
        reason="dead_session",
    )
    result = future.result(timeout=2)

    assert result.status == "released"
    assert computer_use.drain_computer_use_cleanup(timeout=1.0) is True
    assert backend.stop_calls == 1
    assert computer_use.computer_use_cleanup_snapshot() == {
        "pending": 0,
        "deferred": 0,
        "capacity": 64,
        "deferred_capacity": 256,
        "closed": False,
    }
    assert isinstance(computer_use.computer_use_process_snapshot(), list)


def test_supervised_release_retries_transient_stop_failures():
    from tools.computer_use import tool as computer_use

    backend = _Backend(stop_error=RuntimeError("transient"))
    original_stop = backend.stop

    def flaky_stop():
        if backend.stop_calls < 2:
            original_stop()
        backend.stop_error = None
        original_stop()

    backend.stop = flaky_stop
    with patch("tools.computer_use.cua_backend.CuaDriverBackend", return_value=backend):
        computer_use._get_backend("session-retry")
    generation = computer_use.get_computer_use_session_generation("session-retry")

    future = computer_use.submit_computer_use_session_release(
        "session-retry",
        expected_generation=generation,
        timeout=0.2,
        reason="retry_test",
        max_attempts=3,
        retry_delay=0.0,
    )
    result = future.result(timeout=2)

    assert result.status == "released"
    assert backend.stop_calls == 3
    assert computer_use.get_computer_use_session_generation("session-retry") is None


def test_generation_tombstones_are_bounded_without_evicting_active_owners():
    from tools.computer_use import tool as computer_use

    with patch.object(computer_use, "_BACKEND_GENERATION_TOMBSTONE_CAP", 3), patch(
        "tools.computer_use.cua_backend.CuaDriverBackend",
        side_effect=lambda permission_mode="standard": _Backend(),
    ):
        computer_use._get_backend("active")
        for index in range(6):
            sid = f"closed-{index}"
            computer_use._get_backend(sid)
            generation = computer_use.get_computer_use_session_generation(sid)
            assert computer_use.release_computer_use_session_result(
                sid,
                expected_generation=generation,
                reason="tombstone_test",
            ).status == "released"

    assert "active" in computer_use._backend_generation_counters
    assert len(computer_use._backend_generation_counters) <= 3


def test_evicted_tombstone_never_reuses_generation_or_accepts_stale_release():
    from tools.computer_use import tool as computer_use

    old_victim = _Backend()
    other = _Backend()
    fresh_victim = _Backend()
    with patch.object(computer_use, "_BACKEND_GENERATION_TOMBSTONE_CAP", 1), patch(
        "tools.computer_use.cua_backend.CuaDriverBackend",
        side_effect=[old_victim, other, fresh_victim],
    ):
        computer_use._get_backend("victim")
        stale_generation = computer_use.get_computer_use_session_generation("victim")
        assert computer_use.release_computer_use_session_result(
            "victim",
            expected_generation=stale_generation,
            timeout=0.2,
            reason="first_close",
        ).status == "released"

        computer_use._get_backend("other")
        other_generation = computer_use.get_computer_use_session_generation("other")
        assert computer_use.release_computer_use_session_result(
            "other",
            expected_generation=other_generation,
            timeout=0.2,
            reason="evict_old_tombstone",
        ).status == "released"

        computer_use._get_backend("victim")
        fresh_generation = computer_use.get_computer_use_session_generation("victim")
        assert fresh_generation != stale_generation
        stale = computer_use.release_computer_use_session_result(
            "victim",
            expected_generation=stale_generation,
            timeout=0.2,
            reason="delayed_stale_close",
        )

    assert stale.status == "mismatch"
    assert fresh_victim.stop_calls == 0
    assert computer_use.get_computer_use_session_generation("victim") == fresh_generation


def test_cua_session_timeout_is_cancelled_and_only_then_confirmed_closed():
    from tools.computer_use.cua_backend import _CuaDriverSession

    future = MagicMock()
    future.result.side_effect = [
        concurrent.futures.TimeoutError(),
        concurrent.futures.CancelledError(),
    ]
    future.done.return_value = True
    session = _CuaDriverSession(MagicMock())
    session._started = True
    session._lifecycle_future = future
    session._signal_shutdown_locked = MagicMock()
    session._closed_event.set()

    session.stop()

    future.cancel.assert_called_once_with()
    assert session._lifecycle_future is None


def test_cua_session_unconfirmed_cancellation_raises_and_remains_retryable():
    from tools.computer_use.cua_backend import _CuaDriverSession

    future = MagicMock()
    future.result.side_effect = [
        concurrent.futures.TimeoutError(),
        concurrent.futures.TimeoutError(),
    ]
    session = _CuaDriverSession(MagicMock())
    session._started = True
    session._lifecycle_future = future
    session._signal_shutdown_locked = MagicMock()
    session._closed_event = MagicMock()
    session._closed_event.wait.return_value = False

    with pytest.raises(RuntimeError, match="did not exit after cancellation"):
        session.stop()

    assert session._lifecycle_future is future
    assert session._started is False


def test_backend_stop_retains_bridge_when_session_stop_is_unconfirmed():
    from tools.computer_use.cua_backend import CuaDriverBackend

    backend = CuaDriverBackend()
    session = MagicMock()
    session._started = False
    session.stop.side_effect = RuntimeError("session teardown unconfirmed")
    bridge = MagicMock()
    backend._session = session
    backend._bridge = bridge
    backend._embedded_daemon = None

    with pytest.raises(RuntimeError, match="session teardown unconfirmed"):
        backend.stop()

    bridge.stop.assert_not_called()

    session.stop.side_effect = None
    backend.stop()
    assert session.stop.call_count == 2
    bridge.stop.assert_called_once_with()


def test_embedded_daemon_stop_failure_retains_process_for_retry():
    from tools.computer_use.cua_backend import _EmbeddedCuaDaemon

    daemon = _EmbeddedCuaDaemon("cua-driver", "unrestricted")
    process = MagicMock()
    process.poll.return_value = None
    process.wait.side_effect = OSError("wait failed")
    daemon._process = process

    with patch("tools.computer_use.cua_backend.subprocess.run"), pytest.raises(
        OSError, match="wait failed"
    ):
        daemon.stop()

    assert daemon._process is process


def test_async_bridge_run_cancels_and_confirms_timed_out_coroutine():
    from tools.computer_use.cua_backend import _AsyncBridge

    bridge = _AsyncBridge()
    started = threading.Event()
    finalized = threading.Event()

    async def blocked_action():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finalized.set()

    bridge.start()
    try:
        with pytest.raises(concurrent.futures.TimeoutError):
            bridge.run(blocked_action(), timeout=0.02)
        assert started.is_set()
        assert finalized.wait(timeout=0.5)
    finally:
        bridge.stop()


def test_async_bridge_run_retains_unconfirmed_timeout_until_coroutine_finishes():
    from tools.computer_use.cua_backend import _AsyncBridge

    bridge = _AsyncBridge()
    bridge._CALL_CANCEL_CONFIRM_TIMEOUT = 0.02
    release = threading.Event()
    finalized = threading.Event()

    async def cancellation_resistant_action():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            while not release.is_set():
                await asyncio.sleep(0.005)
        finally:
            finalized.set()

    bridge.start()
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        with pytest.raises(RuntimeError, match="cancellation was not confirmed"):
            bridge.run(cancellation_resistant_action(), timeout=0.01)
        with bridge._inflight_lock:
            assert any(not event.is_set() for event in bridge._inflight_events)
        assert finalized.wait(timeout=1.0)
        bridge.stop()
    finally:
        timer.cancel()
        release.set()
        if bridge._thread is not None:
            bridge.stop()


def test_async_bridge_stop_refuses_to_stop_with_unconfirmed_inflight_call():
    from tools.computer_use.cua_backend import _AsyncBridge

    bridge = _AsyncBridge()
    loop = MagicMock()
    loop.is_running.return_value = True
    thread = MagicMock()
    thread.is_alive.return_value = False
    pending = threading.Event()
    bridge._loop = loop
    bridge._thread = thread
    bridge._inflight_events.add(pending)
    bridge._INFLIGHT_DRAIN_TIMEOUT = 0.01

    with pytest.raises(RuntimeError, match="in-flight action"):
        bridge.stop()

    loop.stop.assert_not_called()
    assert bridge._thread is thread
    assert bridge._loop is loop


def test_async_bridge_stop_refuses_to_forget_a_live_thread():
    from tools.computer_use.cua_backend import _AsyncBridge

    bridge = _AsyncBridge()
    loop = MagicMock()
    loop.is_running.return_value = True
    thread = MagicMock()
    thread.is_alive.return_value = True
    bridge._loop = loop
    bridge._thread = thread

    with pytest.raises(RuntimeError, match="did not stop within 2s"):
        bridge.stop()

    assert bridge._thread is thread
    assert bridge._loop is loop
