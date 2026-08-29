"""Resource-aware, FIFO admission control for gateway agent turns."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def host_available_memory_mb(meminfo_path: Path = Path("/proc/meminfo")) -> Optional[int]:
    """Return Linux MemAvailable in MiB, or None when unavailable."""
    try:
        for line in meminfo_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def cgroup_available_memory_mb(
    cgroup_file: Path = Path("/proc/self/cgroup"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> Optional[int]:
    """Return remaining cgroup-v2 MemoryMax headroom in MiB."""
    try:
        relative = next(
            line.partition("::")[2].lstrip("/")
            for line in cgroup_file.read_text(encoding="utf-8").splitlines()
            if line.startswith("0::")
        )
        root = cgroup_root / relative
        raw_max = (root / "memory.max").read_text(encoding="utf-8").strip()
        raw_current = (root / "memory.current").read_text(encoding="utf-8").strip()
        if not raw_max.isdigit() or not raw_current.isdigit():
            return None
        return max(0, int(raw_max) - int(raw_current)) // (1024 * 1024)
    except (OSError, StopIteration, ValueError):
        return None


def available_resource_memory_mb() -> Optional[int]:
    """Return the tighter of host and current-cgroup memory headroom."""
    candidates = [host_available_memory_mb(), cgroup_available_memory_mb()]
    finite = [value for value in candidates if value is not None]
    return min(finite) if finite else None


@dataclass(frozen=True)
class AdmissionSnapshot:
    active: int
    queued: int
    max_parallel: Optional[int]
    available_memory_mb: Optional[int]
    host_available_memory_mb: Optional[int]
    cgroup_available_memory_mb: Optional[int]
    min_headroom_mb: int
    active_task_ids: tuple[str, ...]
    queued_task_ids: tuple[str, ...]


class AdmissionRejected(RuntimeError):
    """Raised when the bounded queue is full or the controller is closing."""


class AgentAdmissionController:
    """Admit new turns FIFO without disturbing already-running work."""

    def __init__(
        self,
        *,
        max_parallel: Optional[int],
        min_headroom_mb: int = 0,
        queue_limit: int = 32,
        poll_interval_seconds: float = 2.0,
        memory_reader: Callable[[], Optional[int]] = available_resource_memory_mb,
    ) -> None:
        self.max_parallel = max_parallel if max_parallel and max_parallel > 0 else None
        self.min_headroom_mb = max(0, int(min_headroom_mb or 0))
        self.queue_limit = max(0, int(queue_limit or 0))
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds or 2.0))
        self._memory_reader = memory_reader
        self._condition = asyncio.Condition()
        self._active: set[str] = set()
        self._queue: list[str] = []
        self._closed_reason: Optional[str] = None

    def snapshot(self) -> AdmissionSnapshot:
        available = self._memory_reader()
        return AdmissionSnapshot(
            active=len(self._active),
            queued=len(self._queue),
            max_parallel=self.max_parallel,
            available_memory_mb=available,
            host_available_memory_mb=host_available_memory_mb(),
            cgroup_available_memory_mb=cgroup_available_memory_mb(),
            min_headroom_mb=self.min_headroom_mb,
            active_task_ids=tuple(sorted(self._active)),
            queued_task_ids=tuple(self._queue),
        )

    def _capacity_reason(self, available_mb: Optional[int]) -> Optional[str]:
        if self.max_parallel is not None and len(self._active) >= self.max_parallel:
            return f"parallel-agent capacity ({len(self._active)}/{self.max_parallel})"
        if (
            self.min_headroom_mb > 0
            and available_mb is not None
            and available_mb < self.min_headroom_mb
        ):
            return (
                "memory headroom "
                f"({available_mb} MiB available; {self.min_headroom_mb} MiB required)"
            )
        return None

    def _log(self, decision: str, task_id: str, reason: str = "") -> None:
        snap = self.snapshot()
        logger.info(
            "HERMES_ADMISSION %s",
            json.dumps(
                {
                    "decision": decision,
                    "task_id": task_id,
                    "reason": reason,
                    "active_workers": snap.active,
                    "queued_tasks": snap.queued,
                    "max_parallel": snap.max_parallel,
                    "available_memory_mb": snap.available_memory_mb,
                    "host_available_memory_mb": snap.host_available_memory_mb,
                    "cgroup_available_memory_mb": snap.cgroup_available_memory_mb,
                    "min_headroom_mb": snap.min_headroom_mb,
                },
                sort_keys=True,
            ),
        )

    async def acquire(
        self,
        task_id: str,
        *,
        on_queued: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        """Wait for capacity and reserve a slot for *task_id*."""
        task_id = str(task_id)
        queued_notice_sent = False
        async with self._condition:
            try:
                while True:
                    if self._closed_reason is not None:
                        raise AdmissionRejected(self._closed_reason)
                    available = self._memory_reader()
                    at_front = not self._queue or self._queue[0] == task_id
                    reason = self._capacity_reason(available)
                    if at_front and reason is None:
                        if self._queue and self._queue[0] == task_id:
                            self._queue.pop(0)
                        self._active.add(task_id)
                        self._log("start", task_id)
                        return
                    if task_id not in self._queue:
                        if self.queue_limit == 0 or len(self._queue) >= self.queue_limit:
                            self._log("reject", task_id, "queue full")
                            raise AdmissionRejected(
                                f"Agent queue is full ({len(self._queue)}/{self.queue_limit}). "
                                "Existing tasks are still running; please retry later."
                            )
                        self._queue.append(task_id)
                        reason = reason or "waiting for earlier queued task"
                        self._log("queue", task_id, reason)
                    if not queued_notice_sent and on_queued is not None:
                        queued_notice_sent = True
                        position = self._queue.index(task_id) + 1
                        notice = f"Queued: system at {reason or 'safe capacity'}; position {position}."
                        # Do not hold the controller lock during network I/O.
                        self._condition.release()
                        try:
                            await on_queued(notice)
                        except Exception as exc:
                            logger.warning(
                                "Could not deliver admission queue notice for %s: %s",
                                task_id,
                                exc,
                            )
                        finally:
                            await self._condition.acquire()
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(), timeout=self.poll_interval_seconds
                        )
                    except asyncio.TimeoutError:
                        pass
            except BaseException:
                if task_id in self._queue:
                    self._queue.remove(task_id)
                    self._condition.notify_all()
                raise

    async def release(self, task_id: str, *, outcome: str = "finished") -> None:
        async with self._condition:
            self._active.discard(str(task_id))
            self._log(outcome, str(task_id))
            self._condition.notify_all()

    async def close(self, reason: str) -> tuple[str, ...]:
        """Reject waiters explicitly; running turns remain owned by shutdown."""
        async with self._condition:
            self._closed_reason = str(reason or "Gateway is shutting down; resend after restart.")
            queued = tuple(self._queue)
            self._queue.clear()
            self._condition.notify_all()
            return queued
