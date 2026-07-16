-- Evidence-first lead research application tables.
-- Mirrors server/lead_research/schema.py without SQLite-specific pragmas.
CREATE TABLE IF NOT EXISTS research_campaigns (
  id text PRIMARY KEY, company_id text NOT NULL REFERENCES companies(id), name text NOT NULL,
  status text NOT NULL DEFAULT 'draft', version integer NOT NULL DEFAULT 1,
  config text NOT NULL, estimate text, run_id text, created_at double precision NOT NULL,
  updated_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_definitions (
  company_id text NOT NULL REFERENCES companies(id), source_id text NOT NULL,
  installed integer NOT NULL DEFAULT 0, enabled integer NOT NULL DEFAULT 0,
  definition text NOT NULL, health text NOT NULL DEFAULT 'active',
  last_checked_at double precision, updated_at double precision NOT NULL,
  PRIMARY KEY(company_id, source_id)
);
CREATE TABLE IF NOT EXISTS dataset_snapshots (
  id text PRIMARY KEY, company_id text NOT NULL REFERENCES companies(id), source_id text NOT NULL,
  campaign_id text REFERENCES research_campaigns(id), status text NOT NULL, path text,
  raw_hash text NOT NULL, record_count integer NOT NULL DEFAULT 0, data text NOT NULL DEFAULT '{}',
  retrieved_at double precision NOT NULL, UNIQUE(company_id, source_id, raw_hash)
);
CREATE TABLE IF NOT EXISTS organizations (
  id text PRIMARY KEY, company_id text NOT NULL REFERENCES companies(id), display_name text NOT NULL,
  normalized_name text NOT NULL, domain text, country text, data text NOT NULL DEFAULT '{}',
  created_at double precision NOT NULL, updated_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_links (
  id text PRIMARY KEY, company_id text NOT NULL REFERENCES companies(id), organization_id text NOT NULL,
  identifier_type text NOT NULL, identifier_value text NOT NULL, source_id text NOT NULL,
  reversible integer NOT NULL DEFAULT 1, created_at double precision NOT NULL,
  UNIQUE(company_id, identifier_type, identifier_value)
);
CREATE TABLE IF NOT EXISTS evidence_records (
  id text PRIMARY KEY, company_id text NOT NULL REFERENCES companies(id), campaign_id text,
  organization_id text, source_id text NOT NULL, source_record_id text NOT NULL, snapshot_id text NOT NULL,
  record_type text NOT NULL, payload text NOT NULL, provenance_url text, raw_hash text NOT NULL,
  method text NOT NULL, confidence double precision NOT NULL, observed_at double precision,
  retrieved_at double precision NOT NULL, withdrawn_at double precision,
  UNIQUE(company_id, source_id, source_record_id, snapshot_id, raw_hash)
);
CREATE TABLE IF NOT EXISTS feature_claims (
  id text PRIMARY KEY, company_id text NOT NULL REFERENCES companies(id), campaign_id text,
  organization_id text NOT NULL, field text NOT NULL, status text NOT NULL, value text,
  confidence double precision NOT NULL, method text NOT NULL, evidence_ids text NOT NULL,
  data text NOT NULL DEFAULT '{}', verified_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_partitions (
  id text PRIMARY KEY, company_id text NOT NULL REFERENCES companies(id), campaign_id text NOT NULL,
  source_id text NOT NULL, target_country text, sector_id text, status text NOT NULL,
  checkpoint text, metrics text NOT NULL DEFAULT '{}', error_category text,
  updated_at double precision NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_metrics (
  company_id text NOT NULL REFERENCES companies(id), campaign_id text NOT NULL,
  dimension text NOT NULL, dimension_value text NOT NULL, metrics text NOT NULL,
  updated_at double precision NOT NULL,
  PRIMARY KEY(company_id, campaign_id, dimension, dimension_value)
);
CREATE TABLE IF NOT EXISTS research_issues (
  id text PRIMARY KEY, company_id text NOT NULL REFERENCES companies(id), campaign_id text NOT NULL,
  organization_id text, issue_type text NOT NULL, status text NOT NULL DEFAULT 'open',
  data text NOT NULL DEFAULT '{}', created_at double precision NOT NULL,
  updated_at double precision NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_research_campaigns_tenant ON research_campaigns(company_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_research_sources_tenant ON dataset_definitions(company_id, source_id);
CREATE INDEX IF NOT EXISTS ix_research_evidence_tenant ON evidence_records(company_id, campaign_id, source_id);
CREATE INDEX IF NOT EXISTS ix_research_claims_tenant ON feature_claims(company_id, campaign_id, organization_id);
CREATE INDEX IF NOT EXISTS ix_research_partitions_tenant ON campaign_partitions(company_id, campaign_id, source_id);
