from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ..auth import Principal, company_scope, current_principal
from ..db import Database, json_dump, json_load, new_id, now
from ..product_import import ProductImportConflict, ProductImportValidationError, import_products, parse_product_catalog
from ..schemas import DataPatch


router = APIRouter(tags=["company-knowledge"])
DOCUMENT_TYPES = {
    "product_catalog", "technical_sheet", "price_list", "past_sales", "past_customers",
    "current_contacts", "proposal_example", "pitch_deck", "certificate", "case_study",
    "dealer_list", "distributor_list", "lost_deals", "other",
}


class ProductCreate(BaseModel):
    product_name: str = Field(min_length=1)
    data: dict[str, Any] = Field(default_factory=dict)


class ProductPatch(BaseModel):
    product_name: str | None = None
    data: dict[str, Any] | None = None


class ExtractionRequest(BaseModel):
    document_ids: list[str] = Field(min_length=1)


class BrainBuildRequest(BaseModel):
    source_document_ids: list[str] = Field(default_factory=list)


class BrainApproveRequest(BaseModel):
    snapshot_id: str


def _scope(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


def _document(row) -> dict:
    """The customer's view of a document.

    Deliberately hand-written rather than a row splat: the row also carries the
    artifact ids, checksums, local mirror path, and attempt pointer, none of
    which a customer surface has any use for. `status_detail` is the only new
    field — a plain sentence, never a reason code.
    """
    return {
        "id": row["id"], "company_id": row["company_id"], "document_type": row["document_type"],
        "name": row["name"], "content_type": row["content_type"], "size_bytes": row["size_bytes"],
        "status": row["status"], "status_detail": row["status_detail"],
        "processing_run_id": row["processing_run_id"],
        "data": json_load(row["data"], {}), "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _product(row) -> dict:
    data = json_load(row["data"], {})
    return {"id": row["id"], "company_id": row["company_id"],
            "product_name": row["name"], **data,
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


@router.get("/documents")
def list_documents(request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return [_document(row) for row in request.app.state.db.all(
        "SELECT * FROM documents WHERE company_id=? ORDER BY created_at DESC", (company_id,)
    )]


@router.post("/documents/upload", status_code=201)
async def upload_document(
    request: Request,
    document_type: str = Form(...),
    file: UploadFile = File(...),
    principal: Principal = Depends(current_principal),
    x_company_id: str | None = Header(default=None),
):
    if document_type not in DOCUMENT_TYPES:
        raise HTTPException(422, "Unsupported document_type")
    company_id = _scope(principal, x_company_id)
    limit = max(0, int(request.app.state.settings.max_upload_bytes))
    # Read one byte past the limit and reject before anything is committed, so
    # an oversized upload never leaves an orphaned document row behind.
    content = await file.read(limit + 1 if limit else -1)
    if limit and len(content) > limit:
        raise HTTPException(413, "Document exceeds the configured upload limit")

    document_id = new_id("doc")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "upload").name)[:180]
    stamp = now()
    request.app.state.db.execute(
        "INSERT INTO documents(id,company_id,document_type,name,storage_path,content_type,size_bytes,status,data,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (document_id, company_id, document_type, safe_name, None, file.content_type,
         len(content), "uploaded", json_dump({}), stamp, stamp),
    )
    request.app.state.db.execute(
        "UPDATE documents SET origin='onboarding_upload' WHERE id=?", (document_id,)
    )
    request.app.state.document_artifacts.store_original(
        company_id, document_id, safe_name,
        file.content_type or "application/octet-stream", content,
    )
    request.app.state.db.activity(company_id, principal.id, "document_uploaded", "document", document_id)
    # Processing starts immediately and runs behind the response: the customer
    # sees Uploaded/Processing right away rather than waiting on the work.
    request.app.state.document_processing.submit(company_id, document_id)
    return _document(request.app.state.db.one("SELECT * FROM documents WHERE id=?", (document_id,)))


@router.get("/documents/{document_id}")
def get_document(document_id: str, request: Request, principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one("SELECT * FROM documents WHERE id=? AND company_id=?",
                                   (document_id, company_id))
    if not row:
        raise HTTPException(404, "Document not found")
    return _document(row)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: str, request: Request, principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    try:
        request.app.state.document_artifacts.delete_document(company_id, document_id)
    except LookupError as exc:
        raise HTTPException(404, "Document not found") from exc


# Product-safe copy for the states a semantic run cannot start from. Names the
# file's condition and the customer's next action, never the machinery.
_NOT_READY_MESSAGES = {
    "uploaded": "This file is still being prepared. Please try again in a moment.",
    "processing": "This file is still being prepared. Please try again in a moment.",
    "needs_attention": "This file needs attention before it can be used.",
    "failed": "We couldn't use this file. Please upload it again.",
}


@router.post("/documents/{document_id}/process", status_code=202)
def process_document(document_id: str, request: Request, principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    document = request.app.state.db.one("SELECT * FROM documents WHERE id=? AND company_id=?",
                                        (document_id, company_id))
    if not document:
        raise HTTPException(404, "Document not found")
    if document["status"] != "ready":
        raise HTTPException(409, _NOT_READY_MESSAGES.get(
            document["status"], "This file can't be used yet. Please try again in a moment."
        ))

    artifacts = request.app.state.document_artifacts
    processed = artifacts.get_active_processed(company_id, document_id)
    if processed is None:
        raise HTTPException(409, _NOT_READY_MESSAGES["processing"])
    # Verified on every start: the agent must read the same bytes the admin
    # previews, even if the mirror was cleared since the document went Ready.
    path = artifacts.materialize(company_id, processed.id)

    run = request.app.state.runs.create(
        company_id, "document_processing",
        {"document_id": document_id, "document_type": document["document_type"],
         "source_document_id": document_id, "path": str(path)},
        f"document-process:{document_id}:{processed.checksum}",
    )
    # The document stays `ready`: this run is semantic extraction, a separate
    # concern from whether the file itself is usable.
    request.app.state.db.execute(
        "UPDATE documents SET processing_run_id=?,updated_at=? WHERE id=? AND company_id=?",
        (run["id"], now(), document_id, company_id),
    )
    if run["status"] == "queued":
        run = request.app.state.runs.start(company_id, run["id"])
    return run


@router.get("/documents/{document_id}/processing-status")
def document_status(document_id: str, request: Request, principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    document = get_document(document_id, request, principal, x_company_id)
    run = None
    if document["processing_run_id"]:
        run = request.app.state.runs.get(document["company_id"], document["processing_run_id"])
    return {"document_id": document_id, "status": document["status"], "run": run}


@router.get("/products")
def list_products(request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return [_product(row) for row in request.app.state.db.all(
        "SELECT * FROM products WHERE company_id=? ORDER BY name", (company_id,)
    )]


@router.post("/products", status_code=201)
def create_product(body: ProductCreate, request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    company_id, stamp = _scope(principal, x_company_id), now()
    product_id = new_id("prd")
    normalized = " ".join(body.product_name.lower().split())
    data = {**body.data, "product_name": body.product_name}
    try:
        request.app.state.db.execute(
            "INSERT INTO products VALUES(?,?,?,?,?,?,?)",
            (product_id, company_id, body.product_name, normalized, json_dump(data), stamp, stamp),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "A product with this name already exists") from exc
        raise
    return _product(request.app.state.db.one("SELECT * FROM products WHERE id=?", (product_id,)))


@router.post("/products/import", status_code=201)
async def import_product_catalog(
    request: Request,
    file: UploadFile = File(...),
    principal: Principal = Depends(current_principal),
    x_company_id: str | None = Header(default=None),
):
    """Add a customer-supplied CSV or JSON catalog without partial imports."""
    try:
        rows = parse_product_catalog(file.filename or "", await file.read())
    except ProductImportValidationError as exc:
        raise HTTPException(422, {"errors": exc.errors}) from exc
    try:
        return import_products(request.app.state.db, _scope(principal, x_company_id), rows)
    except ProductImportConflict as exc:
        raise HTTPException(409, {"errors": exc.errors}) from exc


@router.get("/products/{product_id}")
def get_product(product_id: str, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one("SELECT * FROM products WHERE id=? AND company_id=?",
                                   (product_id, company_id))
    if not row:
        raise HTTPException(404, "Product not found")
    return _product(row)


@router.patch("/products/{product_id}")
def patch_product(product_id: str, body: ProductPatch, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one("SELECT * FROM products WHERE id=? AND company_id=?",
                                   (product_id, company_id))
    if not row:
        raise HTTPException(404, "Product not found")
    name = body.product_name or row["name"]
    data = {**json_load(row["data"], {}), **(body.data or {}), "product_name": name}
    request.app.state.db.execute(
        "UPDATE products SET name=?,normalized_name=?,data=?,updated_at=? WHERE id=?",
        (name, " ".join(name.lower().split()), json_dump(data), now(), product_id),
    )
    return get_product(product_id, request, principal, x_company_id)


@router.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: str, request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("DELETE FROM products WHERE id=? AND company_id=?",
                                        (product_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "Product not found")


@router.post("/products/extract-from-documents", status_code=202)
def extract_products(body: ExtractionRequest, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    count = request.app.state.db.one(
        f"SELECT COUNT(*) AS n FROM documents WHERE company_id=? AND id IN "
        f"({','.join('?' for _ in body.document_ids)})", (company_id, *body.document_ids),
    )["n"]
    if count != len(set(body.document_ids)):
        raise HTTPException(422, "One or more documents do not belong to this company")
    run = request.app.state.runs.create(company_id, "product_extraction",
                                        {"document_ids": body.document_ids})
    return request.app.state.runs.start(company_id, run["id"])


def _brain_product_value(product_id: str, key: str, request: Request, principal: Principal,
                         company_header: str | None):
    product = get_product(product_id, request, principal, company_header)
    company_id = _scope(principal, company_header)
    brain = request.app.state.db.one(
        "SELECT content FROM company_brain_snapshots WHERE company_id=? AND status='approved' "
        "ORDER BY version DESC LIMIT 1", (company_id,),
    )
    if not brain:
        raise HTTPException(409, "Approve a Company Brain snapshot first")
    content = json_load(brain["content"], {})
    return {"product_id": product_id, key: content.get(key, {})}


@router.post("/products/{product_id}/generate-buyer-roles")
def buyer_roles(product_id: str, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    return _brain_product_value(product_id, "buyer_roles", request, principal, x_company_id)


@router.post("/products/{product_id}/generate-market-fit")
def market_fit(product_id: str, request: Request, principal: Principal = Depends(current_principal),
               x_company_id: str | None = Header(default=None)):
    return _brain_product_value(product_id, "market_assumptions", request, principal, x_company_id)


def _snapshot(row) -> dict:
    return {"id": row["id"], "company_id": row["company_id"], "version": row["version"],
            "status": row["status"], "content": json_load(row["content"], {}),
            "sources": json_load(row["sources"], []), "run_id": row["run_id"],
            "approved_by": row["approved_by"], "created_at": row["created_at"],
            "approved_at": row["approved_at"]}


@router.get("/company-brain")
def company_brain(request: Request, principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one(
        "SELECT * FROM company_brain_snapshots WHERE company_id=? "
        "ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END,version DESC LIMIT 1", (company_id,),
    )
    return _snapshot(row) if row else None


def _build_brain(body: BrainBuildRequest, request: Request, principal: Principal,
                 company_header: str | None):
    company_id = _scope(principal, company_header)
    run = request.app.state.runs.create(company_id, "company_brain_build",
                                        {"sources": body.source_document_ids})
    return request.app.state.runs.start(company_id, run["id"])


@router.post("/company-brain/build", status_code=202)
def build_brain(body: BrainBuildRequest, request: Request,
                principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    return _build_brain(body, request, principal, x_company_id)


@router.post("/company-brain/rebuild", status_code=202)
def rebuild_brain(body: BrainBuildRequest, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    return _build_brain(body, request, principal, x_company_id)


@router.patch("/company-brain")
def patch_brain(body: DataPatch, request: Request, principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one(
        "SELECT * FROM company_brain_snapshots WHERE company_id=? AND status='draft' ORDER BY version DESC LIMIT 1",
        (company_id,),
    )
    if not row:
        raise HTTPException(409, "No draft Company Brain snapshot")
    content = {**json_load(row["content"], {}), **body.data}
    request.app.state.db.execute("UPDATE company_brain_snapshots SET content=? WHERE id=?",
                                 (json_dump(content), row["id"]))
    return _snapshot(request.app.state.db.one("SELECT * FROM company_brain_snapshots WHERE id=?", (row["id"],)))


@router.post("/company-brain/approve")
def approve_brain(body: BrainApproveRequest, request: Request,
                  principal: Principal = Depends(current_principal),
                  x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one(
        "SELECT * FROM company_brain_snapshots WHERE id=? AND company_id=? AND status='draft'",
        (body.snapshot_id, company_id),
    )
    if not row:
        raise HTTPException(404, "Draft snapshot not found")
    stamp = now()
    with request.app.state.db.transaction() as conn:
        conn.execute("UPDATE company_brain_snapshots SET status='archived' "
                     "WHERE company_id=? AND status='approved'", (company_id,))
        conn.execute("UPDATE company_brain_snapshots SET status='approved',approved_by=?,approved_at=? WHERE id=?",
                     (principal.id, stamp, body.snapshot_id))
    request.app.state.db.activity(company_id, principal.id, "company_brain_approved",
                                  "company_brain", body.snapshot_id)
    return _snapshot(request.app.state.db.one("SELECT * FROM company_brain_snapshots WHERE id=?",
                                              (body.snapshot_id,)))


@router.get("/company-brain/snapshots")
def brain_snapshots(request: Request, principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return [_snapshot(row) for row in request.app.state.db.all(
        "SELECT * FROM company_brain_snapshots WHERE company_id=? ORDER BY version DESC", (company_id,)
    )]


@router.get("/company-brain/snapshots/{snapshot_id}")
def brain_snapshot(snapshot_id: str, request: Request, principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM company_brain_snapshots WHERE id=? AND company_id=?",
                                   (snapshot_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Snapshot not found")
    return _snapshot(row)
