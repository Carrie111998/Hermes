-- Deny-all RLS for the credential tables.
--
-- `auth_sessions` and `password_reset_tokens` (001) hold session token hashes,
-- refresh hashes, and password-reset token hashes. They were skipped by 001's
-- tenant loop because they have no `company_id`, which left them readable by
-- anyone holding the project's anon key via the Supabase REST endpoint. That is
-- a worse exposure than the tenant tables the loop did cover.
--
-- The fix is RLS with NO policies: PostgreSQL then denies every row to every
-- non-owner role. No policy is wanted here — nothing outside the API server has
-- any business reading these tables.
--
-- Why this cannot break login: server/postgres.py connects through
-- SUPABASE_DB_URL and never issues set_config('request.jwt.claims', ...). Table
-- owners bypass RLS unless FORCE ROW LEVEL SECURITY is set, which it is not.
-- The 21 tables that 001 already put under RLS prove the point empirically — if
-- enabling RLS blocked the server's own connection, those tables would return
-- zero rows and the product would already be non-functional. These two tables
-- therefore carry exactly the same (zero) risk to the application.
--
-- If a future deployment moves the API to a non-owner role, it must set the
-- JWT claims per request; at that point these tables need an explicit
-- service-role policy rather than a relaxation of this migration.

alter table if exists auth_sessions enable row level security;
alter table if exists password_reset_tokens enable row level security;

-- Fail loudly if either table slipped back to RLS-off.
do $$
declare
  unprotected text;
begin
  select string_agg(c.relname, ', ')
    into unprotected
    from pg_class c
    join pg_namespace n on n.oid = c.relnamespace
   where n.nspname = 'public'
     and c.relrowsecurity = false
     and c.relname in ('auth_sessions', 'password_reset_tokens');
  if unprotected is not null then
    raise exception 'credential tables still without RLS: %', unprotected;
  end if;
end $$;

insert into schema_migrations(version) values ('005_auth_table_rls')
on conflict (version) do nothing;
