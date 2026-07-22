from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from .models import Provider, canonical_session_id

_VISIBILITY_ORIGIN_PREFIX = "claude-visibility:"
_REPLACE_ATTEMPTS = 3
_REPLACE_RETRY_SECONDS = 0.05
_CLI_SESSION_ID_PATTERN = re.compile(r'"cliSessionId"\s*:\s*"([^"]+)"')


def default_ccd_sessions_base() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Claude" / "claude-code-sessions"


def discover_ccd_registry_root(base: Path | None) -> Path | None:
    """Locate the desktop app's session-registry leaf directory.

    The registry lives two opaque scope levels below ``claude-code-sessions``.
    Prefer the leaf that already holds ``local_*.json`` records; fall back to
    a sole leaf directory; refuse to guess when ambiguous.
    """
    if base is None or not base.is_dir():
        return None
    leaves = [path for path in base.glob("*/*") if path.is_dir()]
    scored = sorted(
        ((len(list(leaf.glob("local_*.json"))), str(leaf), leaf) for leaf in leaves),
        reverse=True,
    )
    populated = [entry for entry in scored if entry[0] > 0]
    if populated:
        return populated[0][2]
    if len(leaves) == 1:
        return leaves[0]
    return None


class _MirrorFloatSkip(Exception):
    """Internal: this mirror cannot be floated safely; count it as skipped."""


class ClaudeMirrorFloatWorker:
    """Surface Claude visibility mirrors in the desktop app and float them.

    The Claude Code desktop sidebar lists its own session registry (one
    ``local_*.json`` record per session, linked to the transcript by
    ``cliSessionId``) — not the raw ``~/.claude/projects`` transcripts — so a
    CLI-registered visibility mirror is invisible there until a registry
    record exists. This worker, for every visible mirror:

    - writes a registry record if none references the mirror's Claude UUID
      (idempotent; the desktop picks new records up on its next launch), and
    - floats both the transcript file mtime (CLI resume picker ordering) and
      the record's ``lastActivityAt`` (desktop sidebar ordering) to the
      source session's ``last_active``.

    Setting times to the source activity (never "now") keeps repeated cycles
    idempotent; the minimum interval bounds write churn for continuously
    active sources. Only marker-owned visibility mirrors are ever touched,
    and every per-mirror failure is contained as a skip.
    """

    def __init__(
        self,
        store: Any,
        *,
        min_interval_seconds: float = 900.0,
        registry_root: Path | None = None,
        id_factory: Callable[[], str] | None = None,
        run_min_interval_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        interval = float(min_interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("min_interval_seconds must be finite and positive")
        run_interval = float(run_min_interval_seconds)
        if not math.isfinite(run_interval) or run_interval < 0:
            raise ValueError("run_min_interval_seconds must be finite and non-negative")
        if registry_root is not None and not isinstance(registry_root, Path):
            raise TypeError("registry_root must be a Path or None")
        self._store = store
        self._min_interval_seconds = interval
        self._registry_root = registry_root
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._run_min_interval_seconds = run_interval
        self._monotonic = monotonic
        self._last_run_at: float | None = None

    def run_once(self) -> dict[str, int]:
        now = self._monotonic()
        if (
            self._last_run_at is not None
            and now - self._last_run_at < self._run_min_interval_seconds
        ):
            return {
                "examined": 0,
                "floated": 0,
                "skipped": 0,
                "registered": 0,
                "throttled": 1,
            }
        self._last_run_at = now
        examined = floated = skipped = registered = 0
        registry_index = self._load_registry_index()
        for row in self._store.list_visible_claude_visibility_mirrors():
            examined += 1
            try:
                mirror_floated, mirror_registered = self._float_one(row, registry_index)
            except (_MirrorFloatSkip, OSError, TypeError, ValueError, KeyError):
                skipped += 1
                continue
            floated += int(mirror_floated)
            registered += int(mirror_registered)
        return {
            "examined": examined,
            "floated": floated,
            "skipped": skipped,
            "registered": registered,
            "throttled": 0,
        }

    def _float_one(
        self,
        row: Mapping[str, Any],
        registry_index: dict[str, Path],
    ) -> tuple[bool, bool]:
        claude_uuid = str(row["claude_uuid"])
        activity = self._resolve_source_activity(str(row["source_session_id"]))
        canonical_id = canonical_session_id(Provider.CLAUDE, claude_uuid)
        mirror = self._store.get_external_session(canonical_id)
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

        floated = False
        mtime = os.stat(native_path).st_mtime
        if activity - mtime >= self._min_interval_seconds:
            os.utime(native_path, (activity, activity))
            floated = True

        registered = False
        if self._registry_root is not None:
            registered, record_floated = self._ensure_registry_record(
                canonical_id, claude_uuid, activity, registry_index
            )
            floated = floated or record_floated
        return floated, registered

    def _ensure_registry_record(
        self,
        canonical_id: str,
        claude_uuid: str,
        activity: float,
        registry_index: dict[str, Path],
    ) -> tuple[bool, bool]:
        activity_ms = int(activity * 1000)
        existing = registry_index.get(claude_uuid)
        if existing is None:
            session_row = self._store.db.get_session(canonical_id) or {}
            record_id = f"local_{self._id_factory()}"
            title = session_row.get("title") or f"[Bridge] {claude_uuid}"
            cwd = session_row.get("cwd") or ""
            started_at = session_row.get("started_at")
            created_ms = (
                int(float(started_at) * 1000)
                if isinstance(started_at, (int, float))
                and not isinstance(started_at, bool)
                and math.isfinite(float(started_at))
                else activity_ms
            )
            record = {
                "sessionId": record_id,
                "cliSessionId": claude_uuid,
                "cwd": cwd,
                "originCwd": cwd,
                "createdAt": created_ms,
                "lastActivityAt": activity_ms,
                "model": session_row.get("model") or "claude-fable-5",
                "isArchived": False,
                "title": title,
                "permissionMode": "default",
                "alwaysAllowedReasons": [],
                "sessionPermissionUpdates": [],
            }
            path = self._registry_root / f"{record_id}.json"
            self._write_record(path, record)
            registry_index[claude_uuid] = path
            return True, False
        try:
            record = json.loads(existing.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise _MirrorFloatSkip("registry record unreadable") from None
        recorded_ms = record.get("lastActivityAt")
        if (
            not isinstance(recorded_ms, (int, float))
            or isinstance(recorded_ms, bool)
            or not math.isfinite(float(recorded_ms))
        ):
            recorded_ms = 0
        if activity_ms - float(recorded_ms) < self._min_interval_seconds * 1000:
            return False, False
        record["lastActivityAt"] = activity_ms
        self._write_record(existing, record)
        return False, True

    def _write_record(self, path: Path, record: Mapping[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(record, separators=(",", ":")), encoding="utf-8"
        )
        last_error: OSError | None = None
        for _attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                return
            except OSError as exc:
                last_error = exc
                time.sleep(_REPLACE_RETRY_SECONDS)
        temporary.unlink(missing_ok=True)
        raise last_error if last_error is not None else OSError("replace failed")

    def _load_registry_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        if self._registry_root is None or not self._registry_root.is_dir():
            return index
        for path in self._registry_root.glob("local_*.json"):
            try:
                match = _CLI_SESSION_ID_PATTERN.search(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if match:
                index[match.group(1)] = path
        return index

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
