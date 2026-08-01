import asyncio
import concurrent.futures
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, HomeChannel, Platform
from gateway.platforms.base import MessageEvent
from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
from gateway.session import AsyncSessionStore, SessionStore, build_session_key
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
async def test_cancel_background_tasks_cancels_inflight_message_processing():
    _runner, adapter = make_restart_runner()
    release = asyncio.Event()

    async def block_forever(_event):
        await release.wait()
        return None

    adapter.set_message_handler(block_forever)
    event = MessageEvent(text="work", source=make_restart_source(), message_id="1")

    await adapter.handle_message(event)
    await asyncio.sleep(0)

    session_key = build_session_key(event.source)
    assert session_key in adapter._active_sessions
    assert adapter._background_tasks

    await adapter.cancel_background_tasks()

    assert adapter._background_tasks == set()
    assert adapter._active_sessions == {}
    assert adapter._pending_messages == {}


def test_cleanup_agent_resources_reaps_stale_aux_clients():
    runner, _adapter = make_restart_runner()
    agent = MagicMock()

    with patch("agent.auxiliary_client.cleanup_stale_async_clients") as cleanup_mock:
        runner._cleanup_agent_resources(agent)

    agent.shutdown_memory_provider.assert_called_once()
    agent.close.assert_called_once()
    cleanup_mock.assert_called_once()


@pytest.mark.asyncio
async def test_gateway_stop_interrupts_running_agents_and_cancels_adapter_tasks():
    runner, adapter = make_restart_runner()
    runner._pending_messages = {"session": "pending text"}
    runner._pending_approvals = {"session": {"command": "rm -rf /tmp/x"}}
    runner._restart_drain_timeout = 0.0

    release = asyncio.Event()

    async def block_forever(_event):
        await release.wait()
        return None

    adapter.set_message_handler(block_forever)
    event = MessageEvent(text="work", source=make_restart_source(), message_id="1")
    await adapter.handle_message(event)
    await asyncio.sleep(0)

    disconnect_mock = AsyncMock()
    adapter.disconnect = disconnect_mock

    session_key = build_session_key(event.source)
    running_agent = MagicMock()
    runner._running_agents = {session_key: running_agent}

    with (
        patch("gateway.status.remove_pid_file"),
        patch("gateway.status.write_runtime_status"),
        patch("agent.auxiliary_client.shutdown_cached_clients") as shutdown_cached_clients,
    ):
        await runner.stop()

    running_agent.interrupt.assert_called_once_with("Gateway shutting down")
    disconnect_mock.assert_awaited_once()
    shutdown_cached_clients.assert_called_once()
    assert runner.adapters == {}
    assert runner._running_agents == {}
    assert runner._pending_messages == {}
    assert runner._pending_approvals == {}
    assert runner._shutdown_event.is_set() is True


@pytest.mark.asyncio
async def test_gateway_stop_drains_running_agents_before_disconnect():
    runner, adapter = make_restart_runner()
    # Opt into a grace window (the default is 0 = interrupt immediately).
    # This exercises the path where an agent finishes within the drain
    # window and must NOT be interrupted.
    runner._restart_drain_timeout = 5.0
    disconnect_mock = AsyncMock()
    adapter.disconnect = disconnect_mock

    running_agent = MagicMock()
    runner._running_agents = {"session": running_agent}

    async def finish_agent():
        await asyncio.sleep(0.05)
        runner._running_agents.clear()

    asyncio.create_task(finish_agent())

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    running_agent.interrupt.assert_not_called()
    disconnect_mock.assert_awaited_once()
    assert runner._shutdown_event.is_set() is True


@pytest.mark.asyncio
async def test_gateway_stop_cancels_secondary_reconnects_before_session_drain():
    runner, _adapter = make_restart_runner()
    order: list[str] = []

    async def _cancel_secondary_reconnects() -> None:
        order.append("secondary_reconnect_cancel")

    async def _notify_sessions() -> None:
        order.append("notify_sessions")

    runner._cancel_secondary_profile_reconnect_tasks = _cancel_secondary_reconnects
    runner._notify_active_sessions_of_shutdown = _notify_sessions

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    assert order[:2] == ["secondary_reconnect_cancel", "notify_sessions"]


@pytest.mark.asyncio
async def test_gateway_stop_interrupts_after_drain_timeout():
    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.05

    disconnect_mock = AsyncMock()
    adapter.disconnect = disconnect_mock

    running_agent = MagicMock()
    runner._running_agents = {"session": running_agent}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    running_agent.interrupt.assert_called_once_with("Gateway shutting down")
    disconnect_mock.assert_awaited_once()
    assert runner._shutdown_event.is_set() is True


