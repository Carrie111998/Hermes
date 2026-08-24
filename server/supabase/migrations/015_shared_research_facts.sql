-- Promotion-safe research fact pools. Shared rows carry no tenant/campaign
-- origin and remain service-role-only; private/licensed/unvalidated facts stay
-- behind tenant RLS.

create table if not exists shared_organizations (
  id text primary key,
  display_name text not null,
  normalized_name text not null,
  country text,
  domain text,
  registry_id text,
  created_at double precision not null,
  updated_at double precision not null
);

create table if not exists shared_evidence_records (
  id text primary key,
  source_id text,
  provenance_url text,
  raw_hash text,
  source_class text not null,
  visibility text not null,
  source_language text not null,
  original_text text not null,
  span_start integer not null,
  span_end integer not null,
  content_hash text not null unique,
  retrieved_at double precision not null,
  created_at double precision not null
);

create table if not exists shared_facts (
  id text primary key,
  organization_id text not null references shared_organizations(id),
  field text not null,
  value_en text not null,
  value_hash text not null,
  primary_evidence_id text not null references shared_evidence_records(id),
  derivation_kind text not null,
  period text,
  unit text,
  currency text,
  status text not null,
  confidence double precision not null,
  validation_basis text not null,
  source_class text not null,
  visibility text not null,
  mechanically_validated integer not null,
  observed_at double precision,
  retrieved_at double precision not null,
  expires_at double precision not null,
  created_at double precision not null,
  updated_at double precision not null
);

create table if not exists shared_fact_evidence (
  fact_id text not null references shared_facts(id),
  evidence_id text not null references shared_evidence_records(id),
  primary key(fact_id, evidence_id)
);

create table if not exists tenant_facts (
  id text primary key,
  company_id text not null references companies(id),
  campaign_id text references research_campaigns(id),
  organization_id text not null,
  field text not null,
  value_en text not null,
  value_hash text not null,
  original_text text not null,
  source_language text not null,
  derivation_kind text not null,
  period text,
  unit text,
  currency text,
  status text not null,
  confidence double precision not null,
  validation_basis text not null,
  evidence_id text not null,
  span_start integer not null,
  span_end integer not null,
  source_class text not null,
  visibility text not null,
  mechanically_validated integer not null,
  observed_at double precision,
  retrieved_at double precision not null,
  expires_at double precision not null,
  created_at double precision not null,
  updated_at double precision not null,
  unique(company_id, organization_id, field, value_hash, evidence_id)
);

create table if not exists research_fact_consumers (
  company_id text not null references companies(id),
  shared_fact_id text not null references shared_facts(id),
  first_used_at double precision not null,
  last_used_at double precision not null,
  primary key(company_id, shared_fact_id)
);

alter table organizations
  add column if not exists shared_organization_id text references shared_organizations(id);
alter table evidence_records
  add column if not exists shared_evidence_id text references shared_evidence_records(id);
alter table feature_claims
  add column if not exists shared_fact_id text references shared_facts(id);

create unique index if not exists ux_shared_organizations_domain
  on shared_organizations(domain) where domain is not null;
create unique index if not exists ux_shared_organizations_registry
  on shared_organizations(country, registry_id) where registry_id is not null;
create unique index if not exists ux_shared_fact_identity
  on shared_facts(organization_id, field, value_hash, primary_evidence_id);
create index if not exists ix_shared_facts_reusable
  on shared_facts(organization_id, field, status, expires_at);
create index if not exists ix_tenant_facts_reusable
  on tenant_facts(company_id, organization_id, field, status, expires_at);
create index if not exists ix_organizations_shared_identity
  on organizations(company_id, shared_organization_id);

-- No client policy is created for shared tables. RLS deny-all prevents direct
-- reads that could turn reusable service knowledge into a bulk data export.
alter table shared_organizations enable row level security;
alter table shared_evidence_records enable row level security;
alter table shared_facts enable row level security;
alter table shared_fact_evidence enable row level security;

alter table tenant_facts enable row level security;
alter table research_fact_consumers enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='tenant_facts'
      and policyname='tenant_facts_tenant'
  ) then
    create policy tenant_facts_tenant on tenant_facts for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public' and tablename='research_fact_consumers'
      and policyname='research_fact_consumers_tenant'
  ) then
    create policy research_fact_consumers_tenant on research_fact_consumers for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

insert into schema_migrations(version) values ('015_shared_research_facts')
on conflict (version) do nothing;
