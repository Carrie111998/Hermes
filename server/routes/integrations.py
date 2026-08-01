from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from ..auth import Principal, company_scope, current_principal
from ..db import json_dump, json_load, new_id, now
from ..email_providers import EMAIL_PROVIDERS
from ..whatsapp_provider import WhatsAppCloudProvider


router = APIRouter(tags=["integrations"])


class IntegrationConnect(BaseModel):
    credentials: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)


class IntegrationPatch(BaseModel):
    credentials: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    status: str | None = None


class WhatsAppProfile(BaseModel):
    business_name: str = Field(min_length=1)
    whatsapp_business_account_id: str = Field(min_length=1)
    phone_number_id: str = Field(min_length=1)
    display_phone_number: str = ""
    business_country: str = "TR"
    default_language: str = "en"


def _scope(principal: Principal, header: str | None) -> str:
    return company_scope(principal, header)


def _integration(row) -> dict:
    return {"id": row["id"], "company_id": row["company_id"], "kind": row["kind"],
            "provider": row["provider"], "status": row["status"],
            "data": json_load(row["data"], {}), "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def _oauth_connect_required(provider: str) -> None:
    raise HTTPException(409, {
        "message": f"Connect {provider} mailboxes with OAuth",
        "oauth_start": f"/api/v1/integrations/email/oauth/{provider}/start",
    })


def _connect(kind: str, provider: str, body: IntegrationConnect, request: Request,
             principal: Principal, company_header: str | None):
    company_id, stamp = _scope(principal, company_header), now()
    if provider != "stub" and not request.app.state.cipher.configured:
        raise HTTPException(503, "Credential encryption is not configured")
    encrypted = request.app.state.cipher.encrypt(body.credentials) if body.credentials else None
    integration_id = new_id("int")
    request.app.state.db.execute(
        "INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)",
        (integration_id, company_id, kind, provider, "connected", encrypted,
         json_dump(body.data), stamp, stamp),
    )
    request.app.state.db.activity(company_id, principal.id, "integration_connected",
                                  "integration", integration_id, {"kind": kind, "provider": provider})
    return _integration(request.app.state.db.one("SELECT * FROM integrations WHERE id=?", (integration_id,)))


@router.get("/integrations/email")
def email_integrations(request: Request, principal: Principal = Depends(current_principal),
                       x_company_id: str | None = Header(default=None)):
    return [_integration(row) for row in request.app.state.db.all(
        "SELECT * FROM integrations WHERE company_id=? AND kind='email' ORDER BY created_at DESC",
        (_scope(principal, x_company_id),),
    )]


@router.post("/integrations/email/connect/google", status_code=201)
def connect_google(request: Request, body: IntegrationConnect | None = None,
                   principal: Principal = Depends(current_principal),
                   x_company_id: str | None = Header(default=None)):
    _oauth_connect_required("google")


@router.post("/integrations/email/connect/microsoft", status_code=201)
def connect_microsoft(request: Request, body: IntegrationConnect | None = None,
                      principal: Principal = Depends(current_principal),
                      x_company_id: str | None = Header(default=None)):
    _oauth_connect_required("microsoft")


@router.post("/integrations/email/connect/smtp", status_code=201)
def connect_smtp(body: IntegrationConnect, request: Request,
                 principal: Principal = Depends(current_principal),
                 x_company_id: str | None = Header(default=None)):
    """Any email service via username + password (SMTP send, optional IMAP)."""
    required = {"username", "password", "smtp_host"}
    missing = required - {k for k, v in body.credentials.items() if v}
    if missing:
        raise HTTPException(422, {"message": "Missing SMTP credentials", "fields": sorted(missing)})
    data = {**body.data, "mailbox": body.credentials.get("from_addr") or body.credentials["username"]}
    return _connect("email", "smtp", IntegrationConnect(credentials=body.credentials, data=data),
                    request, principal, x_company_id)


@router.post("/integrations/email/connect/browser", status_code=201)
def connect_browser(body: IntegrationConnect, request: Request,
                    principal: Principal = Depends(current_principal),
                    x_company_id: str | None = Header(default=None)):
    """Agent-browser webmail: the agent signs into the webmail UI and drives it."""
    required = {"webmail_url", "username", "password"}
    missing = required - {k for k, v in body.credentials.items() if v}
    if missing:
        raise HTTPException(422, {"message": "Missing webmail credentials", "fields": sorted(missing)})
    data = {**body.data, "mailbox": body.credentials["username"],
            "webmail_url": body.credentials["webmail_url"]}
    return _connect("email", "browser", IntegrationConnect(credentials=body.credentials, data=data),
                    request, principal, x_company_id)


@router.get("/integrations/email/{integration_id}")
def get_email_integration(integration_id: str, request: Request,
                          principal: Principal = Depends(current_principal),
                          x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one(
        "SELECT * FROM integrations WHERE id=? AND company_id=? AND kind='email'",
        (integration_id, _scope(principal, x_company_id)),
    )
    if not row:
        raise HTTPException(404, "Email integration not found")
    return _integration(row)


@router.patch("/integrations/email/{integration_id}")
def patch_email_integration(integration_id: str, body: IntegrationPatch, request: Request,
                            principal: Principal = Depends(current_principal),
                            x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM integrations WHERE id=? AND company_id=? AND kind='email'",
                                   (integration_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Email integration not found")
    data = {**json_load(row["data"], {}), **(body.data or {})}
    encrypted = row["encrypted_credentials"]
    if body.credentials is not None:
        encrypted = request.app.state.cipher.encrypt(body.credentials)
    request.app.state.db.execute(
        "UPDATE integrations SET status=?,encrypted_credentials=?,data=?,updated_at=? WHERE id=?",
        (body.status or row["status"], encrypted, json_dump(data), now(), integration_id),
    )
    return get_email_integration(integration_id, request, principal, x_company_id)


@router.delete("/integrations/email/{integration_id}", status_code=204)
def delete_email_integration(integration_id: str, request: Request,
                             principal: Principal = Depends(current_principal),
                             x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("DELETE FROM integrations WHERE id=? AND company_id=? AND kind='email'",
                                        (integration_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "Email integration not found")


def _email_adapter(row, request: Request):
    credentials = request.app.state.cipher.decrypt(row["encrypted_credentials"])
    cls = EMAIL_PROVIDERS.get(row["provider"])
    if not cls:
        raise HTTPException(422, "Integration provider cannot be tested")
    adapter = cls()
    adapter.connect_account(credentials)
    return adapter


@router.post("/integrations/email/{integration_id}/test")
def test_email_integration(integration_id: str, request: Request,
                           principal: Principal = Depends(current_principal),
                           x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM integrations WHERE id=? AND company_id=? AND kind='email'",
                                   (integration_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Email integration not found")
    adapter = _email_adapter(row, request)
    # A lightweight mailbox read validates token scopes without sending.
    adapter.list_recent_replies()
    return {"ok": True, "provider": row["provider"]}


@router.post("/integrations/email/{integration_id}/refresh-token")
def refresh_email_integration(integration_id: str, request: Request,
                              principal: Principal = Depends(current_principal),
                              x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM integrations WHERE id=? AND company_id=? AND kind='email'",
                                   (integration_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "Email integration not found")
    adapter = _email_adapter(row, request)
    adapter.refresh_token()
    request.app.state.db.execute("UPDATE integrations SET encrypted_credentials=?,updated_at=? WHERE id=?",
                                 (request.app.state.cipher.encrypt(adapter.credentials), now(), integration_id))
    return {"refreshed": True}


@router.get("/integrations/whatsapp")
def whatsapp_integrations(request: Request, principal: Principal = Depends(current_principal),
                          x_company_id: str | None = Header(default=None)):
    return [_integration(row) for row in request.app.state.db.all(
        "SELECT * FROM integrations WHERE company_id=? AND kind='whatsapp' ORDER BY created_at DESC",
        (_scope(principal, x_company_id),),
    )]


def _whatsapp_profile_row(request: Request, company_id: str):
    return request.app.state.db.one(
        "SELECT * FROM integrations WHERE company_id=? AND kind='whatsapp' "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id,),
    )


def _whatsapp_profile(row) -> dict | None:
    if not row:
        return None
    data = json_load(row["data"], {})
    return {
        **data,
        "id": row["id"],
        "company_id": row["company_id"],
        "provider": row["provider"],
        "status": row["status"],
        "data": data,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# Literal profile routes must stay above /integrations/whatsapp/{integration_id}.
@router.get("/integrations/whatsapp/profile")
def whatsapp_profile(request: Request, principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    return _whatsapp_profile(_whatsapp_profile_row(request, company_id))


@router.put("/integrations/whatsapp/profile")
def save_whatsapp_profile(body: WhatsAppProfile, request: Request,
                          principal: Principal = Depends(current_principal),
                          x_company_id: str | None = Header(default=None)):
    company_id, stamp = _scope(principal, x_company_id), now()
    row = _whatsapp_profile_row(request, company_id)
    existing = json_load(row["data"], {}) if row else {}
    data = {
        **existing,
        **body.model_dump(),
        "profile_state": "saved",
        "credential_state": "configured" if row and row["encrypted_credentials"] else "server_required",
        "template_status": existing.get("template_status", "not_configured"),
        "verification": None,
        "profile_saved_at": stamp,
    }
    if row:
        request.app.state.db.execute(
            "UPDATE integrations SET data=?,updated_at=? WHERE id=?",
            (json_dump(data), stamp, row["id"]),
        )
        integration_id = row["id"]
    else:
        integration_id = new_id("int")
        request.app.state.db.execute(
            "INSERT INTO integrations VALUES(?,?,?,?,?,?,?,?,?)",
            (integration_id, company_id, "whatsapp", "profile", "not_connected", None,
             json_dump(data), stamp, stamp),
        )
    request.app.state.db.activity(
        company_id, principal.id, "whatsapp_profile_saved", "integration", integration_id,
    )
    return _whatsapp_profile(request.app.state.db.one(
        "SELECT * FROM integrations WHERE id=?", (integration_id,),
    ))


@router.post("/integrations/whatsapp/profile/verify")
def verify_whatsapp_profile(request: Request, principal: Principal = Depends(current_principal),
                            x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = _whatsapp_profile_row(request, company_id)
    if not row:
        raise HTTPException(422, "Save a WhatsApp Business profile before verifying it")
    data = json_load(row["data"], {})
    required = ("business_name", "whatsapp_business_account_id", "phone_number_id")
    missing = [field for field in required if not str(data.get(field) or "").strip()]
    if missing:
        raise HTTPException(422, {"message": "WhatsApp profile is incomplete", "fields": missing})
    # Only a successful Graph call proves the profile is usable. Passing the
    # local field check alone is "complete", never "verified".
    credentials = request.app.state.db.one(
        "SELECT encrypted_credentials FROM integrations "
        "WHERE company_id=? AND kind='whatsapp' AND encrypted_credentials IS NOT NULL",
        (company_id,),
    )
    if not credentials:
        verification = {"status": "incomplete", "checked_at": now(),
                        "message": "Connect WhatsApp credentials before verifying the profile."}
    else:
        try:
            account = WhatsAppCloudProvider(
                request.app.state.cipher.decrypt(credentials["encrypted_credentials"])).test()
            verification = {"status": "verified", "checked_at": now(),
                            "message": "Meta Graph confirmed the phone number.",
                            "account": account}
        except (httpx.HTTPError, KeyError, RuntimeError) as exc:
            verification = {"status": "failed", "checked_at": now(),
                            "message": f"Meta Graph rejected the credentials: {exc}"}
    data.update({"profile_state": verification["status"], "verification": verification})
    request.app.state.db.execute(
        "UPDATE integrations SET data=?,updated_at=? WHERE id=?",
        (json_dump(data), now(), row["id"]),
    )
    request.app.state.db.activity(
        company_id, principal.id, "whatsapp_profile_verified", "integration", row["id"],
    )
    return verification


def json_loads_or_400(raw: bytes) -> dict:
    try:
        payload = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(400, "Webhook payload is not valid JSON") from exc
    return payload if isinstance(payload, dict) else {}


def _whatsapp_credentials(request: Request):
    """Yield (company_id, credentials) for every WhatsApp integration.

    Webhooks arrive unauthenticated, so the tenant has to be resolved from the
    payload signature. Undecryptable rows are skipped rather than fatal: one
    tenant with a rotated key must not break delivery for everyone else.
    """
    for row in request.app.state.db.all(
        "SELECT company_id, encrypted_credentials FROM integrations WHERE kind='whatsapp'"
    ):
        try:
            yield row["company_id"], request.app.state.cipher.decrypt(row["encrypted_credentials"])
        except RuntimeError:
            continue


def _whatsapp_tenant_for_signature(request: Request, raw: bytes, header: str | None) -> str:
    """Resolve the tenant that signed this payload, or 403.

    Meta signs the raw body with the app secret as HMAC-SHA256. Matching the
    signature both authenticates the call and identifies the company, so the
    same check that rejects forgeries also scopes the writes.
    """
    if not header or not header.startswith("sha256="):
        raise HTTPException(403, "Missing webhook signature")
    provided = header.split("=", 1)[1]
    for company_id, credentials in _whatsapp_credentials(request):
        secret = str(credentials.get("app_secret", ""))
        if not secret:
            continue
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, provided):
            return company_id
    raise HTTPException(403, "Webhook signature verification failed")


@router.get("/integrations/whatsapp/webhook")
def verify_whatsapp_webhook(
    request: Request,
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if mode != "subscribe" or not verify_token:
        raise HTTPException(403, "Webhook verification failed")
    for _company_id, credentials in _whatsapp_credentials(request):
        if secrets.compare_digest(str(credentials.get("webhook_verify_token", "")), verify_token):
            return Response(content=challenge or "", media_type="text/plain")
    raise HTTPException(403, "Webhook verification failed")


@router.post("/integrations/whatsapp/connect", status_code=201)
def connect_whatsapp(body: IntegrationConnect, request: Request,
                     principal: Principal = Depends(current_principal),
                     x_company_id: str | None = Header(default=None)):
    # app_secret is required: without it the webhook cannot verify Meta's
    # signature, and an unverifiable webhook is an unauthenticated write.
    required = {"access_token", "phone_number_id", "whatsapp_business_account_id", "app_secret"}
    missing = required - set(body.credentials)
    if missing:
        raise HTTPException(422, {"message": "Missing WhatsApp credentials", "fields": sorted(missing)})
    return _connect("whatsapp", "meta_cloud", body, request, principal, x_company_id)


@router.get("/integrations/whatsapp/{integration_id}")
def get_whatsapp_integration(integration_id: str, request: Request,
                             principal: Principal = Depends(current_principal),
                             x_company_id: str | None = Header(default=None)):
    row = request.app.state.db.one("SELECT * FROM integrations WHERE id=? AND company_id=? AND kind='whatsapp'",
                                   (integration_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "WhatsApp integration not found")
    return _integration(row)


@router.patch("/integrations/whatsapp/{integration_id}")
def patch_whatsapp_integration(integration_id: str, body: IntegrationPatch, request: Request,
                               principal: Principal = Depends(current_principal),
                               x_company_id: str | None = Header(default=None)):
    # Same storage contract as email integrations.
    row = request.app.state.db.one("SELECT * FROM integrations WHERE id=? AND company_id=? AND kind='whatsapp'",
                                   (integration_id, _scope(principal, x_company_id)))
    if not row:
        raise HTTPException(404, "WhatsApp integration not found")
    encrypted = (request.app.state.cipher.encrypt(body.credentials)
                 if body.credentials is not None else row["encrypted_credentials"])
    request.app.state.db.execute("UPDATE integrations SET status=?,encrypted_credentials=?,data=?,updated_at=? WHERE id=?",
                                 (body.status or row["status"], encrypted,
                                  json_dump({**json_load(row["data"], {}), **(body.data or {})}),
                                  now(), integration_id))
    return get_whatsapp_integration(integration_id, request, principal, x_company_id)


@router.delete("/integrations/whatsapp/{integration_id}", status_code=204)
def delete_whatsapp_integration(integration_id: str, request: Request,
                                principal: Principal = Depends(current_principal),
                                x_company_id: str | None = Header(default=None)):
    if not request.app.state.db.execute("DELETE FROM integrations WHERE id=? AND company_id=? AND kind='whatsapp'",
                                        (integration_id, _scope(principal, x_company_id))):
        raise HTTPException(404, "WhatsApp integration not found")


@router.post("/integrations/whatsapp/{integration_id}/test")
def test_whatsapp_integration(integration_id: str, request: Request,
                              principal: Principal = Depends(current_principal),
                              x_company_id: str | None = Header(default=None)):
    company_id = _scope(principal, x_company_id)
    row = request.app.state.db.one("SELECT * FROM integrations WHERE id=? AND company_id=? AND kind='whatsapp'",
                                   (integration_id, company_id))
    if not row:
        raise HTTPException(404, "WhatsApp integration not found")
    return {"ok": True, "account": WhatsAppCloudProvider(
        request.app.state.cipher.decrypt(row["encrypted_credentials"])).test()}


@router.post("/integrations/whatsapp/webhook", status_code=204)
async def whatsapp_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
):
    raw = await request.body()
    company_id = _whatsapp_tenant_for_signature(request, raw, signature)
    payload = json_loads_or_400(raw)
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for status_event in value.get("statuses", []):
                provider_id = status_event.get("id")
                value_status = status_event.get("status")
                if provider_id and value_status:
                    request.app.state.db.execute(
                        "UPDATE outreach_messages SET status=?,updated_at=? "
                        "WHERE provider_message_id=? AND company_id=?",
                        (value_status, now(), provider_id, company_id),
                    )
            for inbound in value.get("messages", []):
                context_id = (inbound.get("context") or {}).get("id")
                if context_id:
                    request.app.state.db.execute(
                        "UPDATE outreach_messages SET replied_at=?,updated_at=? "
                        "WHERE provider_message_id=? AND company_id=?",
                        (now(), now(), context_id, company_id),
                    )
