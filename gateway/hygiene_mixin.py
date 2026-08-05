"""Gateway session-hygiene compression helpers (shard s1 cluster c3).

Extracted verbatim from gateway/run.py (shard s1 cluster c3, wave 1).
Module-level functions re-imported into gateway.run so every call site
and monkeypatch contract (``gateway.run._record_hygiene_cooldown`` etc.)
is unchanged.  ``_stamp_hygiene_compression_provenance`` stays in
gateway.run (claimed by open PR #77722).  ``logger`` is bound to the
same logger name as gateway.run's module logger so log records keep
their origin.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("gateway.run")


def _record_hygiene_cooldown(gateway, session_id: str, cooldown_seconds: float) -> None:
    """Persist a session-hygiene compression-failure cooldown to the state DB.

    Uses the same ``compression_failure_cooldown_until`` column and
    ``record_compression_failure_cooldown`` method that the in-conversation
    compression path (``agent/context_compressor.py``) already uses, so the
    cooldown survives gateway restarts (#74136).
    """
    import time as _time
    session_db = getattr(gateway, "_session_db", None)
    if session_db is None:
        return
    session_db = getattr(session_db, "_db", session_db)
    recorder = getattr(session_db, "record_compression_failure_cooldown", None)
    if recorder is None:
        return
    try:
        recorder(session_id, _time.time() + cooldown_seconds)
    except Exception as exc:
        logger.debug("session hygiene cooldown persist failed: %s", exc)


def _seed_hygiene_system_prompt(
    agent: Any,
    session_row: Optional[Dict[str, Any]],
) -> bool:
    """Keep gateway hygiene from rebuilding a live session's system prompt.

    The hygiene helper intentionally skips memory-provider initialization.
    Compression is allowed to persist a system prompt, so letting that helper
    rebuild one would strip external provider blocks from the live session.
    Seed the exact persisted prompt instead.  When no usable prompt can be
    restored, seed an empty cache entry.  Compression either preserves that
    unusable value or rebuilds with the hygiene-only platform marker; the real
    turn will rebuild either form with its fully initialized providers.
    """
    stored_prompt = ""
    if isinstance(session_row, dict):
        raw_prompt = session_row.get("system_prompt")
        if isinstance(raw_prompt, str) and raw_prompt.strip():
            stored_prompt = raw_prompt

    agent._cached_system_prompt = stored_prompt
    return bool(stored_prompt)
