"""Durable at-most-once claims for explicit outbound email operations.

The claim is committed before SMTP.  A claimed/submitted/uncertain row blocks
another worker from re-submitting the same operation.  This intentionally
prefers a missed retry over a duplicate email when SMTP outcome is ambiguous.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from hermes_constants import get_hermes_home


_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


@dataclass(frozen=True)
class EmailSendClaim:
    acquired: bool
    status: str
    operation_id: str
    recipient_key: str
    message_class: str
    message_digest: str


def _validate_identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not _SAFE_ID_RE.fullmatch(normalized):
        raise ValueError(
            f"{label} must be 1-200 characters using letters, numbers, '.', '_', ':', '/', or '-'"
        )
    return normalized


def _recipient_key(recipient: str) -> str:
    normalized = str(recipient or "").strip().lower()
    if not normalized or "@" not in normalized:
        raise ValueError("recipient must be a valid email address")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _message_digest(content: str) -> str:
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def claim_store_path() -> Path:
    """Return the profile-scoped durable claim database path."""
    return get_hermes_home() / "state" / "outbound-email-claims.sqlite3"


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else claim_store_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_send_claims (
            operation_id TEXT NOT NULL,
            recipient_key TEXT NOT NULL,
            message_class TEXT NOT NULL,
            message_digest TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('claimed', 'submitted', 'uncertain')),
            claimed_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            message_id TEXT,
            error TEXT,
            PRIMARY KEY (operation_id, recipient_key, message_class)
        )
        """
    )
    return conn


def claim_email_send(
    *,
    operation_id: str,
    recipient: str,
    message_class: str,
    content: str,
    path: Path | None = None,
) -> EmailSendClaim:
    """Atomically claim an email operation before SMTP.

    Existing rows are reconciled under ``BEGIN IMMEDIATE``.  Reusing a stable
    key with different content is a conflict rather than a silent duplicate.
    """
    op_id = _validate_identifier(operation_id, "operation_id")
    msg_class = _validate_identifier(message_class, "message_class").lower()
    recipient_key = _recipient_key(recipient)
    digest = _message_digest(content)
    now = time.time()

    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT message_digest, status
            FROM email_send_claims
            WHERE operation_id = ? AND recipient_key = ? AND message_class = ?
            """,
            (op_id, recipient_key, msg_class),
        ).fetchone()
        if row is not None:
            existing_digest, status = row
            conn.commit()
            if existing_digest != digest:
                return EmailSendClaim(
                    acquired=False,
                    status="conflict",
                    operation_id=op_id,
                    recipient_key=recipient_key,
                    message_class=msg_class,
                    message_digest=digest,
                )
            return EmailSendClaim(
                acquired=False,
                status=str(status),
                operation_id=op_id,
                recipient_key=recipient_key,
                message_class=msg_class,
                message_digest=digest,
            )

        conn.execute(
            """
            INSERT INTO email_send_claims (
                operation_id, recipient_key, message_class, message_digest,
                status, claimed_at, updated_at
            ) VALUES (?, ?, ?, ?, 'claimed', ?, ?)
            """,
            (op_id, recipient_key, msg_class, digest, now, now),
        )
        conn.commit()
        return EmailSendClaim(
            acquired=True,
            status="claimed",
            operation_id=op_id,
            recipient_key=recipient_key,
            message_class=msg_class,
            message_digest=digest,
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def mark_email_send_submitted(
    claim: EmailSendClaim,
    *,
    message_id: str | None = None,
    path: Path | None = None,
) -> None:
    _mark_claim(claim, status="submitted", message_id=message_id, path=path)


def mark_email_send_uncertain(
    claim: EmailSendClaim,
    *,
    error: str,
    path: Path | None = None,
) -> None:
    _mark_claim(claim, status="uncertain", error=error, path=path)


def _mark_claim(
    claim: EmailSendClaim,
    *,
    status: str,
    message_id: str | None = None,
    error: str | None = None,
    path: Path | None = None,
) -> None:
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            UPDATE email_send_claims
            SET status = ?, updated_at = ?, message_id = ?, error = ?
            WHERE operation_id = ? AND recipient_key = ? AND message_class = ?
              AND message_digest = ? AND status = 'claimed'
            """,
            (
                status,
                time.time(),
                message_id,
                (error or "")[:1000] or None,
                claim.operation_id,
                claim.recipient_key,
                claim.message_class,
                claim.message_digest,
            ),
        )
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
