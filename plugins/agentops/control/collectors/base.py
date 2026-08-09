"""Protocol, deadlines and failure isolation for read-only collectors."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Protocol
import threading


_DETACHED_LIMIT = 4
_detached_lock = threading.Lock()
_detached_workers = 0
_worker_slots = 0

from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    Target,
    utc_now,
)


class Collector(Protocol):
    name: str
    source_id: str

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch: ...


def failed_batch(target: Target, collector: str, reason: str, *, source_id: str = "", worker_detached: bool = False) -> CollectionBatch:
    """Return non-echoing bounded failure evidence, never an exception payload."""
    return CollectionBatch(
        target_id=target.target_id,
        collector=collector,
        collected_at=utc_now(),
        signals=(),
        health=CollectorHealth(healthy=False, reason=reason, worker_detached=worker_detached),
        source_id=source_id,
    )


def collect_all(
    target: Target,
    collectors: Iterable[Collector],
    cursors: Mapping[tuple[str, str], LogCursor] | Mapping[str, LogCursor] | None = None,
    *,
    deadline_seconds: float = 1.0,
) -> tuple[CollectionBatch, ...]:
    """Apply a caller-visible deadline and keep every collector isolated.

    A timed-out worker is never awaited at shutdown. It has no mutable control
    surface; its caller receives a bounded unhealthy batch and the next
    collector still runs.
    """
    if deadline_seconds <= 0:
        raise ValueError("invalid collector deadline")
    batches: list[CollectionBatch] = []
    known_signal_ids: set[str] = set()
    cursor_by_key = {} if cursors is None else dict(cursors)
    for collector in collectors:
        name = getattr(collector, "name", "unknown")
        source_id = getattr(collector, "source_id", "")
        cursor = cursor_by_key.get((name, source_id), cursor_by_key.get(name))
        global _detached_workers, _worker_slots
        with _detached_lock:
            if _worker_slots >= _DETACHED_LIMIT:
                batches.append(failed_batch(target, name, "collector_timeout_worker_budget", source_id=source_id, worker_detached=True))
                continue
            _worker_slots += 1
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agentops-observer")
        slot_released = False
        try:
            future = executor.submit(collector.collect, target, cursor)
        except Exception:
            with _detached_lock:
                _worker_slots = max(0, _worker_slots - 1)
            executor.shutdown(wait=False, cancel_futures=True)
            batches.append(failed_batch(target, name, "collector_submit_failed", source_id=source_id))
            continue
        try:
            batch = future.result(timeout=min(deadline_seconds, float(getattr(collector, "deadline_seconds", deadline_seconds))))
            if not isinstance(batch, CollectionBatch) or batch.target_id != target.target_id:
                raise ValueError("invalid collection batch")
            unique_signals = tuple(signal for signal in batch.signals if signal.signal_id not in known_signal_ids)
            known_signal_ids.update(signal.signal_id for signal in unique_signals)
            if len(unique_signals) != len(batch.signals):
                batch = CollectionBatch(
                    target_id=batch.target_id,
                    collector=batch.collector,
                    collected_at=batch.collected_at,
                    signals=unique_signals,
                    health=batch.health,
                    next_cursor=batch.next_cursor,
                    source_id=batch.source_id,
                    observation_id=batch.observation_id,
                )
            with _detached_lock:
                _worker_slots = max(0, _worker_slots - 1)
            slot_released = True
        except (FutureTimeout, TimeoutError):
            future.cancel()
            with _detached_lock:
                _detached_workers += 1
            def _release(_future):
                global _detached_workers, _worker_slots
                with _detached_lock:
                    _detached_workers = max(0, _detached_workers - 1)
                    _worker_slots = max(0, _worker_slots - 1)
            future.add_done_callback(_release)
            batch = failed_batch(target, name, "collector_timeout", source_id=source_id, worker_detached=True)
        except Exception:
            if not slot_released:
                with _detached_lock:
                    _worker_slots = max(0, _worker_slots - 1)
            batch = failed_batch(target, name, "collector_failed", source_id=source_id)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        batches.append(batch)
    return tuple(batches)
