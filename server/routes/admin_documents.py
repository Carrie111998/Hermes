"""Admin-only observability for uploaded documents and the runs that read them.

Everything here is cross-tenant by design, so every route requires admin and
every query names its company explicitly — a document id alone is never enough
to reach a row.

The split with the customer document API is deliberate: customers see a status
and a sentence, admins see both stored forms, every attempt with its technical
reason code, the semantic result, and the sources the agent actually consulted.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..auth import Principal, require_admin
from ..db import json_load

router = APIRouter(prefix="/admin", tags=["admin"])


def _document_row(request: Request, document_id: str, company_id: str | None = None):
    if company_id:
        return request.app.state.db.one(
            "SELECT * FROM documents WHERE id=? AND company_id=?", (document_id, company_id)
        )
    return request.app.state.db.one("SELECT * FROM documents WHERE id=?", (document_id,))


def _artifact_json(artifact) -> dict:
    """Metadata only. Bytes are streamed by the artifact endpoint, never inlined."""
    return {
        "id": artifact.id, "role": artifact.role, "filename": artifact.filename,
        "content_type": artifact.content_type, "checksum": artifact.checksum,
        "size_bytes": artifact.size_bytes, "attempt_id": artifact.attempt_id,
        "metadata": artifact.metadata, "created_at": artifact.created_at,
    }


def _attempt_json(attempt) -> dict:
    return {
        "id": attempt.id, "public_status": attempt.public_status,
        "public_message": attempt.public_message, "internal_stage": attempt.internal_stage,
        "reason_code": attempt.reason_code, "diagnostic": attempt.diagnostic,
        "input_checksum": attempt.input_checksum, "output_checksum": attempt.output_checksum,
        "run_id": attempt.run_id, "started_at": attempt.started_at,
        "completed_at": attempt.completed_at,
    }


@router.get("/documents")
def list_documents(
    request: Request,
    company_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    created_from: float | None = Query(default=None),
    created_to: float | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    _: Principal = Depends(require_admin),
):
    clauses, params = [], []
    for column, value in (("d.company_id", company_id), ("d.status", status),
                          ("d.origin", origin)):
        if value:
            clauses.append(f"{column}=?")
            params.append(value)
    if created_from is not None:
        clauses.append("d.created_at>=?")
        params.append(created_from)
    if created_to is not None:
        clauses.append("d.created_at<=?")
        params.append(created_to)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = request.app.state.db.all(
        "SELECT d.id, d.company_id, d.document_type, d.name, d.content_type,"
        " d.size_bytes, d.status, d.status_detail, d.origin, d.created_at,"
        " d.updated_at, d.active_processed_artifact_id, c.name AS company_name"
        f" FROM documents d LEFT JOIN companies c ON c.id=d.company_id {where}"
        " ORDER BY d.created_at DESC LIMIT ?",
        tuple(params),
    )
    return [
        {
            "id": row["id"], "company_id": row["company_id"],
            "company_name": row["company_name"], "document_type": row["document_type"],
            "name": row["name"], "content_type": row["content_type"],
            "size_bytes": row["size_bytes"], "status": row["status"],
            "status_detail": row["status_detail"], "origin": row["origin"],
            "has_processed_artifact": bool(row["active_processed_artifact_id"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }
        for row in rows
    ]


@router.get("/documents/{document_id}")
def document_detail(document_id: str, request: Request,
                    _: Principal = Depends(require_admin)):
    row = _document_row(request, document_id)
    if not row:
        raise HTTPException(404, "Document not found")
    company_id = row["company_id"]
    artifacts = request.app.state.document_artifacts

    agent_run = None
    if row["processing_run_id"]:
        try:
            agent_run = request.app.state.runs.detail(company_id, row["processing_run_id"])
        except HTTPException:
            agent_run = None

    semantic = json_load(row["data"], {}) or {}
    return {
        "document": {
            "id": row["id"], "company_id": company_id,
            "document_type": row["document_type"], "name": row["name"],
            "content_type": row["content_type"], "size_bytes": row["size_bytes"],
            "status": row["status"], "status_detail": row["status_detail"],
            "origin": row["origin"], "original_checksum": row["original_checksum"],
            "active_processed_artifact_id": row["active_processed_artifact_id"],
            "processing_started_at": row["processing_started_at"],
            "ready_at": row["ready_at"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        },
        "artifacts": [_artifact_json(a) for a in artifacts.list_artifacts(company_id, document_id)],
        "attempts": [_attempt_json(a) for a in artifacts.list_attempts(company_id, document_id)],
        "records": semantic.get("records", []),
        "rejects": semantic.get("rejects", []),
        "agent_run": agent_run,
    }


@router.get("/documents/{document_id}/artifacts/{role}")
def download_artifact(document_id: str, role: str, request: Request,
                      _: Principal = Depends(require_admin)):
    if role not in {"original", "processed"}:
        raise HTTPException(404, "Unknown artifact role")
    row = _document_row(request, document_id)
    if not row:
        raise HTTPException(404, "Document not found")

    company_id = row["company_id"]
    artifacts = request.app.state.document_artifacts
    artifact = (
        artifacts.get_original(company_id, document_id)
        if role == "original"
        else artifacts.get_active_processed(company_id, document_id)
    )
    if artifact is None:
        raise HTTPException(404, "Artifact not found")

    # Rebuilds and checksum-verifies the mirror, so a cleared disk still serves
    # the exact bytes the database holds.
    path = artifacts.materialize(company_id, artifact.id)
    return FileResponse(
        path,
        media_type=artifact.content_type or "application/octet-stream",
        filename=artifact.filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.post("/documents/{document_id}/retry", status_code=202)
def retry_document(document_id: str, request: Request,
                   actor: Principal = Depends(require_admin)):
    row = _document_row(request, document_id)
    if not row:
        raise HTTPException(404, "Document not found")
    company_id = row["company_id"]
    attempt = request.app.state.document_processing.submit(company_id, document_id, force=True)
    request.app.state.db.activity(
        company_id, actor.id, "document_processing_retried", "document", document_id
    )
    return {"document_id": document_id, "attempt_id": attempt.id, "status": "processing"}


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, request: Request,
                    actor: Principal = Depends(require_admin)):
    row = _document_row(request, document_id)
    if not row:
        raise HTTPException(404, "Document not found")
    company_id = row["company_id"]
    result = request.app.state.document_artifacts.delete_document(company_id, document_id)
    request.app.state.db.activity(
        company_id, actor.id, "document_deleted", "document", document_id,
        {"cleanup_error": result.get("cleanup_error")},
    )


@router.get("/agent-runs/{run_id}/detail")
def agent_run_detail(run_id: str, request: Request, _: Principal = Depends(require_admin)):
    """Cross-company run detail.

    The company is resolved from the run itself rather than accepted from the
    caller: taking a caller-supplied company id would let a guessed run id be
    paired with any tenant. An unknown run is a plain 404.
    """
    row = request.app.state.db.one(
        "SELECT company_id FROM agent_runs WHERE id=?", (run_id,)
    )
    if not row:
        raise HTTPException(404, "Run not found")
    return request.app.state.runs.detail(row["company_id"], run_id)
