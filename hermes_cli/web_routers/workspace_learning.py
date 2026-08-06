"""Project Workspace learning-candidate and self-improvement routes."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hermes_cli.web_routers.workspace import _profile_home
from hermes_cli.workspace_learning import (
    LearningController,
    LearningDestinationAdapter,
    LearningStore,
    ProfileLearningAdapter,
)
from hermes_constants import get_hermes_home

router = APIRouter()
_stores: dict[str, LearningStore] = {}


class LearningSignalBody(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    kind: str = Field(min_length=1, max_length=64)
    project_id: str | None = Field(default=None, max_length=128)
    provenance: list[dict[str, str]] = Field(min_length=1, max_length=50)
    reusable: bool = False


class LearningCandidateBody(BaseModel):
    destination: str = Field(min_length=1, max_length=32)
    proposal: dict[str, Any]
    risk: str = Field(min_length=1, max_length=16)
    signal_ids: list[str] = Field(min_length=1, max_length=100)
    ttl_seconds: float = Field(default=30 * 24 * 60 * 60, gt=0, le=30 * 24 * 60 * 60)


class LearningEvaluationBody(BaseModel):
    baseline: dict[str, Any]
    candidate: dict[str, Any]
    held_out_digest: str = Field(min_length=64, max_length=64)
    policy_digest: str = Field(min_length=64, max_length=64)


class LearningCanaryBody(BaseModel):
    metrics: dict[str, Any]


class LearningReasonBody(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


def _principal(request: Request) -> str:
    session = getattr(request.state, "session", None)
    token_principal = getattr(request.state, "token_principal", None)
    value = getattr(session, "user_id", None) or getattr(token_principal, "principal", None)
    if value:
        return str(value)
    legacy_token = request.headers.get("x-hermes-session-token") or request.query_params.get("token")
    if legacy_token:
        del legacy_token  # Authentication was already enforced by web_server middleware.
        path = get_hermes_home() / ".dashboard-actor-id"
        if path.is_file():
            actor_id = path.read_text(encoding="utf-8").strip()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            actor_id = f"dashboard-user:{secrets.token_hex(16)}"
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                actor_id = path.read_text(encoding="utf-8").strip()
            else:
                try:
                    os.write(descriptor, actor_id.encode("utf-8"))
                finally:
                    os.close(descriptor)
        if not actor_id.startswith("dashboard-user:"):
            raise HTTPException(status_code=500, detail="Dashboard actor identity is invalid")
        return actor_id
    raise HTTPException(status_code=401, detail="authenticated learning actor is required")


def _store(profile: str | None) -> tuple[Path, LearningStore]:
    home = _profile_home(profile)
    key = str(home)
    store = _stores.get(key)
    if store is None:
        store = LearningStore(home / "workspace-learning.db")
        _stores[key] = store
    return home, store


def _controller(home: Path, store: LearningStore) -> LearningController:
    adapters: dict[str, LearningDestinationAdapter] = {}
    if home == get_hermes_home().expanduser().resolve():
        profile_adapter = ProfileLearningAdapter(home)
        adapters = {
            "memory": profile_adapter,
            "skill": profile_adapter,
            "user_memory": profile_adapter,
        }
    return LearningController(store, adapters)


def _conflict(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


def reset_workspace_learning_state_for_tests() -> None:
    for store in _stores.values():
        store.close()
    _stores.clear()


@router.get("/api/workspace/learning/candidates")
def list_learning_candidates(
    include_terminal: bool = Query(default=True),
    profile: str | None = Query(default=None),
):
    _home, store = _store(profile)
    store.recover_stale_applications()
    store.expire_stale()
    return {"candidates": store.list_candidates(include_terminal=include_terminal)}


@router.get("/api/workspace/learning/operator-queue")
def workspace_learning_operator_queue(
    request: Request,
    role: str = Query(pattern="^(evaluator|promoter)$"),
    profile: str | None = Query(default=None),
) -> dict[str, Any]:
    actor_id = _principal(request)
    _home, store = _store(profile)
    store.recover_stale_applications()
    candidates = store.list_candidates(include_terminal=False)
    if role == "evaluator":
        tasks = [
            candidate
            for candidate in candidates
            if candidate["status"] == "staged" and candidate["proposer_id"] != actor_id
        ]
        next_action = "POST /api/workspace/learning/candidates/{id}/evaluate"
    else:
        tasks = [
            candidate
            for candidate in candidates
            if candidate["status"] in {"approved", "canary_passed"}
            and actor_id not in {candidate["proposer_id"], candidate.get("evaluator_id")}
        ]
        next_action = (
            "POST /api/workspace/learning/candidates/{id}/canary for approved; "
            "POST /api/workspace/learning/candidates/{id}/apply for canary_passed"
        )
    return {
        "actor_id": actor_id,
        "contract": "Operators are external role-separated workers; requests are durable CAS transitions.",
        "next_action": next_action,
        "role": role,
        "tasks": tasks,
    }


@router.post("/api/workspace/learning/signals")
def create_learning_signal(
    body: LearningSignalBody,
    request: Request,
    profile: str | None = Query(default=None),
):
    _home, store = _store(profile)
    try:
        return {
            "signal": store.record_signal(
                actor_id=_principal(request),
                content=body.content,
                kind=body.kind,
                project_id=body.project_id,
                provenance=body.provenance,
                reusable=body.reusable,
            )
        }
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/api/workspace/learning/candidates")
def create_learning_candidate(
    body: LearningCandidateBody,
    request: Request,
    profile: str | None = Query(default=None),
):
    _home, store = _store(profile)
    try:
        return {
            "candidate": store.propose_candidate(
                destination=body.destination,
                proposer_id=_principal(request),
                proposal=body.proposal,
                risk=body.risk,
                signal_ids=body.signal_ids,
                ttl_seconds=body.ttl_seconds,
            )
        }
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/api/workspace/learning/candidates/{candidate_id}/evaluate")
def evaluate_learning_candidate(
    candidate_id: str,
    body: LearningEvaluationBody,
    request: Request,
    profile: str | None = Query(default=None),
):
    _home, store = _store(profile)
    try:
        return {
            "candidate": store.evaluate_candidate(
                candidate_id,
                evaluator_id=_principal(request),
                baseline=body.baseline,
                candidate=body.candidate,
                held_out_digest=body.held_out_digest,
                policy_digest=body.policy_digest,
            )
        }
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/api/workspace/learning/candidates/{candidate_id}/approve")
def approve_learning_candidate(
    candidate_id: str,
    request: Request,
    profile: str | None = Query(default=None),
):
    _home, store = _store(profile)
    try:
        return {
            "candidate": store.approve_candidate(
                candidate_id,
                approver_id=_principal(request),
            )
        }
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/api/workspace/learning/candidates/{candidate_id}/reject")
def reject_learning_candidate(
    candidate_id: str,
    body: LearningReasonBody,
    request: Request,
    profile: str | None = Query(default=None),
):
    _home, store = _store(profile)
    try:
        return {
            "candidate": store.reject_candidate(
                candidate_id,
                actor_id=_principal(request),
                reason=body.reason,
            )
        }
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/api/workspace/learning/candidates/{candidate_id}/canary")
def canary_learning_candidate(
    candidate_id: str,
    body: LearningCanaryBody,
    request: Request,
    profile: str | None = Query(default=None),
):
    home, store = _store(profile)
    try:
        candidate = _controller(home, store).run_canary(
            candidate_id,
            promoter_id=_principal(request),
            metrics=body.metrics,
        )
        return {"candidate": candidate}
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/api/workspace/learning/candidates/{candidate_id}/apply")
def apply_learning_candidate(
    candidate_id: str,
    request: Request,
    profile: str | None = Query(default=None),
):
    home, store = _store(profile)
    try:
        candidate = _controller(home, store).apply_candidate(
            candidate_id,
            promoter_id=_principal(request),
        )
        return {"candidate": candidate}
    except ValueError as exc:
        raise _conflict(exc) from exc


@router.post("/api/workspace/learning/candidates/{candidate_id}/rollback")
def rollback_learning_candidate(
    candidate_id: str,
    body: LearningReasonBody,
    request: Request,
    profile: str | None = Query(default=None),
):
    home, store = _store(profile)
    try:
        candidate = _controller(home, store).rollback_candidate(
            candidate_id,
            actor_id=_principal(request),
            reason=body.reason,
        )
        return {"candidate": candidate}
    except ValueError as exc:
        raise _conflict(exc) from exc
