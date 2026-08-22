"""Provider-owned admission control for native Azure Foundry requests.

The controller is deliberately transport agnostic: callers reserve immediately
before invoking the already-configured SDK client and reconcile immediately
after it returns.  SQLite ``BEGIN IMMEDIATE`` transactions make the ledger
shared by CLI, gateway and auxiliary worker processes without adding a proxy.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

SCHEMA_VERSION = 1
PRODUCER_VERSION = "azure-quota-v1"
WINDOW_SECONDS = 60.0

OUTPUT_CEILINGS = {
    "title": (64, 256),
    "monitor": (1, 4_000), "classification": (1, 4_000),
    "triage": (1, 4_000), "compression": (1, 4_000),
    "summarisation": (1, 4_000), "summarization": (1, 4_000),
    "recovery": (1, 4_000), "planning": (1, 8_000),
    "tool_followup": (1, 8_000), "vision": (1, 8_000),
    "guarded_review": (1, 12_000), "primary_implementation": (1, 20_000),
    "primary": (1, 20_000), "embeddings": (0, 0),
}


class AzureQuotaError(RuntimeError):
    """Fail-closed admission error with a stable machine reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class Admission:
    reservation_id: str
    generation: int
    bucket: str
    queue: str
    reserved_tokens: int
    request_class: str


def trusted_output_ceiling(request_class: str, requested: Any = None) -> int:
    key = str(request_class or "").strip().lower().replace("-", "_")
    if key not in OUTPUT_CEILINGS:
        raise AzureQuotaError("untrusted_request_class")
    floor, cap = OUTPUT_CEILINGS[key]
    if requested is None:
        return cap
    if isinstance(requested, bool):
        raise AzureQuotaError("invalid_output_ceiling")
    try:
        value = int(requested)
    except (TypeError, ValueError) as exc:
        raise AzureQuotaError("invalid_output_ceiling") from exc
    # Caller/prompt policy may narrow all the way down; only widening is
    # forbidden.  ``floor`` documents the trusted title policy range rather
    # than granting callers authority to inflate a smaller request.
    return max(0, min(value, cap))


def quota_identity(base_url: str, deployment: str) -> tuple[str, str]:
    """Return privacy-safe actual quota bucket and Terra/Luna queue identity."""
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    dep = str(deployment or "").strip().lower()
    if not host or not dep:
        raise AzureQuotaError("missing_quota_identity")
    digest = hashlib.sha256(f"{host}\0{dep}".encode()).hexdigest()[:20]
    queue = "luna" if "luna" in dep else "terra"
    return f"azq_{digest}", queue


