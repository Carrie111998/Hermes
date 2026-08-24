-- Hidden label history and audited fact corrections. Customer result payloads
-- do not join either table; both are administrative quality controls.

create table if not exists research_label_assignments (
  id text primary key,
  company_id text not null references companies(id),
  result_id text not null references research_results(id),
  label_id text not null,
  value text not null,
  scope text not null,
  source text not null check (source in ('system','admin','outcome_analysis')),
  actor_id text not null,
  reason text not null,
  profile_version_id text not null,
  effective_from double precision not null,
  effective_until double precision
);

create table if not exists research_fact_corrections (
  id text primary key,
  company_id text references companies(id),
  fact_id text not null,
  corrected_value_en text not null,
  actor_id text not null,
  reason text not null,
  applied integer not null default 0,
  impact text not null default '{}',
  created_at double precision not null
);

create index if not exists ix_research_labels_result_history
  on research_label_assignments(company_id, result_id, effective_from desc);
create index if not exists ix_research_fact_corrections_fact
  on research_fact_corrections(fact_id, created_at desc);

alter table research_label_assignments enable row level security;
alter table research_fact_corrections enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies where schemaname='public'
      and tablename='research_label_assignments'
      and policyname='research_label_assignments_tenant'
  ) then
    create policy research_label_assignments_tenant on research_label_assignments for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
  if not exists (
    select 1 from pg_policies where schemaname='public'
      and tablename='research_fact_corrections'
      and policyname='research_fact_corrections_tenant'
  ) then
    create policy research_fact_corrections_tenant on research_fact_corrections for all
      using (company_id is not null and interfaze_company_access(company_id))
      with check (company_id is not null and interfaze_company_access(company_id));
  end if;
end $$;

insert into schema_migrations(version) values ('018_research_labels_corrections')
on conflict (version) do nothing;