@pytest.mark.asyncio
async def test_gateway_stop_systemd_service_restart_uses_tempfail(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    monkeypatch.setenv("INVOCATION_ID", "systemd-test")
    runner._launch_systemd_restart_shortcut = MagicMock()

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True, service_restart=True)

    runner._launch_systemd_restart_shortcut.assert_called_once_with()
    # Exit 75 (EX_TEMPFAIL) so RestartForceExitStatus=75 in the unit
    # file revives the gateway via Restart=on-failure, even when the
    # planned-restart helper fails (Polkit denial, missing user bus,
    # headless box, or operator-managed unit using on-failure instead
    # of always).  StartLimitBurst still bounds accidental loops.
    assert runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE
    assert (tmp_path / ".restart_pending.json").exists()


def test_service_restart_process_exits_75_with_live_worker_and_session_db_lock(
    tmp_path,
):
    """Process-level regression for the production SIGTERM hang.

    A real ThreadPoolExecutor worker keeps both the agent persistence RLock and
    the real SessionDB lock after the canonical pre-interrupt recovery receipt
    is durable. ``GatewayRunner.stop`` must not close the agent or state.db
    underneath that worker; it must finish bounded teardown and hard-exit 75.
    """
    repo_root = Path(__file__).resolve().parents[2]
    child_code = textwrap.dedent(
        """
        import asyncio
        import concurrent.futures
        import json
        import logging
        import os
        import threading
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock, patch

        from gateway.restart import GATEWAY_SERVICE_RESTART_EXIT_CODE
        from gateway.run import _GatewayAgentWorker, _exit_after_graceful_shutdown
        from gateway.session import SessionStore
        from tests.gateway.restart_test_helpers import (
            make_restart_runner,
            make_restart_source,
        )

        logging.basicConfig(level=logging.WARNING)

        runner, adapter = make_restart_runner()
        adapter.disconnect = AsyncMock()
        runner._restart_drain_timeout = 0.0
        runner._launch_systemd_restart_shortcut = MagicMock()
        home = Path(os.environ["HERMES_HOME"])
        store = SessionStore(
            sessions_dir=home / "sessions",
            config=runner.config,
            db_path=home / "state.db",
        )
        entry = store.get_or_create_session(
            make_restart_source(chat_id="production-lock-e2e")
        )
        session_key = entry.session_key
        runner.session_store = store

        class BusyPersistAgent:
            def __init__(self):
                self._session_persist_lock = threading.RLock()
                self._session_messages = [
                    {"role": "user", "content": "active production-sized turn"}
                ]
                self.session_id = "process-e2e-busy-persist"
                self.flush_calls = 0
                self.closed = False

            def _flush_messages_to_session_db(self, _messages):
                self.flush_calls += 1
                with self._session_persist_lock:
                    pass

            def _drop_trailing_empty_response_scaffolding(self, _messages):
                pass

            def interrupt(self, _reason):
                runner._running_agents.clear()

            def shutdown_memory_provider(self, _messages=None):
                pass

            def close(self):
                self.closed = True

        agent = BusyPersistAgent()
        runner._running_agents = {session_key: agent}

        allow_db_lock = threading.Event()
        db_lock_held = threading.Event()

        def run_real_agent_worker():
            with agent._session_persist_lock:
                if not allow_db_lock.wait(timeout=5):
                    raise RuntimeError("canonical receipt never released DB lock")
                with store._db._lock:
                    db_lock_held.set()
                    threading.Event().wait()

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        worker_future = executor.submit(run_real_agent_worker)
        runner._running_agent_workers = {
            session_key: [
                _GatewayAgentWorker(
                    future=worker_future,
                    agent_holder=[agent],
                    run_generation=None,
                )
            ]
        }

        real_mark_resume_pending = store.mark_resume_pending
        mark_calls = []

        def mark_then_let_worker_take_db(*args, **kwargs):
            result = real_mark_resume_pending(*args, **kwargs)
            mark_calls.append((args, kwargs))
            if len(mark_calls) == 2:
                # The second canonical write is the authoritative timestamp at
                # actual interrupt. Let the worker take SessionDB only after it
                # is durable, then prove teardown never waits for/close-races it.
                allow_db_lock.set()
                if not db_lock_held.wait(timeout=2):
                    raise RuntimeError("worker failed to acquire SessionDB lock")
            return result

        store.mark_resume_pending = mark_then_let_worker_take_db

        async def stop_for_service_restart():
            with (
                patch("gateway.status.remove_pid_file"),
                patch("gateway.status.release_gateway_runtime_lock"),
                patch("gateway.status.write_runtime_status"),
            ):
                await runner.stop(
                    restart=True,
                    detached_restart=False,
                    service_restart=True,
                )

        asyncio.run(stop_for_service_restart())
        receipt = {
            "agent_closed": agent.closed,
            "db_connection_open": store._db._conn is not None,
            "exit_code": runner._exit_code,
            "flush_calls": agent.flush_calls,
            "resume_mark_calls": len(mark_calls),
            "session_key": session_key,
            "shutdown_event": runner._shutdown_event.is_set(),
            "worker_done": worker_future.done(),
        }
        receipt_path = Path(os.environ["SHUTDOWN_RECEIPT_PATH"])
        with receipt_path.open("w", encoding="utf-8") as handle:
            json.dump(receipt, handle)
            handle.flush()
            os.fsync(handle.fileno())

        if runner._exit_code != GATEWAY_SERVICE_RESTART_EXIT_CODE:
            raise SystemExit(1)
        # The production entry point uses the same bounded hard-exit so Python
        # never waits for the intentionally wedged non-daemon executor worker.
        _exit_after_graceful_shutdown(runner._exit_code)
        """
    )
    hermes_home = tmp_path / "hermes-home"
    receipt_path = tmp_path / "shutdown-receipt.json"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["INVOCATION_ID"] = "shutdown-persist-lock-process-e2e"
    env["SHUTDOWN_RECEIPT_PATH"] = str(receipt_path)

    completed = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=12,
        check=False,
    )

    assert completed.returncode == GATEWAY_SERVICE_RESTART_EXIT_CODE, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt == {
        "agent_closed": False,
        "db_connection_open": True,
        "exit_code": GATEWAY_SERVICE_RESTART_EXIT_CODE,
        "flush_calls": 0,
        "resume_mark_calls": 2,
        "session_key": receipt["session_key"],
        "shutdown_event": True,
        "worker_done": False,
    }
    assert "reason=executor_worker_still_running" in completed.stderr
    assert "Skipping SessionDB close" in completed.stderr
    assert (hermes_home / ".restart_pending.json").exists()
    assert not (hermes_home / ".clean_shutdown").exists()

    # The child hard-exit released the OS lock. Reload from the canonical
    # state.db (not sessions.json) and prove the pre-interrupt receipt survived.
    reloaded = SessionStore(
        sessions_dir=hermes_home / "sessions",
        config=GatewayConfig(),
        db_path=hermes_home / "state.db",
    )
    try:
        reloaded._ensure_loaded()
        recovered = reloaded._entries[receipt["session_key"]]
        assert recovered.resume_pending is True
        assert recovered.resume_reason == "restart_timeout"
    finally:
        reloaded._db.close()


