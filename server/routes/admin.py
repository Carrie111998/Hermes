from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..auth import Principal, hash_password, require_admin
from ..db import Database, json_dump, json_load, new_id, now
from ..schemas import AssignCompany, CompanyCreate, CompanyPatch, ResetPassword, UserCreate, UserPatch


router = APIRouter(prefix="/admin", tags=["admin"])


def _company(row, db: Database | None = None) -> dict:
    result = {
        "id": row["id"], "name": row["name"], "legal_name": row["legal_name"],
        "status": row["status"], "data": json_load(row["data"], {}),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }
    if db is not None:
        result["users"] = db.one(
            "SELECT COUNT(*) AS n FROM users WHERE company_id=? AND status='active'", (row["id"],)
        )["n"]
        result["last_seen_at"] = db.one(
            "SELECT MAX(s.created_at) AS value FROM auth_sessions s "
            "JOIN users u ON u.id=s.user_id WHERE u.company_id=?", (row["id"],)
        )["value"]
    return result


def _user(row) -> dict:
    return {
        "id": row["id"], "email": row["email"], "role": row["role"],
        "company_id": row["company_id"], "status": row["status"],
        "data": json_load(row["data"], {}), "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/errors")
def errors(request: Request, limit: int = Query(default=200, ge=1, le=500),
           _: Principal = Depends(require_admin)):
    return [{
        "id": row["id"],
        "level": "error",
        "area": row["run_type"],
        "message": row["error"] or "Agent run failed",
        "at": row["completed_at"] or row["updated_at"],
        "company_id": row["company_id"],
    } for row in request.app.state.db.all(
        "SELECT id,company_id,run_type,error,completed_at,updated_at FROM agent_runs "
        "WHERE status='failed' ORDER BY COALESCE(completed_at,updated_at) DESC LIMIT ?",
        (limit,),
    )]


@router.get("/logs")
def logs(request: Request, limit: int = Query(default=100, ge=1, le=500),
         _: Principal = Depends(require_admin)):
    return [{
        "id": row["id"],
        "area": row["entity_type"] or "system",
        "message": str(row["action"]).replace("_", " "),
        "at": row["created_at"],
        "company_id": row["company_id"],
    } for row in request.app.state.db.all(
        "SELECT id,company_id,action,entity_type,created_at FROM activity_log "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )]


@router.get("/companies")
def companies(request: Request, _: Principal = Depends(require_admin)):
    db = request.app.state.db
    return [_company(row, db) for row in db.all("SELECT * FROM companies ORDER BY name")]


@router.post("/companies", status_code=201)
def create_company(body: CompanyCreate, request: Request, actor: Principal = Depends(require_admin)):
    db: Database = request.app.state.db
    company_id, stamp = new_id("cmp"), now()
    db.execute("INSERT INTO companies VALUES(?,?,?,?,?,?,?)",
               (company_id, body.name, body.legal_name, body.status,
                json_dump(body.data), stamp, stamp))
    db.execute("INSERT INTO onboarding(company_id,updated_at) VALUES(?,?)", (company_id, stamp))
    db.activity(company_id, actor.id, "company_created", "company", company_id)
    return _company(db.one("SELECT * FROM companies WHERE id=?", (company_id,)), db)


@router.get("/companies/{company_id}")
def get_company(company_id: str, request: Request, _: Principal = Depends(require_admin)):
    row = request.app.state.db.one("SELECT * FROM companies WHERE id=?", (company_id,))
    if not row:
        raise HTTPException(404, "Company not found")
    return _company(row, request.app.state.db)


@router.patch("/companies/{company_id}")
def patch_company(company_id: str, body: CompanyPatch, request: Request,
                  actor: Principal = Depends(require_admin)):
    db: Database = request.app.state.db
    row = db.one("SELECT * FROM companies WHERE id=?", (company_id,))
    if not row:
        raise HTTPException(404, "Company not found")
    values = body.model_dump(exclude_unset=True)
    data = values.pop("data", None)
    if data is not None:
        values["data"] = json_dump({**json_load(row["data"], {}), **data})
    values["updated_at"] = now()
    db.execute(f"UPDATE companies SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
               (*values.values(), company_id))
    db.activity(company_id, actor.id, "company_updated", "company", company_id)
    return _company(db.one("SELECT * FROM companies WHERE id=?", (company_id,)), db)


@router.delete("/companies/{company_id}", status_code=204)
def delete_company(company_id: str, request: Request, actor: Principal = Depends(require_admin)):
    db: Database = request.app.state.db
    # Preserve tenant history and referential integrity: PRODUCT's DELETE is a
    # soft-delete operation for companies.
    if not db.execute("UPDATE companies SET status='disabled',updated_at=? WHERE id=?", (now(), company_id)):
        raise HTTPException(404, "Company not found")
    db.activity(company_id, actor.id, "company_disabled", "company", company_id)


def _company_status(company_id: str, value: str, request: Request, actor: Principal):
    db: Database = request.app.state.db
    if not db.execute("UPDATE companies SET status=?,updated_at=? WHERE id=?", (value, now(), company_id)):
        raise HTTPException(404, "Company not found")
    db.activity(company_id, actor.id, f"company_{value}", "company", company_id)
    return _company(db.one("SELECT * FROM companies WHERE id=?", (company_id,)), db)


@router.post("/companies/{company_id}/activate")
def activate_company(company_id: str, request: Request, actor: Principal = Depends(require_admin)):
    return _company_status(company_id, "active", request, actor)


@router.post("/companies/{company_id}/disable")
def disable_company(company_id: str, request: Request, actor: Principal = Depends(require_admin)):
    return _company_status(company_id, "disabled", request, actor)


