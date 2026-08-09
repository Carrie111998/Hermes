"""Protocol and failure isolation for read-only observer collectors."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from plugins.agentops.control.observer_models import (
    CollectionBatch,
    CollectorHealth,
    LogCursor,
    Target,
    utc_now,
)


class Collector(Protocol):
    name: str

    def collect(self, target: Target, cursor: LogCursor | None = None) -> CollectionBatch: ...


def failed_batch(target: Target, collector: str, reason: str) -> CollectionBatch:
    """Return a non-echoing failure record rather than leaking collector input."""
    return CollectionBatch(
        target_id=target.target_id,
        collector=collector,
        collected_at=utc_now(),
        signals=(),
        health=CollectorHealth(healthy=False, reason=reason),
    )


def collect_all(
    target: Target,
    collectors: Iterable[Collector],
    cursors: Mapping[str, LogCursor] | None = None,
) -> tuple[CollectionBatch, ...]:
    """Contain a collector failure and suppress duplicate signal identities."""
    batches: list[CollectionBatch] = []
    known_signal_ids: set[str] = set()
    cursor_by_collector = {} if cursors is None else dict(cursors)
    for collector in collectors:
        name = getattr(collector, "name", "unknown")
        try:
            batch = collector.collect(target, cursor_by_collector.get(name))
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
                )
        except TimeoutError:
            batch = failed_batch(target, name, "collector_timeout")
        except Exception:
            batch = failed_batch(target, name, "collector_failed")
        batches.append(batch)
    return tuple(batches)
