import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import threading
import time

import pytest
import psutil

import agent.local_endpoint_lane as lane_module
from agent.local_endpoint_lane import (
    async_local_endpoint_lane,
    lane_dir_for_endpoint,
    local_endpoint_lane,
)


ENDPOINT = "http://127.0.0.1:10240/v1"


def _wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def _process_worker(
    state_root: str,
    name: str,
    order_path: str,
    release_path: str,
    hold: bool,
    crash: bool = False,
) -> None:
    with local_endpoint_lane(
        ENDPOINT,
        state_root=Path(state_root),
        poll_interval_s=0.01,
        stale_after_s=1.0,
        timeout_s=8.0,
    ):
        with open(order_path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}\n")
        if crash:
            os._exit(17)
        if hold:
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline and not Path(release_path).exists():
                time.sleep(0.01)


def test_remote_endpoint_is_noop(tmp_path: Path) -> None:
    with local_endpoint_lane(
        "https://api.example.com/v1",
        state_root=tmp_path,
    ) as lease:
        assert lease.wait_seconds == 0
        assert lease.coordinated is False

    assert not any(path.name != "hermes_test" for path in tmp_path.iterdir())


def test_disabled_local_endpoint_is_noop(tmp_path: Path) -> None:
    with local_endpoint_lane(ENDPOINT, state_root=tmp_path, enabled=False) as lease:
        assert lease.coordinated is False

    assert not any(path.name != "hermes_test" for path in tmp_path.iterdir())


def test_lane_path_is_endpoint_scoped_without_leaking_url(tmp_path: Path) -> None:
    first = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)
    same_origin = lane_dir_for_endpoint(
        "http://localhost:10240/other/path",
        state_root=tmp_path,
    )
    other_port = lane_dir_for_endpoint(
        "http://127.0.0.1:10241/v1",
        state_root=tmp_path,
    )

    assert first == same_origin
    assert first != other_port
    assert "10240" not in first.name
    assert "127.0.0.1" not in first.name


def test_default_lane_root_is_shared_across_hermes_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lane_module.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-a"))
    first = lane_dir_for_endpoint(ENDPOINT)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-b"))
    second = lane_dir_for_endpoint(ENDPOINT)

    assert first == second
    assert first.parent.parent == tmp_path


def test_fifo_order_across_threads(tmp_path: Path) -> None:
    order: list[str] = []
    errors: list[BaseException] = []
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def worker(name: str, hold: bool = False) -> None:
        try:
            with local_endpoint_lane(
                ENDPOINT,
                state_root=tmp_path,
                poll_interval_s=0.01,
                timeout_s=5.0,
            ):
                order.append(name)
                if hold:
                    holder_ready.set()
                    release_holder.wait(timeout=5.0)
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=worker, args=("first", True), daemon=True)
    second = threading.Thread(target=worker, args=("second",), daemon=True)
    third = threading.Thread(target=worker, args=("third",), daemon=True)

    first.start()
    assert holder_ready.wait(timeout=2.0)
    second.start()
    lane_dir = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)
    _wait_for(lambda: len(list(lane_dir.glob("ticket-*.json"))) == 1)
    third.start()
    _wait_for(lambda: len(list(lane_dir.glob("ticket-*.json"))) == 2)
    release_holder.set()

    for thread in (first, second, third):
        thread.join(timeout=5.0)

    assert errors == []
    assert order == ["first", "second", "third"]
    assert not any(thread.is_alive() for thread in (first, second, third))


def test_wait_is_interruptible_and_removes_ticket(tmp_path: Path) -> None:
    holder_ready = threading.Event()
    release_holder = threading.Event()
    cancel_waiter = threading.Event()
    waiter_errors: list[BaseException] = []

    def holder() -> None:
        with local_endpoint_lane(ENDPOINT, state_root=tmp_path):
            holder_ready.set()
            release_holder.wait(timeout=5.0)

    def waiter() -> None:
        try:
            with local_endpoint_lane(
                ENDPOINT,
                state_root=tmp_path,
                poll_interval_s=0.01,
                cancel_check=cancel_waiter.is_set,
            ):
                raise AssertionError("cancelled waiter acquired the lane")
        except InterruptedError as exc:
            waiter_errors.append(exc)

    holder_thread = threading.Thread(target=holder, daemon=True)
    waiter_thread = threading.Thread(target=waiter, daemon=True)
    holder_thread.start()
    assert holder_ready.wait(timeout=2.0)
    waiter_thread.start()

    lane_dir = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)
    _wait_for(lambda: bool(list(lane_dir.glob("ticket-*.json"))))
    cancel_waiter.set()
    waiter_thread.join(timeout=2.0)
    release_holder.set()
    holder_thread.join(timeout=2.0)

    assert len(waiter_errors) == 1
    assert not list(lane_dir.glob("ticket-*.json"))


