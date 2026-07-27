"""Governed company-email provider edge with independent message read-back."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol
from urllib.parse import quote

import httpx


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS company_email_operations (
    id TEXT PRIMARY KEY, organization_id TEXT NOT NULL,
    objective_id TEXT NOT NULL, action_id TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL, inbox_id TEXT NOT NULL,
    message_id TEXT NOT NULL, thread_id TEXT,
    recipients_json TEXT NOT NULL, subject_sha256 TEXT NOT NULL,
    body_sha256 TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL, provider_evidence_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS company_email_suppressions (
    organization_id TEXT NOT NULL, address TEXT NOT NULL,
    reason TEXT NOT NULL, source_reference TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY(organization_id,address)
);
CREATE TRIGGER IF NOT EXISTS company_email_operations_immutable_update
BEFORE UPDATE ON company_email_operations
BEGIN SELECT RAISE(ABORT, 'company email operations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS company_email_operations_immutable_delete
BEFORE DELETE ON company_email_operations
BEGIN SELECT RAISE(ABORT, 'company email operations are immutable'); END;
"""

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CompanyEmailError(RuntimeError):
    pass


class EmailProvider(Protocol):
    name: str

    def send(
        self,
        *,
        inbox_id: str,
        recipients: list[str],
        subject: str,
        text: str,
        html: Optional[str],
        idempotency_key: str,
    ) -> Mapping[str, Any]: ...

    def get_message(self, *, inbox_id: str, message_id: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class EmailConfiguration:
    inbox_id: str
    provider: EmailProvider


class AgentMailProvider:
    name = "agentmail"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.agentmail.to",
        timeout: float = 30,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        if not api_key:
            raise ValueError("AgentMail API key is required")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs) -> Mapping[str, Any]:
        response = self._client.request(method, path, **kwargs)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = ""
            try:
                code = str(response.json().get("code") or "")
            except Exception:
                pass
            raise CompanyEmailError(
                f"AgentMail request failed ({response.status_code}, {code or 'unknown'})"
            ) from exc
        value = response.json()
        if not isinstance(value, dict):
            raise CompanyEmailError("AgentMail returned a non-object response")
        return value

    def send(
        self,
        *,
        inbox_id: str,
        recipients: list[str],
        subject: str,
        text: str,
        html: Optional[str],
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {
            "to": recipients,
            "subject": subject,
            "text": text,
        }
        if html:
            body["html"] = html
        return self._request(
            "POST",
            f"/v0/inboxes/{quote(inbox_id, safe='')}/messages/send",
            headers={"Idempotency-Key": idempotency_key},
            json=body,
        )

    def get_message(self, *, inbox_id: str, message_id: str) -> Mapping[str, Any]:
        return self._request(
            "GET",
            (
                f"/v0/inboxes/{quote(inbox_id, safe='')}/messages/"
                f"{quote(message_id, safe='')}"
            ),
        )


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def configured_agentmail(config: Mapping[str, Any]) -> Optional[EmailConfiguration]:
    email = (((config.get("agentic") or {}).get("communications") or {}).get("email") or {})
    if str(email.get("provider") or "").lower() != "agentmail":
        return None
    inbox_id = str(email.get("inbox_id") or "").strip()
    api_key = os.getenv("AGENTMAIL_API_KEY", "").strip()
    if not inbox_id or not api_key:
        return None
    return EmailConfiguration(inbox_id, AgentMailProvider(api_key))


def suppress(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    address: str,
    reason: str,
    source_reference: str,
) -> None:
    ensure_schema(conn)
    normalized = address.strip().lower()
    if not _EMAIL.fullmatch(normalized) or not reason or not source_reference:
        raise ValueError("valid address, reason, and source reference are required")
    with conn:
        conn.execute(
            """INSERT INTO company_email_suppressions
               (organization_id,address,reason,source_reference,created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(organization_id,address) DO UPDATE SET
                 reason=excluded.reason,source_reference=excluded.source_reference,
                 created_at=excluded.created_at""",
            (organization_id, normalized, reason, source_reference, int(time.time())),
        )


def validate_send(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    payload: Mapping[str, Any],
) -> None:
    ensure_schema(conn)
    recipients = payload.get("to")
    if not isinstance(recipients, list) or not recipients:
        raise CompanyEmailError("email requires a non-empty recipient list")
    normalized = [str(value).strip().lower() for value in recipients]
    if any(not _EMAIL.fullmatch(value) for value in normalized):
        raise CompanyEmailError("email contains an invalid recipient address")
    placeholders = ",".join("?" for _ in normalized)
    blocked = conn.execute(
        f"""SELECT address FROM company_email_suppressions
            WHERE organization_id=? AND address IN ({placeholders}) LIMIT 1""",
        (organization_id, *normalized),
    ).fetchone()
    if blocked is not None:
        raise CompanyEmailError(f"recipient {blocked['address']} is suppressed")
    kind = str(payload.get("communication_type") or "")
    if kind not in {"transactional", "relationship", "commercial"}:
        raise CompanyEmailError("email communication_type is invalid")
    if kind == "commercial":
        required = (
            "consent_basis",
            "sender_identity",
            "physical_address",
            "unsubscribe_url",
        )
        missing = [name for name in required if not str(payload.get(name) or "").strip()]
        if missing:
            raise CompanyEmailError(
                "commercial email missing compliance fields: " + ", ".join(missing)
            )


def record_send(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    objective_id: str,
    action_id: str,
    inbox_id: str,
    payload: Mapping[str, Any],
    response: Mapping[str, Any],
) -> str:
    ensure_schema(conn)
    message_id = str(response.get("message_id") or "")
    if not message_id:
        raise CompanyEmailError("AgentMail send response omitted message_id")
    recipients = sorted(str(item).strip().lower() for item in payload["to"])
    recipients_json = json.dumps(recipients, separators=(",", ":"))
    subject_sha256 = hashlib.sha256(str(payload["subject"]).encode()).hexdigest()
    body_sha256 = hashlib.sha256(str(payload["text"]).encode()).hexdigest()
    existing = conn.execute(
        """SELECT * FROM company_email_operations
            WHERE idempotency_key=?""",
        (payload["idempotency_key"],),
    ).fetchone()
    if existing is not None:
        if (
            str(existing["organization_id"]) != organization_id
            or str(existing["objective_id"]) != objective_id
            or str(existing["action_id"]) != action_id
            or str(existing["inbox_id"]) != inbox_id
            or str(existing["recipients_json"]) != recipients_json
            or str(existing["subject_sha256"]) != subject_sha256
            or str(existing["body_sha256"]) != body_sha256
        ):
            raise CompanyEmailError(
                "email idempotency key was reused with different send parameters"
            )
        return str(existing["id"])
    operation_id = f"email_{uuid.uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO company_email_operations
               (id,organization_id,objective_id,action_id,provider,inbox_id,
                message_id,thread_id,recipients_json,subject_sha256,body_sha256,
                idempotency_key,status,provider_evidence_json,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_id, organization_id, objective_id, action_id,
                "agentmail", inbox_id,
                message_id, response.get("thread_id"),
                recipients_json,
                subject_sha256,
                body_sha256,
                payload["idempotency_key"], "sent",
                json.dumps(dict(response), separators=(",", ":"), sort_keys=True),
                int(time.time()),
            ),
        )
    return operation_id


def usage(conn: sqlite3.Connection, organization_id: str) -> dict[str, int]:
    ensure_schema(conn)
    now = int(time.time())
    day = now - 86_400
    month = now - 30 * 86_400
    return {
        "emails_day": int(
            conn.execute(
                """SELECT COUNT(*) FROM company_email_operations
                   WHERE organization_id=? AND created_at>=?""",
                (organization_id, day),
            ).fetchone()[0]
        ),
        "emails_month": int(
            conn.execute(
                """SELECT COUNT(*) FROM company_email_operations
                   WHERE organization_id=? AND created_at>=?""",
                (organization_id, month),
            ).fetchone()[0]
        ),
    }
