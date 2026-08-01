"""OAuth authorization-code flow for Gmail and Microsoft Graph.

Without this, connecting a mailbox meant an operator hand-pasting a
`refresh_token` and `client_secret` into `POST /integrations/email/connect/*`,
which no customer can be asked to do. One OAuth app per provider is shared by
every tenant; the tenant authorizes against it and only its own refresh token is
stored, encrypted, against its company id.

The callback is necessarily unauthenticated — the provider redirects the
customer's browser to it with no bearer token. Authorization therefore rides in
the `state` parameter, which is an HMAC over (company_id, provider, nonce,
issued_at). That both prevents CSRF and identifies the tenant, and it expires,
so a leaked callback URL cannot be replayed later.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import time

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from ..auth import Principal, company_scope, current_principal
from ..db import json_dump, new_id, now


router = APIRouter(tags=["integrations"])

STATE_TTL_SECONDS = 600
CALLBACK_STATUSES = frozenset({"connected", "cancelled", "failed"})

# scope choices: send + read for reply polling, and the minimum beyond that.
PROVIDERS = {
    "google": {
        "kind": "google",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scope": "https://www.googleapis.com/auth/gmail.modify",
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
    "microsoft": {
        "kind": "microsoft",
        "authorize": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        "scope": "offline_access Mail.ReadWrite Mail.Send User.Read",
        "extra": {},
    },
}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _app_credentials(settings, provider: str) -> tuple[str, str]:
    if provider == "google":
        pair = (settings.google_oauth_client_id, settings.google_oauth_client_secret)
        env = "GOOGLE_OAUTH_CLIENT_ID/GOOGLE_OAUTH_CLIENT_SECRET"
    else:
        pair = (settings.microsoft_oauth_client_id, settings.microsoft_oauth_client_secret)
        env = "MICROSOFT_OAUTH_CLIENT_ID/MICROSOFT_OAUTH_CLIENT_SECRET"
    if not all(pair):
        raise HTTPException(503, f"OAuth is not configured on this server: set {env}")
    return pair


def _secret(settings) -> str:
    if not settings.credential_key:
        raise HTTPException(503, "INTERFAZE_CREDENTIAL_KEY is required for OAuth")
    return settings.credential_key


def sign_state(secret: str, company_id: str, provider: str) -> str:
    payload = json.dumps({"c": company_id, "p": provider, "t": int(time.time()),
                          "n": secrets.token_urlsafe(8)},
                         separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:16]
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_state(secret: str, state: str, provider: str) -> str:
    """Return the company id a valid, unexpired state was issued for."""
    if not state or "." not in state:
        raise HTTPException(400, "Missing or malformed OAuth state")
    encoded, signature = state.rsplit(".", 1)
    try:
        payload = _unb64(encoded)
        provided = _unb64(signature)
    except (ValueError, TypeError):
        raise HTTPException(400, "Malformed OAuth state")
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(400, "OAuth state signature does not verify")
    try:
        data = json.loads(payload)
    except ValueError:
        raise HTTPException(400, "Malformed OAuth state")
    if data.get("p") != provider:
        raise HTTPException(400, "OAuth state was issued for a different provider")
    if int(time.time()) - int(data.get("t", 0)) > STATE_TTL_SECONDS:
        raise HTTPException(400, "This authorization link has expired; start again")
    return str(data.get("c") or "")


def _redirect_uri(settings, provider: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/v1/integrations/email/oauth/{provider}/callback"


@router.post("/integrations/email/oauth/{provider}/start")
def start_oauth(provider: str, request: Request,
                principal: Principal = Depends(current_principal),
                x_company_id: str | None = Header(default=None)):
    """Return the URL the customer's browser should visit to authorize."""
    spec = PROVIDERS.get(provider)
    if not spec:
        raise HTTPException(404, f"Unsupported OAuth provider: {provider}")
    settings = request.app.state.settings
    client_id, _secret_value = _app_credentials(settings, provider)
    company_id = company_scope(principal, x_company_id)
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(settings, provider),
        "response_type": "code",
        "scope": spec["scope"],
        "state": sign_state(_secret(settings), company_id, provider),
        **spec["extra"],
    }
    authorize = spec["authorize"].format(tenant=settings.microsoft_oauth_tenant)
    return {"authorize_url": f"{authorize}?{httpx.QueryParams(params)}",
            "redirect_uri": params["redirect_uri"], "expires_in": STATE_TTL_SECONDS}


