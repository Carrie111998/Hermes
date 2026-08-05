"""Keep-alive PTY sessions for dashboard terminals.

A PTY process outlives the WebSocket that created it: a single drain task
always reads the PTY into a bounded RingBuffer and forwards to the attached
socket when present. Reconnecting with the same opaque token replays the
buffer and resumes live. See
docs/superpowers/specs/2026-06-20-pty-keepalive-reattach-design.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Callable, Dict, Optional, Tuple

WS_CLOSE_PROCESS_EXITED = 4410
WS_CLOSE_SUPERSEDED = 4409
WS_CLOSE_TERMINATED = 4411


class RingBuffer:
    """Keeps only the most recent ``capacity`` bytes appended to it."""

    def __init__(self, capacity: int) -> None:
        self._cap = capacity
        self._buf = bytearray()
        self._truncated = False

    def append(self, data: bytes) -> None:
        self._buf.extend(data)
        overflow = len(self._buf) - self._cap
        if overflow > 0:
            del self._buf[:overflow]
            self._truncated = True

    def snapshot(self) -> bytes:
        return bytes(self._buf)

    @property
    def truncated(self) -> bool:
        return self._truncated


class TokenTombstones:
    """Fixed-memory process-lifetime revocation filter with no false negatives."""

    def __init__(self, byte_size: int = 1 << 20) -> None:
        self._bits = bytearray(byte_size)
        self._bit_count = byte_size * 8

    def _indices(self, token: str):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
        for offset in range(0, 16, 4):
            yield int.from_bytes(digest[offset:offset + 4], "big") % self._bit_count

    def add(self, token: str) -> None:
        for index in self._indices(token):
            self._bits[index >> 3] |= 1 << (index & 7)

    def __contains__(self, token: object) -> bool:
        if not isinstance(token, str):
            return False
        return all(
            self._bits[index >> 3] & (1 << (index & 7))
            for index in self._indices(token)
        )


class PtySession:
    def __init__(self, key: str, bridge, *, buffer_cap: int, read_timeout: float) -> None:
        self.key = key
        self.bridge = bridge
        self.buffer = RingBuffer(buffer_cap)
        self.alive = True
        self.attached = False
        self.last_detached_at: Optional[float] = None
        self._read_timeout = read_timeout
        self._ws = None
        self._attaching_ws = None
        self._drain_task: Optional[asyncio.Task] = None
        self._output_lock = asyncio.Lock()
        self._closing = False
        self._close_task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            chunk = await loop.run_in_executor(None, self.bridge.read, self._read_timeout)
            if chunk is None:                       # EOF — the agent process exited
                self.alive = False
                ws = self._ws
                if ws is not None:
                    try:
                        await ws.close(code=WS_CLOSE_PROCESS_EXITED)
                    except Exception:
                        pass
                return
            if not chunk:                            # idle tick
                await asyncio.sleep(0)
                continue
            async with self._output_lock:
                self.buffer.append(chunk)
                ws = self._ws
                if ws is not None:
                    try:
                        await ws.send_bytes(chunk)
                    except Exception:
                        pass                         # detached mid-send; keep buffering

    async def attach(self, ws) -> None:
        # Drain forwarding shares this barrier, so buffered replay is always
        # delivered before bytes read concurrently from the live PTY.
        async with self._output_lock:
            if self._closing:
                raise SessionTerminated(self.key)
            old = self._ws
            self._attaching_ws = ws
            try:
                if old is not None and old is not ws:
                    try:
                        await old.close(code=WS_CLOSE_SUPERSEDED)
                    except Exception:
                        pass
                snap = self.buffer.snapshot()
                if snap:
                    await ws.send_bytes(snap)
                if self._closing:
                    raise SessionTerminated(self.key)
                self._ws = ws
                self._attaching_ws = None
                self.attached = True
                self.last_detached_at = None
            except BaseException:
                if self._attaching_ws is ws:
                    self._attaching_ws = None
                if self._ws is old:
                    self._ws = None
                    self.attached = False
                    self.last_detached_at = time.monotonic()
                raise

    def detach(self, ws) -> None:
        # Only the currently-attached socket may mark the session detached.
        # A superseded socket's handler also calls detach on its way out
        # (its ``finally`` runs after the new tab attached); flipping
        # ``attached`` then would make a session with a live viewer look
        # idle and reapable.
        if self._ws is not ws:
            return
        self._ws = None
        self.attached = False
        self.last_detached_at = time.monotonic()

    async def _finish_close(self, sockets) -> None:
        for ws in sockets:
            try:
                await ws.close(code=WS_CLOSE_TERMINATED)
            except Exception:
                pass
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            # bridge.close() joins the child — blocking; keep it off the
            # event loop (#53227).
            await asyncio.to_thread(self.bridge.close)
        except Exception:
            pass

    def begin_close(self) -> asyncio.Task[None]:
        """Atomically start idempotent cleanup before any cancellation point."""
        task = self._close_task
        if task is None:
            self._closing = True
            self.alive = False
            sockets = []
            for ws in (self._ws, self._attaching_ws):
                if ws is not None and ws not in sockets:
                    sockets.append(ws)
            self._ws = None
            self._attaching_ws = None
            self.attached = False
            task = asyncio.create_task(self._finish_close(sockets))
            self._close_task = task
        return task

    async def close(self) -> None:
        task = self.begin_close()
        # Caller cancellation must not cancel process cleanup. Concurrent and
        # retrying callers all wait for the same idempotent close operation.
        await asyncio.shield(task)

class RegistryFull(Exception):
    pass


class SessionTerminated(Exception):
    """An attach was explicitly terminated while its PTY was spawning."""


async def run_reaper(registry: "PtySessionRegistry", *, interval: float = 60.0) -> None:
    """Periodically reap idle/dead keep-alive sessions. Cancelled on shutdown."""
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.reap_idle()
        except Exception:
            pass


class PtySessionRegistry:
    def __init__(self, *, ttl: float, max_sessions: int,
                 buffer_cap: int, read_timeout: float) -> None:
        self._ttl = ttl
        self._max = max_sessions
        self._buffer_cap = buffer_cap
        self._read_timeout = read_timeout
        self._sessions: Dict[str, PtySession] = {}
        self._pending: Dict[str, asyncio.Task[PtySession]] = {}
        self._terminated_tokens = TokenTombstones()
        self._lock = asyncio.Lock()

    @staticmethod
    def _base_token(key: str) -> str:
        return key.split("\0", 1)[0]


    @staticmethod
    async def _await_tasks(tasks: list[asyncio.Task]) -> None:
        if tasks:
            await asyncio.gather(
                *(asyncio.shield(task) for task in tasks),
                return_exceptions=True,
            )

    async def _spawn_and_register(
        self,
        key: str,
        spawn: Callable[[], object],
    ) -> PtySession:
        session: Optional[PtySession] = None
        close_task: Optional[asyncio.Task[None]] = None
        try:
            bridge = await asyncio.to_thread(spawn)
            session = PtySession(
                key,
                bridge,
                buffer_cap=self._buffer_cap,
                read_timeout=self._read_timeout,
            )
            await session.start()
            async with self._lock:
                terminated = (
                    self._base_token(key) in self._terminated_tokens
                )
                if not terminated:
                    self._sessions[key] = session
                else:
                    close_task = session.begin_close()
            if terminated:
                assert close_task is not None
                await asyncio.shield(close_task)
                raise SessionTerminated(key)
            return session
        finally:
            current = asyncio.current_task()
            async with self._lock:
                if self._pending.get(key) is current:
                    self._pending.pop(key, None)

    async def attach_or_spawn(self, key: str, *, spawn: Callable[[], object]
                              ) -> Tuple[PtySession, bool]:
        await self.reap_idle()
        close_tasks: list[asyncio.Task[None]] = []
        async with self._lock:
            if self._base_token(key) in self._terminated_tokens:
                raise SessionTerminated(key)
            existing = self._sessions.get(key)
            if existing is not None and existing.alive:
                return existing, False
            if existing is not None:                   # dead remnant
                self._sessions.pop(key, None)
                close_tasks.append(existing.begin_close())

            task = self._pending.get(key)
            created = task is None
            if task is None:
                if len(self._sessions) + len(self._pending) >= self._max:
                    close_tasks.append(self._reap_one_idle_or_raise().begin_close())
                task = asyncio.create_task(
                    self._spawn_and_register(key, spawn)
                )
                self._pending[key] = task

        await self._await_tasks(close_tasks)
        # A disconnected creator must not cancel a spawn that another attach
        # is already awaiting. Explicit termination uses the generation check.
        return await asyncio.shield(task), created

    def detach(self, key: str, ws) -> None:
        s = self._sessions.get(key)
        if s is not None:
            s.detach(ws)

    async def terminate_attach_token(self, token: str) -> int:
        """Close every direct/profile/resume session owned by an attach token."""
        qualified_prefix = f"{token}\0"
        async with self._lock:
            self._terminated_tokens.add(token)
            keys = [
                key
                for key in self._sessions
                if key == token or key.startswith(qualified_prefix)
            ]
            pending_count = sum(
                key == token or key.startswith(qualified_prefix)
                for key in self._pending
            )
            sessions = [self._sessions.pop(key) for key in keys]
            close_tasks = [session.begin_close() for session in sessions]
        await self._await_tasks(close_tasks)
        return len(sessions) + pending_count

    async def reap_idle(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        async with self._lock:
            doomed = [
                key for key, s in self._sessions.items()
                if (not s.alive)
                or (not s.attached and s.last_detached_at is not None
                    and (now - s.last_detached_at) > self._ttl)
            ]
            sessions = [self._sessions.pop(key) for key in doomed]
            close_tasks = [session.begin_close() for session in sessions]
        await self._await_tasks(close_tasks)

    def _reap_one_idle_or_raise(self) -> PtySession:
        idle = [s for s in self._sessions.values()
                if not s.attached and s.last_detached_at is not None]
        if not idle:
            raise RegistryFull()
        oldest = min(idle, key=lambda s: s.last_detached_at or 0.0)
        self._sessions.pop(oldest.key, None)
        return oldest

    async def close_all(self) -> None:
        async with self._lock:
            pending_tasks = list(self._pending.values())
            for key in self._pending:
                token = self._base_token(key)
                self._terminated_tokens.add(token)
            sessions = list(self._sessions.values())
            self._sessions.clear()
            close_tasks = [session.begin_close() for session in sessions]
        # Pending spawns are registry-owned through bridge creation. Their
        # generation/tombstone checks will start and await bridge cleanup.
        await self._await_tasks([*close_tasks, *pending_tasks])
