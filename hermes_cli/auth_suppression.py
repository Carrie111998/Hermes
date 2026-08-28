"""Canonical credential-source suppression policy.

This bounded owner keeps deny-list parsing and profile/root composition out of
``hermes_cli.auth`` while that legacy module is fractured under #78637.  The
legacy module retains thin compatibility delegates for existing imports.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def suppressed_sources_for(store: Mapping[str, Any], provider_id: str) -> list[str]:
    """Return source names suppressed for one provider in one auth store.

    Writers use a list today, while older stores may contain a mapping keyed by
    source name. Reading is shape-tolerant and never mutates the supplied store.
    """
    suppressed = store.get("suppressed_sources")
    if not isinstance(suppressed, dict):
        return []
    entries = suppressed.get(provider_id)
    if isinstance(entries, dict):
        return [str(name) for name in entries]
    if isinstance(entries, (list, tuple, set)):
        return [str(name) for name in entries]
    return []


def suppression_union(
    local_store: Mapping[str, Any],
    global_store: Mapping[str, Any],
    provider_id: str,
) -> set[str]:
    """Return the union of local and root deny-lists for ``provider_id``."""
    return set(suppressed_sources_for(local_store, provider_id)) | set(
        suppressed_sources_for(global_store, provider_id)
    )


def source_is_suppressed(
    local_store: Mapping[str, Any],
    global_store: Mapping[str, Any],
    provider_id: str,
    source: str,
) -> bool:
    """Return whether either scope denies ``source`` for ``provider_id``."""
    return source in suppression_union(local_store, global_store, provider_id)


def filter_suppressed_entries(
    entries: list[Any], suppressed_sources: set[str]
) -> list[Any]:
    """Remove global fallback entries whose explicit source is denied."""
    if not suppressed_sources:
        return list(entries)
    return [
        entry
        for entry in entries
        if not (
            isinstance(entry, dict)
            and entry.get("source") in suppressed_sources
        )
    ]