def test_wait_timeout_removes_ticket(tmp_path: Path) -> None:
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        with local_endpoint_lane(ENDPOINT, state_root=tmp_path):
            holder_ready.set()
            release_holder.wait(timeout=5.0)

    holder_thread = threading.Thread(target=holder, daemon=True)
    holder_thread.start()
    assert holder_ready.wait(timeout=2.0)

    with pytest.raises(TimeoutError):
        with local_endpoint_lane(
            ENDPOINT,
            state_root=tmp_path,
            poll_interval_s=0.01,
            timeout_s=0.05,
        ):
            pass

    release_holder.set()
    holder_thread.join(timeout=2.0)
    lane_dir = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)
    assert not list(lane_dir.glob("ticket-*.json"))


def test_exception_releases_lane(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="provider failed"):
        with local_endpoint_lane(ENDPOINT, state_root=tmp_path):
            raise RuntimeError("provider failed")

    with local_endpoint_lane(
        ENDPOINT,
        state_root=tmp_path,
        timeout_s=0.2,
    ) as lease:
        assert lease.coordinated is True


def test_lane_is_held_until_stream_consumption_finishes(tmp_path: Path) -> None:
    first_yielded = threading.Event()
    release_stream = threading.Event()
    second_acquired = threading.Event()

    def consume_stream() -> None:
        with local_endpoint_lane(ENDPOINT, state_root=tmp_path):
            first_yielded.set()
            release_stream.wait(timeout=5.0)

    def second_request() -> None:
        with local_endpoint_lane(ENDPOINT, state_root=tmp_path):
            second_acquired.set()

    first = threading.Thread(target=consume_stream, daemon=True)
    second = threading.Thread(target=second_request, daemon=True)
    first.start()
    assert first_yielded.wait(timeout=2.0)
    second.start()
    assert second_acquired.wait(timeout=0.1) is False
    release_stream.set()
    assert second_acquired.wait(timeout=2.0)
    first.join(timeout=2.0)
    second.join(timeout=2.0)


def test_different_endpoint_origins_do_not_block_each_other(tmp_path: Path) -> None:
    with local_endpoint_lane(ENDPOINT, state_root=tmp_path):
        started = time.monotonic()
        with local_endpoint_lane(
            "http://127.0.0.1:10241/v1",
            state_root=tmp_path,
            timeout_s=0.2,
        ):
            pass

    assert time.monotonic() - started < 0.2


def test_fifo_order_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    order_path = tmp_path / "order.txt"
    release_path = tmp_path / "release"
    lane_dir = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)

    def start(name: str, hold: bool = False):
        process = context.Process(
            target=_process_worker,
            args=(
                str(tmp_path),
                name,
                str(order_path),
                str(release_path),
                hold,
            ),
        )
        process.start()
        return process

    first = start("first", hold=True)
    _wait_for(lambda: order_path.exists() and order_path.read_text() == "first\n")
    second = start("second")
    _wait_for(lambda: len(list(lane_dir.glob("ticket-*.json"))) == 1)
    third = start("third")
    _wait_for(lambda: len(list(lane_dir.glob("ticket-*.json"))) == 2)
    release_path.write_text("release\n", encoding="utf-8")

    for process in (first, second, third):
        process.join(timeout=8.0)

    assert [process.exitcode for process in (first, second, third)] == [0, 0, 0]
    assert order_path.read_text(encoding="utf-8").splitlines() == [
        "first",
        "second",
        "third",
    ]


