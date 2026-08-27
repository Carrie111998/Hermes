"""Durable, exact-draft approval gate for Kira outbound Gmail.

This module owns the local authorization boundary.  It deliberately does not
know Composio credentials, URLs, or account IDs.  A caller supplies a narrow
provider bridge which must use the configured ``rlord`` account alias.

The store contains private draft content, so it is profile-scoped and never
returns recipient, subject, body, or thread from its public status method.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol


SCHEMA_VERSION = 1
ACCOUNT_ALIAS = "rlord"
OWNER_EMAIL = "rlord@goldentouchremodeling.com"
LEGAL_STATES = frozenset({
    "PENDING", "APPROVED", "REJECTED", "SENDING", "SENT", "EXPIRED", "FAILED",
})


class KiraApprovalError(ValueError):
    """Raised for an invalid draft or refused state transition."""


class KiraEmailProvider(Protocol):
    """Narrow Composio bridge required by :meth:`KiraEmailApprovalGate.send`.

    Implementations must use only the Composio Router's
    ``COMPOSIO_MULTI_EXECUTE_TOOL`` operation, account alias ``rlord``, and
    ``user_id: me``.  This protocol keeps provider credentials and the dynamic
    MCP runtime outside the durable gate.
    """

    def get_profile(self, *, account: str) -> Mapping[str, Any]: ...
    def create_email_draft(self, *, account: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def get_email_draft(self, *, account: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def send_draft(self, *, account: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...


def canonical_payload(recipient: str, subject: str, body: str, thread: str) -> str:
    """Return the immutable v1 canonical JSON payload without normalization."""
    _validate_payload(recipient, subject, body, thread)
    return json.dumps(
        [SCHEMA_VERSION, recipient, subject, body, thread],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def payload_sha256(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_payload(recipient: str, subject: str, body: str, thread: str) -> None:
    if not all(isinstance(value, str) for value in (recipient, subject, body, thread)):
        raise KiraApprovalError("recipient, subject, body, and thread must be strings")
    if not recipient or not body:
        raise KiraApprovalError("recipient and body are required")
    if bool(subject) == bool(thread):
        raise KiraApprovalError("new mail requires subject and replies require thread")


def _result_data(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept direct Composio data or its normal {data: ...} envelope."""
    data = value.get("data") if isinstance(value, Mapping) else None
    return data if isinstance(data, Mapping) else value