@pytest.mark.asyncio
async def test_shutdown_cleanup_uses_one_global_deadline_across_agents():
    runner, _adapter = make_restart_runner()
    release_cleanup = threading.Event()

    first_agent = MagicMock()
    first_agent._session_messages = []
    first_agent.shutdown_memory_provider.side_effect = (
        lambda *_args: release_cleanup.wait(timeout=5)
    )
    second_agent = MagicMock()
    second_agent._session_messages = []

    started = time.monotonic()
    try:
        unsafe = await runner._finalize_shutdown_agents(
            {"first": first_agent, "second": second_agent},
            deadline_monotonic=started + 0.05,
        )
        elapsed = time.monotonic() - started
    finally:
        release_cleanup.set()
        await asyncio.sleep(0.05)
        runner._shutdown_executor()

    assert elapsed < 0.5
    assert unsafe == {"first", "second"}
    first_agent.shutdown_memory_provider.assert_called_once()
    second_agent.shutdown_memory_provider.assert_not_called()
    second_agent.close.assert_not_called()


@pytest.mark.asyncio
async def test_session_db_close_sees_stale_real_worker_after_public_slot_release():
    """The complete executor registry, not _running_agents, owns DB safety."""
    runner, _adapter = make_restart_runner()
    worker_future: concurrent.futures.Future = concurrent.futures.Future()
    agent = MagicMock()
    runner._running_agents = {}
    runner._running_agent_workers = {
        "same-key": [
            gateway_run._GatewayAgentWorker(
                future=worker_future,
                agent_holder=[agent],
                run_generation=1,
            )
        ]
    }
    session_db = MagicMock()
    runner.session_store._db = session_db

    skipped = await runner._close_shutdown_session_dbs(
        deadline_monotonic=time.monotonic() + 1,
        unsafe_reasons=set(),
    )
    assert skipped is False
    session_db.close.assert_not_called()

    worker_future.set_result(None)
    closed = await runner._close_shutdown_session_dbs(
        deadline_monotonic=time.monotonic() + 1,
        unsafe_reasons=set(),
    )
    runner._shutdown_executor()

    assert closed is True
    session_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_async_store_call_remains_visible_to_db_close(tmp_path):
    """A cancelled await cannot hide its still-running SQLite worker."""
    runner, _adapter = make_restart_runner()
    runner._executor_lock = threading.Lock()
    runner._executor = None
    runner._executor_closing = False
    runner._gateway_executor_futures = set()
    runner._running_agent_workers = {}
    runner._session_db = None

    store = SessionStore(
        sessions_dir=tmp_path / "sessions",
        config=runner.config,
        db_path=tmp_path / "state.db",
    )
    store._db.set_meta("shutdown-race", "intact")
    runner.session_store = store
    facade = AsyncSessionStore(
        store,
        offload=runner._run_in_executor_with_context,
    )

    worker_entered = threading.Event()
    release_worker = threading.Event()

    def delayed_db_read():
        worker_entered.set()
        if not release_worker.wait(timeout=5):
            raise RuntimeError("test worker release timed out")
        return store._db.get_meta("shutdown-race")

    store.delayed_db_read = delayed_db_read  # type: ignore[attr-defined]
    call_task = asyncio.create_task(facade.delayed_db_read())
    try:
        for _ in range(100):
            if worker_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert worker_entered.is_set()

        call_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task

        assert any(
            not future.done() for future in runner._gateway_executor_futures
        )
        skipped = await runner._close_shutdown_session_dbs(
            deadline_monotonic=time.monotonic() + 1,
            unsafe_reasons=set(),
        )
        assert skipped is False
        assert store._db._conn is not None

        release_worker.set()
        for _ in range(100):
            if not any(
                not future.done()
                for future in runner._gateway_executor_futures
            ):
                break
            await asyncio.sleep(0.01)

        assert not any(
            not future.done() for future in runner._gateway_executor_futures
        )
        closed = await runner._close_shutdown_session_dbs(
            deadline_monotonic=time.monotonic() + 1,
            unsafe_reasons=set(),
        )
        assert closed is True
        assert store._db._conn is None
    finally:
        release_worker.set()
        runner._shutdown_executor()
        if store._db._conn is not None:
            store._db.close()


