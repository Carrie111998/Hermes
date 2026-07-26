-- interfaze-agent Sales Agent MVP schema for Supabase Postgres.
-- Apply through the Supabase migration runner before starting interfaze-api.

create table if not exists companies (
  id text primary key, name text not null, legal_name text, status text not null default 'active',
  data jsonb not null default '{}'::jsonb, created_at double precision not null, updated_at double precision not null
);
create table if not exists users (
  id text primary key, email text not null unique, password_hash text, external_id text unique,
  role text not null check (role in ('admin','customer')), company_id text references companies(id),
  status text not null default 'active', data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null
);
create table if not exists auth_sessions (
  token_hash text primary key, refresh_hash text unique not null, user_id text not null references users(id),
  expires_at double precision not null, refresh_expires_at double precision not null,
  revoked_at double precision, created_at double precision not null
);
create table if not exists password_reset_tokens (
  token_hash text primary key, user_id text not null references users(id), expires_at double precision not null,
  used_at double precision, created_at double precision not null
);
create table if not exists company_sections (
  company_id text not null references companies(id), section text not null, data jsonb not null,
  updated_at double precision not null, primary key(company_id,section)
);
create table if not exists onboarding (
  company_id text primary key references companies(id), status text not null default 'not_started',
  current_step text, completed_steps jsonb not null default '[]'::jsonb,
  started_at double precision, completed_at double precision, updated_at double precision not null
);
create table if not exists documents (
  id text primary key, company_id text not null references companies(id), document_type text not null,
  name text not null, storage_path text, content_type text, size_bytes bigint not null default 0,
  status text not null default 'uploaded', processing_run_id text, data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null
);
create table if not exists products (
  id text primary key, company_id text not null references companies(id), name text not null,
  normalized_name text not null, data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null,
  unique(company_id,normalized_name)
);
create table if not exists company_brain_snapshots (
  id text primary key, company_id text not null references companies(id), version integer not null,
  status text not null default 'draft', content jsonb not null, sources jsonb not null default '[]'::jsonb,
  run_id text, approved_by text references users(id), created_at double precision not null,
  approved_at double precision, unique(company_id,version)
);
create table if not exists selected_countries (
  company_id text not null references companies(id), country_code text not null,
  created_at double precision not null, primary key(company_id,country_code)
);
create table if not exists lead_scans (
  id text primary key, company_id text not null references companies(id), status text not null default 'draft',
  config jsonb not null, run_id text, created_at double precision not null, updated_at double precision not null
);
create table if not exists leads (
  id text primary key, company_id text not null references companies(id), scan_id text references lead_scans(id),
  company_name text not null, website text, country text, status text not null default 'new',
  do_not_contact integer not null default 0, data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null
);
create table if not exists research (
  id text primary key, company_id text not null references companies(id), lead_id text references leads(id),
  status text not null default 'queued', insights jsonb not null default '{}'::jsonb, run_id text,
  created_at double precision not null, updated_at double precision not null
);
create table if not exists contacts (
  id text primary key, company_id text not null references companies(id), lead_id text references leads(id),
  email text, phone text, linkedin_url text, status text not null default 'active',
  do_not_contact integer not null default 0, data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null
);
create table if not exists outreach_campaigns (
  id text primary key, company_id text not null references companies(id), name text not null,
  channel text not null default 'email', status text not null default 'draft',
  data jsonb not null default '{}'::jsonb, created_at double precision not null, updated_at double precision not null
);
create table if not exists outreach_messages (
  id text primary key, company_id text not null references companies(id),
  campaign_id text references outreach_campaigns(id), lead_id text references leads(id),
  contact_id text references contacts(id), channel text not null, status text not null default 'pending_approval',
  revision integer not null default 1, content_hash text not null, content jsonb not null,
  approval_hash text, approved_by text references users(id), approved_at double precision,
  provider_message_id text, sent_at double precision, replied_at double precision, bounced_at double precision,
  idempotency_key text, data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null,
  unique(company_id,idempotency_key)
);
create table if not exists delivery_attempts (
  id text primary key, company_id text not null references companies(id),
  message_id text not null references outreach_messages(id), mode text not null,
  idempotency_key text not null unique, status text not null default 'reserved',
  provider_message_id text, error text, created_at double precision not null, updated_at double precision not null
);
create table if not exists cc_rules (
  id text primary key, company_id text not null references companies(id), name text not null,
  data jsonb not null, created_at double precision not null, updated_at double precision not null
);
create table if not exists integrations (
  id text primary key, company_id text not null references companies(id), kind text not null,
  provider text not null, status text not null default 'disconnected', encrypted_credentials text,
  data jsonb not null default '{}'::jsonb, created_at double precision not null, updated_at double precision not null
);
create table if not exists linkedin_actions (
  id text primary key, company_id text not null references companies(id), lead_id text references leads(id),
  contact_id text references contacts(id), status text not null default 'generated', profile_url text,
  note text, data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null
);
create table if not exists data_sources (
  id text primary key, company_id text not null references companies(id), source_type text not null,
  name text not null, enabled integer not null default 1, data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null
);
create table if not exists exports (
  id text primary key, company_id text not null references companies(id), export_type text not null,
  status text not null default 'queued', path text, data jsonb not null default '{}'::jsonb,
  created_at double precision not null, updated_at double precision not null
);
create table if not exists activity_log (
  id text primary key, company_id text, actor_id text, action text not null,
  entity_type text, entity_id text, data jsonb not null default '{}'::jsonb,
  created_at double precision not null
);
create table if not exists agent_runs (
  id text primary key, company_id text not null references companies(id), run_type text not null,
  status text not null, payload jsonb not null, output jsonb, error text, output_ref text,
  idempotency_key text, cancellation_requested integer not null default 0,
  cost double precision not null default 0, created_at double precision not null,
  started_at double precision, completed_at double precision, updated_at double precision not null,
  unique(company_id,idempotency_key)
);
create table if not exists run_events (
  id bigint generated always as identity primary key,
  run_id text not null references agent_runs(id) on delete cascade, company_id text not null,
  ts double precision not null, kind text not null, message text not null default '',
  data jsonb not null default '{}'::jsonb
);
create table if not exists chat_sessions (
  id text primary key, company_id text not null references companies(id),
  user_id text not null references users(id), profile text not null default 'default',
  history jsonb not null default '[]'::jsonb,
  created_at double precision not null, updated_at double precision not null
);