def conservative_token_estimate(payload: Mapping[str, Any], output_ceiling: int) -> int:
    """Conservatively cover text, tool schemas/results and encoded media."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AzureQuotaError("unreservable_payload") from exc
    # Four UTF-8 bytes per token is optimistic for prose; two is intentionally
    # conservative and also charges base64/media and tool schemas in full.
    return max(1, (len(encoded.encode("utf-8")) + 1) // 2) + output_ceiling


def ceiling_from_headers(headers: Mapping[str, Any], hard_cap: int) -> int | None:
    if hard_cap <= 0:
        raise AzureQuotaError("invalid_hard_cap")
    raw = next((v for k, v in headers.items() if str(k).lower() == "x-ratelimit-limit-tokens"), None)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        limit = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    return min(hard_cap, int(limit * 0.8))


class AzureQuotaController:
    def __init__(self, path: str | Path, *, hard_cap: int, max_depth: int = 128,
                 max_wait: float = 120.0, stale_after: float = 300.0,
                 clock: Callable[[], float] = time.time,
                 sleeper: Callable[[float], None] = time.sleep):
        self.path, self.hard_cap = Path(path), int(hard_cap)
        self.max_depth, self.max_wait, self.stale_after = int(max_depth), float(max_wait), float(stale_after)
        self.clock, self.sleeper = clock, sleeper
        if self.hard_cap <= 0 or self.max_depth <= 0 or self.max_wait < 0:
            raise AzureQuotaError("invalid_config")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self):
        with self._connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS buckets(bucket TEXT PRIMARY KEY, ceiling INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, bucket TEXT NOT NULL, queue TEXT NOT NULL, generation INTEGER NOT NULL,
                tokens INTEGER NOT NULL, created REAL NOT NULL, state TEXT NOT NULL, request_class TEXT NOT NULL, identity TEXT NOT NULL UNIQUE);
              CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL, created REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS reservations_active ON reservations(bucket,queue,state,created);
            """)

    def admit(self, *, base_url: str, deployment: str, request_class: str,
              payload: Mapping[str, Any], requested_output: Any = None,
              request_identity: str | None = None, cancelled: Callable[[], bool] = lambda: False) -> Admission:
        bucket, queue = quota_identity(base_url, deployment)
        output = trusted_output_ceiling(request_class, requested_output)
        tokens = conservative_token_estimate(payload, output)
        if tokens > self.hard_cap:
            raise AzureQuotaError("reservation_exceeds_hard_cap")
        identity = hashlib.sha256(str(request_identity or uuid.uuid4()).encode()).hexdigest()[:24]
        started = self.clock()
        rid = uuid.uuid4().hex
        queued_once = False
        while True:
            if cancelled():
                if queued_once:
                    with self._connect() as db:
                        db.execute("DELETE FROM reservations WHERE id=? AND state='queued'", (rid,))
                raise AzureQuotaError("cancelled_pre_send")
            now = self.clock()
            if now - started > self.max_wait:
                if queued_once:
                    with self._connect() as db:
                        db.execute("DELETE FROM reservations WHERE id=? AND state='queued'", (rid,))
                raise AzureQuotaError("queue_wait_exceeded")
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE reservations SET state='stale' WHERE state IN ('queued','admitted') AND created < ?", (now - self.stale_after,))
                db.execute("INSERT OR IGNORE INTO buckets(bucket,ceiling,generation) VALUES(?,?,0)", (bucket, self.hard_cap))
                existing = db.execute("SELECT id,state FROM reservations WHERE identity=?", (identity,)).fetchone()
                if existing and existing[0] != rid:
                    db.rollback(); raise AzureQuotaError("replay_refused")
                depth = db.execute("SELECT COUNT(*) FROM reservations WHERE queue=? AND state IN ('queued','admitted')", (queue,)).fetchone()[0]
                if depth >= self.max_depth:
                    db.rollback(); raise AzureQuotaError("queue_depth_exceeded")
                ceiling, generation = db.execute("SELECT ceiling,generation FROM buckets WHERE bucket=?", (bucket,)).fetchone()
                cutoff = now - WINDOW_SECONDS
                used = db.execute("SELECT COALESCE(SUM(tokens),0) FROM reservations WHERE bucket=? AND state IN ('admitted','complete','unknown') AND created>=?", (bucket, cutoff)).fetchone()[0]
                # Oldest waiter wins; recording a queued row prevents a stream
                # of small arrivals from starving an earlier large request.
                queued = db.execute("SELECT id FROM reservations WHERE queue=? AND state='queued' ORDER BY created,id LIMIT 1", (queue,)).fetchone()
                owns_head = queued is None or queued[0] == rid
                if owns_head and used + tokens <= ceiling:
                    generation += 1
                    db.execute("UPDATE buckets SET generation=? WHERE bucket=?", (generation, bucket))
                    if queued_once:
                        db.execute("UPDATE reservations SET generation=?,state='admitted' WHERE id=?", (generation,rid))
                    else:
                        db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)", (rid,bucket,queue,generation,tokens,now,"admitted",request_class,identity))
                    db.commit()
                    return Admission(rid,generation,bucket,queue,tokens,request_class)
                if not queued_once:
                    db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)", (rid,bucket,queue,0,tokens,now,"queued",request_class,identity))
                    queued_once = True
                db.commit()
            # Event-style bounded sleep; cancellation is checked in small slices.
            self.sleeper(min(0.1, max(0.0, self.max_wait - (self.clock() - started))))

    def reconcile(self, admission: Admission, *, usage_tokens: int | None,
                  headers: Mapping[str, Any] | None = None, status: str = "complete",
                  cancelled: bool = False) -> None:
        now = self.clock()
        state = "cancelled_post_send" if cancelled else ("complete" if usage_tokens is not None else "unknown")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT tokens,state,generation FROM reservations WHERE id=?", (admission.reservation_id,)).fetchone()
            if not row or row[1] != "admitted" or row[2] != admission.generation:
                db.rollback(); raise AzureQuotaError("invalid_generation")
            # Valid provider usage is authoritative even if it exceeds the
            # estimate; never erase incurred cost to make the ledger prettier.
            charged = row[0] if usage_tokens is None else max(0, int(usage_tokens))
            db.execute("UPDATE reservations SET tokens=?,state=? WHERE id=?", (charged,state,admission.reservation_id))
            lowered = ceiling_from_headers(headers or {}, self.hard_cap)
            if lowered is not None:
                db.execute("UPDATE buckets SET ceiling=MIN(ceiling,?) WHERE bucket=?", (lowered,admission.bucket))
            receipt = {"schema":SCHEMA_VERSION,"producer":PRODUCER_VERSION,"profile":"azure-foundry",
                       "request_class":admission.request_class,"bucket":admission.bucket,"queue":admission.queue,
                       "generation":admission.generation,"reserved":admission.reserved_tokens,"usage":usage_tokens if usage_tokens is not None else "unknown",
                       "reconciliation":state,"status":str(status)[:40],"at":round(now,3),"no_fallback":True}
            db.execute("INSERT INTO receipts(body,created) VALUES(?,?)", (json.dumps(receipt,separators=(",",":")),now))
            db.execute("DELETE FROM receipts WHERE id NOT IN (SELECT id FROM receipts ORDER BY id DESC LIMIT 1000)")
            db.commit()


def default_controller_path(hermes_home: str | Path) -> Path:
    return Path(hermes_home) / "state" / "azure_quota.sqlite3"
