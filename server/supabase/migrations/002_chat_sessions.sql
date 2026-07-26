-- Phase 3: tenant-scoped WebUI chat history. Safe to apply after 001_initial.sql.

-- Applied-migration ledger. Canonically defined in 001_initial.sql; repeated here
-- so this file still records itself on databases that pre-date that definition.
-- RLS with no policy in this file means deny-all for anon/authenticated roles;
-- 001_initial.sql grants the admin read policy. The service role always bypasses.
create table if not exists schema_migrations (
  version text primary key,
  applied_at timestamptz not null default now()
);
alter table schema_migrations enable row level security;

create table if not exists chat_sessions (
  id text primary key,
  company_id text not null references companies(id),
  user_id text not null references users(id),
  profile text not null default 'default',
  history jsonb not null default '[]'::jsonb,
  created_at double precision not null,
  updated_at double precision not null
);

create index if not exists ix_chat_sessions_tenant
  on chat_sessions(company_id,user_id,updated_at desc);

alter table chat_sessions enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='chat_sessions'
      and policyname='chat_sessions_tenant'
  ) then
    create policy chat_sessions_tenant on chat_sessions for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

insert into schema_migrations(version) values ('002_chat_sessions')
on conflict (version) do nothing;
