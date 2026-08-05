import asyncio
import threading
import time

import pytest

from hermes_cli.pty_session import RingBuffer


def test_ringbuffer_keeps_everything_under_capacity():
    rb = RingBuffer(10)
    rb.append(b"abc")
    rb.append(b"def")
    assert rb.snapshot() == b"abcdef"
    assert rb.truncated is False


def test_ringbuffer_drops_oldest_over_capacity():
    rb = RingBuffer(4)
    rb.append(b"abcdef")          # 6 bytes into a 4-byte buffer
    assert rb.snapshot() == b"cdef"
    assert rb.truncated is True




class FakeBridge:
    """Implements the bridge contract PtySession depends on."""

    def __init__(self, chunks):
        self._chunks = list(chunks)   # bytes; b"" = idle tick; None = EOF
        self.written = bytearray()
        self.closed = False
        self.resized = None

    def read(self, timeout):
        if not self._chunks:
            return b""                # idle
        return self._chunks.pop(0)

    def write(self, data):
        self.written.extend(data)

    def resize(self, cols, rows):
        self.resized = (cols, rows)

    def close(self):
        self.closed = True


class FakeWS:
    def __init__(self):
        self.sent = []               # list of ("bytes"|"text", payload)
        self.close_code = None

    async def send_bytes(self, data):
        self.sent.append(("bytes", bytes(data)))

    async def send_text(self, text):
        self.sent.append(("text", text))

    async def close(self, code=1000, reason=""):
        self.close_code = code


@pytest.mark.asyncio
async def test_attach_replays_buffer_then_streams_live():
    from hermes_cli.pty_session import PtySession
    bridge = FakeBridge([b"hello ", b"world", None])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    await asyncio.sleep(0.05)                      # drain consumes "hello world"
    ws = FakeWS()
    await s.attach(ws)
    replay = b"".join(p for kind, p in ws.sent if kind == "bytes")
    assert replay == b"hello world"
    await s.close()


@pytest.mark.asyncio
async def test_attach_replays_buffer_before_concurrent_live_output():
    from hermes_cli.pty_session import PtySession

    bridge = FakeBridge([])
    session = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    session.buffer.append(b"OLD")
    replay_started = asyncio.Event()
    release_replay = asyncio.Event()

    class BlockingReplayWS(FakeWS):
        async def send_bytes(self, data):
            if data == b"OLD":
                replay_started.set()
                await release_replay.wait()
            await super().send_bytes(data)

    ws = BlockingReplayWS()
    attach_task = asyncio.create_task(session.attach(ws))
    await replay_started.wait()
    session.bridge._chunks.append(b"LIVE")
    await session.start()
    await asyncio.sleep(0.02)
    release_replay.set()
    await attach_task
    await asyncio.sleep(0.02)

    assert [payload for kind, payload in ws.sent if kind == "bytes"] == [
        b"OLD",
        b"LIVE",
    ]
    await session.close()


@pytest.mark.asyncio
async def test_failed_initial_replay_rolls_back_attachment_state():
    from hermes_cli.pty_session import PtySession

    bridge = FakeBridge([])
    session = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    session.buffer.append(b"OLD")

    class FailingReplayWS(FakeWS):
        async def send_bytes(self, data):
            raise RuntimeError("replay failed")

    with pytest.raises(RuntimeError, match="replay failed"):
        await session.attach(FailingReplayWS())

    assert session.attached is False
    assert session._ws is None
    assert session.last_detached_at is not None
    await session.close()


@pytest.mark.asyncio
async def test_cancelled_initial_replay_rolls_back_attachment_state():
    from hermes_cli.pty_session import PtySession

    session = PtySession(
        "k", FakeBridge([]), buffer_cap=1024, read_timeout=0.01
    )
    session.buffer.append(b"OLD")
    replay_started = asyncio.Event()

    class BlockingReplayWS(FakeWS):
        async def send_bytes(self, data):
            replay_started.set()
            await asyncio.Event().wait()

    attach_task = asyncio.create_task(session.attach(BlockingReplayWS()))
    await replay_started.wait()
    attach_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await attach_task

    assert session.attached is False
    assert session._ws is None
    assert session._attaching_ws is None
    assert session.last_detached_at is not None
    await session.close()




