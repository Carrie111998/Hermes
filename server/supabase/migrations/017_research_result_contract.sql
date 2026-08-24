-- Immutable scoring decisions and one operational lead per resolved tenant
-- organization. Historical campaign outcomes remain separate result rows.

alter table leads
  add column if not exists resolved_organization_id text;
alter table research_results
  add column if not exists profile_version_id text references company_profile_versions(id);
alter table research_results
  add column if not exists snapshot_json text;

create unique index if not exists ux_leads_resolved_organization
  on leads(company_id, resolved_organization_id)
  where resolved_organization_id is not null;

create table if not exists research_score_snapshots (
  id text primary key,
  company_id text not null references companies(id),
  result_id text not null,
  campaign_id text not null references research_campaigns(id),
  profile_version_id text references company_profile_versions(id),
  organization_id text not null,
  snapshot_json text not null,
  created_at double precision not null
);

create index if not exists ix_research_score_snapshots_result
  on research_score_snapshots(company_id, result_id, created_at desc);

alter table research_score_snapshots enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='research_score_snapshots'
      and policyname='research_score_snapshots_tenant'
  ) then
    create policy research_score_snapshots_tenant on research_score_snapshots for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

create or replace function reject_research_score_snapshot_mutation()
returns trigger language plpgsql as $$
begin
  raise exception 'research score snapshots are immutable';
end $$;

drop trigger if exists protect_research_score_snapshot_update on research_score_snapshots;
create trigger protect_research_score_snapshot_update
before update on research_score_snapshots
for each row execute function reject_research_score_snapshot_mutation();

drop trigger if exists protect_research_score_snapshot_delete on research_score_snapshots;
create trigger protect_research_score_snapshot_delete
before delete on research_score_snapshots
for each row execute function reject_research_score_snapshot_mutation();

insert into schema_migrations(version) values ('017_research_result_contract')
on conflict (version) do nothing;
