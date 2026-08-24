-- Conservative SQL-safe classifications for rows created before the complete
-- lead-research contract. JSON transformations are performed separately by
-- `python -m server backfill-lead-research-contract` after this migration.

-- Candidate corpora had service-wide visibility before ownership existed.
-- NULL therefore means the old service-public contract, never cross-tenant
-- tenant-private data.
update candidate_datasets
set visibility='service_public', owner_company_id=null
where visibility is null;

-- A syntactically valid legacy address is not verified evidence. Leave any
-- existing tier untouched; otherwise classify least-trust. checked_at remains
-- NULL so the application backfill can inspect existing official evidence once
-- and mark the row complete.
update contacts
set verification_tier='red',
    contact_kind=case
      when lower(split_part(coalesce(email,''), '@', 1)) in
        ('admin','contact','hello','info','office','sales','support','team',
         'export','purchasing','procurement','orders','enquiries')
      then 'generic' else 'person' end,
    verification_method='legacy_unverified',
    verification_evidence_ids='[]'
where verification_tier is null;

-- Deliberately no shared-fact write: old evidence predates exact-span
-- and mechanical-validation guarantees, so automatic promotion would leak
-- tenant claims into the shared pool.

insert into schema_migrations(version) values ('020_lead_research_contract_backfill')
on conflict (version) do nothing;
