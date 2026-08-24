-- Mechanical contact verification. Tiers are evidence outcomes, not model
-- confidence, and live on the already tenant-scoped contacts table.

alter table contacts add column if not exists verification_tier text;
alter table contacts add column if not exists contact_kind text;
alter table contacts add column if not exists verification_method text;
alter table contacts add column if not exists verification_evidence_ids text;
alter table contacts add column if not exists verification_checked_at double precision;

alter table contacts drop constraint if exists contacts_verification_tier_check;
alter table contacts add constraint contacts_verification_tier_check
  check (verification_tier is null or verification_tier in ('green','yellow','red'));
alter table contacts drop constraint if exists contacts_contact_kind_check;
alter table contacts add constraint contacts_contact_kind_check
  check (contact_kind is null or contact_kind in ('person','generic'));

create index if not exists ix_contacts_verification_rank
  on contacts(company_id, contact_kind, verification_tier, verification_checked_at desc);

-- contacts already has RLS and its tenant policy from 005_auth_table_rls. The
-- columns inherit that boundary; no new cross-tenant table is introduced.
insert into schema_migrations(version) values ('019_contact_verification_tiers')
on conflict (version) do nothing;
