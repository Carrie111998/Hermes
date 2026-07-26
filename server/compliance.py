"""Cold-outreach compliance: opt-out tokens, suppression, and footer injection.

CAN-SPAM (US), GDPR Art. 21 / ePrivacy (EU), and KVKK (TR) all require that a
commercial message tell the recipient how to stop receiving them and honor that
request. This module is the single place that guarantees it, so no send path can
skip it: ``inject_footer`` is called by the delivery adapter boundary and
``assert_not_suppressed`` by the eligibility gate.

Tokens are stateless HMACs over ``company_id:email`` so an unsubscribe link
stays valid without a lookup table and cannot be forged into another tenant.
"""
from __future__ import annotations

import base64
import hmac
import hashlib

from .db import now


UNSUBSCRIBE_MARKER = "{{unsubscribe_url}}"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def sign_token(secret: str, company_id: str, email: str) -> str:
    """Opaque, tamper-evident opt-out token. Empty secret disables signing."""
    if not secret:
        raise RuntimeError("INTERFAZE_CREDENTIAL_KEY is required to issue opt-out links")
    payload = f"{company_id}:{normalize_email(email)}".encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:16]
    return f"{_b64(payload)}.{_b64(signature)}"


def verify_token(secret: str, token: str) -> tuple[str, str] | None:
    """Return ``(company_id, email)`` for a valid token, else ``None``."""
    if not secret or not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    try:
        payload = _unb64(encoded)
        provided = _unb64(signature)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(secret.encode(), payload, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(expected, provided):
        return None
    decoded = payload.decode("utf-8", "replace")
    if ":" not in decoded:
        return None
    company_id, email = decoded.split(":", 1)
    return company_id, email


def unsubscribe_url(base_url: str, secret: str, company_id: str, email: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1/unsubscribe/{sign_token(secret, company_id, email)}"


def inject_footer(body: str, url: str, language: str | None = None) -> str:
    """Guarantee the body carries a working opt-out link.

    A template may place ``{{unsubscribe_url}}`` deliberately; otherwise a
    footer is appended. Either way the sent body always contains the URL, which
    is what ``quality.preflight_message`` asserts.
    """
    if UNSUBSCRIBE_MARKER in body:
        return body.replace(UNSUBSCRIBE_MARKER, url)
    if url in body:
        return body
    notice = _FOOTERS.get((language or "en").lower()[:2], _FOOTERS["en"])
    return f"{body.rstrip()}\n\n---\n{notice.format(url=url)}\n"


# ponytail: two languages, because the product ships Turkish-first and sells in
# English. Add a locale when a customer actually sends in one.
_FOOTERS = {
    "en": "You received this because we believe it is relevant to your business. "
          "To stop receiving these emails, unsubscribe here: {url}",
    "tr": "Bu e-postayı işiniz için ilgili olduğunu düşündüğümüz için aldınız. "
          "Bu e-postaları almayı durdurmak için: {url}",
}


def is_suppressed(db, company_id: str, email: str) -> bool:
    if not email:
        return False
    return db.one(
        "SELECT email FROM suppressions WHERE company_id=? AND email=?",
        (company_id, normalize_email(email)),
    ) is not None


def suppress(db, company_id: str, email: str, reason: str) -> bool:
    """Record a tenant-wide opt-out. Returns False if already suppressed.

    Tenant-wide on purpose: a per-lead ``do_not_contact`` flag is resurrected
    the moment the same address is re-imported as a new lead.
    """
    email = normalize_email(email)
    if not email or is_suppressed(db, company_id, email):
        return False
    db.execute(
        "INSERT INTO suppressions(company_id,email,reason,created_at) VALUES(?,?,?,?)",
        (company_id, email, reason, now()),
    )
    # Mirror onto contacts so the UI shows it immediately. Not the enforcement
    # point — `is_suppressed` is, and it is checked for every recipient at send
    # time. `leads` is skipped deliberately: it has no email column (the address
    # lives in its data JSON), and reaching into that adds nothing here.
    db.execute(
        "UPDATE contacts SET do_not_contact=1 WHERE company_id=? AND lower(email)=?",
        (company_id, email),
    )
    return True