@router.post("/companies/{company_id}/suspend")
def suspend_company(company_id: str, request: Request, actor: Principal = Depends(require_admin)):
    return _company_status(company_id, "suspended", request, actor)


@router.get("/users")
def users(request: Request, _: Principal = Depends(require_admin)):
    return [_user(row) for row in request.app.state.db.all("SELECT * FROM users ORDER BY email")]


@router.post("/users", status_code=201)
def create_user(body: UserCreate, request: Request, actor: Principal = Depends(require_admin)):
    db: Database = request.app.state.db
    if body.role == "customer" and not body.company_id:
        raise HTTPException(422, "Customer users require company_id")
    if body.company_id and not db.one("SELECT id FROM companies WHERE id=?", (body.company_id,)):
        raise HTTPException(422, "Company not found")
    user_id, stamp, external_id = new_id("usr"), now(), None
    if request.app.state.settings.auth_mode == "supabase":
        settings = request.app.state.settings
        if not settings.supabase_url or not settings.supabase_service_role_key:
            raise HTTPException(503, "Supabase admin provisioning is not configured")
        if not body.password:
            raise HTTPException(422, "A temporary password is required for Supabase user creation")
        try:
            remote = httpx.post(
                f"{settings.supabase_url}/auth/v1/admin/users",
                headers={"Authorization": f"Bearer {settings.supabase_service_role_key}",
                         "apikey": settings.supabase_service_role_key},
                json={"email": body.email.lower(), "password": body.password,
                      "email_confirm": True, "user_metadata": {"interfaze_user_id": user_id}},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Supabase user provisioning failed") from exc
        if remote.status_code >= 400:
            raise HTTPException(502, f"Supabase user provisioning failed: {remote.text[:300]}")
        external_id = remote.json()["id"]
    try:
        db.execute(
            "INSERT INTO users(id,email,password_hash,external_id,role,company_id,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, body.email.lower(),
             hash_password(body.password) if body.password and not external_id else None,
             external_id, body.role, body.company_id, "active", json_dump(body.data), stamp, stamp),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc):
            raise HTTPException(409, "Email already exists") from exc
        raise
    db.activity(body.company_id, actor.id, "user_created", "user", user_id)
    return _user(db.one("SELECT * FROM users WHERE id=?", (user_id,)))


@router.get("/users/{user_id}")
def get_user(user_id: str, request: Request, _: Principal = Depends(require_admin)):
    row = request.app.state.db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(404, "User not found")
    return _user(row)


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, request: Request,
               actor: Principal = Depends(require_admin)):
    db: Database = request.app.state.db
    row = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not row:
        raise HTTPException(404, "User not found")
    values = body.model_dump(exclude_unset=True)
    data = values.pop("data", None)
    if data is not None:
        values["data"] = json_dump({**json_load(row["data"], {}), **data})
    if "email" in values and values["email"]:
        values["email"] = values["email"].lower()
    values["updated_at"] = now()
    db.execute(f"UPDATE users SET {','.join(f'{key}=?' for key in values)} WHERE id=?",
               (*values.values(), user_id))
    db.activity(values.get("company_id", row["company_id"]), actor.id,
                "user_updated", "user", user_id)
    return _user(db.one("SELECT * FROM users WHERE id=?", (user_id,)))


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: str, request: Request, actor: Principal = Depends(require_admin)):
    if not request.app.state.db.execute(
        "UPDATE users SET status='disabled',updated_at=? WHERE id=?", (now(), user_id)
    ):
        raise HTTPException(404, "User not found")


@router.post("/users/{user_id}/assign-company")
def assign_company(user_id: str, body: AssignCompany, request: Request,
                   actor: Principal = Depends(require_admin)):
    if not request.app.state.db.one("SELECT id FROM companies WHERE id=?", (body.company_id,)):
        raise HTTPException(422, "Company not found")
    request.app.state.db.execute("UPDATE users SET company_id=?,updated_at=? WHERE id=?",
                                 (body.company_id, now(), user_id))
    return get_user(user_id, request, actor)


@router.post("/users/{user_id}/reset-password", status_code=204)
def admin_reset_password(user_id: str, body: ResetPassword, request: Request,
                         _: Principal = Depends(require_admin)):
    db: Database = request.app.state.db
    user = db.one("SELECT * FROM users WHERE id=?", (user_id,))
    if not user:
        raise HTTPException(404, "User not found")
    if request.app.state.settings.auth_mode == "supabase":
        settings = request.app.state.settings
        if not user["external_id"]:
            raise HTTPException(409, "User is not bound to Supabase Auth")
        try:
            response = httpx.put(
                f"{settings.supabase_url}/auth/v1/admin/users/{user['external_id']}",
                headers={"Authorization": f"Bearer {settings.supabase_service_role_key}",
                         "apikey": settings.supabase_service_role_key},
                json={"password": body.password}, timeout=15,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(503, "Supabase password reset failed") from exc
        if response.status_code >= 400:
            raise HTTPException(502, "Supabase password reset failed")
        return
    if not db.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?",
                      (hash_password(body.password), now(), user_id)):
        raise HTTPException(404, "User not found")
    db.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
               (now(), user_id))


@router.post("/users/{user_id}/disable", status_code=204)
def disable_user(user_id: str, request: Request, _: Principal = Depends(require_admin)):
    if not request.app.state.db.execute("UPDATE users SET status='disabled',updated_at=? WHERE id=?",
                                        (now(), user_id)):
        raise HTTPException(404, "User not found")
