-- Immutable operator assertions about a candidate dataset version.
--
-- A curated buyer list is evidence only because someone asserted something
-- checkable about its rows. NULL means the dataset was imported before that
-- assertion could be recorded, and such a version stays selection-only
-- forever: backfilling an assertion nobody made is exactly the guess the
-- evidence contract exists to forbid.

alter table candidate_datasets
  add column if not exists assertion_manifest jsonb;

insert into schema_migrations(version) values ('021_balanced_lead_results')
on conflict (version) do nothing;
