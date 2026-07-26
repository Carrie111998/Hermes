-- Row level security for the lead-research tables created in 003_lead_research.sql.
-- 001_initial.sql enables RLS from a hardcoded table array that predates 003, so
-- every table below shipped with RLS off and was readable by any authenticated
-- Supabase client. Apply this immediately after 003.
--
-- All ten tables created by 003 carry `company_id text not null references
-- companies(id)`, so every one gets the same tenant predicate 001 uses for its
-- product tables: interfaze_company_access(company_id) for both USING and WITH
-- CHECK. There is no shared/global lookup table in 003 -- dataset_definitions and
-- campaign_metrics are per-tenant (company_id is part of their primary key), and
-- dataset_snapshots/organization_links are tenant-scoped despite naming that
-- suggests otherwise. No table needed a different or degraded policy.
--
-- The helper functions interfaze_is_admin() and interfaze_company_access() are
-- the security definer functions defined in 001_initial.sql; they are reused
-- verbatim here rather than re-deriving the predicate.
--
-- Every create policy is wrapped in a pg_policies existence check (the idiom from
-- 002_chat_sessions.sql) so this file is safe to re-run. `alter table ... enable
-- row level security` is already a no-op when RLS is on.

-- Applied-migration ledger. Canonically defined in 001_initial.sql; repeated here
-- so this file still records itself on databases that pre-date that definition.
-- RLS with no policy in this file means deny-all for anon/authenticated roles;
-- 001_initial.sql grants the admin read policy. The service role always bypasses.
create table if not exists schema_migrations (
  version text primary key,
  applied_at timestamptz not null default now()
);
alter table schema_migrations enable row level security;

alter table research_campaigns enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='research_campaigns'
      and policyname='research_campaigns_tenant'
  ) then
    create policy research_campaigns_tenant on research_campaigns for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table dataset_definitions enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='dataset_definitions'
      and policyname='dataset_definitions_tenant'
  ) then
    create policy dataset_definitions_tenant on dataset_definitions for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table dataset_snapshots enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='dataset_snapshots'
      and policyname='dataset_snapshots_tenant'
  ) then
    create policy dataset_snapshots_tenant on dataset_snapshots for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table organizations enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='organizations'
      and policyname='organizations_tenant'
  ) then
    create policy organizations_tenant on organizations for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table organization_links enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='organization_links'
      and policyname='organization_links_tenant'
  ) then
    create policy organization_links_tenant on organization_links for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table evidence_records enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='evidence_records'
      and policyname='evidence_records_tenant'
  ) then
    create policy evidence_records_tenant on evidence_records for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table feature_claims enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='feature_claims'
      and policyname='feature_claims_tenant'
  ) then
    create policy feature_claims_tenant on feature_claims for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table campaign_partitions enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='campaign_partitions'
      and policyname='campaign_partitions_tenant'
  ) then
    create policy campaign_partitions_tenant on campaign_partitions for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table campaign_metrics enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='campaign_metrics'
      and policyname='campaign_metrics_tenant'
  ) then
    create policy campaign_metrics_tenant on campaign_metrics for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table research_issues enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='research_issues'
      and policyname='research_issues_tenant'
  ) then
    create policy research_issues_tenant on research_issues for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

-- Fail loudly if a future 003 revision adds a table this file does not cover.
do $$
declare missing text;
begin
  select string_agg(c.relname, ', ' order by c.relname) into missing
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public'
    and c.relkind = 'r'
    and c.relrowsecurity = false
    and c.relname in (
      'research_campaigns','dataset_definitions','dataset_snapshots','organizations',
      'organization_links','evidence_records','feature_claims','campaign_partitions',
      'campaign_metrics','research_issues'
    );
  if missing is not null then
    raise exception 'lead-research tables still without RLS: %', missing;
  end if;
end $$;

insert into schema_migrations(version) values ('004_lead_research_rls')
on conflict (version) do nothing;
