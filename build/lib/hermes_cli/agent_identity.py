"""Public-key identity and replay protection for non-human business actors."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
from typing import Any, Mapping, Optional


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agent_identities (
    organization_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    public_key_b64 TEXT NOT NULL, status TEXT NOT NULL,
    valid_from INTEGER NOT NULL, valid_until INTEGER,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (organization_id, agent_id)
);
CREATE TABLE IF NOT EXISTS agent_request_nonces (
    organization_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    nonce TEXT NOT NULL, used_at INTEGER NOT NULL,
    PRIMARY KEY (organization_id, agent_id, nonce)
);
"""


class AgentIdentityError(PermissionError):
    pass


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)


def register_public_identity(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    agent_id: str,
    public_key: bytes,
    valid_until: Optional[int] = None,
) -> None:
    """Register only a public key. Private signing material stays in a vault."""
    if len(public_key) != 32:
        raise ValueError("Ed25519 public key must be 32 bytes")
    ensure_schema(conn)
    now = int(time.time())
    with conn:
        conn.execute(
            """INSERT INTO agent_identities
               (organization_id, agent_id, public_key_b64, status,
                valid_from, valid_until, created_at)
               VALUES (?, ?, ?, 'active', ?, ?, ?)
               ON CONFLICT(organization_id, agent_id) DO UPDATE SET
                 public_key_b64=excluded.public_key_b64, status='active',
                 valid_from=excluded.valid_from, valid_until=excluded.valid_until""",
            (
                organization_id,
                agent_id,
                base64.b64encode(public_key).decode("ascii"),
                now,
                valid_until,
                now,
            ),
        )


def canonical_request(
    *,
    organization_id: str,
    agent_id: str,
    nonce: str,
    issued_at: int,
    payload: Mapping[str, Any],
) -> bytes:
    return json.dumps(
        {
            "agent_id": agent_id,
            "issued_at": int(issued_at),
            "nonce": nonce,
            "organization_id": organization_id,
            "payload": payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def verify_machine_request(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    agent_id: str,
    nonce: str,
    issued_at: int,
    payload: Mapping[str, Any],
    signature: bytes,
    max_age_seconds: int = 300,
) -> None:
    """Verify identity, freshness, signature, and single-use nonce."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    ensure_schema(conn)
    now = int(time.time())
    if not nonce or abs(now - int(issued_at)) > max_age_seconds:
        raise AgentIdentityError("machine request is missing a fresh nonce/timestamp")
    row = conn.execute(
        """SELECT * FROM agent_identities
           WHERE organization_id = ? AND agent_id = ?""",
        (organization_id, agent_id),
    ).fetchone()
    if row is None or row["status"] != "active":
        raise AgentIdentityError("machine identity is not active")
    if int(row["valid_from"]) > now or (
        row["valid_until"] is not None and int(row["valid_until"]) <= now
    ):
        raise AgentIdentityError("machine identity is outside its validity window")
    if conn.execute(
        """SELECT 1 FROM agent_request_nonces
           WHERE organization_id = ? AND agent_id = ? AND nonce = ?""",
        (organization_id, agent_id, nonce),
    ).fetchone():
        raise AgentIdentityError("machine request nonce has already been used")
    public_key = base64.b64decode(row["public_key_b64"])
    message = canonical_request(
        organization_id=organization_id,
        agent_id=agent_id,
        nonce=nonce,
        issued_at=issued_at,
        payload=payload,
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except Exception as exc:
        raise AgentIdentityError("machine request signature is invalid") from exc
    with conn:
        conn.execute(
            """INSERT INTO agent_request_nonces
               (organization_id, agent_id, nonce, used_at) VALUES (?, ?, ?, ?)""",
            (organization_id, agent_id, nonce, now),
        )
