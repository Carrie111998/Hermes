"""Project Workspace dashboard routes with opaque device bindings."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hermes_cli import projects_db
from hermes_cli.web_deps import late
from hermes_constants import get_hermes_home
from hermes_cli.workspace_approval_store import WorkspaceApprovalStore
from hermes_cli.workspace_context_store import WorkspaceContextStore

router = APIRouter()
_cron_profile_home = late("_cron_profile_home")
_open_session_db_for_profile = late("_open_session_db_for_profile")


def _binding_key(profile_home: Path) -> bytes:
    key_path = profile_home / ".workspace-binding-key"
    try:
        value = key_path.read_bytes()
        if len(value) != 32:
            raise ValueError("workspace binding key is invalid")
        return value
    except FileNotFoundError:
        profile_home.mkdir(parents=True, exist_ok=True)
        value = secrets.token_bytes(32)
        try:
            descriptor = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return _binding_key(profile_home)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        return value


def _binding_id(profile_home: Path, local_path: str) -> str:
    normalized = str(Path(local_path).expanduser().resolve())
    digest = hmac.new(
        _binding_key(profile_home),
        normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"b_{digest[:32]}"


def _runner_id(profile_home: Path) -> str:
    digest = hmac.new(
        _binding_key(profile_home),
        b"local-profile-runner",
        hashlib.sha256,
    ).hexdigest()
    return f"r_{digest[:24]}"


def workspace_project_payload(
    project: projects_db.Project,
    *,
    conversations: list[dict[str, Any]] | None = None,
    profile_home: Path,
) -> dict[str, Any]:
    bindings = []
    runner_id = _runner_id(profile_home)
    for index, folder in enumerate(project.folders):
        candidate = Path(folder.path).expanduser()
        bindings.append(
            {
                "binding_id": _binding_id(profile_home, folder.path),
                "capabilities": ["local.chat"],
                "chat_available": True,
                "is_primary": bool(folder.is_primary or index == 0),
                "label": folder.label or candidate.name or project.name,
                "runner_id": runner_id,
                "status": "online" if candidate.is_dir() else "offline",
            }
        )
    return {
        "archived": bool(project.archived),
        "bindings": bindings,
        "color": project.color,
        "conversations": conversations or [],
        "context": WorkspaceContextStore(profile_home).get(project.id),
        "created_at": project.created_at,
        "description": project.description,
        "icon": project.icon,
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
    }


def merge_remote_runner_bindings(
    projects: list[dict[str, Any]],
    runners: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_project = {str(project["id"]): project for project in projects}
    for runner in runners:
        runner_id = str(runner.get("runner_id") or "")
        capabilities = [str(item) for item in runner.get("capabilities") or []]
        for binding in runner.get("bindings") or []:
            project = by_project.get(str(binding.get("project_id") or ""))
            if project is None:
                continue
            public_binding = {
                "binding_id": str(binding.get("binding_id") or ""),
                "capabilities": capabilities,
                "chat_available": False,
                "is_primary": False,
                "label": str(binding.get("label") or runner.get("label") or "Remote device"),
                "runner_id": runner_id,
                "status": str(binding.get("status") or runner.get("status") or "offline"),
            }
            identity = (public_binding["runner_id"], public_binding["binding_id"])
            if not any(
                (item.get("runner_id"), item.get("binding_id")) == identity
                for item in project["bindings"]
            ):
                project["bindings"].append(public_binding)
    return projects


def _remote_runner_inventory(profile_home: Path) -> list[dict[str, Any]]:
    if profile_home != get_hermes_home().expanduser().resolve():
        return []
    from hermes_cli.web_routers.workspace_runners import get_workspace_runner_registry

    return get_workspace_runner_registry().list_runners()


def resolve_workspace_binding(
    binding_id: str,
    *,
    db_path: Path | None = None,
    profile_home: Path | None = None,
) -> Path:
    home = (profile_home or get_hermes_home()).expanduser().resolve()
    database = db_path or home / "projects.db"
    with projects_db.connect_closing(database) as connection:
        for project in projects_db.list_projects(connection, include_archived=False):
            for folder in project.folders:
                expected = _binding_id(home, folder.path)
                if hmac.compare_digest(expected, binding_id):
                    candidate = Path(folder.path).expanduser().resolve()
                    if not candidate.is_dir():
                        raise ValueError("workspace binding is offline")
                    return candidate
    raise ValueError("workspace binding is unknown")


def _profile_home(profile: str | None) -> Path:
    if not profile:
        return get_hermes_home().expanduser().resolve()
    _profile_name, home = _cron_profile_home(profile)
    return Path(home).expanduser().resolve()


def resolve_workspace_binding_for_profile(binding_id: str, profile: str | None) -> Path:
    home = _profile_home(profile)
    return resolve_workspace_binding(
        binding_id,
        db_path=home / "projects.db",
        profile_home=home,
    )


def workspace_scope_for_path(local_path: str, profile: str | None = None) -> tuple[str | None, str | None]:
    home = _profile_home(profile)
    candidate = Path(local_path).expanduser().resolve()
    best: tuple[int, str, str] | None = None
    with projects_db.connect_closing(home / "projects.db") as connection:
        for project in projects_db.list_projects(connection, include_archived=False):
            for folder in project.folders:
                root = Path(folder.path).expanduser().resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    continue
                match = (len(root.parts), project.id, _binding_id(home, folder.path))
                if best is None or match[0] > best[0]:
                    best = match
    return (best[1], best[2]) if best else (None, None)


def publish_workspace_push_approval(
    approval_request: dict[str, Any],
    *,
    local_path: str,
    profile: str | None = None,
) -> dict[str, Any]:
    home = _profile_home(profile)
    project_id, binding_id = workspace_scope_for_path(local_path, profile)
    store = WorkspaceApprovalStore(home / "workspace-approvals.db")
    try:
        return store.publish(
            approval_request,
            binding_id=binding_id,
            project_id=project_id,
        )
    finally:
        store.close()


def complete_workspace_push_approval(
    request_id: str,
    *,
    error: str | None = None,
    profile: str | None = None,
) -> None:
    home = _profile_home(profile)
    store = WorkspaceApprovalStore(home / "workspace-approvals.db")
    try:
        try:
            store.mark_result(request_id, error=error)
        except ValueError:
            pass
    finally:
        store.close()


class WorkspaceApprovalDecisionBody(BaseModel):
    approved: bool


class WorkspaceContextBody(BaseModel):
    notion_page_ids: list[str] = Field(default_factory=list)
    slack_channel_ids: list[str] = Field(default_factory=list)


def _request_principal(request: Request) -> str:
    session = getattr(request.state, "session", None)
    token_principal = getattr(request.state, "token_principal", None)
    principal = getattr(session, "user_id", None) or getattr(token_principal, "principal", None)
    if principal:
        return str(principal)
    raw_token = request.headers.get("x-hermes-session-token") or request.query_params.get("token")
    if raw_token:
        digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()[:20]
        return f"dashboard-session:{digest}"
    raise HTTPException(status_code=401, detail="Authenticated approval principal required")


def _project_conversations(project: projects_db.Project, profile: str | None) -> list[dict[str, Any]]:
    database = _open_session_db_for_profile(profile, read_only=True)
    try:
        rows_by_id: dict[str, dict[str, Any]] = {}
        for folder in project.folders:
            rows = database.list_sessions_rich(
                compact_rows=True,
                cwd_prefix=folder.path,
                limit=25,
                offset=0,
                order_by_last_active=True,
            )
            for row in rows:
                session_id = str(row.get("id") or "")
                if not session_id:
                    continue
                rows_by_id[session_id] = {
                    "ended_at": row.get("ended_at"),
                    "id": session_id,
                    "is_active": row.get("ended_at") is None,
                    "last_active": row.get("last_active") or row.get("started_at"),
                    "message_count": row.get("message_count") or 0,
                    "model": row.get("model"),
                    "preview": row.get("preview"),
                    "source": row.get("source"),
                    "started_at": row.get("started_at"),
                    "title": row.get("title"),
                }
        return sorted(
            rows_by_id.values(),
            key=lambda row: float(row.get("last_active") or 0),
            reverse=True,
        )[:25]
    finally:
        database.close()


@router.get("/api/workspace/projects")
def workspace_projects(
    include_archived: bool = False,
    profile: str | None = Query(default=None),
):
    try:
        home = _profile_home(profile)
        with projects_db.connect_closing(home / "projects.db") as connection:
            projects = projects_db.list_projects(
                connection,
                include_archived=include_archived,
            )
        payloads = [
            workspace_project_payload(
                project,
                conversations=_project_conversations(project, profile),
                profile_home=home,
            )
            for project in projects
        ]
        return {
            "generated_at": time.time(),
            "projects": merge_remote_runner_bindings(
                payloads,
                _remote_runner_inventory(home),
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Workspace projects unavailable") from exc


@router.put("/api/workspace/projects/{project_id}/context")
def workspace_project_context(
    project_id: str,
    body: WorkspaceContextBody,
    profile: str | None = Query(default=None),
):
    home = _profile_home(profile)
    with projects_db.connect_closing(home / "projects.db") as connection:
        if projects_db.get_project(connection, project_id) is None:
            raise HTTPException(status_code=404, detail="Workspace project not found")
    try:
        context = WorkspaceContextStore(home).set(
            project_id,
            notion_page_ids=body.notion_page_ids,
            slack_channel_ids=body.slack_channel_ids,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"context": context, "project_id": project_id}


@router.get("/api/workspace/approvals")
def workspace_approvals(profile: str | None = Query(default=None)):
    home = _profile_home(profile)
    store = WorkspaceApprovalStore(home / "workspace-approvals.db")
    try:
        return {"approvals": store.list_pending()}
    finally:
        store.close()


@router.post("/api/workspace/approvals/{request_id}/decision")
def workspace_approval_decision(
    request_id: str,
    body: WorkspaceApprovalDecisionBody,
    request: Request,
    profile: str | None = Query(default=None),
):
    home = _profile_home(profile)
    approved_by = _request_principal(request)
    store = WorkspaceApprovalStore(home / "workspace-approvals.db")
    try:
        try:
            decision = store.decide(
                request_id,
                approved=body.approved,
                approved_by=str(approved_by),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not body.approved:
            return {"decision": decision, "status": "denied"}

        from hermes_cli.web_git import review_push_approved_by_request_id

        try:
            result = review_push_approved_by_request_id(decision)
        except Exception as exc:
            store.mark_result(request_id, error=str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        store.mark_result(request_id)
        return {"decision": decision, "result": result, "status": "completed"}
    finally:
        store.close()


@router.get("/api/workspace/projects/{project_id}")
def workspace_project(project_id: str, profile: str | None = Query(default=None)):
    try:
        home = _profile_home(profile)
        with projects_db.connect_closing(home / "projects.db") as connection:
            project = projects_db.get_project(connection, project_id)
        if project is None or project.archived:
            raise HTTPException(status_code=404, detail="Workspace project not found")
        payload = workspace_project_payload(
            project,
            conversations=_project_conversations(project, profile),
            profile_home=home,
        )
        return merge_remote_runner_bindings([payload], _remote_runner_inventory(home))[0]
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Workspace project unavailable") from exc
