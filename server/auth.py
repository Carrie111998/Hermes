"""Authentication and tenant authorization for the product API."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
import threading
from dataclasses import dataclass

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .db import Database, json_dump, new_id, now


_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    id: str
    email: str
    role: str
    company_id: str | None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return "pbkdf2_sha256$310000$%s$%s" % (
        base64.urlsafe_b64encode(salt).decode(),
        base64.urlsafe_b64encode(digest).decode(),
    )


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded:
        return False
    try:
        algorithm, rounds, salt_b64, expected_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(expected_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._login_attempts: dict[str, list[float]] = {}
        self._login_lock = threading.Lock()

    def bootstrap_admin(self) -> None:
        if not self.settings.bootstrap_admin_email or not self.settings.bootstrap_admin_password:
            return
        existing = self.db.one("SELECT id FROM users WHERE role='admin' LIMIT 1")
        if existing:
            return
        stamp = now()
        self.db.execute(
            "INSERT INTO users(id,email,password_hash,role,status,data,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (new_id("usr"), self.settings.bootstrap_admin_email.lower(),
             hash_password(self.settings.bootstrap_admin_password), "admin", "active",
             json_dump({}), stamp, stamp),
        )

    def login(self, email: str, password: str, client_id: str = "unknown") -> dict:
        if self.settings.auth_mode != "local":
            return self._supabase_password_login(email, password)
        key = f"{client_id}:{email.strip().lower()}"
        stamp = time.monotonic()
        with self._login_lock:
            cutoff = stamp - self.settings.auth_window_seconds
            attempts = [value for value in self._login_attempts.get(key, []) if value >= cutoff]
            self._login_attempts[key] = attempts
            if len(attempts) >= self.settings.auth_max_attempts:
                raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many sign-in attempts. Try again shortly.")
        user = self.db.one("SELECT * FROM users WHERE lower(email)=lower(?)", (email,))
        if not user or user["status"] != "active" or not verify_password(password, user["password_hash"]):
            with self._login_lock:
                self._login_attempts.setdefault(key, []).append(stamp)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
        with self._login_lock:
            self._login_attempts.pop(key, None)
        return self._issue_session(user["id"])

    def _issue_session(self, user_id: str) -> dict:
        access = secrets.token_urlsafe(36)
        refresh = secrets.token_urlsafe(48)
        stamp = now()
        expires = stamp + 3600
        refresh_expires = stamp + 30 * 86400
        self.db.execute(
            "INSERT INTO auth_sessions VALUES(?,?,?,?,?,?,?)",
            (_token_hash(access), _token_hash(refresh), user_id, expires,
             refresh_expires, None, stamp),
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": 3600,
        }

    def refresh(self, refresh_token: str) -> dict:
        if self.settings.auth_mode == "supabase":
            return self._supabase_refresh(refresh_token)
        row = self.db.one(
            "SELECT * FROM auth_sessions WHERE refresh_hash=? AND revoked_at IS NULL",
            (_token_hash(refresh_token),),
        )
        if not row or row["refresh_expires_at"] <= now():
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
        self.db.execute("UPDATE auth_sessions SET revoked_at=? WHERE refresh_hash=?",
                        (now(), row["refresh_hash"]))
        return self._issue_session(row["user_id"])

    def logout(self, token: str) -> None:
        if self.settings.auth_mode == "supabase":
            if self.settings.supabase_url and self.settings.supabase_anon_key:
                try:
                    httpx.post(
                        f"{self.settings.supabase_url}/auth/v1/logout",
                        headers={"Authorization": f"Bearer {token}",
                                 "apikey": self.settings.supabase_anon_key}, timeout=10,
                    )
                except httpx.HTTPError:
                    pass
            return
        self.db.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?",
                        (now(), _token_hash(token)))

    def authenticate(self, token: str) -> Principal:
        if self.settings.auth_mode == "supabase":
            return self._authenticate_supabase(token)
        row = self.db.one(
            "SELECT u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?",
            (_token_hash(token), now()),
        )
        if not row or row["status"] != "active":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired access token")
        return Principal(row["id"], row["email"], row["role"], row["company_id"])

    def _authenticate_supabase(self, token: str) -> Principal:
        if not self.settings.supabase_url or not self.settings.supabase_anon_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Supabase auth is not configured")
        try:
            response = httpx.get(
                f"{self.settings.supabase_url}/auth/v1/user",
                headers={"Authorization": f"Bearer {token}", "apikey": self.settings.supabase_anon_key},
                timeout=10,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication service unavailable") from exc
        if response.status_code != 200:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Supabase token")
        remote = response.json()
        subject, email = remote.get("id"), (remote.get("email") or "").lower()
        row = self.db.one(
            "SELECT * FROM users WHERE external_id=? OR lower(email)=lower(?)",
            (subject, email),
        )
        if not row or row["status"] != "active":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "User is not provisioned")
        if not row["external_id"]:
            self.db.execute("UPDATE users SET external_id=?,updated_at=? WHERE id=?",
                            (subject, now(), row["id"]))
        return Principal(row["id"], row["email"], row["role"], row["company_id"])

    def _supabase_password_login(self, email: str, password: str) -> dict:
        if not self.settings.supabase_url or not self.settings.supabase_anon_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Supabase auth is not configured")
        try:
            response = httpx.post(
                f"{self.settings.supabase_url}/auth/v1/token?grant_type=password",
                headers={"apikey": self.settings.supabase_anon_key},
                json={"email": email, "password": password},
                timeout=15,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication service unavailable") from exc
        if response.status_code >= 400:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
        return response.json()

    def _supabase_refresh(self, refresh_token: str) -> dict:
        if not self.settings.supabase_url or not self.settings.supabase_anon_key:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Supabase auth is not configured")
        try:
            response = httpx.post(
                f"{self.settings.supabase_url}/auth/v1/token?grant_type=refresh_token",
                headers={"apikey": self.settings.supabase_anon_key},
                json={"refresh_token": refresh_token}, timeout=15,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication service unavailable") from exc
        if response.status_code >= 400:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
        return response.json()

    def request_password_reset(self, email: str) -> dict:
        if self.settings.auth_mode == "supabase":
            if not self.settings.supabase_url or not self.settings.supabase_anon_key:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Supabase auth is not configured")
            try:
                httpx.post(
                    f"{self.settings.supabase_url}/auth/v1/recover",
                    headers={"apikey": self.settings.supabase_anon_key},
                    json={"email": email}, timeout=15,
                )
            except httpx.HTTPError:
                pass
            return {"accepted": True}
        row = self.db.one("SELECT id FROM users WHERE lower(email)=lower(?) AND status='active'", (email,))
        if not row:
            return {"accepted": True}
        token = secrets.token_urlsafe(40)
        self.db.execute(
            "INSERT INTO password_reset_tokens VALUES(?,?,?,?,?)",
            (_token_hash(token), row["id"], now() + 3600, None, now()),
        )
        self.db.activity(None, row["id"], "password_reset_requested", "user", row["id"])
        # Local auth is a development backend. Returning the token makes its
        # reset flow usable without pretending an email delivery service exists.
        return {"accepted": True, "reset_token": token}

    def confirm_supabase_password_reset(self, access_token: str, password: str) -> dict:
        try:
            response = httpx.put(
                f"{self.settings.supabase_url}/auth/v1/user",
                headers={"Authorization": f"Bearer {access_token}",
                         "apikey": self.settings.supabase_anon_key},
                json={"password": password}, timeout=15,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication service unavailable") from exc
        if response.status_code >= 400:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired recovery token")
        return {"reset": True}


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    service: AuthService = Depends(auth_service),
) -> Principal:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bearer token required")
    return service.authenticate(credentials.credentials)


def require_admin(principal: Principal = Depends(current_principal)) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Administrator role required")
    return principal


def company_scope(principal: Principal, requested_company_id: str | None = None) -> str:
    if principal.is_admin:
        if not requested_company_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "company_id is required for admin requests")
        return requested_company_id
    if not principal.company_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "User is not assigned to a company")
    if requested_company_id and requested_company_id != principal.company_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-company access denied")
    return principal.company_id
