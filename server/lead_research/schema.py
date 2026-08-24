"""Application SQL for evidence-first lead research."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS research_campaigns (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id), name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft', version INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL, estimate TEXT, run_id TEXT,
    profile_version_id TEXT REFERENCES company_profile_versions(id),
    scope_snapshot TEXT,
    created_by TEXT REFERENCES users(id), updated_by TEXT REFERENCES users(id),
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS company_profile_versions (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
    version INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','confirmed','superseded')),
    profile_json TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
    confirmed_by TEXT REFERENCES users(id), created_at REAL NOT NULL,
    confirmed_at REAL, superseded_at REAL, UNIQUE(company_id, version)
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
CREATE TABLE IF NOT EXISTS research_results (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
    campaign_id TEXT NOT NULL REFERENCES research_campaigns(id), organization_id TEXT NOT NULL,
    lead_id TEXT REFERENCES leads(id), verdict TEXT NOT NULL, fit_score INTEGER NOT NULL,
    evidence_confidence REAL NOT NULL, data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(company_id, campaign_id, organization_id)
);
CREATE TABLE IF NOT EXISTS research_score_snapshots (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
    result_id TEXT NOT NULL,
    campaign_id TEXT NOT NULL REFERENCES research_campaigns(id),
    profile_version_id TEXT REFERENCES company_profile_versions(id),
    organization_id TEXT NOT NULL, snapshot_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS research_label_assignments (
    id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(id),
    result_id TEXT NOT NULL REFERENCES research_results(id), label_id TEXT NOT NULL,
    value TEXT NOT NULL, scope TEXT NOT NULL, source TEXT NOT NULL,
    actor_id TEXT NOT NULL, reason TEXT NOT NULL, profile_version_id TEXT NOT NULL,
    effective_from REAL NOT NULL, effective_until REAL
);
CREATE TABLE IF NOT EXISTS research_fact_corrections (
    id TEXT PRIMARY KEY, company_id TEXT REFERENCES companies(id), fact_id TEXT NOT NULL,
    corrected_value_en TEXT NOT NULL, actor_id TEXT NOT NULL, reason TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0, impact TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS research_translations (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    fact_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    source_language TEXT NOT NULL,
    display_locale TEXT NOT NULL CHECK(display_locale IN ('en','tr')),
    value_en TEXT NOT NULL,
    display_value TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(company_id, fact_key, content_hash, source_language, display_locale)
);
CREATE TABLE IF NOT EXISTS shared_organizations (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    country TEXT,
    domain TEXT,
    registry_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_evidence_records (
    id TEXT PRIMARY KEY,
    source_id TEXT,
    provenance_url TEXT,
    raw_hash TEXT,
    source_class TEXT NOT NULL,
    visibility TEXT NOT NULL,
    source_language TEXT NOT NULL,
    original_text TEXT NOT NULL,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    retrieved_at REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_facts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES shared_organizations(id),
    field TEXT NOT NULL,
    value_en TEXT NOT NULL,
    value_hash TEXT NOT NULL,
    primary_evidence_id TEXT NOT NULL REFERENCES shared_evidence_records(id),
    derivation_kind TEXT NOT NULL,
    period TEXT,
    unit TEXT,
    currency TEXT,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    validation_basis TEXT NOT NULL,
    source_class TEXT NOT NULL,
    visibility TEXT NOT NULL,
    mechanically_validated INTEGER NOT NULL,
    observed_at REAL,
    retrieved_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shared_fact_evidence (
    fact_id TEXT NOT NULL REFERENCES shared_facts(id),
    evidence_id TEXT NOT NULL REFERENCES shared_evidence_records(id),
    PRIMARY KEY(fact_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS tenant_facts (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    campaign_id TEXT REFERENCES research_campaigns(id),
    organization_id TEXT NOT NULL,
    field TEXT NOT NULL,
    value_en TEXT NOT NULL,
    value_hash TEXT NOT NULL,
    original_text TEXT NOT NULL,
    source_language TEXT NOT NULL,
    derivation_kind TEXT NOT NULL,
    period TEXT,
    unit TEXT,
    currency TEXT,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    validation_basis TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    source_class TEXT NOT NULL,
    visibility TEXT NOT NULL,
    mechanically_validated INTEGER NOT NULL,
    observed_at REAL,
    retrieved_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(company_id, organization_id, field, value_hash, evidence_id)
);
CREATE TABLE IF NOT EXISTS research_fact_consumers (
    company_id TEXT NOT NULL REFERENCES companies(id),
    shared_fact_id TEXT NOT NULL REFERENCES shared_facts(id),
    first_used_at REAL NOT NULL,
    last_used_at REAL NOT NULL,
    PRIMARY KEY(company_id, shared_fact_id)
);
CREATE TABLE IF NOT EXISTS research_search_attempts (
    id TEXT PRIMARY KEY,
    company_id TEXT REFERENCES companies(id),
    shareable INTEGER NOT NULL CHECK(shareable IN (0,1)),
    organization_id TEXT NOT NULL,
    field TEXT NOT NULL,
    query_hash TEXT NOT NULL,
    source_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('empty','failed','succeeded')),
    reason TEXT,
    request_count INTEGER NOT NULL DEFAULT 1,
    attempted_at REAL NOT NULL,
    retry_after REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK((shareable=1 AND company_id IS NULL) OR (shareable=0 AND company_id IS NOT NULL))
);
-- Candidate corpora are service-only shared inputs.  They deliberately have
-- no company_id: a later campaign may evaluate them, but importing a corpus
-- cannot create a tenant lead, organization, research row, or evidence.
CREATE TABLE IF NOT EXISTS candidate_datasets (
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    owner_company_id TEXT REFERENCES companies(id),
    visibility TEXT NOT NULL DEFAULT 'service_public'
        CHECK(visibility IN ('service_public','tenant_private','licensed_private')),
    source_filename TEXT NOT NULL,
    raw_hash TEXT NOT NULL,
    imported_at REAL NOT NULL,
    record_count INTEGER NOT NULL,
    PRIMARY KEY(dataset_id, version)
);
CREATE TABLE IF NOT EXISTS candidate_records (
    dataset_id TEXT NOT NULL,
    version TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    company_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    country TEXT NOT NULL,
    domain TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    -- Everything a product term matches against, normalised and plural-folded
    -- once at import. Selection used to rebuild this per row on every run — JSON
    -- decode plus several diacritic-stripping passes — so ten campaigns into one
    -- country did the same work ten times. NULL means a corpus imported before
    -- this column existed; selection falls back to computing it, so a corpus
    -- stays usable without a backfill.
    search_text TEXT,
    PRIMARY KEY(dataset_id, version, source_record_id),
    FOREIGN KEY(dataset_id, version) REFERENCES candidate_datasets(dataset_id, version)
);
CREATE INDEX IF NOT EXISTS ix_research_campaigns_tenant ON research_campaigns(company_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_company_profile_versions_current
    ON company_profile_versions(company_id, status, version DESC);
CREATE INDEX IF NOT EXISTS ix_research_sources_tenant ON dataset_definitions(company_id, source_id);
CREATE INDEX IF NOT EXISTS ix_research_evidence_tenant ON evidence_records(company_id, campaign_id, source_id);
CREATE INDEX IF NOT EXISTS ix_research_claims_tenant ON feature_claims(company_id, campaign_id, organization_id);
CREATE INDEX IF NOT EXISTS ix_research_partitions_tenant ON campaign_partitions(company_id, campaign_id, source_id);
CREATE INDEX IF NOT EXISTS ix_research_results_tenant ON research_results(company_id, campaign_id, verdict);
CREATE INDEX IF NOT EXISTS ix_research_score_snapshots_result
    ON research_score_snapshots(company_id, result_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_research_labels_result_history
    ON research_label_assignments(company_id, result_id, effective_from DESC);
CREATE INDEX IF NOT EXISTS ix_research_fact_corrections_fact
    ON research_fact_corrections(fact_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_research_translations_tenant
    ON research_translations(company_id, fact_key, display_locale);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shared_organizations_domain
    ON shared_organizations(domain) WHERE domain IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_shared_organizations_registry
    ON shared_organizations(country, registry_id) WHERE registry_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_shared_fact_identity
    ON shared_facts(organization_id, field, value_hash, primary_evidence_id);
CREATE INDEX IF NOT EXISTS ix_shared_facts_reusable
    ON shared_facts(organization_id, field, status, expires_at);
CREATE INDEX IF NOT EXISTS ix_tenant_facts_reusable
    ON tenant_facts(company_id, organization_id, field, status, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS ux_research_search_attempt_shared
    ON research_search_attempts(query_hash) WHERE shareable=1;
CREATE UNIQUE INDEX IF NOT EXISTS ux_research_search_attempt_private
    ON research_search_attempts(company_id, query_hash) WHERE shareable=0;
CREATE INDEX IF NOT EXISTS ix_research_search_attempt_retry
    ON research_search_attempts(shareable, company_id, retry_after);
-- Evidence reuse reads by tenant, source and age; the tenant index above leads
-- with campaign_id, which this lookup deliberately does not filter on, so
-- without this it scanned every evidence row the tenant owns once per run.
CREATE INDEX IF NOT EXISTS ix_research_evidence_reuse
    ON evidence_records(company_id, source_id, retrieved_at);
CREATE INDEX IF NOT EXISTS ix_candidate_records_country ON candidate_records(country, dataset_id, version);
CREATE UNIQUE INDEX IF NOT EXISTS ux_candidate_records_domain
    ON candidate_records(dataset_id, version, domain) WHERE domain IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_candidate_records_normalized_name_country
    ON candidate_records(dataset_id, version, normalized_name, country);

-- A new version may supersede an old one, but a confirmed snapshot's inputs
-- never change in place. Campaigns can therefore keep pointing at an exact
-- research contract even after the company edits its profile.
CREATE TRIGGER IF NOT EXISTS protect_confirmed_company_profile_update
BEFORE UPDATE OF company_id, version, profile_json, created_by, created_at
ON company_profile_versions
WHEN OLD.status IN ('confirmed','superseded')
BEGIN
    SELECT RAISE(ABORT, 'confirmed company profiles are immutable');
END;
CREATE TRIGGER IF NOT EXISTS protect_confirmed_company_profile_delete
BEFORE DELETE ON company_profile_versions
WHEN OLD.status IN ('confirmed','superseded')
BEGIN
    SELECT RAISE(ABORT, 'confirmed company profiles are immutable');
END;
CREATE TRIGGER IF NOT EXISTS protect_research_score_snapshot_update
BEFORE UPDATE ON research_score_snapshots
BEGIN
    SELECT RAISE(ABORT, 'research score snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS protect_research_score_snapshot_delete
BEFORE DELETE ON research_score_snapshots
BEGIN
    SELECT RAISE(ABORT, 'research score snapshots are immutable');
END;
"""


# Existing SQLite databases add visibility/ownership after SCHEMA runs. Any
# index that names those columns must therefore run after COLUMN_MIGRATIONS.
POST_COLUMN_SCHEMA = """
CREATE INDEX IF NOT EXISTS ix_candidate_datasets_visibility_owner
    ON candidate_datasets(visibility, owner_company_id, dataset_id, version);
CREATE INDEX IF NOT EXISTS ix_organizations_shared_identity
    ON organizations(company_id, shared_organization_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_leads_resolved_organization
    ON leads(company_id, resolved_organization_id)
    WHERE resolved_organization_id IS NOT NULL;
"""
