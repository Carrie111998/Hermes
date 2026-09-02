"""Lossless text-batch merge primitives for the SimpleX adapter."""

from __future__ import annotations

from typing import Any

from gateway.event_sidecars import CORRELATED_MESSAGE_ITEMS_KEY


def prepend_cancelled_batch(cancelled: Any, newer: Any | None) -> Any:
    """Restore a cancelled in-flight event ahead of any newly queued event.

    The cancelled event remains the aggregate holder so its source,
    timestamp, and first message ID stay authoritative. Per-item metadata is
    merged with the text/media payload; later edit events therefore retain a
    stable map back to every SimpleX chat item in the batch.
    """
    if newer is None:
        return cancelled

    if newer.text:
        cancelled.text = (
            f"{cancelled.text}\n{newer.text}"
            if cancelled.text
            else newer.text
        )
    if newer.media_urls:
        cancelled.media_urls.extend(newer.media_urls)
        cancelled.media_types.extend(newer.media_types)

    cancelled.metadata = dict(cancelled.metadata or {})
    newer_metadata = dict(newer.metadata or {})
    cancelled_items = cancelled.metadata.setdefault(CORRELATED_MESSAGE_ITEMS_KEY, [])
    newer_items = newer_metadata.get(CORRELATED_MESSAGE_ITEMS_KEY, [])
    if isinstance(cancelled_items, list) and isinstance(newer_items, list):
        cancelled_items.extend(newer_items)
    return cancelled