def _page(title: str, body: str, *, provider: str, status: str,
          status_code: int = 200, close: bool = False) -> HTMLResponse:
    if provider not in PROVIDERS or status not in CALLBACK_STATUSES:
        raise ValueError("invalid OAuth callback message")
    message = json.dumps({
        "type": "interfaze:oauth",
        "provider": provider,
        "status": status,
    }).replace("</", "<\\/")
    close_script = "window.close();" if close else ""
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:14vh auto;padding:0 1.5rem}}</style>
</head><body><h1>{html.escape(title)}</h1><p>{html.escape(body)}</p>
<script>(()=>{{const message={message};if(window.opener&&!window.opener.closed){{window.opener.postMessage(message,window.location.origin);}}{close_script}}})();</script>
</body></html>""",
        status_code=status_code,
    )


def _failure_page(provider: str, status_code: int, body: str) -> HTMLResponse:
    return _page("Authorization failed", body, provider=provider,
                 status="failed", status_code=status_code)


def _invalid_request_page(provider: str) -> HTMLResponse:
    return _failure_page(
        provider,
        400,
        "This authorization request is invalid or expired. Return to Interfaze and start again.",
    )


def _exchange_failure_page(provider: str) -> HTMLResponse:
    return _failure_page(
        provider,
        502,
        "The provider could not complete the connection. Return to Interfaze and try again.",
    )


@router.get("/integrations/email/oauth/{provider}/callback", response_class=HTMLResponse)
def oauth_callback(provider: str, request: Request,
                   code: str | None = Query(default=None),
                   state: str | None = Query(default=None),
                   error: str | None = Query(default=None)):
    spec = PROVIDERS.get(provider)
    if not spec:
        raise HTTPException(404, f"Unsupported OAuth provider: {provider}")
    settings = request.app.state.settings
    try:
        company_id = verify_state(_secret(settings), state or "", provider)
    except HTTPException as exc:
        if exc.status_code == 503:
            return _failure_page(provider, 503, "OAuth is not configured on this server.")
        return _invalid_request_page(provider)
    if error:
        return _page(
            "Authorization cancelled",
            "The provider authorization was cancelled. Return to Interfaze to try again.",
            provider=provider,
            status="cancelled",
        )
    if not code:
        return _invalid_request_page(provider)
    try:
        client_id, client_secret = _app_credentials(settings, provider)
    except HTTPException:
        return _failure_page(provider, 503, "OAuth is not configured on this server.")
    token_url = spec["token"].format(tenant=settings.microsoft_oauth_tenant)
    try:
        response = httpx.post(token_url, timeout=30, data={
            "grant_type": "authorization_code", "code": code,
            "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": _redirect_uri(settings, provider),
        })
    except httpx.HTTPError:
        return _exchange_failure_page(provider)
    if response.status_code >= 400:
        return _exchange_failure_page(provider)
    try:
        tokens = response.json()
    except ValueError:
        return _exchange_failure_page(provider)
    if not isinstance(tokens, dict):
        return _exchange_failure_page(provider)
    refresh = tokens.get("refresh_token")
    if not refresh:
        return _exchange_failure_page(provider)
    credentials = {"refresh_token": refresh, "access_token": tokens.get("access_token", ""),
                   "client_id": client_id, "client_secret": client_secret}
    try:
        _store(request, company_id, spec["kind"], credentials)
    except HTTPException as exc:
        return _failure_page(
            provider,
            exc.status_code,
            "Credential encryption is not configured on this server.",
        )
    return _page(
        "Mailbox connected",
        "Your mailbox is connected. Return to Interfaze if this window does not close.",
        provider=provider,
        status="connected",
        close=True,
    )


def _store(request: Request, company_id: str, provider_kind: str, credentials: dict) -> None:
    """Replace any prior email integration for this tenant, encrypted at rest."""
    db, cipher, stamp = request.app.state.db, request.app.state.cipher, now()
    if not cipher.configured:
        raise HTTPException(503, "Credential encryption is not configured")
    existing = db.one(
        "SELECT id FROM integrations WHERE company_id=? AND kind='email' AND provider=?",
        (company_id, provider_kind),
    )
    encrypted = cipher.encrypt(credentials)
    if existing:
        db.execute(
            "UPDATE integrations SET encrypted_credentials=?,status='connected',updated_at=? WHERE id=?",
            (encrypted, stamp, existing["id"]),
        )
        integration_id = existing["id"]
    else:
        integration_id = new_id("int")
        db.execute(
            "INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)",
            (integration_id, company_id, "email", provider_kind, "connected",
             encrypted, json_dump({"connected_via": "oauth"}), stamp, stamp),
        )
    db.activity(company_id, None, "email_integration_connected", "integration", integration_id,
                {"provider": provider_kind, "via": "oauth"})