@pytest.mark.asyncio
async def test_eof_marks_dead_and_closes_socket_4410():
    from hermes_cli.pty_session import PtySession
    bridge = FakeBridge([b"bye", None])
    s = PtySession("k", bridge, buffer_cap=1024, read_timeout=0.01)
    await s.start()
    ws = FakeWS()
    await s.attach(ws)
    await asyncio.sleep(0.05)                      # drain hits None (EOF)
    assert s.alive is False
    assert ws.close_code == 4410
    await s.close()


from hermes_cli.pty_session import (
    PtySessionRegistry,
    RegistryFull,
    SessionTerminated,
)


def make_registry(ttl=1800.0, max_sessions=16):
    return PtySessionRegistry(ttl=ttl, max_sessions=max_sessions,
                              buffer_cap=1024, read_timeout=0.01)


@pytest.mark.asyncio
async def test_same_key_reattaches_same_session():
    reg = make_registry()
    b1 = FakeBridge([b"", b"", b""])
    s1, created1 = await reg.attach_or_spawn("tok", spawn=lambda: b1)
    s2, created2 = await reg.attach_or_spawn("tok", spawn=lambda: FakeBridge([]))
    assert created1 is True and created2 is False
    assert s1 is s2
    assert s2.bridge is b1                     # second spawn callable was NOT used
    await reg.close_all()


@pytest.mark.asyncio
async def test_concurrent_same_key_attaches_share_one_spawn():
    reg = make_registry()
    spawn_started = threading.Event()
    release_spawn = threading.Event()
    bridges = []

    def blocked_spawn():
        bridge = FakeBridge([b""])
        bridges.append(bridge)
        spawn_started.set()
        assert release_spawn.wait(timeout=2)
        return bridge

    first = asyncio.create_task(reg.attach_or_spawn("token", spawn=blocked_spawn))
    assert await asyncio.to_thread(spawn_started.wait, 2)
    second = asyncio.create_task(
        reg.attach_or_spawn("token", spawn=lambda: FakeBridge([b""]))
    )
    release_spawn.set()

    (first_session, first_created), (second_session, second_created) = (
        await asyncio.gather(first, second)
    )
    assert len(bridges) == 1
    assert first_session is second_session
    assert sorted((first_created, second_created)) == [False, True]
    await reg.close_all()


@pytest.mark.asyncio
async def test_termination_wins_over_in_flight_spawn():
    reg = make_registry()
    spawn_started = threading.Event()
    release_spawn = threading.Event()
    bridge = FakeBridge([b""])

    def blocked_spawn():
        spawn_started.set()
        assert release_spawn.wait(timeout=2)
        return bridge

    attach = asyncio.create_task(
        reg.attach_or_spawn("token\0profile\0resume", spawn=blocked_spawn)
    )
    assert await asyncio.to_thread(spawn_started.wait, 2)

    assert await reg.terminate_attach_token("token") == 1
    release_spawn.set()

    with pytest.raises(SessionTerminated):
        await attach
    assert bridge.closed is True
    assert not reg._sessions
    assert not reg._pending


@pytest.mark.asyncio
async def test_close_all_awaits_tombstoned_pending_spawn_cleanup():
    reg = make_registry()
    spawn_started = threading.Event()
    release_spawn = threading.Event()
    bridges = []

    def blocked_spawn():
        bridge = FakeBridge([b""])
        bridges.append(bridge)
        spawn_started.set()
        release_spawn.wait(timeout=2)
        return bridge

    attach_task = asyncio.create_task(
        reg.attach_or_spawn("token", spawn=blocked_spawn)
    )
    assert await asyncio.to_thread(spawn_started.wait, 1)
    close_task = asyncio.create_task(reg.close_all())
    await asyncio.sleep(0)
    returned_before_spawn = close_task.done()
    release_spawn.set()
    await close_task
    with pytest.raises(SessionTerminated):
        await attach_task

    assert returned_before_spawn is False
    assert bridges[0].closed is True
    assert not reg._pending
    assert not reg._sessions


@pytest.mark.asyncio
async def test_cancelled_close_all_does_not_abandon_pending_spawn_cleanup():
    reg = make_registry()
    spawn_started = threading.Event()
    release_spawn = threading.Event()
    bridges = []

    def blocked_spawn():
        bridge = FakeBridge([b""])
        bridges.append(bridge)
        spawn_started.set()
        release_spawn.wait(timeout=2)
        return bridge

    attach_task = asyncio.create_task(
        reg.attach_or_spawn("token", spawn=blocked_spawn)
    )
    assert await asyncio.to_thread(spawn_started.wait, 1)
    close_task = asyncio.create_task(reg.close_all())
    await asyncio.sleep(0)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    release_spawn.set()
    with pytest.raises(SessionTerminated):
        await attach_task

    assert bridges[0].closed is True
    assert not reg._pending
    assert not reg._sessions