@pytest.mark.asyncio
async def test_db_close_seal_atomically_rejects_late_executor_admission():
    """No worker can slip in after DB-close safety was snapshotted."""
    runner, _adapter = make_restart_runner()
    runner._session_db = None
    close_entered = threading.Event()
    release_close = threading.Event()

    class BlockingCloseDB:
        def close(self):
            close_entered.set()
            if not release_close.wait(timeout=5):
                raise RuntimeError("test DB close release timed out")

    runner.session_store._db = BlockingCloseDB()
    close_task = asyncio.create_task(
        runner._close_shutdown_session_dbs(
            deadline_monotonic=time.monotonic() + 5,
            unsafe_reasons=set(),
        )
    )
    try:
        for _ in range(100):
            if close_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert close_entered.is_set()

        with pytest.raises(RuntimeError, match="shutting down"):
            runner._submit_in_executor_with_context(lambda: None)
    finally:
        release_close.set()

    assert await close_task is True
    runner._shutdown_executor()


@pytest.mark.asyncio
async def test_cancelled_topic_recovery_worker_remains_visible_to_db_close(
    tmp_path,
):
    """Base-adapter offloads inherit the runner's real-worker close barrier."""
    runner, adapter = make_restart_runner()
    adapter.gateway_runner = runner
    store = SessionStore(
        sessions_dir=tmp_path / "sessions",
        config=runner.config,
        db_path=tmp_path / "state.db",
    )
    store._db.set_meta("topic-recovery", "intact")
    runner._session_db = None
    runner.session_store = store
    worker_entered = threading.Event()
    release_worker = threading.Event()
    worker_errors = []

    def recover_topic(_source):
        worker_entered.set()
        if not release_worker.wait(timeout=5):
            raise RuntimeError("test topic recovery release timed out")
        try:
            store._db.get_meta("topic-recovery")
        except Exception as exc:
            worker_errors.append(exc)
            raise
        return None

    adapter.set_topic_recovery_fn(recover_topic)
    adapter.set_message_handler(AsyncMock(return_value=None))
    event = MessageEvent(
        text="topic recovery",
        source=make_restart_source(),
        message_id="topic-recovery-1",
    )
    call_task = asyncio.create_task(adapter.handle_message(event))
    try:
        for _ in range(100):
            if worker_entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert worker_entered.is_set()

        call_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call_task

        assert any(
            not future.done() for future in runner._gateway_executor_futures
        )
        assert await runner._close_shutdown_session_dbs(
            deadline_monotonic=time.monotonic() + 1,
            unsafe_reasons=set(),
        ) is False
        assert store._db._conn is not None
    finally:
        release_worker.set()

    for _ in range(100):
        if not any(
            not future.done() for future in runner._gateway_executor_futures
        ):
            break
        await asyncio.sleep(0.01)
    assert worker_errors == []
    assert await runner._close_shutdown_session_dbs(
        deadline_monotonic=time.monotonic() + 1,
        unsafe_reasons=set(),
    ) is True
    runner._shutdown_executor()


