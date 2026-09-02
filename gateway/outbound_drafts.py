"""P1 — durable outbound-email draft store with exact one-shot approval (#99876).

While ``platforms.email.extra.draft_only`` is enabled, every outbound email
lands here as a ``pending`` draft instead of reaching SMTP.  An explicit
``approve_and_claim_draft`` (Desktop/RPC only, owner identity required) is the
only path that ever transmits a draft, and each draft is sent **at most once**
— even across gateway restarts, concurrent approvers, and replayed approval
RPCs.

State machine
-------------
``pending`` is the only state that may be claimed.  A successful claim moves
the row to ``claimed``; the caller then records the SMTP outcome, which moves
it to a terminal state:

- ``sent``              — SMTP accepted the message (``message_id`` recorded)
- ``unknown_delivery``  — SMTP timed out / the wire outcome is unknown;
                          NEVER auto-resends (the recipient may have it)
- ``failed``            — SMTP permanently rejected; never retried

Drafts can also leave ``pending`` without sending:

- ``denied``            — the owner explicitly rejected the draft (RPC deny)
- ``cancelled``         — the owning generation was stopped / owner cancelled
- ``expired``           — swept by :meth:`OutboundDraftStore.expire_drafts`
                          (approval window closed)

Guarantees implemented here
---------------------------
- **Idempotency**: ``create_draft`` with a repeated ``(profile,
  idempotency_key)`` returns the existing row instead of duplicating it.
- **Content binding**: ``content_hash`` is the SHA-256 of the canonical
  JSON payload; ``approve_and_claim_draft`` refuses a hash that does not
  match the stored row, so approval always matches what the owner saw.
- **Row-level atomic claim**: the claim runs as ``UPDATE ... WHERE
  draft_id=? AND state='pending' AND expires_at > now`` inside an EXCLUSIVE
  SQLite transaction, serialized with a process lock, so two concurrent
  approvers yield exactly one winner.
- **Budgets + circuit breaker**: ``check_delivery_allowed`` enforces
  per-session / per-hour / per-day send budgets and a circuit breaker that
  opens after ``_circuit_trip_sends`` confirmed sends inside
  ``_circuit_window_minutes`` and stays open for
  ``_circuit_cooldown_minutes``.  Budgets block *delivery*, never draft
  creation.
- **Durability**: rows survive gateway restarts (fresh store instance on the
  same file), so a restart before approval leaves the draft pending and a
  restart after approval never re-sends.

In-process stop fence
---------------------
``mark_session_stopped`` / ``session_stopped`` record stopped
(session, generation) pairs so racing callbacks from a stopped turn can
neither create a new draft nor reach SMTP.  The registry is in-memory by
design: after a real restart there are no racing callbacks left to fence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:  # hermes_constants is import-safe anywhere in the tree
    from hermes_constants import get_hermes_home as _get_hermes_home
except Exception:  # pragma: no cover - never expected outside the repo
    _get_hermes_home = None


def _now() -> datetime:
    """UTC now truncated to whole seconds (stable text round-trips)."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def default_store_path() -> str:
    """Canonical draft-store file for this Hermes home (used when no explicit
    path is configured).  The gateway adapter, the RPC approval surface, and
    the standalone cron transport all resolve the same file so one install has
    exactly one approval queue."""
    if _get_hermes_home is not None:
        home = Path(str(_get_hermes_home()))
    else:
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return str(home / "outbound_drafts.db")


def _canonical_payload(
    *,
    profile: str,
    session_key: str,
    session_id: str,
    turn_generation: str,
    platform: str,
    recipient: str,
    subject: str,
    in_reply_to: str,
    references: str,
    body: str,
    attachment_manifest: List[Any],
) -> bytes:
    """Deterministic JSON encoding of everything the eventual SMTP send will
    transmit.  The SHA-256 of this payload is the draft's content hash."""
    canonical = {
        "profile": profile or "",
        "session_key": session_key or "",
        "session_id": session_id or "",
        "turn_generation": turn_generation or "",
        "platform": platform or "email",
        "recipient": recipient or "",
        "subject": subject or "",
        "in_reply_to": in_reply_to or "",
        "references": references or "",
        "body": body or "",
        "attachment_manifest": attachment_manifest or [],
    }
    return json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


