from __future__ import annotations

import math
import os
from typing import Any, Mapping

from .models import Provider, canonical_session_id

_VISIBILITY_ORIGIN_PREFIX = "claude-visibility:"


class _MirrorFloatSkip(Exception):
    """Internal: this mirror cannot be floated safely; count it as skipped."""


class ClaudeMirrorFloatWorker:
    """Float Claude visibility mirrors on source-session activity.

    Claude Code orders its session list by transcript-file recency, and
    visibility mirrors are written once at registration, so an active source
    session's mirror sinks below newer native sessions. This worker sets each
    visible mirror's file mtime to its source session's ``last_active`` —
    keeping ordering faithful to real source activity without writing any
    transcript content. Setting mtime to the source activity time (never
    "now") means repeated cycles are naturally idempotent, and the minimum
    interval bounds filesystem churn for continuously active sources.
    """

    def __init__(self, store: Any, *, min_interval_seconds: float = 900.0) -> None:
        interval = float(min_interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("min_interval_seconds must be finite and positive")
        self._store = store
        self._min_interval_seconds = interval

    def run_once(self) -> dict[str, int]:
        examined = floated = skipped = 0
        for row in self._store.list_visible_claude_visibility_mirrors():
            examined += 1
            try:
                if self._float_one(row):
                    floated += 1
            except (_MirrorFloatSkip, OSError, TypeError, ValueError, KeyError):
                skipped += 1
        return {"examined": examined, "floated": floated, "skipped": skipped}

    def _float_one(self, row: Mapping[str, Any]) -> bool:
        activity = self._resolve_source_activity(str(row["source_session_id"]))
        mirror = self._store.get_external_session(
            canonical_session_id(Provider.CLAUDE, str(row["claude_uuid"]))
        )
        if not isinstance(mirror, Mapping):
            raise _MirrorFloatSkip("mirror catalog row missing")
        origin_bridge_id = mirror.get("origin_bridge_id")
        if not (
            isinstance(origin_bridge_id, str)
            and origin_bridge_id.startswith(_VISIBILITY_ORIGIN_PREFIX)
        ):
            raise _MirrorFloatSkip("mirror is not a visibility mirror")
        native_path = mirror.get("native_path")
        if not isinstance(native_path, str) or not native_path:
            raise _MirrorFloatSkip("mirror has no native path")
        mtime = os.stat(native_path).st_mtime
        if activity - mtime < self._min_interval_seconds:
            return False
        os.utime(native_path, (activity, activity))
        return True

    def _resolve_source_activity(self, source_session_id: str) -> float:
        if ":" in source_session_id:
            # External (codex/claude) sources carry an indexed watermark.
            activity = self._store.get_external_activity(source_session_id)
        else:
            # Hermes sources are host-native rows in the local SessionDB.
            activity = self._hermes_last_active(source_session_id)
        if (
            not isinstance(activity, (int, float))
            or isinstance(activity, bool)
            or not math.isfinite(float(activity))
        ):
            raise _MirrorFloatSkip("source activity unavailable")
        return float(activity)

    def _hermes_last_active(self, source_session_id: str) -> float | None:
        rows = self._store.db.list_sessions_rich(
            id_query=source_session_id, limit=5, min_message_count=0
        )
        for row in rows:
            if row.get("id") == source_session_id:
                return row.get("last_active")
        return None