def test_start_gateway_owned_watchdog_remains_armed_through_post_run_tail(
    tmp_path,
):
    """Process proof: runner.stop cannot disarm start_gateway's tail watchdog."""
    repo_root = Path(__file__).resolve().parents[2]
    receipt_path = tmp_path / "tail-watchdog-receipt.json"
    child_code = textwrap.dedent(
        """
        import asyncio
        import json
        import os
        import time
        from pathlib import Path
        from unittest.mock import patch

        from tests.gateway.restart_test_helpers import make_restart_runner

        runner, _adapter = make_restart_runner()
        runner._defer_shutdown_watchdog_disarm = True
        runner._restart_drain_timeout = 0.0

        async def stop_runner():
            with (
                patch("gateway.run.resolve_shutdown_watchdog_delay", return_value=1.0),
                patch("gateway.status.remove_pid_file"),
                patch("gateway.status.release_gateway_runtime_lock"),
                patch("gateway.status.write_runtime_status"),
            ):
                await runner.stop()

        asyncio.run(stop_runner())
        receipt = {
            "done_is_set": runner._shutdown_watchdog_done.is_set(),
            "shutdown_event": runner._shutdown_event.is_set(),
        }
        path = Path(os.environ["TAIL_WATCHDOG_RECEIPT"])
        with path.open("w", encoding="utf-8") as handle:
            json.dump(receipt, handle)
            handle.flush()
            os.fsync(handle.fileno())

        # Simulate a wedged cron/housekeeping/MCP post-run tail. The still-armed
        # OS-thread watchdog must terminate the process.
        time.sleep(3)
        raise SystemExit(99)
        """
    )
    env = os.environ.copy()
    env.pop("PYTEST_CURRENT_TEST", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    env["TAIL_WATCHDOG_RECEIPT"] = str(receipt_path)

    completed = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=7,
        check=False,
    )

    assert completed.returncode == 1, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == {
        "done_is_set": False,
        "shutdown_event": True,
    }


@pytest.mark.parametrize(
    "finalization_phase",
    ("pending_task", "async_generator", "default_executor"),
)
def test_start_gateway_watchdog_covers_asyncio_run_finalization(
    tmp_path,
    finalization_phase,
):
    """The process watchdog covers every blocking ``asyncio.run`` tail."""
    repo_root = Path(__file__).resolve().parents[2]
    receipt_path = tmp_path / f"{finalization_phase}-watchdog-receipt.json"
    phase_marker_path = tmp_path / f"{finalization_phase}-entered"
    child_code = textwrap.dedent(
        """
        import asyncio
        import json
        import os
        import threading
        from pathlib import Path
        from unittest.mock import patch

        from tests.gateway.restart_test_helpers import make_restart_runner

        runner, _adapter = make_restart_runner()
        runner._defer_shutdown_watchdog_disarm = True
        runner._restart_drain_timeout = 0.0
        phase = os.environ["WATCHDOG_FINALIZATION_PHASE"]
        marker_path = Path(os.environ["WATCHDOG_PHASE_MARKER"])

        def wedged_worker():
            marker_path.write_text("entered", encoding="utf-8")
            threading.Event().wait()

        async def exercise_start_gateway_return_boundary():
            with (
                patch("gateway.run.resolve_shutdown_watchdog_delay", return_value=3.0),
                patch("gateway.status.remove_pid_file"),
                patch("gateway.status.release_gateway_runtime_lock"),
                patch("gateway.status.write_runtime_status"),
            ):
                await runner.stop()

            if phase == "pending_task":
                async def cancellation_resistant_task():
                    try:
                        await asyncio.Future()
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None:
                            current.uncancel()
                        marker_path.write_text("entered", encoding="utf-8")
                        await asyncio.Future()

                asyncio.create_task(cancellation_resistant_task())
                await asyncio.sleep(0)
            elif phase == "async_generator":
                async def generator_with_wedged_finalizer():
                    try:
                        yield "ready"
                    finally:
                        marker_path.write_text("entered", encoding="utf-8")
                        await asyncio.Future()

                generator = generator_with_wedged_finalizer()
                await anext(generator)
            elif phase == "default_executor":
                worker_task = asyncio.create_task(asyncio.to_thread(wedged_worker))
                while not marker_path.exists():
                    await asyncio.sleep(0.01)
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
            else:
                raise RuntimeError(f"unknown phase: {phase}")

            receipt = {
                "done_is_set": runner._shutdown_watchdog_done.is_set(),
                "phase": phase,
            }
            path = Path(os.environ["WATCHDOG_FINALIZATION_RECEIPT"])
            with path.open("w", encoding="utf-8") as handle:
                json.dump(receipt, handle)
                handle.flush()
                os.fsync(handle.fileno())

            # Returning is the exact end-of-start_gateway boundary.
            # asyncio.run() next cancels pending tasks, finalizes async
            # generators, and drains its default executor. Whichever selected
            # phase wedges must remain covered by the already-armed watchdog.
        asyncio.run(exercise_start_gateway_return_boundary())
        raise SystemExit(99)
        """
    )
    env = os.environ.copy()
    env.pop("PYTEST_CURRENT_TEST", None)
    env["HERMES_HOME"] = str(tmp_path / "hermes-home")
    env["WATCHDOG_FINALIZATION_PHASE"] = finalization_phase
    env["WATCHDOG_FINALIZATION_RECEIPT"] = str(receipt_path)
    env["WATCHDOG_PHASE_MARKER"] = str(phase_marker_path)

    completed = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=9,
        check=False,
    )

    assert completed.returncode == 1, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == {
        "done_is_set": False,
        "phase": finalization_phase,
    }
    assert phase_marker_path.read_text(encoding="utf-8") == "entered"


