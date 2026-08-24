-- Query-level negative caching. Generic public attempts carry no tenant id;
-- queries influenced by customer, hidden-label, or licensed inputs are private.

create table if not exists research_search_attempts (
  id text primary key,
  company_id text references companies(id),
  shareable integer not null check (shareable in (0, 1)),
  organization_id text not null,
  field text not null,
  query_hash text not null,
  source_id text not null,
  status text not null check (status in ('empty', 'failed', 'succeeded')),
  reason text,
  request_count integer not null default 1,
  attempted_at double precision not null,
  retry_after double precision not null,
  created_at double precision not null,
  updated_at double precision not null,
  check (
    (shareable = 1 and company_id is null)
    or (shareable = 0 and company_id is not null)
  )
);

create unique index if not exists ux_research_search_attempt_shared
  on research_search_attempts(query_hash) where shareable = 1;
create unique index if not exists ux_research_search_attempt_private
  on research_search_attempts(company_id, query_hash) where shareable = 0;
create index if not exists ix_research_search_attempt_retry
  on research_search_attempts(shareable, company_id, retry_after);

alter table research_search_attempts enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'research_search_attempts'
      and policyname = 'research_search_attempts_tenant'
  ) then
    create policy research_search_attempts_tenant on research_search_attempts for all
      using (shareable = 0 and interfaze_company_access(company_id))
      with check (shareable = 0 and interfaze_company_access(company_id));
  end if;
end $$;

insert into schema_migrations(version) values ('016_research_search_attempts')
on conflict (version) do nothing;