create index if not exists ix_users_company on users(company_id);
create index if not exists ix_documents_company on documents(company_id);
create index if not exists ix_leads_company on leads(company_id);
create index if not exists ix_contacts_company on contacts(company_id);
create index if not exists ix_messages_company on outreach_messages(company_id);
create index if not exists ix_runs_company on agent_runs(company_id,created_at desc);
create index if not exists ix_activity_company on activity_log(company_id,created_at desc);
create index if not exists ix_chat_sessions_tenant on chat_sessions(company_id,user_id,updated_at desc);

create or replace function interfaze_is_admin() returns boolean
language sql stable security definer set search_path=public as $$
  select exists(select 1 from users where external_id=auth.uid()::text and role='admin' and status='active');
$$;
create or replace function interfaze_company_access(target_company text) returns boolean
language sql stable security definer set search_path=public as $$
  select interfaze_is_admin() or exists(
    select 1 from users where external_id=auth.uid()::text and company_id=target_company and status='active'
  );
$$;

-- Applied-migration ledger. Created after the helper functions so its own policy
-- can reference interfaze_is_admin(). `if not exists` keeps re-runs safe.
create table if not exists schema_migrations (
  version text primary key,
  applied_at timestamptz not null default now()
);
alter table schema_migrations enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='schema_migrations'
      and policyname='schema_migrations_admin_read'
  ) then
    create policy schema_migrations_admin_read on schema_migrations for select
      using (interfaze_is_admin());
  end if;
end $$;

alter table companies enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='companies'
      and policyname='companies_tenant_select'
  ) then
    create policy companies_tenant_select on companies for select using (interfaze_company_access(id));
  end if;
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='companies'
      and policyname='companies_admin_write'
  ) then
    create policy companies_admin_write on companies for all using (interfaze_is_admin()) with check (interfaze_is_admin());
  end if;
end $$;

alter table users enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='users'
      and policyname='users_self_or_admin'
  ) then
    create policy users_self_or_admin on users for select using (external_id=auth.uid()::text or interfaze_is_admin());
  end if;
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='users'
      and policyname='users_admin_write'
  ) then
    create policy users_admin_write on users for all using (interfaze_is_admin()) with check (interfaze_is_admin());
  end if;
end $$;

-- Every remaining product row is guarded by its company_id. The API normally
-- uses a server connection; these policies also protect direct Supabase reads.
do $$
declare table_name text;
begin
  foreach table_name in array array[
    'company_sections','onboarding','documents','products','company_brain_snapshots',
    'selected_countries','lead_scans','leads','research','contacts','outreach_campaigns',
    'outreach_messages','delivery_attempts','cc_rules','integrations','linkedin_actions',
    'data_sources','exports','agent_runs','run_events','chat_sessions'
  ] loop
    execute format('alter table %I enable row level security', table_name);
    if not exists (
      select 1 from pg_policies
      where schemaname='public' and tablename=table_name
        and policyname=table_name || '_tenant'
    ) then
      execute format(
        'create policy %I on %I for all using (interfaze_company_access(company_id)) '
        'with check (interfaze_company_access(company_id))', table_name || '_tenant', table_name
      );
    end if;
  end loop;
end $$;

alter table activity_log enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='activity_log'
      and policyname='activity_tenant'
  ) then
    create policy activity_tenant on activity_log for select
      using (company_id is not null and interfaze_company_access(company_id));
  end if;
end $$;

insert into storage.buckets(id,name,public)
values ('interfaze-documents','interfaze-documents',false)
on conflict (id) do nothing;

do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='storage' and tablename='objects'
      and policyname='interfaze_storage_read'
  ) then
    create policy interfaze_storage_read on storage.objects for select
    using (bucket_id='interfaze-documents' and interfaze_company_access((storage.foldername(name))[1]));
  end if;
  if not exists (
    select 1 from pg_policies
    where schemaname='storage' and tablename='objects'
      and policyname='interfaze_storage_write'
  ) then
    create policy interfaze_storage_write on storage.objects for insert
    with check (bucket_id='interfaze-documents' and interfaze_company_access((storage.foldername(name))[1]));
  end if;
  if not exists (
    select 1 from pg_policies
    where schemaname='storage' and tablename='objects'
      and policyname='interfaze_storage_delete'
  ) then
    create policy interfaze_storage_delete on storage.objects for delete
    using (bucket_id='interfaze-documents' and interfaze_company_access((storage.foldername(name))[1]));
  end if;
end $$;

insert into schema_migrations(version) values ('001_initial')
on conflict (version) do nothing;