@pytest.mark.asyncio
async def test_direct_or_startup_failure_stop_disarms_its_watchdog():
    """Before tail ownership transfers, runner.stop owns the full lifecycle."""
    runner, _adapter = make_restart_runner()
    assert runner._defer_shutdown_watchdog_disarm is False

    with (
        patch("gateway.status.remove_pid_file"),
        patch("gateway.status.release_gateway_runtime_lock"),
        patch("gateway.status.write_runtime_status"),
    ):
        await runner.stop()

    assert runner._shutdown_event.is_set()
    assert runner._shutdown_watchdog_done.is_set()


def test_hard_exit_disarms_process_watchdog_only_at_exit_boundary():
    done = threading.Event()
    observed = {}

    def fake_exit(code):
        observed["code"] = code
        observed["done"] = done.is_set()
        observed["published"] = gateway_run._PROCESS_SHUTDOWN_WATCHDOG_DONE
        raise RuntimeError("exit sentinel")

    gateway_run._publish_process_shutdown_watchdog(done)
    try:
        with (
            patch("gateway.status.remove_pid_file"),
            patch("gateway.status.release_gateway_runtime_lock"),
            patch("hermes_logging.drain_log_queue"),
            patch("gateway.run.os._exit", side_effect=fake_exit),
            pytest.raises(RuntimeError, match="exit sentinel"),
        ):
            gateway_run._exit_after_graceful_shutdown(17)
    finally:
        gateway_run._disarm_process_shutdown_watchdog()

    assert observed == {"code": 17, "done": True, "published": None}


@pytest.mark.asyncio
async def test_gateway_stop_launchd_service_restart_keeps_nonzero_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()

    with patch("gateway.run.sys.platform", "darwin"), patch(
        "gateway.status.remove_pid_file"
    ), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True, service_restart=True)

    assert runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE


@pytest.mark.asyncio
async def test_restart_shutdown_warning_uses_restart_command_reply_anchor_for_active_session():
    runner, adapter = make_restart_runner()
    source = make_restart_source(thread_id="42")
    session_key = build_session_key(source)
    runner._running_agents = {session_key: MagicMock()}
    runner._cache_session_source(session_key, source)
    restart_source = make_restart_source(thread_id="42")
    restart_source.message_id = "restart-command"
    runner._restart_requested = True
    runner._restart_command_source = restart_source
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id=source.chat_id,
        name="Telegram",
        thread_id=source.thread_id,
    )

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent_calls) == 1
    chat_id, message, metadata = adapter.sent_calls[0]
    assert chat_id == source.chat_id
    assert "Gateway restarting" in message
    assert metadata["thread_id"] == source.thread_id
    assert metadata["telegram_dm_topic_reply_fallback"] is True
    assert metadata["direct_messages_topic_id"] == source.thread_id
    assert metadata["telegram_reply_to_message_id"] == "restart-command"


@pytest.mark.asyncio
async def test_in_chat_restart_skips_home_shutdown_even_with_active_session():
    runner, adapter = make_restart_runner()
    source = make_restart_source(thread_id="42")
    session_key = build_session_key(source)
    runner._running_agents = {session_key: MagicMock()}
    runner._cache_session_source(session_key, source)
    restart_source = make_restart_source(thread_id="42")
    restart_source.message_id = "restart-command"
    runner._restart_requested = True
    runner._restart_command_source = restart_source
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-chat",
        name="Telegram Home",
    )

    await runner._notify_active_sessions_of_shutdown()

    assert len(adapter.sent_calls) == 1
    chat_id, message, metadata = adapter.sent_calls[0]
    assert chat_id == source.chat_id
    assert "Gateway restarting" in message
    assert metadata["telegram_reply_to_message_id"] == "restart-command"


@pytest.mark.asyncio
async def test_idle_in_chat_restart_does_not_send_interruption_warning():
    runner, adapter = make_restart_runner()
    source = make_restart_source(thread_id="42")
    source.message_id = "restart-command"
    runner._restart_requested = True
    runner._restart_command_source = source
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id=source.chat_id,
        name="Telegram",
        thread_id=source.thread_id,
    )

    await runner._notify_active_sessions_of_shutdown()

    assert adapter.sent_calls == []