class KiraEmailApprovalGate:
    """SQLite-backed exact-draft state machine and controlled send worker."""

    def __init__(
        self,
        path: Path,
        *,
        ops_space: str,
        approvers: set[str],
        ttl_seconds: int = 600,
        now: Any = time.time,
    ) -> None:
        self.path = Path(path)
        self.ops_space = str(ops_space or "").strip()
        self.approvers = {str(email).strip().lower() for email in approvers if str(email).strip()}
        self.ttl_seconds = max(30, int(ttl_seconds))
        self._now = now
        self._schema_lock = threading.Lock()
        if not self.ops_space:
            raise KiraApprovalError("a configured GTR Ops space is required")
        if not self.approvers:
            raise KiraApprovalError("an explicit literal-email approver allowlist is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # ``INSERT OR REPLACE`` is a delete+insert.  SQLite only fires the
        # delete trigger below when recursive triggers are enabled per
        # connection, so this must be set on every reader/writer connection.
        conn.execute("PRAGMA recursive_triggers=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def _initialize_schema(self) -> None:
        with self._schema_lock, self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS drafts (
                    id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    recipient TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    body TEXT NOT NULL,
                    thread TEXT NOT NULL,
                    canonical_payload TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    account_alias TEXT NOT NULL,
                    owner_email TEXT NOT NULL,
                    source_space TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('PENDING','APPROVED','REJECTED','SENDING','SENT','EXPIRED','FAILED')),
                    provider_draft_id TEXT,
                    provider_message_id TEXT,
                    provider_thread_id TEXT,
                    failure_code TEXT,
                    failure_outcome TEXT
                );
                CREATE TABLE IF NOT EXISTS outbox_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id),
                    claimed_at REAL NOT NULL,
                    provider_draft_id TEXT,
                    provider_message_id TEXT,
                    outcome TEXT NOT NULL,
                    error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL REFERENCES drafts(id),
                    timestamp REAL NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    action TEXT NOT NULL,
                    actor_principal TEXT,
                    chat_event_id TEXT,
                    payload_sha256 TEXT NOT NULL,
                    provider_draft_id TEXT,
                    provider_message_id TEXT,
                    provider_thread_id TEXT,
                    error_class TEXT
                );
                CREATE TRIGGER IF NOT EXISTS kira_audit_no_update
                BEFORE UPDATE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS kira_audit_no_delete
                BEFORE DELETE ON audit_events BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS kira_draft_payload_immutable
                BEFORE UPDATE OF recipient, subject, body, thread, canonical_payload, payload_sha256, account_alias ON drafts
                BEGIN SELECT RAISE(ABORT, 'draft payload is immutable'); END;
                """
            )
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _audit(
        self,
        conn: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        action: str,
        from_state: str | None = None,
        to_state: str | None = None,
        actor_principal: str | None = None,
        chat_event_id: str | None = None,
        provider_draft_id: str | None = None,
        provider_message_id: str | None = None,
        provider_thread_id: str | None = None,
        error_class: str | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO audit_events(
                    event_id,draft_id,timestamp,from_state,to_state,action,
                    actor_principal,chat_event_id,payload_sha256,provider_draft_id,
                    provider_message_id,provider_thread_id,error_class
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex, row["id"], self._now(), from_state, to_state, action,
                actor_principal, chat_event_id, row["payload_sha256"], provider_draft_id,
                provider_message_id, provider_thread_id, error_class,
            ),
        )

    @staticmethod
    def _row(conn: sqlite3.Connection, draft_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()

    def _expire_if_needed(self, conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
        if row["state"] == "PENDING" and row["expires_at"] <= self._now():
            conn.execute("UPDATE drafts SET state='EXPIRED' WHERE id=?", (row["id"],))
            self._audit(conn, row, action="expired", from_state="PENDING", to_state="EXPIRED")
            return self._row(conn, row["id"])  # type: ignore[return-value]
        return row

    def create(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        thread: str = "",
        draft_id: str | None = None,
    ) -> dict[str, Any]:
        canonical = canonical_payload(recipient, subject, body, thread)
        digest = payload_sha256(canonical)
        now = float(self._now())
        record = {
            "id": draft_id or uuid.uuid4().hex,
            "schema_version": SCHEMA_VERSION,
            "recipient": recipient,
            "subject": subject,
            "body": body,
            "thread": thread,
            "canonical_payload": canonical,
            "payload_sha256": digest,
            "account_alias": ACCOUNT_ALIAS,
            "owner_email": OWNER_EMAIL,
            "source_space": self.ops_space,
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
            "state": "PENDING",
        }
        with self._transaction() as conn:
            conn.execute(
                """INSERT INTO drafts(
                    id,schema_version,recipient,subject,body,thread,canonical_payload,
                    payload_sha256,account_alias,owner_email,source_space,created_at,
                    expires_at,state
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(record[key] for key in (
                    "id", "schema_version", "recipient", "subject", "body", "thread",
                    "canonical_payload", "payload_sha256", "account_alias", "owner_email",
                    "source_space", "created_at", "expires_at", "state",
                )),
            )
            self._audit(conn, record, action="created", to_state="PENDING")
        return self.status(record["id"])

    def decide(
        self,
        draft_id: str,
        *,
        decision: str,
        payload_hash: str,
        actor_principal: str,
        source_space: str,
        chat_event_id: str,
    ) -> tuple[str, dict[str, Any] | None]:
        """Apply one verified card action, idempotently, inside ``BEGIN IMMEDIATE``."""
        actor = actor_principal.strip().lower()
        with self._transaction() as conn:
            row = self._row(conn, draft_id)
            if row is None:
                return "unknown", None
            if decision not in {"approve", "reject"}:
                self._audit(conn, row, action="invalid_action", actor_principal=actor, chat_event_id=chat_event_id)
                return "invalid_action", self._public_status(conn, row)
            if actor not in self.approvers:
                self._audit(conn, row, action="unauthorized", actor_principal=actor, chat_event_id=chat_event_id)
                return "unauthorized", self._public_status(conn, row)
            if source_space != self.ops_space or source_space != row["source_space"]:
                self._audit(conn, row, action="wrong_space", actor_principal=actor, chat_event_id=chat_event_id)
                return "wrong_space", self._public_status(conn, row)
            row = self._expire_if_needed(conn, row)
            if payload_hash != row["payload_sha256"]:
                self._audit(conn, row, action="hash_rejected", actor_principal=actor, chat_event_id=chat_event_id)
                return "changed_hash", self._public_status(conn, row)
            if row["state"] != "PENDING":
                return "replayed", self._public_status(conn, row)
            state = "APPROVED" if decision == "approve" else "REJECTED"
            action = "approved" if decision == "approve" else "rejected"
            conn.execute("UPDATE drafts SET state=? WHERE id=? AND state='PENDING'", (state, draft_id))
            self._audit(
                conn, row, action=action, from_state="PENDING", to_state=state,
                actor_principal=actor, chat_event_id=chat_event_id,
            )
            return action, self._public_status(conn, self._row(conn, draft_id))

    def _canonical_matches(self, row: Mapping[str, Any]) -> bool:
        try:
            canonical = canonical_payload(row["recipient"], row["subject"], row["body"], row["thread"])
        except (KeyError, KiraApprovalError):
            return False
        return canonical == row["canonical_payload"] and payload_sha256(canonical) == row["payload_sha256"]

    def _claim_for_send(self, draft_id: str) -> sqlite3.Row | None:
        with self._transaction() as conn:
            row = self._row(conn, draft_id)
            if row is None:
                return None
            row = self._expire_if_needed(conn, row)
            if row["state"] != "APPROVED":
                return None
            if not self._canonical_matches(row):
                conn.execute(
                    "UPDATE drafts SET state='FAILED', failure_code='PAYLOAD_HASH_MISMATCH', failure_outcome='NOT_SENT' WHERE id=?",
                    (draft_id,),
                )
                self._audit(conn, row, action="send_refused", from_state="APPROVED", to_state="FAILED", error_class="PAYLOAD_HASH_MISMATCH")
                return None
            attempt_id = uuid.uuid4().hex
            try:
                conn.execute(
                    "INSERT INTO outbox_attempts(attempt_id,draft_id,claimed_at,outcome) VALUES(?,?,?,?)",
                    (attempt_id, draft_id, self._now(), "CLAIMED"),
                )
            except sqlite3.IntegrityError:
                return None
            conn.execute("UPDATE drafts SET state='SENDING' WHERE id=? AND state='APPROVED'", (draft_id,))
            self._audit(conn, row, action="send_attempt", from_state="APPROVED", to_state="SENDING")
            return self._row(conn, draft_id)

    def _fail(self, draft_id: str, code: str, outcome: str, exc: BaseException | None = None) -> None:
        with self._transaction() as conn:
            row = self._row(conn, draft_id)
            if row is None or row["state"] != "SENDING":
                return
            conn.execute(
                "UPDATE drafts SET state='FAILED', failure_code=?, failure_outcome=? WHERE id=?",
                (code, outcome, draft_id),
            )
            conn.execute(
                "UPDATE outbox_attempts SET outcome=?, error_code=? WHERE draft_id=?",
                (outcome, code, draft_id),
            )
            self._audit(
                conn, row, action="send_failed", from_state="SENDING", to_state="FAILED",
                error_class=type(exc).__name__ if exc else code,
            )

    async def _provider_call(self, method: Any, **kwargs: Any) -> Mapping[str, Any]:
        result = method(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise KiraApprovalError("provider returned malformed response")
        return result

    @staticmethod
    def _remote_payload_matches(remote: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
        """Require the provider's persisted draft to prove the exact payload.

        A draft ID is not an authorization: Gmail drafts remain mutable after
        creation.  The Composio bridge must return either the server-side
        canonical hash or each raw payload field from a fresh provider read.
        """
        expected = row["payload_sha256"]
        if str(remote.get("payload_sha256") or "") == expected:
            return True
        try:
            canonical = canonical_payload(
                str(remote["recipient"]),
                str(remote["subject"]),
                str(remote["body"]),
                str(remote.get("thread") or remote.get("thread_id") or ""),
            )
        except (KeyError, KiraApprovalError):
            return False
        return payload_sha256(canonical) == expected

    async def send(self, draft_id: str, provider: KiraEmailProvider) -> dict[str, Any]:
        """Send an already-approved record exactly once, or safely refuse it."""
        row = self._claim_for_send(draft_id)
        if row is None:
            return self.status(draft_id)
        try:
            profile = _result_data(await self._provider_call(provider.get_profile, account=ACCOUNT_ALIAS))
            if profile.get("emailAddress") != OWNER_EMAIL:
                raise KiraApprovalError("provider identity does not match Richard mailbox")
            arguments: dict[str, Any] = {
                "user_id": "me", "recipient_email": row["recipient"], "subject": row["subject"],
                "body": row["body"], "is_html": False,
            }
            if row["thread"]:
                arguments["thread_id"] = row["thread"]
            created = _result_data(await self._provider_call(
                provider.create_email_draft, account=ACCOUNT_ALIAS, arguments=arguments,
            ))
            provider_draft_id = str(created.get("id") or "")
            if not provider_draft_id:
                raise KiraApprovalError("provider did not return a Gmail draft id")
            with self._transaction() as conn:
                live = self._row(conn, draft_id)
                if live is None or live["state"] != "SENDING":
                    raise KiraApprovalError("send claim was lost")
                if not self._canonical_matches(live):
                    raise KiraApprovalError("payload changed before provider send")
                conn.execute("UPDATE drafts SET provider_draft_id=? WHERE id=?", (provider_draft_id, draft_id))
                conn.execute("UPDATE outbox_attempts SET provider_draft_id=? WHERE draft_id=?", (provider_draft_id, draft_id))
            remote = _result_data(await self._provider_call(
                provider.get_email_draft,
                account=ACCOUNT_ALIAS,
                arguments={"user_id": "me", "draft_id": provider_draft_id},
            ))
            if not self._remote_payload_matches(remote, row):
                raise KiraApprovalError("provider draft does not match the approved exact payload")
            with self._transaction() as conn:
                live = self._row(conn, draft_id)
                if live is None or live["state"] != "SENDING":
                    raise KiraApprovalError("send claim was lost")
                self._audit(conn, live, action="provider_payload_verified", provider_draft_id=provider_draft_id)
            sent = _result_data(await self._provider_call(
                provider.send_draft,
                account=ACCOUNT_ALIAS,
                arguments={"user_id": "me", "draft_id": provider_draft_id},
            ))
            message_id = str(sent.get("id") or "")
            thread_id = str(sent.get("threadId") or "")
            if not message_id:
                raise KiraApprovalError("provider did not return a sent message id")
        except asyncio.TimeoutError as exc:
            self._fail(draft_id, "PROVIDER_TIMEOUT", "UNKNOWN", exc)
            return self.status(draft_id)
        except Exception as exc:
            self._fail(draft_id, "PROVIDER_ERROR", "UNKNOWN", exc)
            return self.status(draft_id)
        with self._transaction() as conn:
            live = self._row(conn, draft_id)
            if live is None or live["state"] != "SENDING":
                raise KiraApprovalError("send result cannot be applied")
            conn.execute(
                """UPDATE drafts SET state='SENT', provider_message_id=?, provider_thread_id=?,
                   failure_code=NULL, failure_outcome=NULL WHERE id=?""",
                (message_id, thread_id or None, draft_id),
            )
            conn.execute(
                "UPDATE outbox_attempts SET provider_message_id=?, outcome='SENT', error_code=NULL WHERE draft_id=?",
                (message_id, draft_id),
            )
            self._audit(
                conn, live, action="send_succeeded", from_state="SENDING", to_state="SENT",
                provider_draft_id=live["provider_draft_id"], provider_message_id=message_id,
                provider_thread_id=thread_id or None,
            )
        return self.status(draft_id)

    def _public_status(self, conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
        events = conn.execute(
            """SELECT action,timestamp,actor_principal,chat_event_id,error_class,
                      from_state,to_state,provider_message_id
               FROM audit_events WHERE draft_id=? ORDER BY timestamp,rowid""",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"], "payload_sha256": row["payload_sha256"],
            "created_at": row["created_at"], "expires_at": row["expires_at"],
            "state": row["state"], "provider_message_id": row["provider_message_id"],
            "audit": [dict(event) for event in events],
        }

    def status(self, draft_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            row = self._row(conn, draft_id)
            if row is None:
                raise KiraApprovalError("unknown draft")
            row = self._expire_if_needed(conn, row)
            return self._public_status(conn, row)
