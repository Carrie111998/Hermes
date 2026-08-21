"""Durable product database used by the API and agent workers.

SQLite is the self-contained development backend. The schema deliberately
uses ordinary SQL types and tenant keys so it can be applied to Supabase
Postgres without changing the domain contract.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, legal_name TEXT, status TEXT NOT NULL DEFAULT 'active',
    data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, password_hash TEXT, external_id TEXT UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('admin','customer')), company_id TEXT REFERENCES companies(id),
    status TEXT NOT NULL DEFAULT 'active', data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY, refresh_hash TEXT UNIQUE NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
    expires_at REAL NOT NULL, refresh_expires_at REAL NOT NULL, revoked_at REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), expires_at REAL NOT NULL,
    used_at REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS company_sections (
    company_id TEXT NOT NULL REFERENCES companies(id), section TEXT NOT NULL, data TEXT NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY(company_id, section)
);
CREATE TABLE IF NOT EXISTS onboarding (
    company_id TEXT PRIMARY KEY REFERENCES companies(id), status TEXT NOT NULL DEFAULT 'not_started',
    current_step TEXT, completed_steps TEXT NOT NULL DEFAULT '[]', started_at REAL, completed_at REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), document_type TEXT NOT NULL,
    name TEXT NOT NULL, storage_path TEXT, content_type TEXT, size_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'uploaded', processing_run_id TEXT, data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
    normalized_name TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(company_id, normalized_name)
);
CREATE TABLE IF NOT EXISTS company_brain_snapshots (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft', content TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]',
    run_id TEXT, approved_by TEXT REFERENCES users(id), created_at REAL NOT NULL, approved_at REAL,
    UNIQUE(company_id, version)
);
CREATE TABLE IF NOT EXISTS selected_countries (
    company_id TEXT NOT NULL REFERENCES companies(id), country_code TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY(company_id, country_code)
);
CREATE TABLE IF NOT EXISTS lead_scans (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), status TEXT NOT NULL DEFAULT 'draft',
    config TEXT NOT NULL, run_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), scan_id TEXT REFERENCES lead_scans(id),
    company_name TEXT NOT NULL, website TEXT, country TEXT, status TEXT NOT NULL DEFAULT 'new',
    do_not_contact INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS research (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), lead_id TEXT REFERENCES leads(id),
    status TEXT NOT NULL DEFAULT 'queued', insights TEXT NOT NULL DEFAULT '{}', run_id TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), lead_id TEXT REFERENCES leads(id),
    email TEXT, phone TEXT, linkedin_url TEXT, status TEXT NOT NULL DEFAULT 'active',
    do_not_contact INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
-- Tenant-wide opt-out. Keyed on the address, not on a lead/contact row, so
-- re-importing the same address as a new lead cannot resurrect it.
CREATE TABLE IF NOT EXISTS suppressions (
    company_id TEXT NOT NULL REFERENCES companies(id), email TEXT NOT NULL,
    reason TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (company_id, email)
);
CREATE TABLE IF NOT EXISTS outreach_campaigns (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'email', status TEXT NOT NULL DEFAULT 'draft', data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outreach_messages (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), campaign_id TEXT REFERENCES outreach_campaigns(id),
    lead_id TEXT REFERENCES leads(id), contact_id TEXT REFERENCES contacts(id), channel TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending_approval', revision INTEGER NOT NULL DEFAULT 1,
    content_hash TEXT NOT NULL, content TEXT NOT NULL, approval_hash TEXT, approved_by TEXT REFERENCES users(id),
    approved_at REAL, provider_message_id TEXT, sent_at REAL, replied_at REAL, bounced_at REAL,
    idempotency_key TEXT, data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL,
    -- Set when a rewrite replaces this message: points at the message that took
    -- its place, so the approval queue can hide the original without guessing.
    superseded_by TEXT REFERENCES outreach_messages(id),
    UNIQUE(company_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS delivery_attempts (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
    message_id TEXT NOT NULL REFERENCES outreach_messages(id), mode TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'reserved',
    provider_message_id TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cc_rules (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
    data TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS integrations (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), kind TEXT NOT NULL,
    provider TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'disconnected', encrypted_credentials TEXT,
    data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS linkedin_actions (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), lead_id TEXT REFERENCES leads(id),
    contact_id TEXT REFERENCES contacts(id), status TEXT NOT NULL DEFAULT 'generated', profile_url TEXT,
    note TEXT, data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS data_sources (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), source_type TEXT NOT NULL,
    name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), export_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued', path TEXT, data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_log (
    id TEXT PRIMARY KEY, company_id TEXT, actor_id TEXT, action TEXT NOT NULL,
    entity_type TEXT, entity_id TEXT, data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
-- Daily rhythm: a plan each morning, a report each evening. Stored rather than
-- recomputed so the briefing an operator read is exactly the one that was
-- assembled at the time, and so a late read still shows the whole day.
CREATE TABLE IF NOT EXISTS daily_digests (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
    digest_date TEXT NOT NULL,  -- YYYY-MM-DD, the tenant's day
    kind TEXT NOT NULL,         -- 'plan' (morning) | 'report' (evening)
    data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
    UNIQUE(company_id, digest_date, kind)
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), run_type TEXT NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL, output TEXT, error TEXT, output_ref TEXT,
    idempotency_key TEXT, cancellation_requested INTEGER NOT NULL DEFAULT 0, cost REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, started_at REAL, completed_at REAL, updated_at REAL NOT NULL,
    UNIQUE(company_id, idempotency_key)
);
CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    company_id TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
    data TEXT NOT NULL DEFAULT '{}'
);
-- Every upload keeps both forms: the byte-identical original the customer sent
-- and the Markdown the agent actually reads. `content` is the authority — the
-- local mirror at `local_path` is a cache that materialize() rebuilds from here
-- whenever it goes missing or fails its checksum.
CREATE TABLE IF NOT EXISTS document_processing_attempts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    company_id TEXT NOT NULL REFERENCES companies(id),
    public_status TEXT NOT NULL,
    public_message TEXT,
    internal_stage TEXT NOT NULL,
    reason_code TEXT,
    diagnostic TEXT,
    input_checksum TEXT NOT NULL,
    output_checksum TEXT,
    run_id TEXT,
    started_at REAL NOT NULL,
    completed_at REAL
);
CREATE TABLE IF NOT EXISTS document_artifacts (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    company_id TEXT NOT NULL REFERENCES companies(id),
    role TEXT NOT NULL CHECK(role IN ('original','processed')),
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content BLOB NOT NULL,
    checksum TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    attempt_id TEXT REFERENCES document_processing_attempts(id),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_document_artifacts_scope
    ON document_artifacts(company_id, document_id, role);
CREATE INDEX IF NOT EXISTS ix_document_attempts_scope
    ON document_processing_attempts(company_id, public_status);
-- What a run actually looked at. Separate from run_events (which is a live
-- progress feed that scrolls) so "where did this claim come from?" survives.
CREATE TABLE IF NOT EXISTS agent_run_evidence (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    entity_type TEXT,
    entity_id TEXT,
    source_type TEXT NOT NULL,
    source_url TEXT,
    file_reference TEXT,
    title TEXT,
    retrieved_at REAL,
    metadata TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT 'null',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_run_evidence_run ON agent_run_evidence(company_id, run_id);
CREATE INDEX IF NOT EXISTS ix_run_evidence_entity
    ON agent_run_evidence(company_id, entity_type, entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_run_evidence_dedupe
    ON agent_run_evidence(run_id, source_type, source_url, file_reference);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
    user_id TEXT NOT NULL REFERENCES users(id), profile TEXT NOT NULL DEFAULT 'default',
    history TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_users_company ON users(company_id);
CREATE INDEX IF NOT EXISTS ix_documents_company ON documents(company_id);
CREATE INDEX IF NOT EXISTS ix_leads_company ON leads(company_id);
CREATE INDEX IF NOT EXISTS ix_contacts_company ON contacts(company_id);
CREATE INDEX IF NOT EXISTS ix_messages_company ON outreach_messages(company_id);
CREATE INDEX IF NOT EXISTS ix_delivery_message ON delivery_attempts(message_id);
CREATE INDEX IF NOT EXISTS ix_runs_company ON agent_runs(company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_activity_company ON activity_log(company_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_tenant ON chat_sessions(company_id, user_id, updated_at DESC);
"""