@pytest.mark.asyncio
async def test_terminated_published_session_rejects_late_attach():
    reg = make_registry()
    bridge = FakeBridge([b""])
    session, _ = await reg.attach_or_spawn("token", spawn=lambda: bridge)

    assert await reg.terminate_attach_token("token") == 1

    ws = FakeWS()
    with pytest.raises(SessionTerminated):
        await session.attach(ws)
    assert session.alive is False
    assert session.attached is False
    assert ws.sent == []


@pytest.mark.asyncio
async def test_termination_closes_socket_during_in_progress_attach():
    reg = make_registry()
    session, _ = await reg.attach_or_spawn(
        "token", spawn=lambda: FakeBridge([b"buffered"])
    )
    await asyncio.sleep(0.02)
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    class BlockingWS(FakeWS):
        async def send_bytes(self, data):
            send_started.set()
            await release_send.wait()
            await super().send_bytes(data)

    ws = BlockingWS()
    attach_task = asyncio.create_task(session.attach(ws))
    await send_started.wait()
    assert await reg.terminate_attach_token("token") == 1
    assert ws.close_code == 4411

    release_send.set()
    with pytest.raises(SessionTerminated):
        await attach_task
    assert session.attached is False


@pytest.mark.asyncio
async def test_concurrent_close_callers_wait_for_shared_cleanup():
    from hermes_cli.pty_session import PtySession

    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingCloseWS(FakeWS):
        async def close(self, code=1000, reason=""):
            close_started.set()
            await release_close.wait()
            await super().close(code=code, reason=reason)

    bridge = FakeBridge([b""])
    session = PtySession("token", bridge, buffer_cap=1024, read_timeout=0.01)
    await session.start()
    await session.attach(BlockingCloseWS())
    first = asyncio.create_task(session.close())
    await close_started.wait()
    second = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert second.done() is False

    release_close.set()
    await asyncio.gather(first, second)
    assert bridge.closed is True


@pytest.mark.asyncio
async def test_cancelled_close_caller_does_not_abandon_shared_cleanup():
    from hermes_cli.pty_session import PtySession

    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingCloseWS(FakeWS):
        async def close(self, code=1000, reason=""):
            close_started.set()
            await release_close.wait()
            await super().close(code=code, reason=reason)

    bridge = FakeBridge([b""])
    session = PtySession("token", bridge, buffer_cap=1024, read_timeout=0.01)
    await session.start()
    await session.attach(BlockingCloseWS())
    first = asyncio.create_task(session.close())
    await close_started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    retry = asyncio.create_task(session.close())
    await asyncio.sleep(0)
    assert retry.done() is False
    release_close.set()
    await retry
    assert bridge.closed is True


@pytest.mark.asyncio
async def test_cancelled_termination_still_closes_session_blocked_in_attach():
    reg = make_registry()
    bridge = FakeBridge([b"buffered"])
    session, _ = await reg.attach_or_spawn("token", spawn=lambda: bridge)
    await asyncio.sleep(0.02)
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    class BlockingSendWS(FakeWS):
        async def send_bytes(self, data):
            send_started.set()
            await release_send.wait()
            await super().send_bytes(data)

    attach_task = asyncio.create_task(session.attach(BlockingSendWS()))
    await send_started.wait()
    terminate_task = asyncio.create_task(reg.terminate_attach_token("token"))
    await asyncio.sleep(0)
    terminate_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await terminate_task
    release_send.set()
    with pytest.raises(SessionTerminated):
        await attach_task
    await asyncio.sleep(0.05)
    closed_without_retry = bridge.closed
    await session.close()

    assert closed_without_retry is True
    assert "token" not in reg._sessions


@pytest.mark.asyncio
async def test_cancelled_multi_session_termination_starts_every_cleanup():
    reg = make_registry()
    first_bridge = FakeBridge([b""])
    second_bridge = FakeBridge([b""])
    first, _ = await reg.attach_or_spawn("token", spawn=lambda: first_bridge)
    second, _ = await reg.attach_or_spawn(
        "token\0profile\0resume", spawn=lambda: second_bridge
    )
    close_started = asyncio.Event()
    release_close = asyncio.Event()

    class BlockingCloseWS(FakeWS):
        async def close(self, code=1000, reason=""):
            close_started.set()
            await release_close.wait()
            await super().close(code=code, reason=reason)

    await first.attach(BlockingCloseWS())
    await second.attach(FakeWS())
    terminate_task = asyncio.create_task(reg.terminate_attach_token("token"))
    await close_started.wait()
    terminate_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await terminate_task
    release_close.set()
    await asyncio.sleep(0.05)
    closed_without_retry = (first_bridge.closed, second_bridge.closed)
    await asyncio.gather(first.close(), second.close())

    assert closed_without_retry == (True, True)
    assert not reg._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reap_idle", "close_all"])
