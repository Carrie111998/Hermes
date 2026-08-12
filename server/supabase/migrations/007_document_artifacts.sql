-- Document artifacts.
--
-- An upload used to be a single object in Supabase Storage plus a `documents`
-- row pointing at it. That made the bytes recoverable only as long as the
-- bucket agreed, gave the agent no readable form of a PDF or spreadsheet, and
-- left an operator with nothing to inspect when extraction produced nothing.
--
-- Both forms now live here: the byte-identical `original` the customer sent and
-- the `processed` Markdown the agent actually reads. `content` is the authority
-- and `local_path` is a rebuildable, checksum-verified mirror — losing the disk
-- costs a re-materialize, not the document.
--
-- Attempts are separate from artifacts on purpose: a retry that fails must not
-- disturb the artifact a previous attempt promoted, and the technical reason a
-- conversion failed must live somewhere a customer response never reads from.

create table if not exists document_processing_attempts (
  id text primary key,
  document_id text not null references documents(id) on delete cascade,
  company_id text not null references companies(id),
  -- Exactly the customer-visible vocabulary. Anything finer grained is
  -- internal_stage/reason_code, which no customer surface renders.
  public_status text not null check (public_status in
    ('uploaded','processing','ready','needs_attention','failed')),
  public_message text,
  internal_stage text not null,
  reason_code text,
  diagnostic text,
  input_checksum text not null,
  output_checksum text,
  run_id text,
  started_at double precision not null,
  completed_at double precision
);

create table if not exists document_artifacts (
  id text primary key,
  document_id text not null references documents(id) on delete cascade,
  company_id text not null references companies(id),
  role text not null check (role in ('original','processed')),
  filename text not null,
  content_type text not null,
  content bytea not null,
  checksum text not null,
  size_bytes bigint not null,
  local_path text not null,
  attempt_id text references document_processing_attempts(id),
  metadata jsonb not null default '{}'::jsonb,
  created_at double precision not null
);

create index if not exists ix_document_artifacts_scope
  on document_artifacts (company_id, document_id, role);
create index if not exists ix_document_attempts_scope
  on document_processing_attempts (company_id, public_status);

-- Additive columns on `documents`. Every one is nullable so the migration is
-- safe to apply to a live table without a rewrite or a default backfill.
alter table if exists documents
  add column if not exists status_detail text,
  add column if not exists original_checksum text,
  add column if not exists active_processed_artifact_id text references document_artifacts(id),
  add column if not exists current_processing_attempt_id text references document_processing_attempts(id),
  add column if not exists processing_started_at double precision,
  add column if not exists ready_at double precision,
  add column if not exists origin text;

comment on column documents.status_detail is
  'Customer-facing sentence only. Technical reason codes live on the attempt row.';
comment on column documents.active_processed_artifact_id is
  'Promoted processed artifact. Set only by the transaction that completes a successful attempt.';

-- These tables carry full document bytes, so they need the same tenant guard as
-- every other product row: direct Supabase reads must never cross a company.
do $$
declare table_name text;
begin
  foreach table_name in array array['document_artifacts','document_processing_attempts'] loop
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

insert into schema_migrations(version) values ('007_document_artifacts')
on conflict (version) do nothing;
