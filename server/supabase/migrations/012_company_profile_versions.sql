-- Immutable company research inputs. Campaigns freeze one version so later
-- onboarding edits cannot silently change an in-flight or historical run.

create table if not exists schema_migrations (
  version text primary key,
  applied_at timestamptz not null default now()
);
alter table schema_migrations enable row level security;

create table if not exists company_profile_versions (
  id text primary key,
  company_id text not null references companies(id),
  version integer not null,
  status text not null check (status in ('draft','confirmed','superseded')),
  profile_json text not null,
  created_by text not null references users(id),
  confirmed_by text references users(id),
  created_at double precision not null,
  confirmed_at double precision,
  superseded_at double precision,
  unique(company_id, version)
);

create index if not exists ix_company_profile_versions_current
  on company_profile_versions(company_id, status, version desc);

alter table research_campaigns
  add column if not exists profile_version_id text references company_profile_versions(id),
  add column if not exists created_by text references users(id),
  add column if not exists updated_by text references users(id);

create or replace function interfaze_preserve_confirmed_company_profile()
returns trigger language plpgsql as $$
begin
  if TG_OP = 'DELETE' then
    if OLD.status in ('confirmed', 'superseded') then
      raise exception 'confirmed company profiles are immutable';
    end if;
    return OLD;
  end if;
  if OLD.status in ('confirmed', 'superseded') and (
    NEW.company_id is distinct from OLD.company_id or
    NEW.version is distinct from OLD.version or
    NEW.profile_json is distinct from OLD.profile_json or
    NEW.created_by is distinct from OLD.created_by or
    NEW.created_at is distinct from OLD.created_at
  ) then
    raise exception 'confirmed company profiles are immutable';
  end if;
  return NEW;
end;
$$;

drop trigger if exists protect_confirmed_company_profile on company_profile_versions;
create trigger protect_confirmed_company_profile
before update or delete on company_profile_versions
for each row execute function interfaze_preserve_confirmed_company_profile();

alter table company_profile_versions enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='company_profile_versions'
      and policyname='company_profile_versions_tenant'
  ) then
    create policy company_profile_versions_tenant on company_profile_versions for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

insert into schema_migrations(version) values ('012_company_profile_versions')
on conflict (version) do nothing;