async def test_cancelled_bulk_registry_cleanup_starts_every_session(operation):
    reg = make_registry()
    release_close = threading.Event()

    class BlockingCloseBridge(FakeBridge):
        def __init__(self):
            super().__init__([b""])
            self.close_started = threading.Event()

        def close(self):
            self.close_started.set()
            release_close.wait(timeout=2)
            super().close()

    bridges = [BlockingCloseBridge(), BlockingCloseBridge()]
    sessions = []
    for index, bridge in enumerate(bridges):
        session, _ = await reg.attach_or_spawn(
            f"token-{index}", spawn=lambda bridge=bridge: bridge
        )
        sessions.append(session)

    for session in sessions:
        session.last_detached_at = 0.0
    reg._ttl = 0

    if operation == "reap_idle":
        cleanup = asyncio.create_task(reg.reap_idle(now=1.0))
    else:
        cleanup = asyncio.create_task(reg.close_all())
    assert await asyncio.to_thread(bridges[0].close_started.wait, 1)
    assert await asyncio.to_thread(bridges[1].close_started.wait, 1)
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    release_close.set()
    await asyncio.gather(*(session.close() for session in sessions))

    assert all(bridge.closed for bridge in bridges)
    assert not reg._sessions


@pytest.mark.asyncio
async def test_terminated_token_tombstones_use_fixed_memory():
    reg = make_registry()
    storage_size = len(reg._terminated_tokens._bits)
    first = f"{0:032x}"

    for index in range(2000):
        await reg.terminate_attach_token(f"{index:032x}")

    assert len(reg._terminated_tokens._bits) == storage_size
    assert first in reg._terminated_tokens


@pytest.mark.asyncio
async def test_terminated_token_rejects_late_replacement_spawn():
    reg = make_registry()

    assert await reg.terminate_attach_token("token") == 0

    spawned = False

    def spawn():
        nonlocal spawned
        spawned = True
        return FakeBridge([b""])

    with pytest.raises(SessionTerminated):
        await reg.attach_or_spawn("token\0profile\0resume", spawn=spawn)
    assert spawned is False
    assert not reg._sessions
    assert not reg._pending


@pytest.mark.asyncio
async def test_terminate_attach_token_closes_all_qualified_sessions():
    reg = make_registry()
    direct_bridge = FakeBridge([b""])
    scoped_bridge = FakeBridge([b""])
    other_bridge = FakeBridge([b""])
    await reg.attach_or_spawn("token-a", spawn=lambda: direct_bridge)
    await reg.attach_or_spawn(
        "token-a\0profile\0resume",
        spawn=lambda: scoped_bridge,
    )
    await reg.attach_or_spawn("token-b", spawn=lambda: other_bridge)

    terminated = await reg.terminate_attach_token("token-a")

    assert terminated == 2
    assert direct_bridge.closed is True
    assert scoped_bridge.closed is True
    assert other_bridge.closed is False
    assert set(reg._sessions) == {"token-b"}
    await reg.close_all()




@pytest.mark.asyncio
async def test_new_key_at_capacity_raises_when_none_reapable():
    reg = make_registry(max_sessions=1)
    b = FakeBridge([b"", b""])
    s, _ = await reg.attach_or_spawn("a", spawn=lambda: b)
    await s.attach(FakeWS())                    # attached → not reapable
    with pytest.raises(RegistryFull):
        await reg.attach_or_spawn("b", spawn=lambda: FakeBridge([]))
    await reg.close_all()


@pytest.mark.asyncio
async def test_reaper_loop_invokes_reap(monkeypatch):
    from hermes_cli.pty_session import run_reaper
    reg = make_registry()
    calls = {"n": 0}

    async def fake_reap(now=None):
        calls["n"] += 1

    monkeypatch.setattr(reg, "reap_idle", fake_reap)
    task = asyncio.create_task(run_reaper(reg, interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert calls["n"] >= 2
