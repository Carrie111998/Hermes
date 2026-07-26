from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..auth import AuthService, Principal, auth_service, current_principal, hash_password
from ..db import Database, new_id, now
from ..schemas import LoginRequest, PasswordResetConfirm, PasswordResetRequest, RefreshRequest


router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)


def _user_response(row) -> dict:
    return {
        "id": row["id"], "email": row["email"], "role": row["role"],
        "company_id": row["company_id"], "status": row["status"],
    }


@router.post("/login")
def login(body: LoginRequest, request: Request, service: AuthService = Depends(auth_service)):
    client_id = request.client.host if request.client else "unknown"
    return service.login(body.email, body.password, client_id)


@router.post("/logout", status_code=204)
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    service: AuthService = Depends(auth_service),
):
    if credentials:
        service.logout(credentials.credentials)


@router.get("/me")
def me(principal: Principal = Depends(current_principal), service: AuthService = Depends(auth_service)):
    row = service.db.one("SELECT * FROM users WHERE id=?", (principal.id,))
    return _user_response(row)


@router.post("/refresh")
def refresh(body: RefreshRequest, service: AuthService = Depends(auth_service)):
    return service.refresh(body.refresh_token)


@router.post("/password-reset/request", status_code=202)
def request_password_reset(body: PasswordResetRequest, request: Request):
    return request.app.state.auth.request_password_reset(body.email)


@router.post("/password-reset/confirm")
def confirm_password_reset(body: PasswordResetConfirm, request: Request):
    if request.app.state.settings.auth_mode == "supabase":
        return request.app.state.auth.confirm_supabase_password_reset(body.token, body.password)
    db: Database = request.app.state.db
    digest = hashlib.sha256(body.token.encode()).hexdigest()
    row = db.one(
        "SELECT * FROM password_reset_tokens WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
        (digest, now()),
    )
    if not row:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token")
    with db.transaction() as conn:
        conn.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?",
                     (hash_password(body.password), now(), row["user_id"]))
        conn.execute("UPDATE password_reset_tokens SET used_at=? WHERE token_hash=?", (now(), digest))
        conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                     (now(), row["user_id"]))
    return {"reset": True}
