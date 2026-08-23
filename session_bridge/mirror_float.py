from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .models import Provider, canonical_session_id

_VISIBILITY_ORIGIN_PREFIX = "claude-visibility:"
_BACKUP_MARKERS = (".junction-backup", ".real-", "recovery-backup")
_REPLACE_ATTEMPTS = 3
_REPLACE_RETRY_SECONDS = 0.05
_CLI_SESSION_ID_PATTERN = re.compile(r'"cliSessionId"\s*:\s*"([^"]+)"')
# Sources active within this window register UNARCHIVED so live cross-harness
# work surfaces directly in the desktop sidebar; anything older is historical
# backfill and lands archived (an unarchived default once buried the user's
# real sidebar under thousands of visible imports).
_RECENT_UNARCHIVED_SECONDS = 3 * 86_400


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


def _ccd_user_data_dirs() -> tuple[Path, ...]:
    """Every Claude Code desktop userData dir this machine may run against.

    The subscription harness and the third-party/gateway harness are two modes
    of the same installed app, each with its own userData dir AND its own signed
    in account, so neither one's sidebar can ever show the other's records.
    """
    dirs: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        dirs.append(Path(appdata) / "Claude")
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        dirs.append(Path(local_appdata) / "Claude-3p")
    return tuple(dirs)


def _account_uuid(user_data_dir: Path) -> str | None:
    try:
        raw = (user_data_dir / "config.json").read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get("lastKnownAccountUuid") if isinstance(data, Mapping) else None
    return value if isinstance(value, str) and value else None


def discover_ccd_registry_roots(
    user_data_dirs: Iterable[Path] | None = None,
) -> tuple[Path, ...]:
    """Resolve one live registry leaf per desktop harness.

    Selection is anchored on ``config.json``'s ``lastKnownAccountUuid`` rather
    than "the most populated leaf". The third-party store on this machine holds
    ``.junction-backup-*`` siblings that junction back into the subscription
    store and therefore contain MORE records than the harness's own real
    directory -- a population ranking silently resolves to the wrong harness.
    Rotated/backup siblings are excluded by name, and roots are de-duplicated by
    resolved path so a junctioned harness is written exactly once.
    """
    candidates = (
        tuple(user_data_dirs) if user_data_dirs is not None else _ccd_user_data_dirs()
    )
    roots: list[Path] = []
    seen: set[str] = set()
    for user_data_dir in candidates:
        account = _account_uuid(user_data_dir)
        if not account:
            continue
        account_dir = user_data_dir / "claude-code-sessions" / account
        if not account_dir.is_dir():
            continue
        leaves = [
            leaf
            for leaf in account_dir.iterdir()
            if leaf.is_dir()
            and not any(marker in leaf.name for marker in _BACKUP_MARKERS)
        ]
        if not leaves:
            continue
        leaf = max(leaves, key=lambda path: len(list(path.glob("local_*.json"))))
        try:
            key = os.path.normcase(str(leaf.resolve()))
        except OSError:
            key = os.path.normcase(str(leaf))
        if key in seen:
            continue
        seen.add(key)
        roots.append(leaf)
    return tuple(roots)


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
        registry_roots: Iterable[Path] | None = None,
        id_factory: Callable[[], str] | None = None,
        run_min_interval_seconds: float = 300.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        interval = float(min_interval_seconds)
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("min_interval_seconds must be finite and positive")
        run_interval = float(run_min_interval_seconds)
        if not math.isfinite(run_interval) or run_interval < 0:
            raise ValueError("run_min_interval_seconds must be finite and non-negative")
        if registry_root is not None and not isinstance(registry_root, Path):
            raise TypeError("registry_root must be a Path or None")
        roots: list[Path] = []
        if registry_root is not None:
            roots.append(registry_root)
        for root in registry_roots or ():
            if not isinstance(root, Path):
                raise TypeError("registry_roots must contain Path entries")
            if root not in roots:
                roots.append(root)
        self._store = store
        self._min_interval_seconds = interval
        self._registry_roots = tuple(roots)
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._run_min_interval_seconds = run_interval
        self._monotonic = monotonic
        self._wall_clock = wall_clock
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
        if self._registry_roots:
            registered, record_floated = self._ensure_registry_record(
                canonical_id, claude_uuid, activity, registry_index
            )
            floated = floated or record_floated
        return floated, registered

    @property
    def _registry_root(self) -> Path | None:
        """Back-compat accessor: the primary harness registry root."""
        return self._registry_roots[0] if self._registry_roots else None

    def _ensure_registry_record(
        self,
        canonical_id: str,
        claude_uuid: str,
        activity: float,
        registry_index: dict[Path, dict[str, Path]],
    ) -> tuple[bool, bool]:
        activity_ms = int(activity * 1000)
        session_row: Mapping[str, Any] | None = None
        registered = False
        floated = False
        # Deterministic record id derived from the session's own Claude UUID so
        # every harness store holds the SAME record id for one logical session.
        # A per-harness random id produced cross-harness duplicates once the
        # account union sync spread both variants into every store.
        if session_row is None:
            session_row = self._store.db.get_session(canonical_id) or {}
        record_id = f"local_{claude_uuid}"
        for root in self._registry_roots:
            index = registry_index.setdefault(root, {})
            existing = index.get(claude_uuid)
            if existing is None:
                title = (
                    session_row.get("title")
                    or f"[Bridge] {session_row.get('cwd') or 'untitled session'}"
                )
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
                    # Recently active sources surface unarchived; historical
                    # backfill lands archived (see _RECENT_UNARCHIVED_SECONDS).
                    "isArchived": (
                        self._wall_clock() - activity > _RECENT_UNARCHIVED_SECONDS
                    ),
                    "title": title,
                    "permissionMode": "default",
                    "alwaysAllowedReasons": [],
                    "sessionPermissionUpdates": [],
                }
                path = root / f"{record_id}.json"
                self._write_record(path, record)
                index[claude_uuid] = path
                registered = True
                continue
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
                continue
            record["lastActivityAt"] = activity_ms
            self._write_record(existing, record)
            floated = True
        return registered, floated

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

    def _load_registry_index(self) -> dict[Path, dict[str, Path]]:
        indexes: dict[Path, dict[str, Path]] = {}
        for root in self._registry_roots:
            index: dict[str, Path] = {}
            if root.is_dir():
                for path in root.glob("local_*.json"):
                    try:
                        match = _CLI_SESSION_ID_PATTERN.search(
                            path.read_text(encoding="utf-8")
                        )
                    except OSError:
                        continue
                    if match:
                        index[match.group(1)] = path
            indexes[root] = index
        return indexes

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
