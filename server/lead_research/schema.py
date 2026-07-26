"""Application SQL for evidence-first lead research."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_campaigns (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL, estimate TEXT, run_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_definitions (
    company_id TEXT NOT NULL REFERENCES companies(id), source_id TEXT NOT NULL, installed INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 0, definition TEXT NOT NULL, health TEXT NOT NULL DEFAULT 'active',
    last_checked_at REAL, updated_at REAL NOT NULL, PRIMARY KEY(company_id, source_id)
);
CREATE TABLE IF NOT EXISTS dataset_snapshots (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), source_id TEXT NOT NULL,
    campaign_id TEXT REFERENCES research_campaigns(id), status TEXT NOT NULL, path TEXT,
    raw_hash TEXT NOT NULL, record_count INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL DEFAULT '{}',
    retrieved_at REAL NOT NULL, UNIQUE(company_id, source_id, raw_hash)
);
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), display_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL, domain TEXT, country TEXT, data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_links (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), organization_id TEXT NOT NULL,
    identifier_type TEXT NOT NULL, identifier_value TEXT NOT NULL, source_id TEXT NOT NULL,
    reversible INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
    UNIQUE(company_id, identifier_type, identifier_value)
);
CREATE TABLE IF NOT EXISTS evidence_records (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), campaign_id TEXT,
    organization_id TEXT, source_id TEXT NOT NULL, source_record_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    record_type TEXT NOT NULL, payload TEXT NOT NULL, provenance_url TEXT, raw_hash TEXT NOT NULL,
    method TEXT NOT NULL, confidence REAL NOT NULL, observed_at REAL, retrieved_at REAL NOT NULL,
    withdrawn_at REAL, UNIQUE(company_id, source_id, source_record_id, snapshot_id, raw_hash)
);
CREATE TABLE IF NOT EXISTS feature_claims (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), campaign_id TEXT,
    organization_id TEXT NOT NULL, field TEXT NOT NULL, status TEXT NOT NULL, value TEXT,
    confidence REAL NOT NULL, method TEXT NOT NULL, evidence_ids TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
    verified_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_partitions (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), campaign_id TEXT NOT NULL,
    source_id TEXT NOT NULL, target_country TEXT, sector_id TEXT, status TEXT NOT NULL,
    checkpoint TEXT, metrics TEXT NOT NULL DEFAULT '{}', error_category TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_metrics (
    company_id TEXT NOT NULL REFERENCES companies(id), campaign_id TEXT NOT NULL, dimension TEXT NOT NULL,
    dimension_value TEXT NOT NULL, metrics TEXT NOT NULL, updated_at REAL NOT NULL,
    PRIMARY KEY(company_id, campaign_id, dimension, dimension_value)
);
CREATE TABLE IF NOT EXISTS research_issues (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), campaign_id TEXT NOT NULL,
    organization_id TEXT, issue_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    data TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_research_campaigns_tenant ON research_campaigns(company_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_research_sources_tenant ON dataset_definitions(company_id, source_id);
CREATE INDEX IF NOT EXISTS ix_research_evidence_tenant ON evidence_records(company_id, campaign_id, source_id);
CREATE INDEX IF NOT EXISTS ix_research_claims_tenant ON feature_claims(company_id, campaign_id, organization_id);
CREATE INDEX IF NOT EXISTS ix_research_partitions_tenant ON campaign_partitions(company_id, campaign_id, source_id);
"""
