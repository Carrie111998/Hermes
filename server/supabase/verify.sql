-- Post-migration verification for a hosted Supabase project.
-- Run after applying migrations/001..005, before pointing traffic at the API,
-- either through psql or by pasting the whole file into the Supabase SQL Editor:
--
--   psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f server/supabase/verify.sql
--
-- Every check raises on failure, so a clean run means "no output but VERIFY OK".
-- The whole script runs inside a transaction that always rolls back: the
-- cross-tenant test seeds two throwaway tenants and leaves nothing behind.
--
-- ponytail: schema-level checks only. Chat persistence and export download are
-- verified through the API in tests/server/test_api_mvp.py; what can go wrong
-- *here* is a missing migration or an RLS gap, and that is what this asserts.

begin;

-- 1. All five migrations recorded. postgres.py refuses to boot without these,
--    but failing here names them before the API is even started.
do $$
declare missing text;
begin
  select string_agg(v, ', ') into missing
  from unnest(array['001_initial','002_chat_sessions','003_lead_research',
                    '004_lead_research_rls','005_auth_table_rls']) v
  where v not in (select version from schema_migrations);
  if missing is not null then
    raise exception 'unapplied migrations: %', missing;
  end if;
end $$;

-- 2. Every public table has RLS enabled. Asserted over the live catalog rather
--    than a hardcoded list, so a table added by a future migration that forgets
--    RLS fails here instead of silently shipping world-readable.
do $$
declare unprotected text;
begin
  select string_agg(c.relname, ', ') into unprotected
  from pg_class c join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity;
  if unprotected is not null then
    raise exception 'tables without RLS: %', unprotected;
  end if;
end $$;

-- 3. Every tenant table (has company_id) carries a policy. RLS with no policy
--    is deny-all, which is safe but would break the product; RLS with a policy
--    that forgot company_id is the actual leak, caught by check 5.
do $$
declare unpoliced text;
begin
  select string_agg(t.table_name, ', ') into unpoliced
  from information_schema.columns t
  where t.table_schema = 'public' and t.column_name = 'company_id'
    and not exists (select 1 from pg_policies p
                    where p.schemaname = 'public' and p.tablename = t.table_name);
  if unpoliced is not null then
    raise exception 'tenant tables with no policy: %', unpoliced;
  end if;
end $$;

-- 4. Document storage: the bucket exists and is not public. A public bucket
--    would make every tenant's uploads readable by URL, bypassing RLS.
do $$
declare is_public boolean;
begin
  select public into is_public from storage.buckets where id = 'interfaze-documents';
  if is_public is null then
    raise exception 'storage bucket interfaze-documents is missing (001 creates it)';
  end if;
  if is_public then
    raise exception 'storage bucket interfaze-documents is PUBLIC; uploads are readable by URL';
  end if;
  if not exists (select 1 from pg_policies where schemaname = 'storage'
                 and tablename = 'objects' and policyname = 'interfaze_storage_read') then
    raise exception 'storage.objects read policy is missing';
  end if;
end $$;

-- 5. Cross-tenant denial, exercised for real: seed two tenants, then read as
--    tenant A's Supabase subject and confirm B's rows are invisible.
insert into companies(id, name, created_at, updated_at)
values ('cmp_verify_a', 'Verify A', 0, 0), ('cmp_verify_b', 'Verify B', 0, 0);
insert into users(id, email, external_id, role, company_id, created_at, updated_at) values
  ('usr_verify_a', 'verify-a@example.invalid', '11111111-1111-1111-1111-111111111111',
   'customer', 'cmp_verify_a', 0, 0),
  ('usr_verify_b', 'verify-b@example.invalid', '22222222-2222-2222-2222-222222222222',
   'customer', 'cmp_verify_b', 0, 0);
insert into leads(id, company_id, company_name, created_at, updated_at) values
  ('lead_verify_a', 'cmp_verify_a', 'Lead A', 0, 0),
  ('lead_verify_b', 'cmp_verify_b', 'Lead B', 0, 0);
insert into chat_sessions(id, company_id, user_id, profile, history, created_at, updated_at) values
  ('cht_verify_a', 'cmp_verify_a', 'usr_verify_a', 'default', '[]', 0, 0),
  ('cht_verify_b', 'cmp_verify_b', 'usr_verify_b', 'default', '[]', 0, 0);
insert into exports(id, company_id, export_type, status, created_at, updated_at) values
  ('exp_verify_a', 'cmp_verify_a', 'leads', 'ready', 0, 0),
  ('exp_verify_b', 'cmp_verify_b', 'leads', 'ready', 0, 0);

set local role authenticated;
set local request.jwt.claims = '{"sub":"11111111-1111-1111-1111-111111111111","role":"authenticated"}';

do $$
declare leaked text;
begin
  if (select count(*) from leads where id = 'lead_verify_a') <> 1 then
    raise exception 'tenant A cannot read its own lead: RLS is too strict, the product would be empty';
  end if;
  select string_agg(x, ', ') into leaked from (
    select 'leads' x where exists (select 1 from leads where id = 'lead_verify_b')
    union all
    select 'chat_sessions' where exists (select 1 from chat_sessions where id = 'cht_verify_b')
    union all
    select 'exports' where exists (select 1 from exports where id = 'exp_verify_b')
    union all
    select 'companies' where exists (select 1 from companies where id = 'cmp_verify_b')
  ) s;
  if leaked is not null then
    raise exception 'CROSS-TENANT LEAK: tenant A can read tenant B rows in %', leaked;
  end if;
end $$;

-- 6. Credential tables are unreachable even for an authenticated tenant (005).
do $$
begin
  if exists (select 1 from auth_sessions) then
    raise exception 'auth_sessions is readable by an authenticated tenant';
  end if;
  if exists (select 1 from password_reset_tokens) then
    raise exception 'password_reset_tokens is readable by an authenticated tenant';
  end if;
end $$;

reset role;
rollback;

select 'VERIFY OK' as result;
