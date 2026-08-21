-- Two tables and two indexes that existed only in the SQLite schema.
--
-- `server/db.py` is applied on boot; Postgres only ever gets what a migration
-- writes. So a table added to the SQLite schema without a migration works in
-- every test and raises `relation does not exist` in production — which is what
-- both of these did:
--
--   * `suppressions` is read by ComplianceService before a send, so on Postgres
--     the do-not-contact check itself failed.
--   * `daily_digests` is read and written by the digest scheduler, so no digest
--     could ever be recorded.
--
-- `ix_research_evidence_reuse` matters for cost, not correctness: the evidence
-- reuse lookup filters (company_id, source_id, retrieved_at) and the existing
-- tenant index leads with campaign_id, which that query deliberately does not
-- filter on — so without this index it scanned every evidence row the tenant
-- owns, once per run, exactly the scan it was added to avoid.
--
-- `ix_delivery_message` indexes a foreign key for parity with SQLite.
--
-- tests/server/test_postgres_parity.py now fails when a table or index exists
-- in one backend and not the other, so this class does not recur.

create table if not exists suppressions (
  company_id text not null references companies(id),
  email text not null,
  reason text not null,
  created_at double precision not null,
  primary key (company_id, email)
);

create table if not exists daily_digests (
  id text primary key,
  company_id text not null references companies(id),
  digest_date text not null,
  kind text not null,
  data text not null default '{}',
  created_at double precision not null,
  unique(company_id, digest_date, kind)
);

create index if not exists ix_research_evidence_reuse
  on evidence_records(company_id, source_id, retrieved_at);
create index if not exists ix_delivery_message
  on delivery_attempts(message_id);

alter table suppressions enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='suppressions'
      and policyname='suppressions_tenant'
  ) then
    create policy suppressions_tenant on suppressions for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

alter table daily_digests enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='daily_digests'
      and policyname='daily_digests_tenant'
  ) then
    create policy daily_digests_tenant on daily_digests for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

do $$
begin
  if exists (
    select 1 from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
    where n.nspname='public' and c.relname in ('suppressions','daily_digests')
      and c.relkind='r' and c.relrowsecurity=false
  ) then
    raise exception 'suppressions or daily_digests still without RLS';
  end if;
end $$;

insert into schema_migrations(version) values ('010_digest_suppression_parity')
on conflict (version) do nothing;
