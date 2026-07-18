"""File-locked PipelineManager with append-history + current-state semantics.

Cross-platform locking: fcntl on Unix, msvcrt on Windows. If neither is
available (shouldn't happen), we still atomic-write via temp-file-plus-rename
so the file never corrupts — the worst case is a concurrent writer's change
gets overwritten. Single-user laptop deployment, small race window; good enough.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

PIPELINE_PATH = Path.home() / ".hermes" / "workspaces" / "tracker" / "pipeline.json"

# Bounded retry for the atomic replace in _write_atomic(). On Windows a
# concurrent lock-free reader makes os.replace() fail transiently with
# WinError 5; total worst-case wait is 20+40+60+80 = 200ms across 5 attempts.
_REPLACE_MAX_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.02


# Lazy-cached EventBus singleton — loaded once per process. Used by
# update_stage() to emit STAGE_TRANSITION events without forcing every
# caller to plumb a bus instance through. False sentinel marks "tried
# and failed" so we don't retry on every call.
_event_bus_cache: Any = None


def _get_event_bus() -> Optional[Any]:
    """Return a process-wide EventBus, or None if unavailable.

    PipelineManager is invoked from CLI scripts, agent prompts, and the
    gateway alike; the events package may not be importable in all those
    contexts (e.g. detached CLI without HERMES_HOME). Lazy-import + cache
    keeps the cost amortized and the failure mode silent.
    """
    global _event_bus_cache
    if _event_bus_cache is False:
        return None
    if _event_bus_cache is not None:
        return _event_bus_cache
    try:
        from events.bus import EventBus
        _event_bus_cache = EventBus()
        return _event_bus_cache
    except Exception as exc:
        logger.debug("PipelineManager: EventBus unavailable (%s); stage emit disabled", exc)
        _event_bus_cache = False
        return None

# Canonical stages — matches the dashboard's PROTECTED_STAGES + settings.stages
# in pipeline.json. Additional stages may appear in history but these are the
# ones we validate on update_stage() calls.
VALID_STAGES = (
    "discovered",
    "scored",
    "review",
    "approved",
    "tailoring",
    "ready_to_submit",
    "awaiting_approval",  # LangGraph HITL pause
    "submitted",
    "applied",
    "final_submission",
    "responded",
    "interview",
    "offer",
    "rejected",
    "rejected_by_user",  # Diego rejected HITL
    "archived",
)

# Source values we understand. Any string is accepted, but these are the ones
# mapped to the history.source audit field. Reject unknown sources to catch typos.
KNOWN_SOURCES = (
    "dashboard",           # :3001 jobflow-dashboard
    "legacy_dashboard",    # historical
    "control_center",      # :9120 (new — this file is wiring this)
    "langgraph",           # tracker_update_node
    "telegram",            # future Telegram reply handler
    "whatsapp",            # future WhatsApp reply handler
    "manual_submission",   # Diego did it by hand
    "chrome_extension",    # job-filler extension
    "operator_api",        # apply_packet_operator.py
    "tailor",              # agent
    "matcher",             # agent
    "scout",               # agent
    "applier",             # agent
    "tracker",             # agent
    "critic",              # meta-agent
    "system",              # automated (e.g., archive sweep)
)


# ---------------------------------------------------------------------------
# Cross-platform file lock
# ---------------------------------------------------------------------------

try:
    import fcntl  # Unix
    _HAVE_FCNTL = True
except ImportError:
    _HAVE_FCNTL = False

try:
    import msvcrt  # Windows
    _HAVE_MSVCRT = True
except ImportError:
    _HAVE_MSVCRT = False


@contextlib.contextmanager
def _file_lock(lockpath: Path, timeout: float = 10.0):
    """Acquire an exclusive lock via a sidecar .lock file.

    Using a sidecar instead of locking pipeline.json directly avoids issues
    on Windows where locking can interfere with atomic-rename (the lock
    holder's handle prevents the rename target from being replaced).
    """
    lockpath.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    f = open(lockpath, "ab+")  # ensure file exists
    try:
        while True:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif _HAVE_MSVCRT:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    # No locking primitive available — write path still
                    # atomic via rename, just not serialized.
                    pass
                break
            except (BlockingIOError, OSError):
                if time.monotonic() > deadline:
                    raise TimeoutError(f"could not acquire lock on {lockpath} in {timeout}s")
                time.sleep(0.05)
        yield f
    finally:
        try:
            if _HAVE_FCNTL:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            elif _HAVE_MSVCRT:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            f.close()


# ---------------------------------------------------------------------------
# PipelineManager
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class PipelineManager:
    """Thread/process-safe writer for ~/.hermes/workspaces/tracker/pipeline.json.

    Designed for the write pattern: read → modify → write-atomic. Lock is held
    for the full read→write cycle so concurrent callers can't clobber each
    other's changes (inside this module). External writers that bypass this
    class (e.g. agent scripts that json.dump() directly) remain a race risk
    — documented in the module docstring. We mitigate with atomic rename so
    the file can't corrupt; worst case is a lost write.
    """

    def __init__(self, path: Path = PIPELINE_PATH):
        self.path = Path(path)
        self.lockpath = self.path.with_suffix(self.path.suffix + ".lock")

    # -- low-level I/O --------------------------------------------------------

    def _read(self) -> dict:
        if not self.path.exists():
            return {
                "_schema": "jobflow-pipeline-v1",
                "_description": "Master pipeline state for JobFlow. Single source of truth.",
                "_updated_at": _now_iso(),
                "settings": {},
                "stats": {},
                "jobs": [],
            }
        raw = self.path.read_bytes()
        # Strip BOM if present (older Windows-authored file)
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.exception("pipeline.json parse error; refusing to clobber")
            raise RuntimeError(f"pipeline.json parse error: {e}") from e

    def _write_atomic(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # Write + flush + replace. Preserves a valid pipeline.json on crash.
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        # Windows: os.replace() (MoveFileEx REPLACE_EXISTING) fails with
        # PermissionError WinError 5 (ACCESS_DENIED) if ANY process has the
        # destination open for read at this instant, because CPython opens
        # files without FILE_SHARE_DELETE. Our readers (_read/get_job/
        # list_jobs/stats) are lock-free by design, and cross-process readers
        # (Control Center :9120, JobFlow dashboard :3001) poll this file, so
        # the sub-millisecond replace window genuinely collides in production
        # (observed 2026-07-17: intent-applier dead-letters). The collision is
        # transient — a short bounded retry absorbs it. POSIX rename never
        # hits this (inode swap), so the retry is a no-op there.
        for attempt in range(_REPLACE_MAX_ATTEMPTS):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                if attempt == _REPLACE_MAX_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))

    # -- public API -----------------------------------------------------------

    def get_job(self, job_id: str) -> Optional[dict]:
        """Return the job record (by job_id, then by url if job_id doesn't match).

        Read is lock-free: a reader sees either the previous valid state or the
        new one, never a partial file. But NOTE the Windows caveat — a live read
        handle blocks the writer's os.replace (CPython opens without
        FILE_SHARE_DELETE), so a lock-free read here can make a concurrent
        _write_atomic fail with WinError 5. _write_atomic absorbs that with a
        bounded retry; do not "fix" this by asserting readers never interfere.
        """
        data = self._read()
        for j in data.get("jobs", []) or []:
            if j.get("job_id") == job_id or j.get("id") == job_id:
                return j
            if job_id and j.get("url") and job_id in j["url"]:
                return j
        return None

    def list_jobs(
        self,
        *,
        stage: Optional[str] = None,
        stages_in: Optional[Iterable[str]] = None,
        source: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict]:
        data = self._read()
        jobs = data.get("jobs", []) or []
        if stage:
            jobs = [j for j in jobs if j.get("stage") == stage]
        elif stages_in:
            wanted = set(stages_in)
            jobs = [j for j in jobs if j.get("stage") in wanted]
        if source:
            jobs = [j for j in jobs if j.get("source") == source]
        if limit:
            jobs = jobs[:limit]
        return jobs

    def stats(self) -> dict:
        data = self._read()
        return data.get("stats", {}) or {}

    def update_stage(
        self,
        *,
        job_id: str,
        new_stage: str,
        actor: str,
        source: str,
        notes: str = "",
        metadata: Optional[dict] = None,
    ) -> dict:
        """Transition a job to `new_stage` and append a history entry.

        Creates the job if it doesn't exist yet (unknown-job-upsert). This is
        intentional — LangGraph runs a job through matcher before it's ever
        been in pipeline.json, and the first tracker_update_node call should
        register it. Caller provides title/company/score via `metadata`.

        Args:
            job_id: canonical id. Accepts linkedin-<num>, Scout UUIDs, etc.
            new_stage: one of VALID_STAGES (soft-validated; unknown values log a warning)
            actor: human who took the action ("diego", "main", "applier", ...)
            source: origin surface — should be one of KNOWN_SOURCES
            notes: free-form note for the history entry
            metadata: optional {title, company, score, url, apply_url, location, recommendation, ...}
                      Fields set on the job record if unset or different.

        Returns the updated job record.
        """
        if new_stage not in VALID_STAGES:
            logger.warning("update_stage: new_stage=%r not in VALID_STAGES; accepting anyway", new_stage)
        if source not in KNOWN_SOURCES:
            logger.warning("update_stage: source=%r not in KNOWN_SOURCES; accepting anyway", source)

        with _file_lock(self.lockpath):
            data = self._read()
            jobs = data.setdefault("jobs", [])
            found = None
            for j in jobs:
                if j.get("job_id") == job_id or j.get("id") == job_id:
                    found = j
                    break

            now = _now_iso()
            if found is None:
                # Upsert the minimal shape
                found = {
                    "job_id": job_id,
                    "added_at": now,
                    "history": [],
                    "source": source,
                }
                jobs.append(found)
                logger.info("pipeline_state: upserted new job %s (actor=%s source=%s)", job_id, actor, source)

            # Apply metadata non-destructively (don't overwrite existing fields)
            if metadata:
                for k, v in metadata.items():
                    if v is None:
                        continue
                    # Score: overwrite if newer one is provided (scoring can re-run)
                    if k == "score" or not found.get(k):
                        found[k] = v

            # Append history
            entry = {
                "stage": new_stage,
                "at": now,
                "agent": actor,
                "source": source,
            }
            if notes:
                entry["notes"] = notes
            history = found.setdefault("history", [])
            history.append(entry)

            # Update current-state fields
            prior_stage = found.get("stage")
            found["stage"] = new_stage
            found["stage_updated_at"] = now

            # Keep stats roughly fresh. The dashboard recomputes on its own
            # schedule; we just bump the relevant counter.
            stats = data.setdefault("stats", {})
            key = f"total_{new_stage}"
            if key in stats:
                stats[key] = int(stats.get(key, 0)) + 1

            data["_updated_at"] = now

            self._write_atomic(data)

            logger.info(
                "pipeline_state: %s %s -> %s (actor=%s source=%s)",
                job_id, prior_stage, new_stage, actor, source,
            )

            # Emit STAGE_TRANSITION event for downstream subscribers
            # (Telegram, WhatsApp, dashboard live-sync, Tracker Intent
            # Applier). Wrapped in try/except so a bus failure never
            # blocks the pipeline write — pipeline.json IS canonical.
            # Dashboard ↔ comms wiring (Phase 4 of 2026-04-30 plan):
            # this emit covers BOTH Control Center (port 9120) and
            # JobFlow Dashboard (port 3001/3002) because they both
            # ultimately call update_stage().
            self._emit_stage_transition(
                job_id=job_id,
                prior_stage=prior_stage,
                new_stage=new_stage,
                actor=actor,
                source=source,
                notes=notes,
                metadata={
                    "title": found.get("title"),
                    "company": found.get("company"),
                    "score": found.get("score"),
                    "url": found.get("url"),
                },
            )

            return dict(found)  # defensive copy

    def _emit_stage_transition(
        self,
        *,
        job_id: str,
        prior_stage: Optional[str],
        new_stage: str,
        actor: str,
        source: str,
        notes: str,
        metadata: dict,
    ) -> None:
        """Best-effort STAGE_TRANSITION event emission.

        Lazy-imports EventBus to avoid a startup dependency on the events
        package (PipelineManager is also called from contexts where the bus
        is not initialized — e.g. CLI scripts). All failures degrade to a
        debug log; the canonical pipeline.json write has already succeeded
        when this runs, so observability loss is safe.
        """
        try:
            bus = _get_event_bus()
            if bus is None:
                return
            from events.schema import EventType, Priority
            payload = {
                "job_id": job_id,
                "prior_stage": prior_stage,
                "new_stage": new_stage,
                "actor": actor,
                "source_surface": source,
            }
            if notes:
                payload["notes"] = notes
            for k, v in (metadata or {}).items():
                if v is not None and k not in payload:
                    payload[k] = v

            # Priority bump: dashboard-originated transitions (Diego
            # explicitly clicked approve/reject/stage) and protected-
            # stage transitions are HIGH so they pass significant_only
            # verbosity filters in jobflow_firehose. Agent-driven
            # transitions (matcher → tailor → applier) stay NORMAL so
            # the firehose doesn't drown the human signal.
            human_surfaces = {
                "control_center",
                "legacy_dashboard",
                "dashboard",
                "telegram",
                "whatsapp",
                "manual_submission",
            }
            protected_terminal_stages = {
                "approved",
                "rejected_by_user",
                "submitted",
                "applied",
                "final_submission",
                "interview",
                "offer",
                "responded",
            }
            priority = (
                Priority.HIGH
                if (source in human_surfaces or actor == "diego"
                    or new_stage in protected_terminal_stages)
                else None  # use EventType default (NORMAL)
            )

            bus.emit(
                event_type=EventType.STAGE_TRANSITION,
                source=f"pipeline:{source}",
                payload=payload,
                priority=priority,
                correlation_id=job_id,
                job_id=job_id,
            )
        except Exception as exc:
            logger.debug("PipelineManager: stage_transition emit failed: %s", exc)

    def upsert_metadata(
        self,
        *,
        job_id: str,
        metadata: dict,
        actor: str = "system",
        source: str = "system",
    ) -> dict:
        """Add/update job metadata without a stage transition.

        Used when LangGraph first sees a job: register title/company/score
        without forcing a stage change. If the job is brand new, it lands in
        stage='discovered'.
        """
        with _file_lock(self.lockpath):
            data = self._read()
            jobs = data.setdefault("jobs", [])
            found = None
            for j in jobs:
                if j.get("job_id") == job_id or j.get("id") == job_id:
                    found = j
                    break

            now = _now_iso()
            if found is None:
                found = {
                    "job_id": job_id,
                    "added_at": now,
                    "history": [
                        {"stage": "discovered", "at": now, "agent": actor, "source": source, "notes": "upsert_metadata"},
                    ],
                    "source": source,
                    "stage": "discovered",
                    "stage_updated_at": now,
                }
                jobs.append(found)
                stats = data.setdefault("stats", {})
                stats["total_discovered"] = int(stats.get("total_discovered", 0)) + 1

            for k, v in (metadata or {}).items():
                if v is None:
                    continue
                if k == "score" or not found.get(k):
                    found[k] = v

            data["_updated_at"] = now
            self._write_atomic(data)
            return dict(found)


# ---------------------------------------------------------------------------
# Singleton accessor (optional; callers can also instantiate directly)
# ---------------------------------------------------------------------------

_DEFAULT: Optional[PipelineManager] = None


def get_default_manager() -> PipelineManager:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = PipelineManager()
    return _DEFAULT