# Lead research is an application capability, not a model tool. Keeping its SQL
# in a focused module avoids growing this already-dense product schema while
# ensuring a fresh SQLite database is fully usable in one initialization pass.
from .lead_research.schema import SCHEMA as LEAD_RESEARCH_SCHEMA

SCHEMA = SCHEMA + "\n" + LEAD_RESEARCH_SCHEMA


# (table, column, declaration) applied on boot when the column is absent.
# SQLite requires an added column to be nullable with no UNIQUE/PRIMARY KEY, so
# every entry here must default to NULL.
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("outreach_messages", "superseded_by", "TEXT REFERENCES outreach_messages(id)"),
    # Document processing. `status_detail` is customer-facing copy only; the
    # technical reason code lives on the attempt row, which customers never see.
    ("documents", "status_detail", "TEXT"),
    ("documents", "original_checksum", "TEXT"),
    ("documents", "active_processed_artifact_id", "TEXT REFERENCES document_artifacts(id)"),
    ("documents", "current_processing_attempt_id", "TEXT REFERENCES document_processing_attempts(id)"),
    ("documents", "processing_started_at", "REAL"),
    ("documents", "ready_at", "REAL"),
    ("documents", "origin", "TEXT"),
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def now() -> float:
    return time.time()


def json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_load(value: str | None, default: Any = None) -> Any:
    if value in (None, ""):
        return default
    if not isinstance(value, (str, bytes, bytearray)):
        return value
    return json.loads(value)


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # SCHEMA uses CREATE TABLE IF NOT EXISTS, so an existing database never
            # picks up a new column. These additive migrations close that gap.
            # Additive only, never destructive, and safe to re-run on every boot.
            for table, column, decl in COLUMN_MIGRATIONS:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            conn.commit()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.rowcount

    def activity(
        self,
        company_id: str | None,
        actor_id: str | None,
        action: str,
        entity_type: str | None = None,
        entity_id: str | None = None,
        data: dict | None = None,
    ) -> str:
        activity_id = new_id("act")
        self.execute(
            "INSERT INTO activity_log VALUES(?,?,?,?,?,?,?,?)",
            (activity_id, company_id, actor_id, action, entity_type, entity_id,
             json_dump(data or {}), now()),
        )
        return activity_id