@pytest.mark.asyncio
async def test_in_chat_restart_does_not_write_home_startup_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    source = make_restart_source(thread_id="42")
    source.message_id = "restart-command"
    runner._restart_command_source = source
    runner._launch_systemd_restart_shortcut = MagicMock()
    monkeypatch.setenv("INVOCATION_ID", "systemd-test")

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True, service_restart=True)

    assert not (tmp_path / ".restart_pending.json").exists()


@pytest.mark.asyncio
async def test_drain_active_agents_throttles_status_updates():
    runner, _adapter = make_restart_runner()
    runner._update_runtime_status = MagicMock()

    runner._running_agents = {"a": MagicMock(), "b": MagicMock()}

    async def finish_agents():
        await asyncio.sleep(0.12)
        runner._running_agents.pop("a")
        await asyncio.sleep(0.12)
        runner._running_agents.clear()

    task = asyncio.create_task(finish_agents())
    await runner._drain_active_agents(1.0)
    await task

    # Start, one count-change update, and final update. Allow one extra update
    # if the loop observes the zero-agent state before exiting.
    assert 3 <= runner._update_runtime_status.call_count <= 4


@pytest.mark.asyncio
async def test_gateway_stop_kills_tool_subprocesses_before_adapter_disconnect_on_timeout(monkeypatch):
    """On drain timeout, tool subprocesses must be killed BEFORE adapter
    disconnect so systemd's TimeoutStopSec doesn't SIGKILL the cgroup with
    bash/sleep children still attached (#8202)."""
    runner, adapter = make_restart_runner()
    runner._restart_drain_timeout = 0.01  # force timeout path

    call_order: list[str] = []

    def _fake_kill_all(task_id=None):
        call_order.append("kill_all")
        return 2

    def _fake_cleanup_envs():
        call_order.append("cleanup_environments")

    def _fake_cleanup_browsers():
        call_order.append("cleanup_browsers")

    async def _disconnect():
        call_order.append("disconnect")

    # Patch the module-level names the stop() helper imports lazily.
    import tools.process_registry as _pr
    import tools.terminal_tool as _tt
    import tools.browser_tool as _bt
    monkeypatch.setattr(_pr.process_registry, "kill_all", _fake_kill_all)
    monkeypatch.setattr(_tt, "cleanup_all_environments", _fake_cleanup_envs)
    monkeypatch.setattr(_bt, "cleanup_all_browsers", _fake_cleanup_browsers)

    adapter.disconnect = _disconnect

    runner._running_agents = {"session": MagicMock()}

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    # First kill_all must precede the first disconnect.  (Both the eager
    # post-interrupt cleanup and the final catch-all call _kill_tool_
    # subprocesses, so we expect kill_all to appear twice total.)
    assert "kill_all" in call_order
    assert "disconnect" in call_order
    first_kill = call_order.index("kill_all")
    first_disconnect = call_order.index("disconnect")
    assert first_kill < first_disconnect, (
        f"Tool subprocesses must be killed before adapter disconnect on "
        f"drain timeout, got order: {call_order}"
    )
    # Defense-in-depth final cleanup still runs.
    assert call_order.count("kill_all") >= 2


@pytest.mark.asyncio
async def test_gateway_stop_kills_tool_subprocesses_on_graceful_path(monkeypatch):
    """Graceful shutdown (no drain timeout) must still kill tool subprocesses
    exactly once via the final catch-all — regression guard against
    accidentally removing that call when refactoring."""
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()

    kill_count = 0

    def _fake_kill_all(task_id=None):
        nonlocal kill_count
        kill_count += 1
        return 0

    import tools.process_registry as _pr
    import tools.terminal_tool as _tt
    import tools.browser_tool as _bt
    monkeypatch.setattr(_pr.process_registry, "kill_all", _fake_kill_all)
    monkeypatch.setattr(_tt, "cleanup_all_environments", lambda: None)
    monkeypatch.setattr(_bt, "cleanup_all_browsers", lambda: None)

    # No running agents → drain returns immediately, no timeout, no eager cleanup.
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    # Only the final catch-all fires on the graceful path.
    assert kill_count == 1


# ---------------------------------------------------------------------------
# gateway_state persistence on shutdown (issue #42675)
#
# On Docker/s6, container_boot.py only auto-starts gateways whose last
# persisted gateway_state was "running". An unexpected external signal
# (the SIGTERM s6/Docker sends on `docker compose up --force-recreate`,
# OOM, bare kill) must NOT persist "stopped" — otherwise the gateway
# stays down after every container restart. An operator-initiated stop
# writes a planned-stop marker first, so it is NOT signal-initiated and
# DOES persist "stopped", respecting the explicit intent.
# ---------------------------------------------------------------------------


def _persisted_states(runner) -> list:
    """All gateway_state values passed to _update_runtime_status, in order."""
    states = []
    for call in runner._update_runtime_status.call_args_list:
        args, kwargs = call
        state = kwargs.get("gateway_state", args[0] if args else None)
        states.append(state)
    return states


