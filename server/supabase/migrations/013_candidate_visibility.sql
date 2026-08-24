-- Candidate supply may be service-wide or private to exactly one tenant.

alter table candidate_datasets
  add column if not exists owner_company_id text references companies(id),
  add column if not exists visibility text;

update candidate_datasets
set visibility='service_public'
where visibility is null;

alter table candidate_datasets
  alter column visibility set default 'service_public',
  alter column visibility set not null;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname='candidate_datasets_visibility_valid'
  ) then
    alter table candidate_datasets add constraint candidate_datasets_visibility_valid
      check (
        (visibility='service_public' and owner_company_id is null)
        or (visibility in ('tenant_private','licensed_private') and owner_company_id is not null)
      );
  end if;
end $$;

create index if not exists ix_candidate_datasets_visibility_owner
  on candidate_datasets(visibility, owner_company_id, dataset_id, version);

-- Corpus rows remain unreachable directly: candidate_records keeps the deny-all
-- RLS posture from migration 008. Tenant metadata is visible only to its owner;
-- the API service role supplies both public and private rows through scoped APIs.
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='candidate_datasets'
      and policyname='candidate_datasets_owner'
  ) then
    create policy candidate_datasets_owner on candidate_datasets for all
      using (owner_company_id is not null and interfaze_company_access(owner_company_id))
      with check (owner_company_id is not null and interfaze_company_access(owner_company_id));
  end if;
end $$;

insert into schema_migrations(version) values ('013_candidate_visibility')
on conflict (version) do nothing;
