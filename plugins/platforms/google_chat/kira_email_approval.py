"""Kira v2's local, immutable, default-off direct email approval gate.

The SQLite record is the only approval object.  This module has no transport
credentials and never exposes a send control; a caller must supply an explicit
fake or deployed Router adapter to invoke ``send``.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

ACCOUNT_ALIAS = "rlord"
OWNER_EMAIL = "rlord@goldentouchremodeling.com"
SCHEMA_VERSION = 2
TERMINAL = frozenset({"REJECTED", "EXPIRED", "SENT", "FAILED", "FAILED_UNKNOWN"})
_MAILBOX = re.compile(r"^[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+$")


class KiraApprovalError(ValueError):
    """The request cannot move through the approval state machine."""


class KiraProviderRejected(RuntimeError):
    """A provider rejection that can prove no message was accepted."""

    def __init__(self, message: str, *, no_message_created: bool = False) -> None:
        super().__init__(message)
        self.no_message_created = no_message_created


class KiraEmailProvider(Protocol):
    """Direct Router boundary.  Implementations are alias-bound, never ID-bound."""

    binding_fingerprint: str

    def get_profile(self, *, account: str) -> Mapping[str, Any]: ...
    def send_new(self, *, recipient: str, subject: str, body: str) -> Mapping[str, Any]: ...
    def send_reply(self, *, recipient: str, thread_id: str, body: str) -> Mapping[str, Any]: ...


def _rfc3339(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _frame(name: str, value: str) -> bytes:
    raw = value.encode("utf-8")
    return name.encode("ascii") + b":" + str(len(raw)).encode("ascii") + b":" + raw


def canonical_payload_bytes(
    *, id: str, created_by: str, expires_at: str, recipient: str, subject: str,
    body: str, thread_id: str, mode: str,
) -> bytes:
    """Return the v2 length-prefixed canonical byte sequence without normalization."""
    fields = (
        ("id", id), ("created_by", created_by), ("expires_at", expires_at),
        ("sender_alias", ACCOUNT_ALIAS), ("recipient", recipient), ("subject", subject),
        ("body", body), ("thread_id", thread_id), ("mode", mode), ("is_html", "false"),
    )
    return b"kira-email-v2\0" + b"".join(_frame(name, value) for name, value in fields)


def canonical_payload(recipient: str, subject: str, body: str, thread: str) -> str:
    """Compatibility helper; v2 callers should use :func:`canonical_payload_bytes`."""
    mode = "reply" if thread else "new"
    return canonical_payload_bytes(
        id="", created_by="kira-service", expires_at="", recipient=recipient,
        subject=subject, body=body, thread_id=thread, mode=mode,
    ).decode("utf-8")


def payload_sha256(canonical: str | bytes) -> str:
    return hashlib.sha256(canonical.encode("utf-8") if isinstance(canonical, str) else canonical).hexdigest()


def _data(value: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = value.get("data") if isinstance(value, Mapping) else None
    return nested if isinstance(nested, Mapping) else value


class KiraEmailApprovalGate:
    """SQLite-backed, at-most-once direct sender.  It is default-off by design."""

    def __init__(
        self, path: Path, *, ops_space: str, approvers: set[str], ttl_seconds: int = 600,
        now: Any = time.time, lease_seconds: int = 60,
    ) -> None:
        self.path = Path(path)
        self.ops_space = str(ops_space or "").strip()
        self.approvers = {str(item).strip() for item in approvers if str(item).strip()}
        self.ttl_seconds = max(30, int(ttl_seconds))
        self.lease_seconds = max(10, int(lease_seconds))
        self._now = now
        self._owner = str(uuid.uuid4())
        self._boot_epoch = str(uuid.uuid4())
        self._schema_lock = threading.Lock()
        self._bound_fingerprint: str | None = None
        if not self.ops_space:
            raise KiraApprovalError("a configured GTR Ops space is required")
        if not self.approvers:
            raise KiraApprovalError("an explicit immutable user-ID allowlist is required")
        if any(not item.startswith("users/") for item in self.approvers):
            raise KiraApprovalError("approvers must be immutable Google Chat users/<id> resource IDs")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema()
        self._recover_stale_sends()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA recursive_triggers=ON")
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    def _initialize_schema(self) -> None:
        with self._schema_lock, self._connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS email_requests (
                    id TEXT PRIMARY KEY, recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL,
                    thread_id TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('new','reply')),
                    draft_hash TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
                    expires_epoch REAL NOT NULL, created_by TEXT NOT NULL, sender_alias TEXT NOT NULL,
                    is_html INTEGER NOT NULL CHECK(is_html=0), source_space TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED','SENDING','SENT','FAILED','FAILED_UNKNOWN')),
                    approved_by TEXT, approved_at TEXT, rejected_by TEXT, rejected_at TEXT, claimed_at TEXT,
                    sent_at TEXT, provider_message_id TEXT, provider_thread_id TEXT, failure_code TEXT,
                    failure_detail_redacted TEXT, version INTEGER NOT NULL DEFAULT 1,
                    CHECK((mode='new' AND subject<>'' AND thread_id='') OR (mode='reply' AND subject='' AND thread_id<>''))
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    request_id TEXT PRIMARY KEY REFERENCES email_requests(id), owner TEXT NOT NULL,
                    boot_epoch TEXT NOT NULL, started_at TEXT NOT NULL, provider_message_id TEXT,
                    provider_thread_id TEXT, result TEXT, error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES email_requests(id),
                    timestamp TEXT NOT NULL, actor TEXT, event TEXT NOT NULL, prior_status TEXT,
                    new_status TEXT, draft_hash TEXT NOT NULL, provider_message_id TEXT,
                    provider_thread_id TEXT, error_code TEXT
                );
                CREATE TABLE IF NOT EXISTS sender_lease (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), owner TEXT NOT NULL,
                    boot_epoch TEXT NOT NULL, expires_epoch REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inbound_thread_bindings (
                    thread_id TEXT PRIMARY KEY, recipient TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS kira_v2_audit_no_update BEFORE UPDATE ON audit_events
                    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS kira_v2_audit_no_delete BEFORE DELETE ON audit_events
                    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS kira_v2_payload_immutable BEFORE UPDATE OF id,recipient,subject,body,thread_id,mode,draft_hash,created_at,expires_at,expires_epoch,created_by,sender_alias,is_html,source_space ON email_requests
                    BEGIN SELECT RAISE(ABORT, 'request payload is immutable'); END;
            """)

    def _audit(self, conn: sqlite3.Connection, row: sqlite3.Row | Mapping[str, Any], event: str, prior: str | None, new: str | None, *, actor: str | None = None, message_id: str | None = None, thread_id: str | None = None, error: str | None = None, event_id: str | None = None) -> None:
        conn.execute("INSERT INTO audit_events(event_id,request_id,timestamp,actor,event,prior_status,new_status,draft_hash,provider_message_id,provider_thread_id,error_code) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
            event_id or str(uuid.uuid4()), row["id"], _rfc3339(self._now()), actor, event, prior, new,
            row["draft_hash"], message_id, thread_id, error,
        ))

    def _row(self, conn: sqlite3.Connection, request_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM email_requests WHERE id=?", (request_id,)).fetchone()

    def _valid_hash(self, row: sqlite3.Row | Mapping[str, Any]) -> bool:
        return row["draft_hash"] == self.hash_for_fields(row)

    def hash_for_fields(self, fields: sqlite3.Row | Mapping[str, Any]) -> str:
        return payload_sha256(canonical_payload_bytes(
            id=str(fields["id"]), created_by=str(fields["created_by"]), expires_at=str(fields["expires_at"]),
            recipient=str(fields["recipient"]), subject=str(fields["subject"]), body=str(fields["body"]),
            thread_id=str(fields["thread_id"]), mode=str(fields["mode"]),
        ))

    def _validate_create(self, recipient: str, subject: str, body: str, thread_id: str, mode: str) -> None:
        if not all(isinstance(value, str) for value in (recipient, subject, body, thread_id, mode)):
            raise KiraApprovalError("request fields must be strings")
        if not recipient or not body or not _MAILBOX.fullmatch(recipient):
            raise KiraApprovalError("recipient must be one syntactically valid mailbox")
        if any("\r" in value or "\n" in value for value in (recipient, subject, thread_id, mode)):
            raise KiraApprovalError("recipient, subject, thread_id, and mode may not contain CR/LF")
        if mode not in {"new", "reply"}:
            raise KiraApprovalError("unsupported mode")
        if mode == "new" and (not subject or thread_id):
            raise KiraApprovalError("new mail requires subject and no thread")
        if mode == "reply" and (subject or not thread_id):
            raise KiraApprovalError("reply requires a thread and no subject")

    def create(self, *, recipient: str, subject: str, body: str, thread_id: str = "", thread: str = "", created_by: str = "kira-service") -> dict[str, Any]:
        thread_id = thread_id or thread
        mode = "reply" if thread_id else "new"
        self._validate_create(recipient, subject, body, thread_id, mode)
        if mode == "reply":
            with self._connect() as conn:
                binding = conn.execute("SELECT recipient FROM inbound_thread_bindings WHERE thread_id=?", (thread_id,)).fetchone()
            if binding is None or binding["recipient"] != recipient:
                raise KiraApprovalError("reply thread must be a locally stored inbound binding for recipient")
        now = self._now(); expires_epoch = now + self.ttl_seconds
        row = {
            "id": str(uuid.uuid4()), "recipient": recipient, "subject": subject, "body": body,
            "thread_id": thread_id, "mode": mode, "created_at": _rfc3339(now),
            "expires_at": _rfc3339(expires_epoch), "expires_epoch": expires_epoch,
            "created_by": created_by or "kira-service", "sender_alias": ACCOUNT_ALIAS, "source_space": self.ops_space,
        }
        row["draft_hash"] = self.hash_for_fields(row)
        with self._transaction() as conn:
            conn.execute("""INSERT INTO email_requests(id,recipient,subject,body,thread_id,mode,draft_hash,created_at,expires_at,expires_epoch,created_by,sender_alias,is_html,source_space,status,version)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'PENDING',1)""", (
                row["id"], recipient, subject, body, thread_id, mode, row["draft_hash"], row["created_at"], row["expires_at"], expires_epoch,
                row["created_by"], ACCOUNT_ALIAS, 0, self.ops_space,
            ))
            stored = self._row(conn, row["id"]); assert stored is not None
            self._audit(conn, stored, "created", None, "PENDING", actor=row["created_by"])
            return self._public_status(conn, stored)

    def bind_inbound_thread(self, *, thread_id: str, recipient: str) -> None:
        self._validate_create(recipient, "x", "x", "", "new")
        if not thread_id or "\r" in thread_id or "\n" in thread_id:
            raise KiraApprovalError("thread_id is invalid")
        with self._transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO inbound_thread_bindings(thread_id,recipient,created_at) VALUES(?,?,?)", (thread_id, recipient, _rfc3339(self._now())))

    def _expire(self, conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
        if row["status"] in {"PENDING", "APPROVED"} and row["expires_epoch"] <= self._now():
            old = row["status"]
            conn.execute("UPDATE email_requests SET status='EXPIRED', version=version+1 WHERE id=?", (row["id"],))
            updated = self._row(conn, row["id"]); assert updated is not None
            self._audit(conn, updated, "expired", old, "EXPIRED")
            return updated
        return row

    def decide(self, request_id: str | None = None, *, draft_hash: str | None = None, payload_hash: str | None = None, decision: str, actor_user_id: str | None = None, actor_email: str | None = None, actor_principal: str | None = None, space: str | None = None, source_space: str | None = None, event_id: str | None = None, chat_event_id: str | None = None, verified_credential: bool = False) -> tuple[str, dict[str, Any] | None]:
        request_id = request_id or ""; received_hash = draft_hash or payload_hash or ""; actor = actor_user_id or actor_principal or ""; source = space or source_space or ""; event = event_id or chat_event_id or str(uuid.uuid4())
        with self._transaction() as conn:
            row = self._row(conn, request_id)
            if row is None:
                return "DENIED", None
            row = self._expire(conn, row)
            if not verified_credential:
                self._audit(conn, row, "denied_unsupported_credential", row["status"], row["status"], actor=actor, event_id=event, error="UNVERIFIED_CREDENTIAL")
                return "DENIED", self._public_status(conn, row)
            if not actor or actor not in self.approvers:
                self._audit(conn, row, "denied_identity", row["status"], row["status"], actor=actor or None, event_id=event, error="IDENTITY")
                return "DENIED", self._public_status(conn, row)
            if source != self.ops_space or row["source_space"] != source:
                self._audit(conn, row, "denied_space", row["status"], row["status"], actor=actor, event_id=event, error="SPACE")
                return "DENIED", self._public_status(conn, row)
            if received_hash != row["draft_hash"] or not self._valid_hash(row):
                # A stored-payload modification is terminal and cannot become approvable again.
                old = row["status"]
                if old not in TERMINAL:
                    conn.execute("UPDATE email_requests SET status='FAILED_UNKNOWN',failure_code='TAMPER_DETECTED',version=version+1 WHERE id=?", (row["id"],))
                    row = self._row(conn, row["id"]); assert row is not None
                self._audit(conn, row, "tamper_detected", old, row["status"], actor=actor, event_id=event, error="TAMPER_DETECTED")
                return "DENIED", self._public_status(conn, row)
            if decision not in {"approve", "reject"} or row["status"] != "PENDING":
                self._audit(conn, row, "denied_state", row["status"], row["status"], actor=actor, event_id=event, error="DECISION_OR_STATE")
                return "DENIED", self._public_status(conn, row)
            new = "APPROVED" if decision == "approve" else "REJECTED"
            field = "approved" if decision == "approve" else "rejected"
            if decision == "approve":
                conn.execute("UPDATE email_requests SET status=?,approved_by=?,approved_at=?,version=version+1 WHERE id=? AND status='PENDING'", (new, actor, _rfc3339(self._now()), row["id"]))
            else:
                conn.execute("UPDATE email_requests SET status=?,rejected_by=?,rejected_at=?,version=version+1 WHERE id=? AND status='PENDING'", (new, actor, _rfc3339(self._now()), row["id"]))
            row = self._row(conn, row["id"]); assert row is not None
            self._audit(conn, row, field, "PENDING", new, actor=actor, event_id=event)
            return decision.upper(), self._public_status(conn, row)

    def _acquire_lease(self) -> bool:
        now = self._now(); until = now + self.lease_seconds
        with self._transaction() as conn:
            current = conn.execute("SELECT * FROM sender_lease WHERE singleton=1").fetchone()
            if current and current["expires_epoch"] > now and (current["owner"], current["boot_epoch"]) != (self._owner, self._boot_epoch):
                return False
            conn.execute("INSERT INTO sender_lease(singleton,owner,boot_epoch,expires_epoch) VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET owner=excluded.owner,boot_epoch=excluded.boot_epoch,expires_epoch=excluded.expires_epoch", (self._owner, self._boot_epoch, until))
            return True

    def _release_lease(self) -> None:
        with self._transaction() as conn:
            conn.execute("DELETE FROM sender_lease WHERE singleton=1 AND owner=? AND boot_epoch=?", (self._owner, self._boot_epoch))

    def _recover_stale_sends(self) -> None:
        with self._transaction() as conn:
            lease = conn.execute("SELECT * FROM sender_lease WHERE singleton=1").fetchone()
            if lease and lease["expires_epoch"] > self._now():
                return
            rows = conn.execute("SELECT * FROM email_requests WHERE status='SENDING'").fetchall()
            for row in rows:
                conn.execute("UPDATE email_requests SET status='FAILED_UNKNOWN',failure_code='STALE_SENDER',version=version+1 WHERE id=?", (row["id"],))
                updated = self._row(conn, row["id"]); assert updated is not None
                self._audit(conn, updated, "recovered_stale_sender", "SENDING", "FAILED_UNKNOWN", error="STALE_SENDER")

    async def _call(self, function: Any, **kwargs: Any) -> Mapping[str, Any]:
        result = function(**kwargs)
        if inspect.isawaitable(result): result = await result
        if not isinstance(result, Mapping): raise KiraApprovalError("provider returned malformed response")
        return _data(result)

    def _mark_failed(self, request_id: str, status: str, code: str) -> dict[str, Any]:
        with self._transaction() as conn:
            row = self._row(conn, request_id)
            if row is None: raise KiraApprovalError("unknown request")
            if row["status"] == "SENDING":
                conn.execute("UPDATE email_requests SET status=?,failure_code=?,failure_detail_redacted=?,version=version+1 WHERE id=?", (status, code, code, request_id))
                row = self._row(conn, request_id); assert row is not None
                conn.execute("UPDATE attempts SET result=?,error_code=? WHERE request_id=?", (status, code, request_id))
                self._audit(conn, row, "send_failed", "SENDING", status, error=code)
            return self._public_status(conn, row)

    async def send(self, request_id: str, provider: KiraEmailProvider) -> dict[str, Any]:
        """Perform at most one direct invocation after a durable local claim."""
        if not self._acquire_lease():
            # A concurrent caller never invokes the provider.  Give the owner a
            # brief chance to publish its durable terminal result, then report
            # the actual persisted state rather than attempting recovery.
            for _ in range(100):
                observed = self.status(request_id)
                if observed["status"] in TERMINAL:
                    return observed
                await asyncio.sleep(0.01)
            return self.status(request_id)
        invoked = False
        try:
            with self._transaction() as conn:
                row = self._row(conn, request_id)
                if row is None: raise KiraApprovalError("unknown request")
                row = self._expire(conn, row)
                if row["status"] != "APPROVED": return self._public_status(conn, row)
                if not self._valid_hash(row):
                    conn.execute("UPDATE email_requests SET status='FAILED_UNKNOWN',failure_code='TAMPER_DETECTED',version=version+1 WHERE id=?", (request_id,))
                    row = self._row(conn, request_id); assert row is not None
                    self._audit(conn, row, "tamper_detected", "APPROVED", "FAILED_UNKNOWN", error="TAMPER_DETECTED")
                    return self._public_status(conn, row)
                updated = conn.execute("UPDATE email_requests SET status='SENDING',claimed_at=?,version=version+1 WHERE id=? AND status='APPROVED' AND draft_hash=? AND expires_epoch>?", (_rfc3339(self._now()), request_id, row["draft_hash"], self._now()))
                if updated.rowcount != 1: return self._public_status(conn, self._row(conn, request_id) or row)
                row = self._row(conn, request_id); assert row is not None
                conn.execute("INSERT INTO attempts(request_id,owner,boot_epoch,started_at,result) VALUES(?,?,?,?,?)", (request_id, self._owner, self._boot_epoch, _rfc3339(self._now()), "STARTED"))
                self._audit(conn, row, "provider_invocation_started", "APPROVED", "SENDING")
            row = self._load_verified(request_id)
            fingerprint = str(getattr(provider, "binding_fingerprint", ""))
            if not fingerprint or (self._bound_fingerprint is not None and fingerprint != self._bound_fingerprint):
                return self._mark_failed(request_id, "FAILED", "ALIAS_BINDING_CHANGED")
            self._bound_fingerprint = fingerprint
            profile = await self._call(provider.get_profile, account=ACCOUNT_ALIAS)
            if profile.get("emailAddress") != OWNER_EMAIL:
                return self._mark_failed(request_id, "FAILED", "IDENTITY_MISMATCH")
            if str(getattr(provider, "binding_fingerprint", "")) != fingerprint:
                return self._mark_failed(request_id, "FAILED", "ALIAS_BINDING_CHANGED")
            invoked = True
            if row["mode"] == "new": response = await self._call(provider.send_new, recipient=row["recipient"], subject=row["subject"], body=row["body"])
            else: response = await self._call(provider.send_reply, recipient=row["recipient"], thread_id=row["thread_id"], body=row["body"])
            message_id, thread_id = str(response.get("id") or ""), str(response.get("threadId") or "")
            if not message_id or not thread_id: return self._mark_failed(request_id, "FAILED_UNKNOWN", "MALFORMED_RESPONSE")
        except KiraProviderRejected as exc:
            return self._mark_failed(request_id, "FAILED" if exc.no_message_created else "FAILED_UNKNOWN", "PROVIDER_REJECTED")
        except asyncio.TimeoutError:
            return self._mark_failed(request_id, "FAILED_UNKNOWN", "PROVIDER_TIMEOUT")
        except Exception:
            return self._mark_failed(request_id, "FAILED_UNKNOWN" if invoked else "FAILED", "PROVIDER_ERROR")
        finally:
            # The durable claim is terminal or explicitly ambiguous before this
            # lease is released; a later worker therefore never retries it.
            self._release_lease()
        with self._transaction() as conn:
            row = self._row(conn, request_id)
            if row is None: raise KiraApprovalError("unknown request")
            if row["status"] != "SENDING":
                conn.execute("UPDATE attempts SET provider_message_id=?,provider_thread_id=?,result='LATE_RESULT' WHERE request_id=?", (message_id, thread_id, request_id))
                return self._public_status(conn, row)
            conn.execute("UPDATE email_requests SET status='SENT',sent_at=?,provider_message_id=?,provider_thread_id=?,version=version+1 WHERE id=?", (_rfc3339(self._now()), message_id, thread_id, request_id))
            row = self._row(conn, request_id); assert row is not None
            conn.execute("UPDATE attempts SET provider_message_id=?,provider_thread_id=?,result='SENT' WHERE request_id=?", (message_id, thread_id, request_id))
            self._audit(conn, row, "provider_result", "SENDING", "SENT", message_id=message_id, thread_id=thread_id)
            return self._public_status(conn, row)

    def _load_verified(self, request_id: str) -> sqlite3.Row:
        with self._connect() as conn:
            row = self._row(conn, request_id)
            if row is None or row["status"] != "SENDING" or not self._valid_hash(row):
                raise KiraApprovalError("send claim is not a verified immutable request")
            return row

    def _public_status(self, conn: sqlite3.Connection, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        audit = conn.execute("SELECT timestamp,actor,event,prior_status,new_status,draft_hash,provider_message_id,provider_thread_id,error_code FROM audit_events WHERE request_id=? ORDER BY timestamp,rowid", (row["id"],)).fetchall()
        local, _, domain = row["recipient"].partition("@")
        result = {
            "id": row["id"], "request_id": row["id"], "status": row["status"], "state": row["status"],
            "draft_hash": row["draft_hash"], "payload_sha256": row["draft_hash"], "hash_prefix": row["draft_hash"][:12],
            "created_at": row["created_at"], "expires_at": row["expires_at"], "approved_at": row["approved_at"], "sent_at": row["sent_at"],
            "recipient": (local[:1] + "***@" + domain) if domain else "***", "subject": row["subject"],
            "audit": [dict(item) | {"action": item["event"]} for item in audit],
        }
        if row["status"] == "SENT":
            result["provider_message_id"] = row["provider_message_id"]
            result["provider_thread_id"] = row["provider_thread_id"]
        else:
            result["provider_message_id"] = None; result["provider_thread_id"] = None
        return result

    def status(self, request_id: str) -> dict[str, Any]:
        with self._transaction() as conn:
            row = self._row(conn, request_id)
            if row is None: raise KiraApprovalError("unknown request")
            return self._public_status(conn, self._expire(conn, row))