def test_holder_process_crash_releases_lane(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    order_path = tmp_path / "order.txt"
    release_path = tmp_path / "unused"
    crashing = context.Process(
        target=_process_worker,
        args=(
            str(tmp_path),
            "crash",
            str(order_path),
            str(release_path),
            False,
            True,
        ),
    )
    crashing.start()
    crashing.join(timeout=5.0)
    assert crashing.exitcode == 17

    with local_endpoint_lane(
        ENDPOINT,
        state_root=tmp_path,
        poll_interval_s=0.01,
        timeout_s=1.0,
    ) as lease:
        assert lease.coordinated is True


def test_dead_stale_head_ticket_is_pruned(tmp_path: Path) -> None:
    lane_dir = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)
    lane_dir.mkdir(parents=True)
    stale = lane_dir / "ticket-00000000000000000000-dead.json"
    stale.write_text(
        json.dumps(
            {"pid": 999_999_999, "thread_id": 0, "process_created": 0.0}
        ),
        encoding="utf-8",
    )
    old = time.time() - 3600
    os.utime(stale, (old, old))

    with local_endpoint_lane(
        ENDPOINT,
        state_root=tmp_path,
        poll_interval_s=0.01,
        stale_after_s=0.1,
        timeout_s=1.0,
    ):
        pass

    assert not stale.exists()


def test_live_stale_head_ticket_is_not_pruned(tmp_path: Path) -> None:
    lane_dir = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)
    lane_dir.mkdir(parents=True)
    live = lane_dir / "ticket-00000000000000000000-live.json"
    live.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "thread_id": threading.get_ident(),
                "process_created": psutil.Process().create_time(),
            }
        ),
        encoding="utf-8",
    )
    old = time.time() - 3600
    os.utime(live, (old, old))

    with pytest.raises(TimeoutError):
        with local_endpoint_lane(
            ENDPOINT,
            state_root=tmp_path,
            poll_interval_s=0.01,
            stale_after_s=0.01,
            timeout_s=0.05,
        ):
            pass

    assert live.exists()
    live.unlink()


@pytest.mark.asyncio
async def test_async_lane_preserves_fifo_order(tmp_path: Path) -> None:
    order: list[str] = []
    first_acquired = asyncio.Event()
    release_first = asyncio.Event()

    async def worker(name: str, hold: bool = False) -> None:
        async with async_local_endpoint_lane(
            ENDPOINT,
            state_root=tmp_path,
            poll_interval_s=0.01,
            timeout_s=2.0,
        ):
            order.append(name)
            if hold:
                first_acquired.set()
                await release_first.wait()

    first = asyncio.create_task(worker("first", hold=True))
    await asyncio.wait_for(first_acquired.wait(), timeout=1.0)
    second = asyncio.create_task(worker("second"))
    lane_dir = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)
    await asyncio.to_thread(
        _wait_for,
        lambda: len(list(lane_dir.glob("ticket-*.json"))) == 1,
    )
    third = asyncio.create_task(worker("third"))
    await asyncio.to_thread(
        _wait_for,
        lambda: len(list(lane_dir.glob("ticket-*.json"))) == 2,
    )
    release_first.set()
    await asyncio.wait_for(asyncio.gather(first, second, third), timeout=3.0)

    assert order == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_async_wait_cancellation_cleans_ticket(tmp_path: Path) -> None:
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        with local_endpoint_lane(ENDPOINT, state_root=tmp_path):
            holder_ready.set()
            release_holder.wait(timeout=5.0)

    holder_thread = threading.Thread(target=holder, daemon=True)
    holder_thread.start()
    assert holder_ready.wait(timeout=2.0)

    async def waiter() -> None:
        async with async_local_endpoint_lane(
            ENDPOINT,
            state_root=tmp_path,
            poll_interval_s=0.01,
        ):
            raise AssertionError("cancelled waiter acquired the lane")

    waiter_task = asyncio.create_task(waiter())
    lane_dir = lane_dir_for_endpoint(ENDPOINT, state_root=tmp_path)
    await asyncio.to_thread(
        _wait_for,
        lambda: bool(list(lane_dir.glob("ticket-*.json"))),
    )
    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    assert not list(lane_dir.glob("ticket-*.json"))
    release_holder.set()
    holder_thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_async_holder_cancellation_releases_lane(tmp_path: Path) -> None:
    acquired = asyncio.Event()

    async def holder() -> None:
        async with async_local_endpoint_lane(ENDPOINT, state_root=tmp_path):
            acquired.set()
            await asyncio.Event().wait()

    holder_task = asyncio.create_task(holder())
    await asyncio.wait_for(acquired.wait(), timeout=1.0)
    holder_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder_task

    async with async_local_endpoint_lane(
        ENDPOINT,
        state_root=tmp_path,
        timeout_s=0.2,
    ) as lease:
        assert lease.coordinated is True
