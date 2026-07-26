"""Public opt-out endpoints.

Deliberately unauthenticated: the recipient of a cold email has no account.
Authorization comes from the HMAC in the token, which binds the request to one
(company, email) pair and cannot be forged or retargeted at another tenant.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import compliance


router = APIRouter(tags=["unsubscribe"])


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:12vh auto;padding:0 1.5rem;color:#1a1a1a}}
button{{font:inherit;padding:.6rem 1.2rem;border:0;border-radius:.4rem;background:#1a1a1a;color:#fff;cursor:pointer}}
.muted{{color:#666;font-size:.9rem}}</style></head>
<body><h1>{title}</h1><p>{body}</p>{form}</body></html>"""

_FORM = """<form method="post"><button type="submit">Confirm unsubscribe</button></form>
<p class="muted">{email}</p>"""


def _resolve(request: Request, token: str) -> tuple[str, str]:
    resolved = compliance.verify_token(request.app.state.settings.credential_key, token)
    if not resolved:
        raise HTTPException(404, "This unsubscribe link is invalid or has expired")
    return resolved


@router.get("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_form(token: str, request: Request):
    _company_id, email = _resolve(request, token)
    return HTMLResponse(_PAGE.format(
        title="Unsubscribe", body="Confirm that you no longer want to receive these emails.",
        form=_FORM.format(email=email),
    ))


@router.post("/unsubscribe/{token}", response_class=HTMLResponse)
def unsubscribe_confirm(token: str, request: Request):
    """Also the RFC 8058 one-click target, which mail clients POST directly."""
    company_id, email = _resolve(request, token)
    compliance.suppress(request.app.state.db, company_id, email, "recipient_unsubscribed")
    request.app.state.db.activity(company_id, None, "email_unsubscribed", "suppression", email)
    return HTMLResponse(_PAGE.format(
        title="Unsubscribed",
        body="You will not receive further outreach from this sender.", form="",
    ))