def _stopped_state_persisted(runner) -> bool:
    """True iff _update_runtime_status was called with gateway_state='stopped'."""
    return "stopped" in _persisted_states(runner)


@pytest.mark.asyncio
async def test_signal_initiated_shutdown_persists_running_not_stopped(tmp_path, monkeypatch):
    """Unexpected SIGTERM (container restart / OOM / kill) must persist
    gateway_state=running — NOT stopped, and NOT leave the mid-shutdown
    'draining' marker — so container_boot auto-starts on next boot (#42675)."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._signal_initiated_shutdown = True  # set by handler on unmarked signal

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    assert not _stopped_state_persisted(runner), (
        "signal-initiated shutdown must NOT persist gateway_state=stopped"
    )
    # The FINAL terminal write must be 'running' so container_boot's
    # _AUTOSTART_STATES check passes (it only auto-starts 'running').
    assert _persisted_states(runner)[-1] == "running", (
        f"final state must be 'running', got: {_persisted_states(runner)}"
    )


@pytest.mark.asyncio
async def test_operator_initiated_stop_persists_stopped(tmp_path, monkeypatch):
    """A planned stop (marker written → not signal-initiated) must persist
    gateway_state=stopped so an explicit `hermes gateway stop` stays down."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._signal_initiated_shutdown = False  # planned stop classification

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop()

    assert _stopped_state_persisted(runner), (
        "operator-initiated stop must persist gateway_state=stopped"
    )


@pytest.mark.asyncio
async def test_signal_initiated_restart_still_persists_stopped(tmp_path, monkeypatch):
    """A restart is not a 'stay down' — it persists normally (the new
    process/container brings the gateway back up itself). The suppression
    only applies to a terminal signal-initiated stop, not a restart."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._signal_initiated_shutdown = True
    runner._launch_systemd_restart_shortcut = MagicMock()

    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await runner.stop(restart=True, service_restart=True)

    assert _stopped_state_persisted(runner), (
        "a restart must persist gateway_state=stopped via the normal path"
    )


# ── #42126: zombie PID must be treated as dead in _pid_exists ────────────────
# Under systemd Restart=always, the old gateway becomes a zombie (still in the
# process table, not yet reaped) when the replacement starts. _pid_exists must
# report it dead so --replace proceeds instead of waiting on it and aborting
# with exit 1 (a silent crash loop).


def test_pid_exists_zombie_via_psutil_returns_false(monkeypatch):
    """The live path is psutil. psutil.pid_exists() returns True for a zombie,
    so _pid_exists must additionally check Process.status() == STATUS_ZOMBIE."""
    import sys
    import types

    from gateway import status

    fake_psutil = types.SimpleNamespace()
    fake_psutil.STATUS_ZOMBIE = "zombie"

    class NoSuchProcess(Exception):
        pass

    class PsutilError(Exception):
        pass

    fake_psutil.NoSuchProcess = NoSuchProcess
    fake_psutil.Error = PsutilError

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            return "zombie"

    fake_psutil.Process = _Proc
    # Without the zombie guard, this True would make the caller treat the
    # zombie as a live gateway.
    fake_psutil.pid_exists = lambda pid: True

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert status._pid_exists(4242) is False


def test_pid_exists_live_via_psutil_returns_true(monkeypatch):
    """A genuinely running (non-zombie) process is still reported alive."""
    import sys
    import types

    from gateway import status

    fake_psutil = types.SimpleNamespace()
    fake_psutil.STATUS_ZOMBIE = "zombie"
    fake_psutil.NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    fake_psutil.Error = type("Error", (Exception,), {})

    class _Proc:
        def __init__(self, pid):
            self.pid = pid

        def status(self):
            return "running"

    fake_psutil.Process = _Proc
    fake_psutil.pid_exists = lambda pid: True

    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    assert status._pid_exists(4242) is True


def test_pid_exists_zombie_via_proc_fallback_returns_false(monkeypatch):
    """When psutil is unavailable, the POSIX fallback reads /proc/<pid>/stat
    and must treat state 'Z' as dead before reaching os.kill."""
    import builtins
    import sys

    from gateway import status

    monkeypatch.setitem(sys.modules, "psutil", None)  # force ImportError
    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("psutil disabled for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    monkeypatch.setattr(status, "_IS_WINDOWS", False)

    fake_stat = "4242 (defunct) Z 1 0 0 0 -1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
    fake_path = MagicMock()
    fake_path.read_text.return_value = fake_stat
    monkeypatch.setattr(status, "Path", lambda *_a, **_k: fake_path)

    kill = MagicMock()
    monkeypatch.setattr(status.os, "kill", kill)

    assert status._pid_exists(4242) is False
    kill.assert_not_called()
