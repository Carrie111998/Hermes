-- Stable tenant-scoped display translations. Canonical English and the exact
-- source-language span remain on the fact/evidence rows; this is presentation.

create table if not exists research_translations (
  id text primary key,
  company_id text not null references companies(id),
  fact_key text not null,
  content_hash text not null,
  source_language text not null,
  display_locale text not null check(display_locale in ('en','tr')),
  value_en text not null,
  display_value text not null,
  created_at double precision not null,
  updated_at double precision not null,
  unique(company_id, fact_key, content_hash, source_language, display_locale)
);

create index if not exists ix_research_translations_tenant
  on research_translations(company_id, fact_key, display_locale);

alter table research_translations enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname='public' and tablename='research_translations'
      and policyname='research_translations_tenant'
  ) then
    create policy research_translations_tenant on research_translations for all
      using (interfaze_company_access(company_id))
      with check (interfaze_company_access(company_id));
  end if;
end $$;

insert into schema_migrations(version) values ('014_research_translations')
on conflict (version) do nothing;
