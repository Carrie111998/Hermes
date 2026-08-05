"""Keep-alive PTY sessions for dashboard terminals.

A PTY process outlives the WebSocket that created it: a single drain task
always reads the PTY into a bounded RingBuffer and forwards to the attached
socket when present. Reconnecting with the same opaque token replays the
buffer and resumes live. See
docs/superpowers/specs/2026-06-20-pty-keepalive-reattach-design.md.
"""
from __future__ import annotations

import asyncio
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
        self._drain_task: Optional[asyncio.Task] = None
        self._lifecycle_lock = asyncio.Lock()
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
            self.buffer.append(chunk)
            ws = self._ws
            if ws is not None:
                try:
                    await ws.send_bytes(chunk)
                except Exception:
                    pass                             # detached mid-send; keep buffering

    async def attach(self, ws) -> None:
        async with self._lifecycle_lock:
            if self._closing:
                raise SessionTerminated(self.key)
            old = self._ws
            if old is not None and old is not ws:
                try:
                    await old.close(code=WS_CLOSE_SUPERSEDED)
                except Exception:
                    pass
            self._ws = ws
            self.attached = True
            self.last_detached_at = None
            snap = self.buffer.snapshot()
            if snap:
                await ws.send_bytes(snap)

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

    async def _finish_close(self, ws) -> None:
        if ws is not None:
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

    async def close(self) -> None:
        async with self._lifecycle_lock:
            task = self._close_task
            if task is None:
                self._closing = True
                self.alive = False
                ws = self._ws
                self._ws = None
                self.attached = False
                task = asyncio.create_task(self._finish_close(ws))
                self._close_task = task
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
        self._generations: Dict[str, int] = {}
        self._terminated_tokens: set[str] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def _base_token(key: str) -> str:
        return key.split("\0", 1)[0]

    async def _spawn_and_register(
        self,
        key: str,
        generation: int,
        spawn: Callable[[], object],
    ) -> PtySession:
        session: Optional[PtySession] = None
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
                    or
                    self._generations.get(self._base_token(key), 0) != generation
                )
                if not terminated:
                    self._sessions[key] = session
            if terminated:
                await session.close()
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
        to_close: list[PtySession] = []
        async with self._lock:
            if self._base_token(key) in self._terminated_tokens:
                raise SessionTerminated(key)
            existing = self._sessions.get(key)
            if existing is not None and existing.alive:
                return existing, False
            if existing is not None:                   # dead remnant
                self._sessions.pop(key, None)
                to_close.append(existing)

            task = self._pending.get(key)
            created = task is None
            if task is None:
                if len(self._sessions) + len(self._pending) >= self._max:
                    to_close.append(self._reap_one_idle_or_raise())
                generation = self._generations.get(self._base_token(key), 0)
                task = asyncio.create_task(
                    self._spawn_and_register(key, generation, spawn)
                )
                self._pending[key] = task

        for session in to_close:
            await session.close()
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
            self._generations[token] = self._generations.get(token, 0) + 1
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
        for session in sessions:
            await session.close()
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
        for session in sessions:
            await session.close()

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
            for key in self._pending:
                token = self._base_token(key)
                self._terminated_tokens.add(token)
                self._generations[token] = self._generations.get(token, 0) + 1
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.close()
