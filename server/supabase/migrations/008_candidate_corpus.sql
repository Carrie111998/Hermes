-- Shared service-only candidate corpus.  These rows are deliberately not
-- tenant-owned and must never be read directly through anon/authenticated
-- Supabase clients.  The API service accesses them through its database role.

create table if not exists candidate_datasets (
  dataset_id text not null,
  version text not null,
  source_filename text not null,
  raw_hash text not null,
  imported_at double precision not null,
  record_count integer not null,
  primary key(dataset_id, version)
);

create table if not exists candidate_records (
  dataset_id text not null,
  version text not null,
  source_record_id text not null,
  company_name text not null,
  normalized_name text not null,
  country text not null,
  domain text,
  data jsonb not null default '{}'::jsonb,
  primary key(dataset_id, version, source_record_id),
  foreign key(dataset_id, version) references candidate_datasets(dataset_id, version)
);

create index if not exists ix_candidate_records_country
  on candidate_records(country, dataset_id, version);
create index if not exists ix_candidate_records_name
  on candidate_records(normalized_name);

-- No policies are intentionally created.  With RLS enabled this is deny-all
-- for anon/authenticated, while the server's database role retains access.
alter table candidate_datasets enable row level security;
alter table candidate_records enable row level security;

do $$
declare unprotected text;
begin
  select string_agg(c.relname, ', ' order by c.relname) into unprotected
  from pg_class c
  join pg_namespace n on n.oid = c.relnamespace
  where n.nspname = 'public'
    and c.relkind = 'r'
    and c.relrowsecurity = false
    and c.relname in ('candidate_datasets', 'candidate_records');
  if unprotected is not null then
    raise exception 'candidate corpus tables still without RLS: %', unprotected;
  end if;
end $$;

insert into schema_migrations(version) values ('008_candidate_corpus')
on conflict (version) do nothing;