@dataclass
class Draft:
    """One durable outbound draft row."""

    draft_id: str
    profile: str = ""
    session_key: str = ""
    session_id: str = ""
    turn_generation: str = ""
    platform: str = "email"
    recipient: str = ""
    subject: str = ""
    in_reply_to: str = ""
    references: str = ""
    body: str = ""
    attachment_manifest: List[Any] = field(default_factory=list)
    idempotency_key: Optional[str] = None
    content_hash: str = ""
    state: str = "pending"
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    claimed_at: Optional[datetime] = None
    claimed_by: Optional[str] = None
    sent_at: Optional[datetime] = None
    message_id: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable view for RPC/UI payloads."""
        data = asdict(self)
        for key in ("expires_at", "created_at", "claimed_at", "sent_at"):
            value = data.get(key)
            data[key] = _iso(value) if isinstance(value, datetime) else value
        return data


@dataclass
class Claimed:
    """Result of an atomic ``approve_and_claim_draft`` attempt."""

    draft_id: str
    content_hash: str
    claimed: bool
    reason: str = ""


@dataclass
class DeliveryCheck:
    """Result of a delivery-budget / circuit-breaker gate."""

    allowed: bool
    reason: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_drafts (
    draft_id          TEXT PRIMARY KEY,
    profile           TEXT NOT NULL DEFAULT '',
    session_key       TEXT NOT NULL DEFAULT '',
    session_id        TEXT NOT NULL DEFAULT '',
    turn_generation   TEXT NOT NULL DEFAULT '',
    platform          TEXT NOT NULL DEFAULT 'email',
    recipient         TEXT NOT NULL,
    subject           TEXT NOT NULL DEFAULT '',
    in_reply_to       TEXT NOT NULL DEFAULT '',
    refs              TEXT NOT NULL DEFAULT '',
    body              TEXT NOT NULL DEFAULT '',
    attachment_manifest TEXT NOT NULL DEFAULT '[]',
    idempotency_key   TEXT,
    content_hash      TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'pending',
    expires_at        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    claimed_at        TEXT,
    claimed_by        TEXT,
    sent_at           TEXT,
    message_id        TEXT,
    error             TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_profile_idem
    ON outbound_drafts(profile, idempotency_key)
    WHERE idempotency_key IS NOT NULL AND idempotency_key <> '';
CREATE INDEX IF NOT EXISTS idx_drafts_state ON outbound_drafts(state);
CREATE INDEX IF NOT EXISTS idx_drafts_session
    ON outbound_drafts(session_key, session_id, turn_generation);
CREATE TABLE IF NOT EXISTS outbound_drafts_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _load_manifest(raw: Optional[str]) -> List[Any]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except (ValueError, TypeError):
        return []


def _row_to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        draft_id=row["draft_id"],
        profile=row["profile"] or "",
        session_key=row["session_key"] or "",
        session_id=row["session_id"] or "",
        turn_generation=row["turn_generation"] or "",
        platform=row["platform"] or "email",
        recipient=row["recipient"] or "",
        subject=row["subject"] or "",
        in_reply_to=row["in_reply_to"] or "",
        references=row["refs"] or "",
        body=row["body"] or "",
        attachment_manifest=_load_manifest(row["attachment_manifest"]),
        idempotency_key=row["idempotency_key"],
        content_hash=row["content_hash"] or "",
        state=row["state"] or "pending",
        expires_at=_parse_iso(row["expires_at"]),
        created_at=_parse_iso(row["created_at"]),
        claimed_at=_parse_iso(row["claimed_at"]),
        claimed_by=row["claimed_by"],
        sent_at=_parse_iso(row["sent_at"]),
        message_id=row["message_id"],
        error=row["error"],
    )


# ── Process-wide store resolution ────────────────────────────────────────
# A gateway process has ONE approval queue.  The adapter (draft creation), the
# RPC surface (approval), and the standalone cron transport must all agree on
# which store instance/file they use.  The last-constructed instance is
# authoritative for in-process callers (tests build a store on a temp file and
# then drive the RPC surface against it); out-of-process callers resolve by
# canonical path and share the same SQLite file.
_INSTANCE_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_STORE: Optional["OutboundDraftStore"] = None
_INSTANCES: Dict[str, "OutboundDraftStore"] = {}


def active_store() -> Optional["OutboundDraftStore"]:
    """Return the most recently constructed store in this process."""
    with _ACTIVE_LOCK:
        return _ACTIVE_STORE


def get_or_create_store(path: Optional[str] = None) -> "OutboundDraftStore":
    """Return the process store for *path* (canonical default when omitted).

    Reuses the existing instance for the same file so the adapter and the RPC
    surface share one connection pool and one in-memory budget view.
    """
    resolved = str(path or default_store_path())
    with _INSTANCE_LOCK:
        existing = _INSTANCES.get(resolved)
        if existing is not None:
            return existing
    store = OutboundDraftStore(path=resolved)
    with _INSTANCE_LOCK:
        _INSTANCES[resolved] = store
    return store


# ── In-process stop fence ────────────────────────────────────────────────
# (session, generation) pairs whose turn was stopped.  A racing callback from
# a stopped generation must not create a new draft or reach SMTP.
_STOPPED_LOCK = threading.Lock()
_STOPPED: set = set()


def mark_session_stopped(session_ref: str, turn_generation: Optional[str] = None) -> None:
    """Record that *session_ref* (session id or key) stopped at
    *turn_generation*.  When *turn_generation* is omitted the whole session is
    fenced (any generation is refused)."""
    generation = (turn_generation or "").strip()
    with _STOPPED_LOCK:
        _STOPPED.add((str(session_ref), generation))


def session_stopped(session_ref: str, turn_generation: Optional[str] = None) -> bool:
    """True when a stopped marker covers (session_ref, turn_generation).

    A whole-session stop (marked without a generation) matches any
    generation; a generation-specific stop only matches that generation."""
    session = str(session_ref)
    generation = (turn_generation or "").strip()
    with _STOPPED_LOCK:
        if (session, generation) in _STOPPED:
            return True
        if (session, "") in _STOPPED:
            return True
    return False


def clear_stopped_sessions() -> None:
    """Drop all stop markers (process teardown / tests)."""
    with _STOPPED_LOCK:
        _STOPPED.clear()


class OutboundDraftStore:
    """Durable SQLite store for outbound email drafts."""

    # Budgets / circuit defaults — deliberately generous: they are a safety
    # net over an explicit human approval step, not a per-account quota.
    _max_sends_per_session: int = 50
    _max_sends_per_hour: int = 120
    _max_sends_per_day: int = 500
    _circuit_trip_sends: int = 25
    _circuit_window_minutes: int = 60
    _circuit_cooldown_minutes: int = 240

    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.RLock()
        parent = os.path.dirname(self.path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:  # pragma: no cover - defensive
                pass
        self._init_schema()
        with _ACTIVE_LOCK:
            global _ACTIVE_STORE
            _ACTIVE_STORE = self

    # ── connection helpers ────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _meta_get(self, conn: sqlite3.Connection, key: str) -> Optional[str]:
        row = conn.execute(
            "SELECT value FROM outbound_drafts_meta WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def _meta_set(self, conn: sqlite3.Connection, key: str, value: Optional[str]) -> None:
        if value is None:
            conn.execute("DELETE FROM outbound_drafts_meta WHERE key=?", (key,))
        else:
            conn.execute(
                "INSERT INTO outbound_drafts_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ── draft lifecycle ───────────────────────────────────────────────────

    def create_draft(
        self,
        *,
        profile: str = "",
        session_key: str = "",
        session_id: str = "",
        turn_generation: str = "",
        platform: str = "email",
        recipient: str = "",
        subject: str = "",
        in_reply_to: str = "",
        references: str = "",
        body: str = "",
        attachment_manifest: Optional[List[Any]] = None,
        idempotency_key: Optional[str] = None,
        ttl_hours: float = 72,
    ) -> Draft:
        """Persist a pending draft and return it.

        Replaying the same ``(profile, idempotency_key)`` returns the existing
        draft instead of creating a second row (idempotency)."""
        manifest = list(attachment_manifest or [])
        now = _now()
        content_hash = hashlib.sha256(
            _canonical_payload(
                profile=profile,
                session_key=session_key,
                session_id=session_id,
                turn_generation=turn_generation,
                platform=platform,
                recipient=recipient,
                subject=subject,
                in_reply_to=in_reply_to,
                references=references,
                body=body,
                attachment_manifest=manifest,
            )
        ).hexdigest()

        with self._lock:
            conn = self._connect()
            try:
                if idempotency_key:
                    existing = conn.execute(
                        "SELECT * FROM outbound_drafts "
                        "WHERE profile=? AND idempotency_key=?",
                        (profile or "", str(idempotency_key)),
                    ).fetchone()
                    if existing is not None:
                        return _row_to_draft(existing)

                draft_id = f"draft-{uuid.uuid4().hex}"
                conn.execute(
                    "INSERT INTO outbound_drafts ("
                    "  draft_id, profile, session_key, session_id, turn_generation,"
                    "  platform, recipient, subject, in_reply_to, refs, body,"
                    "  attachment_manifest, idempotency_key, content_hash, state,"
                    "  expires_at, created_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        draft_id,
                        profile or "",
                        session_key or "",
                        session_id or "",
                        turn_generation or "",
                        platform or "email",
                        recipient or "",
                        subject or "",
                        in_reply_to or "",
                        references or "",
                        body or "",
                        json.dumps(manifest, ensure_ascii=False),
                        str(idempotency_key) if idempotency_key else None,
                        content_hash,
                        "pending",
                        _iso(now + timedelta(hours=float(ttl_hours or 72))),
                        _iso(now),
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM outbound_drafts WHERE draft_id=?", (draft_id,)
                ).fetchone()
                return _row_to_draft(row)
            finally:
                conn.close()

    def get_draft(self, draft_id: str) -> Optional[Draft]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM outbound_drafts WHERE draft_id=?", (draft_id,)
                ).fetchone()
                return _row_to_draft(row) if row is not None else None
            finally:
                conn.close()

    def list_drafts(
        self,
        *,
        session_key: Optional[str] = None,
        session_id: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Draft]:
        query = "SELECT * FROM outbound_drafts"
        clauses = []
        params: List[Any] = []
        if session_key:
            clauses.append("session_key=?")
            params.append(session_key)
        if session_id:
            clauses.append("session_id=?")
            params.append(session_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, draft_id ASC"
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(query, params).fetchall()
                return [_row_to_draft(row) for row in rows]
            finally:
                conn.close()

    def approve_and_claim_draft(
        self, draft_id: str, content_hash: str, actor: str = "owner"
    ) -> Claimed:
        """Atomically claim a pending draft for delivery.

        Exactly one concurrent caller wins; every other caller receives
        ``claimed=False`` with a reason.  The claim is bound to
        *content_hash*: a hash that differs from the stored row is refused so
        approval always matches the exact payload the owner reviewed."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT content_hash, state, expires_at FROM outbound_drafts "
                    "WHERE draft_id=?",
                    (draft_id,),
                ).fetchone()
                if row is None:
                    conn.execute("ROLLBACK")
                    return Claimed(
                        draft_id=draft_id,
                        content_hash=content_hash,
                        claimed=False,
                        reason="draft not found",
                    )
                stored_hash = row["content_hash"] or ""
                state = row["state"] or ""
                expires_at = row["expires_at"] or ""
                if state != "pending":
                    conn.execute("ROLLBACK")
                    return Claimed(
                        draft_id=draft_id,
                        content_hash=content_hash,
                        claimed=False,
                        reason=f"draft is not pending (state={state})",
                    )
                if expires_at and expires_at <= _iso(_now()):
                    conn.execute("ROLLBACK")
                    return Claimed(
                        draft_id=draft_id,
                        content_hash=content_hash,
                        claimed=False,
                        reason="draft expired",
                    )
                if stored_hash != (content_hash or ""):
                    conn.execute("ROLLBACK")
                    return Claimed(
                        draft_id=draft_id,
                        content_hash=content_hash,
                        claimed=False,
                        reason="content hash mismatch",
                    )
                cur = conn.execute(
                    "UPDATE outbound_drafts SET state='claimed', claimed_at=?, claimed_by=? "
                    "WHERE draft_id=? AND state='pending' AND expires_at > ?",
                    (_iso(_now()), actor or "", draft_id, _iso(_now())),
                )
                conn.execute("COMMIT")
                if cur.rowcount != 1:
                    return Claimed(
                        draft_id=draft_id,
                        content_hash=content_hash,
                        claimed=False,
                        reason="already claimed",
                    )
                return Claimed(
                    draft_id=draft_id, content_hash=content_hash, claimed=True, reason=""
                )
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def record_send_outcome(
        self,
        draft_id: str,
        outcome: str,
        *,
        message_id: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Record the SMTP outcome for a claimed draft.

        ``sent`` / ``unknown_delivery`` / ``failed`` are terminal: none of
        them can be re-claimed, and only ``sent`` counts toward the send
        budgets.  ``unknown_delivery`` and ``failed`` deliberately never
        auto-resend."""
        if outcome not in ("sent", "unknown_delivery", "failed"):
            raise ValueError(f"invalid send outcome: {outcome!r}")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE outbound_drafts SET state=?, message_id=?, error=?, "
                    "sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END "
                    "WHERE draft_id=? AND state='claimed'",
                    (
                        outcome,
                        message_id if outcome == "sent" else None,
                        error if outcome == "failed" else None,
                        outcome,
                        _iso(_now()),
                        draft_id,
                    ),
                )
            finally:
                conn.close()

    def deny_draft(self, draft_id: str, actor: str = "owner") -> bool:
        """Mark a pending draft denied (owner decision).  Returns True when a
        row actually transitioned."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE outbound_drafts SET state='denied', claimed_by=?, "
                    "claimed_at=? WHERE draft_id=? AND state='pending'",
                    (actor or "", _iso(_now()), draft_id),
                )
                return cur.rowcount == 1
            finally:
                conn.close()

    def cancel_draft(self, draft_id: str, actor: str = "owner") -> bool:
        """Mark a pending draft cancelled (owner/UI decision)."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE outbound_drafts SET state='cancelled', claimed_by=?, "
                    "claimed_at=? WHERE draft_id=? AND state='pending'",
                    (actor or "", _iso(_now()), draft_id),
                )
                return cur.rowcount == 1
            finally:
                conn.close()

    def cancel_generation(
        self, session_id: str, turn_generation: str, actor: str = "session-stop"
    ) -> int:
        """Cancel every pending draft belonging to one stopped generation."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE outbound_drafts SET state='cancelled', claimed_by=?, "
                    "claimed_at=? WHERE session_id=? AND turn_generation=? "
                    "AND state='pending'",
                    (actor or "", _iso(_now()), session_id or "", turn_generation or ""),
                )
                return cur.rowcount
            finally:
                conn.close()

    def expire_drafts(self, actor: str = "expiry-sweep") -> int:
        """Sweep the whole pending queue to ``expired``.

        This is the force-invalidation barrier used when an operator closes
        the approval window (gateway stop, draft mode revocation): every
        still-pending draft becomes unclaimable regardless of its TTL.  It is
        an all-or-nothing policy call, not a lazy TTL sweep."""
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE outbound_drafts SET state='expired', claimed_by=?, "
                    "claimed_at=? WHERE state='pending'",
                    (actor or "", _iso(_now())),
                )
                return cur.rowcount
            finally:
                conn.close()

    # ── delivery policy ───────────────────────────────────────────────────

    def count_smtp_sends(self) -> int:
        """Number of confirmed SMTP sends in this store (state == 'sent')."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM outbound_drafts WHERE state='sent'"
                ).fetchone()
                return int(row["n"] or 0)
            finally:
                conn.close()

    def _count_sent_since(self, conn: sqlite3.Connection, since: datetime) -> int:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM outbound_drafts "
            "WHERE state='sent' AND sent_at >= ?",
            (_iso(since),),
        ).fetchone()
        return int(row["n"] or 0)

    def circuit_open(self) -> bool:
        """True when the send circuit breaker is open (delivery paused).

        The breaker trips after ``_circuit_trip_sends`` confirmed sends inside
        ``_circuit_window_minutes`` and stays open for
        ``_circuit_cooldown_minutes`` from the trip moment.  Draft creation is
        never blocked by an open circuit — only delivery."""
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                window_start = now - timedelta(minutes=float(self._circuit_window_minutes))
                recent = self._count_sent_since(conn, window_start)
                if recent < int(self._circuit_trip_sends):
                    # Below threshold — not open; drop any stale trip marker.
                    self._meta_set(conn, "circuit_tripped_at", None)
                    return False
                tripped_raw = self._meta_get(conn, "circuit_tripped_at")
                tripped_at = _parse_iso(tripped_raw)
                if tripped_at is None:
                    tripped_at = now
                    self._meta_set(conn, "circuit_tripped_at", _iso(tripped_at))
                if now >= tripped_at + timedelta(
                    minutes=float(self._circuit_cooldown_minutes)
                ):
                    # Cooldown elapsed — allow delivery again (a hot burst
                    # re-trips on the next check).
                    self._meta_set(conn, "circuit_tripped_at", None)
                    return False
                return True
            finally:
                conn.close()

    def check_delivery_allowed(self, session_key: str = "") -> DeliveryCheck:
        """Enforce the per-session / per-hour / per-day budgets and the
        circuit breaker before an approved draft may reach SMTP.

        Blocking here never prevents draft creation — it only pauses
        delivery, so a burst of approvals cannot flood the wire."""
        now = _now()
        with self._lock:
            conn = self._connect()
            try:
                if self.circuit_open():
                    return DeliveryCheck(
                        False,
                        f"circuit breaker open ({self._circuit_trip_sends} sends in "
                        f"{self._circuit_window_minutes}m window)",
                    )
                session_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM outbound_drafts "
                    "WHERE state='sent' AND session_key=?",
                    (session_key or "",),
                ).fetchone()
                session_sends = int(session_row["n"] or 0)
                if session_sends >= int(self._max_sends_per_session):
                    return DeliveryCheck(
                        False,
                        f"session budget exceeded ({session_sends}/{self._max_sends_per_session})",
                    )
                hour_start = now - timedelta(hours=1)
                day_start = now - timedelta(hours=24)
                hour_sends = self._count_sent_since(conn, hour_start)
                if hour_sends >= int(self._max_sends_per_hour):
                    return DeliveryCheck(
                        False,
                        f"hourly budget exceeded ({hour_sends}/{self._max_sends_per_hour})",
                    )
                day_sends = self._count_sent_since(conn, day_start)
                if day_sends >= int(self._max_sends_per_day):
                    return DeliveryCheck(
                        False,
                        f"daily budget exceeded ({day_sends}/{self._max_sends_per_day})",
                    )
                return DeliveryCheck(True, "")
            finally:
                conn.close()
