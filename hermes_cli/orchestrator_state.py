"""Durable autonomous orchestration state over SessionDB.state_meta."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def _meta_key(session_id: str) -> str:
    return f"orchestrator:{session_id}"


def _get_session_db():
    from hermes_cli.goals import _get_session_db

    return _get_session_db()


def _clean_path_status(item: Any) -> dict[str, str] | None:
    if isinstance(item, str):
        text = item.strip()
        if not text:
            return None
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            return {"status": parts[0], "path": parts[1]}
        return {"status": "?", "path": text}
    if isinstance(item, dict):
        path = str(item.get("path") or "").strip()
        status = str(item.get("status") or item.get("xy") or "?").strip()[:8]
        if not path:
            return None
        return {"path": path, "status": status}
    return None


def _sanitize_verification(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("status", "command", "exit_code", "started_at", "ended_at"):
        if key in value:
            result[key] = value[key]
    digest = value.get("digest") or value.get("output_digest")
    if digest:
        result["output_digest"] = str(digest)
    return result


@dataclass
class Checkpoint:
    goal: str = ""
    plan_paths: list[str] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    dirty_summary: list[dict[str, str]] = field(default_factory=list)
    process_ids: list[int] = field(default_factory=list)
    session_ids: list[str] = field(default_factory=list)
    next_action: str = ""
    verification: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Checkpoint | None":
        if not isinstance(data, dict):
            return None
        return cls(
            goal=str(data.get("goal") or ""),
            plan_paths=[str(p) for p in data.get("plan_paths") or []],
            evidence_paths=[str(p) for p in data.get("evidence_paths") or []],
            dirty_summary=[d for d in data.get("dirty_summary") or [] if isinstance(d, dict)],
            process_ids=[int(p) for p in data.get("process_ids") or [] if isinstance(p, int) or str(p).isdigit()],
            session_ids=[str(s) for s in data.get("session_ids") or [] if str(s).strip()],
            next_action=str(data.get("next_action") or ""),
            verification=dict(data.get("verification") or {}),
            created_at=float(data.get("created_at", 0.0) or 0.0),
        )


@dataclass
class OrchestratorState:
    status: str = "idle"
    active_job_id: str | None = None
    attempt_id: str | None = None
    session_id: str | None = None
    worker_id: str | None = None
    provider_session_id: str | None = None
    route_decision: dict[str, Any] | None = None
    failure_class: str | None = None
    total_attempts: int = 0
    route_attempts: dict[str, int] = field(default_factory=dict)
    checkpoint: Checkpoint | None = None
    last_verification: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str | None = None
    updated_at: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> "OrchestratorState":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except Exception:
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls(
            status=str(data.get("status") or "idle"),
            active_job_id=data.get("active_job_id"),
            attempt_id=data.get("attempt_id"),
            session_id=data.get("session_id"),
            worker_id=data.get("worker_id"),
            provider_session_id=data.get("provider_session_id"),
            route_decision=data.get("route_decision") if isinstance(data.get("route_decision"), dict) else None,
            failure_class=data.get("failure_class"),
            total_attempts=int(data.get("total_attempts", 0) or 0),
            route_attempts=dict(data.get("route_attempts") or {}),
            checkpoint=Checkpoint.from_dict(data.get("checkpoint")),
            last_verification=dict(data.get("last_verification") or {}),
            blocked_reason=data.get("blocked_reason"),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
        )


class OrchestratorStateStore:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._db = _get_session_db()

    def load(self) -> OrchestratorState:
        if self._db is None:
            return OrchestratorState(session_id=self.session_id)
        try:
            state = OrchestratorState.from_json(self._db.get_meta(_meta_key(self.session_id)))
        except Exception as exc:
            logger.debug("Orchestrator state load failed: %s", exc)
            state = OrchestratorState()
        if state.session_id is None:
            state.session_id = self.session_id
        return state

    def save(self, state: OrchestratorState) -> OrchestratorState:
        state.session_id = state.session_id or self.session_id
        state.updated_at = time.time()
        if self._db is not None:
            self._db.set_meta(_meta_key(self.session_id), state.to_json())
        return state

    def update(self, **kwargs: Any) -> OrchestratorState:
        state = self.load()
        for key, value in kwargs.items():
            if hasattr(state, key):
                setattr(state, key, value)
        if state.active_job_id:
            state.status = "active"
        return self.save(state)

    def record_checkpoint(
        self,
        *,
        goal: str,
        plan_paths: list[str] | None = None,
        evidence_paths: list[str] | None = None,
        dirty_summary: list[Any] | None = None,
        process_ids: list[int] | None = None,
        session_ids: list[str] | None = None,
        next_action: str = "",
        verification: dict[str, Any] | None = None,
    ) -> Checkpoint:
        clean_dirty = []
        for item in dirty_summary or []:
            clean = _clean_path_status(item)
            if clean is not None:
                clean_dirty.append(clean)
        checkpoint = Checkpoint(
            goal=str(goal or ""),
            plan_paths=[str(p) for p in plan_paths or []],
            evidence_paths=[str(p) for p in evidence_paths or []],
            dirty_summary=clean_dirty,
            process_ids=[int(pid) for pid in process_ids or []],
            session_ids=[str(sid) for sid in session_ids or [] if str(sid).strip()],
            next_action=str(next_action or ""),
            verification=_sanitize_verification(verification),
            created_at=time.time(),
        )
        state = self.load()
        state.checkpoint = checkpoint
        state.last_verification = checkpoint.verification
        self.save(state)
        return checkpoint

    def clear_state(self) -> OrchestratorState:
        state = OrchestratorState(session_id=self.session_id, status="cleared")
        return self.save(state)


def migrate_orchestrator_state_to_session(old_session_id: str, new_session_id: str, *, reason: str = "") -> bool:
    if not old_session_id or not new_session_id or old_session_id == new_session_id:
        return False
    old_store = OrchestratorStateStore(old_session_id)
    state = old_store.load()
    if not state.active_job_id and state.checkpoint is None:
        return False
    new_store = OrchestratorStateStore(new_session_id)
    existing = new_store.load()
    if existing.active_job_id or existing.checkpoint is not None:
        return False
    state.session_id = new_session_id
    new_store.save(state)
    old = old_store.load()
    old.status = "migrated"
    old.blocked_reason = reason or "session_migration"
    old_store.save(old)
    return True
