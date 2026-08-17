-- Verdict-bearing research output. Every evaluated tenant candidate is retained,
-- including rejects that deliberately have no lead row.

create table if not exists schema_migrations (
  version text primary key,
  applied_at timestamptz not null default now()
);
alter table schema_migrations enable row level security;

create table if not exists research_results (
  id text primary key,
  company_id text not null references companies(id),
  campaign_id text not null references research_campaigns(id),
  organization_id text not null,
  lead_id text references leads(id),
  verdict text not null,
  fit_score integer not null,
  evidence_confidence double precision not null,
  data text not null default '{}',
  created_at double precision not null,
  updated_at double precision not null,
  unique(company_id, campaign_id, organization_id)
);

create index if not exists ix_research_results_tenant
  on research_results(company_id, campaign_id, verdict);

alter table research_results enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='research_results'
      and policyname='research_results_tenant'
  ) then
    create policy research_results_tenant on research_results for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

do $$
begin
  if exists (
    select 1 from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname='public' and c.relname='research_results'
      and c.relkind='r' and c.relrowsecurity=false
  ) then
    raise exception 'research_results still without RLS';
  end if;
end $$;

insert into schema_migrations(version) values ('009_research_results')
on conflict (version) do nothing;
